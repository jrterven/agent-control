from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .normalization import EventNormalizer
from .limits import MAX_UPSTREAM_JSON_BYTES, validate_json_shape
from .types import NormalizedEvent


class JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"Hermes RPC {code}: {message}")
        self.code = code
        self.data = data


class JsonRpcDisconnected(ConnectionError):
    pass


class JsonRpcGenerationChanged(JsonRpcDisconnected):
    """Connection changed before a request wrote any bytes."""


EventCallback = Callable[[NormalizedEvent], Awaitable[None]]


class JsonRpcClient:
    """Concurrent JSON-RPC 2.0 client with heartbeat and event fan-out.

    A client is intentionally scoped to exactly one ``(gateway, profile)``.
    The caller owns reconnect/backoff so a connection can never silently move
    a request to another profile.
    """

    def __init__(
        self,
        *,
        url: str,
        gateway_id: str,
        profile_name: str,
        token: str | None = None,
        connect_host: str | None = None,
        connect_timeout: float = 15.0,
        request_timeout: float = 120.0,
        heartbeat_interval: float = 15.0,
        inbound_deadline: float = 45.0,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.url = url
        self.gateway_id = gateway_id
        self.profile_name = profile_name
        self.token = token
        self.connect_host = connect_host
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.heartbeat_interval = heartbeat_interval
        self.inbound_deadline = inbound_deadline
        self.event_callback = event_callback
        self._normalizer = EventNormalizer(gateway_id=gateway_id, profile_name=profile_name)
        self._socket: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._pending: dict[str, tuple[int, asyncio.Future[Any]]] = {}
        self._last_inbound = 0.0
        self._send_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._generation = 0

    @property
    def connected(self) -> bool:
        return self._socket is not None and self._reader is not None and not self._reader.done()

    @property
    def generation(self) -> int:
        return self._generation

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            if self.connected:
                return
            if self._socket is not None:
                await self._close_generation(
                    self._socket,
                    self._generation,
                    JsonRpcDisconnected("Hermes gateway reconnecting"),
                )
            from websockets.asyncio.client import connect

            if self.token:
                parts = urlsplit(self.url)
                query = parse_qsl(parts.query, keep_blank_values=True)
                query.append(("token", self.token))
                url = urlunsplit(
                    (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
                )
            else:
                url = self.url
            connection_kwargs: dict[str, Any] = {
                "open_timeout": self.connect_timeout,
                "ping_interval": None,
                "proxy": None,
                "max_size": MAX_UPSTREAM_JSON_BYTES,
                "max_queue": 32,
            }
            if self.connect_host:
                connection_kwargs["host"] = self.connect_host
            connector = connect(url, **connection_kwargs)
            # Endpoint policy is evaluated before constructing this client. Never
            # let the websocket library follow an unvalidated redirect (including
            # a same-host redirect whose DNS answer could have changed).
            connector.process_redirect = lambda exc: exc
            socket = await asyncio.wait_for(connector, timeout=self.connect_timeout + 1)
            self._generation += 1
            generation = self._generation
            self._socket = socket
            self._last_inbound = asyncio.get_running_loop().time()
            self._reader = asyncio.create_task(
                self._read_loop(socket, generation),
                name=f"hermes-rpc-{self.profile_name}-{generation}",
            )
            self._heartbeat = asyncio.create_task(
                self._heartbeat_loop(socket, generation),
                name=f"hermes-heartbeat-{self.profile_name}-{generation}",
            )

    async def close(self) -> None:
        async with self._lifecycle_lock:
            socket = self._socket
            if socket is not None:
                await self._close_generation(
                    socket,
                    self._generation,
                    JsonRpcDisconnected("Hermes gateway disconnected"),
                )
                return
            current = asyncio.current_task()
            stale = [
                task
                for task in (self._heartbeat, self._reader)
                if task is not None and task is not current
            ]
            self._heartbeat = None
            self._reader = None
            for task in stale:
                task.cancel()
            if stale:
                await asyncio.gather(*stale, return_exceptions=True)
            self._fail_pending(JsonRpcDisconnected("Hermes gateway disconnected"))

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        expected_generation: int | None = None,
    ) -> Any:
        socket = self._socket
        generation = self._generation
        if expected_generation is not None and generation != expected_generation:
            raise JsonRpcGenerationChanged(
                "Hermes gateway generation changed before request"
            )
        if socket is None or not self.connected:
            raise JsonRpcDisconnected("Hermes gateway is not connected")
        return await self._request_on_generation(
            socket,
            generation,
            method,
            params,
            timeout=timeout,
        )

    async def _request_on_generation(
        self,
        socket: Any,
        generation: int,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        if not self._owns(socket, generation):
            raise JsonRpcGenerationChanged("Hermes gateway generation is stale")
        request_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = (generation, future)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {**(params or {}), "profile": self.profile_name},
        }
        try:
            async with self._send_lock:
                if not self._owns(socket, generation):
                    raise JsonRpcGenerationChanged(
                        "Hermes gateway generation changed before send"
                    )
                try:
                    await socket.send(json.dumps(payload, separators=(",", ":")))
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except Exception as exc:
                    # Normalize websocket-library-specific send failures.  A
                    # caller such as prompt.submit can then classify the
                    # operation as ambiguous instead of inviting a retry.
                    raise JsonRpcDisconnected("Hermes gateway disconnected while sending") from exc
            return await asyncio.wait_for(future, timeout=timeout or self.request_timeout)
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A generation may have failed the future while this request
                # was still waiting for the send lock. Retrieve that exception
                # even when the direct stale-generation error wins the race.
                future.exception()

    async def _read_loop(self, socket: Any, generation: int) -> None:
        disconnect_error = JsonRpcDisconnected("Hermes gateway disconnected")
        try:
            async for message in socket:
                if not self._owns(socket, generation):
                    return
                self._last_inbound = asyncio.get_running_loop().time()
                raw = json.loads(message)
                validate_json_shape(raw)
                if not isinstance(raw, dict):
                    raise ValueError("Hermes JSON-RPC frame must be an object")
                if "id" in raw and ("result" in raw or "error" in raw):
                    request_id = str(raw["id"])
                    pending = self._pending.get(request_id)
                    if pending is None:
                        continue
                    pending_generation, future = pending
                    if pending_generation != generation or future.done():
                        continue
                    if raw.get("error"):
                        error = raw["error"]
                        future.set_exception(
                            JsonRpcError(
                                int(error.get("code", -32000)),
                                str(error.get("message", "Unknown Hermes error")),
                                error.get("data"),
                            )
                        )
                    else:
                        future.set_result(raw.get("result"))
                elif self.event_callback is not None:
                    await self.event_callback(self._normalizer.normalize(raw))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            disconnect_error = JsonRpcDisconnected(str(exc))
        finally:
            await self._close_generation(socket, generation, disconnect_error)

    async def _heartbeat_loop(self, socket: Any, generation: int) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                if not self._owns(socket, generation):
                    return
                age = asyncio.get_running_loop().time() - self._last_inbound
                if age > self.inbound_deadline:
                    raise JsonRpcDisconnected("Hermes heartbeat deadline exceeded")
                await self._request_on_generation(
                    socket,
                    generation,
                    "gateway.ping",
                    {},
                    timeout=self.heartbeat_interval,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._close_generation(
                socket,
                generation,
                JsonRpcDisconnected(str(exc)),
            )

    def _owns(self, socket: Any, generation: int) -> bool:
        return self._socket is socket and self._generation == generation

    async def _close_generation(
        self,
        socket: Any,
        generation: int,
        error: Exception,
    ) -> None:
        """Retire only the socket/tasks belonging to the supplied generation."""

        if not self._owns(socket, generation):
            return
        current = asyncio.current_task()
        workers = [
            task
            for task in (self._heartbeat, self._reader)
            if task is not None and task is not current
        ]
        self._socket = None
        self._heartbeat = None
        self._reader = None
        for task in workers:
            task.cancel()
        with contextlib.suppress(Exception):
            await socket.close()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._fail_pending(error, generation=generation)

    def _fail_pending(self, error: Exception, *, generation: int | None = None) -> None:
        for pending_generation, future in list(self._pending.values()):
            if generation is not None and pending_generation != generation:
                continue
            if not future.done():
                future.set_exception(error)


async def reconnecting_call(
    operation: Callable[[], Awaitable[Any]],
    reconnect: Callable[[], Awaitable[None]],
    *,
    attempts: int = 4,
) -> Any:
    """Reconnect read-only operations with bounded exponential backoff.

    Prompt submission must not use this helper because delivery may be
    ambiguous after a transport failure.
    """

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except (JsonRpcDisconnected, OSError) as exc:
            last_error = exc
            if attempt + 1 == attempts:
                break
            await asyncio.sleep(min(0.25 * (2**attempt), 3.0) + (0.05 * attempt))
            await reconnect()
    assert last_error is not None
    raise last_error
