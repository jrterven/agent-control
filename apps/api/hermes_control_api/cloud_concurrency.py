"""Bound cloud HTTP work before authentication can check out a DB connection.

WebSocket replies must remain runnable while HTTP operations await them. Keep
admission asynchronous, bound its queue, and hold slots through ASGI cleanup.
"""
from __future__ import annotations

import asyncio

from sqlalchemy.exc import TimeoutError as PoolTimeout


class CloudConcurrencyMiddleware:
    def __init__(self, app, *, active_limit: int = 20, queued_limit: int = 40,
                 wait_seconds: float = 10, probe_limit: int = 2):
        self.app = app
        self.active_limit = active_limit
        self.queued_limit = queued_limit
        self.wait_seconds = wait_seconds
        self.slots = asyncio.Semaphore(active_limit)
        self.probe_slots = asyncio.Semaphore(probe_limit)
        self.admitted = 0

    @staticmethod
    async def busy(send):
        await send({"type": "http.response.start", "status": 503, "headers": [
            (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
            (b"retry-after", b"2"), (b"x-content-type-options", b"nosniff"),
        ]})
        await send({"type": "http.response.body", "body":
            b'{"code":"SERVICE_BUSY","message":"The service is busy. Try again shortly.","retryable":true}'})

    async def invoke(self, scope, receive, send):
        response_started = False

        async def capture(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, capture)
        except PoolTimeout:
            # The cloud pool never waits synchronously on the event loop. An
            # unexpected exhaustion must fail without leaking SQL/credentials.
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013})
            elif scope["type"] == "http" and not response_started:
                if scope.get("method") in {"POST", "PUT", "PATCH", "DELETE"}:
                    # A failure inside admitted work may follow an upstream
                    # side effect. Unlike admission rejection, it cannot
                    # authorize a fresh retry of a mutation.
                    await send({"type": "http.response.start", "status": 409, "headers": [
                        (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
                    ]})
                    await send({"type": "http.response.body", "body":
                        b'{"code":"MUTATION_DELIVERY_UNKNOWN","message":"Mutation outcome is unknown; reconcile before retrying","retryable":false}'})
                else:
                    await self.busy(send)
            else:
                raise

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/"):
            await self.invoke(scope, receive, send)
            return
        if scope["path"] in {"/api/v1/health", "/api/v1/ready"}:
            # A slow user's operation must not monopolize availability probes.
            # Probe traffic itself has a separate small, nonwaiting bound.
            if self.probe_slots.locked():
                await self.busy(send)
                return
            async with self.probe_slots:
                await self.invoke(scope, receive, send)
            return
        if self.admitted >= self.active_limit + self.queued_limit:
            await self.busy(send)
            return
        self.admitted += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(self.wait_seconds):
                    await self.slots.acquire()
                    acquired = True
            except TimeoutError:
                await self.busy(send)
                return
            await self.invoke(scope, receive, send)
        finally:
            if acquired:
                self.slots.release()
            self.admitted -= 1
