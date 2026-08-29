from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from .provider import HermesProvider, ProviderConnection, RuntimeGenerationChanged
from .types import HermesSession, PromptReceipt, SessionRoute


class RouteMismatchError(ValueError):
    pass


ProviderFactory = Callable[[ProviderConnection], Awaitable[HermesProvider] | HermesProvider]


class ProviderPool:
    """Holds exactly one provider for each immutable gateway/profile pair."""

    def __init__(self, factory: ProviderFactory) -> None:
        self.factory = factory
        self._providers: dict[tuple[str, str], HermesProvider] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def get(self, connection: ProviderConnection) -> HermesProvider:
        key = (connection.gateway_id, connection.profile_name)
        provider = self._providers.get(key)
        if provider is not None:
            return provider
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            provider = self._providers.get(key)
            if provider is None:
                candidate = self.factory(connection)
                provider = await candidate if hasattr(candidate, "__await__") else candidate
                self._providers[key] = provider
            return provider

    async def close(self) -> None:
        await asyncio.gather(*(provider.close() for provider in self._providers.values()))
        self._providers.clear()

    async def invalidate(self, gateway_id: str, profile_name: str | None = None) -> None:
        keys = [
            key
            for key in self._providers
            if key[0] == gateway_id and (profile_name is None or key[1] == profile_name)
        ]
        providers = [self._providers.pop(key) for key in keys]
        for key in keys:
            self._locks.pop(key, None)
        if providers:
            await asyncio.gather(*(provider.close() for provider in providers))


class HermesSessionRouter:
    """Serializes route changes and prevents cross-profile session leakage."""

    def __init__(self, pool: ProviderPool) -> None:
        self.pool = pool
        self._route_locks: OrderedDict[tuple[str, str, str], asyncio.Lock] = OrderedDict()
        self._receipts: OrderedDict[
            tuple[str, str, str, str], PromptReceipt
        ] = OrderedDict()
        self._validated_runtime: OrderedDict[
            tuple[str, str, str, str], str
        ] = OrderedDict()
        self._runtime_owner: OrderedDict[tuple[str, str, str], str] = OrderedDict()
        self._owner_generation: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._max_routes = 2_048
        self._max_receipts = 4_096

    @staticmethod
    def assert_route(route: SessionRoute, connection: ProviderConnection) -> None:
        if route.gateway_id != connection.gateway_id or route.profile_name != connection.profile_name:
            raise RouteMismatchError("Session route does not match provider identity")

    def mark_runtime(self, route: SessionRoute, *, generation: str) -> None:
        """Mark a runtime returned by session.create as valid for this process."""
        if route.runtime_session_id:
            self._claim_runtime(route, generation)
            self._validated_runtime[
                (route.gateway_id, route.profile_name, route.stored_session_id, route.runtime_session_id)
            ] = generation
            self._validated_runtime.move_to_end(
                (route.gateway_id, route.profile_name, route.stored_session_id, route.runtime_session_id)
            )
            self._trim(self._validated_runtime, self._max_routes * 2)

    async def ensure_runtime(
        self, route: SessionRoute, connection: ProviderConnection
    ) -> tuple[SessionRoute, HermesSession | None]:
        self.assert_route(route, connection)
        validation_key = (
            route.gateway_id,
            route.profile_name,
            route.stored_session_id,
            route.runtime_session_id or "",
        )
        provider = await self.pool.get(connection)
        generation = provider.runtime_generation
        if route.runtime_session_id and self._validated_runtime.get(validation_key) == generation:
            return route, None
        lock = self._route_lock(
            (route.gateway_id, route.profile_name, route.stored_session_id)
        )
        async with lock:
            generation = provider.runtime_generation
            if route.runtime_session_id and self._validated_runtime.get(validation_key) == generation:
                return route, None
            resumed = await provider.resume_session(route.stored_session_id)
            if resumed.stored_session_id != route.stored_session_id:
                raise RouteMismatchError("Hermes resumed a different stored session")
            if not isinstance(resumed.runtime_session_id, str) or not resumed.runtime_session_id.strip():
                raise RouteMismatchError("Hermes resume did not return a runtime session id")
            routed = SessionRoute(
                    gateway_id=route.gateway_id,
                    profile_name=route.profile_name,
                    stored_session_id=route.stored_session_id,
                    runtime_session_id=resumed.runtime_session_id,
            )
            if routed.runtime_session_id:
                self._claim_runtime(routed, provider.runtime_generation)
                self._validated_runtime[
                    (routed.gateway_id, routed.profile_name, routed.stored_session_id, routed.runtime_session_id)
                ] = provider.runtime_generation
                self._validated_runtime.move_to_end(
                    (routed.gateway_id, routed.profile_name, routed.stored_session_id, routed.runtime_session_id)
                )
                self._trim(self._validated_runtime, self._max_routes * 2)
            return routed, resumed

    def _claim_runtime(self, route: SessionRoute, generation: str) -> None:
        runtime_id = route.runtime_session_id
        if not isinstance(runtime_id, str) or not runtime_id.strip():
            raise RouteMismatchError("Runtime session id is missing")
        provider_key = (route.gateway_id, route.profile_name)
        if self._owner_generation.get(provider_key) != generation:
            self._runtime_owner = {
                key: owner for key, owner in self._runtime_owner.items() if key[:2] != provider_key
            }
            self._runtime_owner = OrderedDict(self._runtime_owner)
            self._validated_runtime = OrderedDict(
                (key, value)
                for key, value in self._validated_runtime.items()
                if key[:2] != provider_key
            )
            self._owner_generation[provider_key] = generation
            self._owner_generation.move_to_end(provider_key)
            self._trim(self._owner_generation, 512)
        key = (route.gateway_id, route.profile_name, runtime_id)
        owner = self._runtime_owner.get(key)
        if owner is not None and owner != route.stored_session_id:
            raise RouteMismatchError("Runtime session id collides with another stored session")
        self._runtime_owner[key] = route.stored_session_id
        self._runtime_owner.move_to_end(key)
        self._trim(self._runtime_owner, self._max_routes * 2)

    async def submit_prompt(
        self,
        *,
        route: SessionRoute,
        connection: ProviderConnection,
        prompt: str,
        idempotency_key: str,
        operation_id: str | None = None,
    ) -> tuple[SessionRoute, PromptReceipt]:
        self.assert_route(route, connection)
        receipt_key = (
            route.gateway_id,
            route.profile_name,
            route.stored_session_id,
            idempotency_key,
        )
        if receipt_key in self._receipts:
            self._receipts.move_to_end(receipt_key)
            return route, self._receipts[receipt_key]
        routed, _ = await self.ensure_runtime(route, connection)
        lock = self._route_lock(
            (route.gateway_id, route.profile_name, route.stored_session_id)
        )
        async with lock:
            if receipt_key in self._receipts:
                self._receipts.move_to_end(receipt_key)
                return routed, self._receipts[receipt_key]
            provider = await self.pool.get(connection)
            validation_key = (
                routed.gateway_id,
                routed.profile_name,
                routed.stored_session_id,
                routed.runtime_session_id or "",
            )
            if (
                not routed.runtime_session_id
                or self._validated_runtime.get(validation_key)
                != provider.runtime_generation
            ):
                routed = await self._resume_locked(routed, provider)
            correlation_id = operation_id or uuid4().hex
            expected_generation = provider.runtime_generation
            try:
                receipt = await provider.submit_prompt(
                    routed,
                    prompt,
                    operation_id=correlation_id,
                    expected_runtime_generation=expected_generation,
                )
            except RuntimeGenerationChanged:
                # The provider proves this exception happens before dispatch,
                # so one official resume and one retry cannot duplicate a
                # prompt. A later transport failure remains delivery_unknown.
                routed = await self._resume_locked(routed, provider)
                receipt = await provider.submit_prompt(
                    routed,
                    prompt,
                    operation_id=correlation_id,
                    expected_runtime_generation=provider.runtime_generation,
                )
            self._receipts[receipt_key] = receipt
            self._receipts.move_to_end(receipt_key)
            self._trim(self._receipts, self._max_receipts)
            return routed, receipt

    async def history(
        self, route: SessionRoute, connection: ProviderConnection
    ) -> tuple[SessionRoute, list[dict]]:
        self.assert_route(route, connection)
        lock = self._route_lock(
            (route.gateway_id, route.profile_name, route.stored_session_id)
        )
        async with lock:
            provider = await self.pool.get(connection)
            # Durable history is available through Hermes' read-only REST
            # store. Never create a runtime merely because a browser opened a
            # transcript: that would bypass the control-dev mutation guard on
            # Newton/Jarvis and make a GET observably stateful upstream.
            rows = await provider.history_readonly(route.stored_session_id)
            return route, rows

    async def interrupt(
        self, route: SessionRoute, connection: ProviderConnection
    ) -> SessionRoute:
        self.assert_route(route, connection)
        lock = self._route_lock(
            (route.gateway_id, route.profile_name, route.stored_session_id)
        )
        async with lock:
            provider = await self.pool.get(connection)
            routed = await self._ensure_locked(route, provider)
            try:
                await provider.interrupt(
                    routed,
                    expected_runtime_generation=provider.runtime_generation,
                )
            except RuntimeGenerationChanged:
                routed = await self._resume_locked(routed, provider)
                await provider.interrupt(
                    routed,
                    expected_runtime_generation=provider.runtime_generation,
                )
            return routed

    async def _ensure_locked(
        self, route: SessionRoute, provider: HermesProvider
    ) -> SessionRoute:
        key = (
            route.gateway_id,
            route.profile_name,
            route.stored_session_id,
            route.runtime_session_id or "",
        )
        if (
            route.runtime_session_id
            and self._validated_runtime.get(key) == provider.runtime_generation
        ):
            return route
        return await self._resume_locked(route, provider)

    async def _resume_locked(
        self, route: SessionRoute, provider: HermesProvider
    ) -> SessionRoute:
        resumed = await provider.resume_session(route.stored_session_id)
        if resumed.stored_session_id != route.stored_session_id:
            raise RouteMismatchError("Hermes resumed a different stored session")
        if not isinstance(resumed.runtime_session_id, str) or not resumed.runtime_session_id.strip():
            raise RouteMismatchError("Hermes resume did not return a runtime session id")
        routed = SessionRoute(
            route.gateway_id,
            route.profile_name,
            route.stored_session_id,
            resumed.runtime_session_id,
        )
        self.mark_runtime(routed, generation=provider.runtime_generation)
        return routed

    def _route_lock(self, key: tuple[str, str, str]) -> asyncio.Lock:
        lock = self._route_locks.get(key)
        if lock is not None:
            self._route_locks.move_to_end(key)
            return lock
        lock = asyncio.Lock()
        self._route_locks[key] = lock
        if len(self._route_locks) > self._max_routes:
            for old_key, old_lock in tuple(self._route_locks.items()):
                if (
                    not old_lock.locked()
                    and not getattr(old_lock, "_waiters", None)
                    and old_key != key
                ):
                    self._route_locks.pop(old_key, None)
                    break
        return lock

    @staticmethod
    def _trim(mapping: OrderedDict, limit: int) -> None:
        while len(mapping) > limit:
            mapping.popitem(last=False)
