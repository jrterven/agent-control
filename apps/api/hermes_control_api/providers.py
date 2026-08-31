from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from hermes_client import (
    HermesGatewayProvider,
    HermesProvider,
    InMemoryHermesProvider,
    ProviderConnection,
    ProviderPool,
)

from .config import Settings


async def authoritative_provider_read(provider: HermesProvider, operation: str):
    """Run a read against the configured Hermes identity, never the synthetic fallback.

    Explicit ``provider_mode=mock`` still uses its provider directly. In auto
    mode a FailoverProvider is unwrapped so capability/profile discovery cannot
    accidentally certify in-memory capabilities for a real profile.
    """

    if operation not in {"capabilities", "list_profiles"}:
        raise ValueError("Only authoritative read operations are allowed")
    target = provider.real if isinstance(provider, FailoverProvider) else provider
    return await getattr(target, operation)()


class FailoverProvider:
    """Uses the deterministic provider only for confirmed connection failures.

    It never retries or fails over an ambiguous prompt submission.
    """

    def __init__(
        self,
        real: HermesGatewayProvider,
        fallback: InMemoryHermesProvider,
        *,
        allow_fallback: bool,
    ) -> None:
        self.connection = real.connection
        self.real = real
        self.fallback = fallback
        self.allow_fallback = allow_fallback
        self.active: HermesProvider = real

    @property
    def runtime_generation(self) -> str:
        return self.active.runtime_generation

    @property
    def session_inventory_complete(self) -> bool:
        return bool(getattr(self.active, "session_inventory_complete", False))

    async def _call(self, operation: str, *args, prompt: bool = False, **kwargs):
        if self.active is self.fallback and operation == "capabilities":
            try:
                result = await self.real.capabilities()
                self.active = self.real
                return result
            except (ConnectionError, OSError, TimeoutError):
                pass
        if self.active is self.fallback and operation not in {
            "capabilities",
            "list_profiles",
        }:
            raise ConnectionError("Mock fallback is read-only; select explicit mock mode")
        method = getattr(self.active, operation)
        try:
            return await method(*args, **kwargs)
        except RuntimeError as exc:
            if prompt and str(exc) == "PROMPT_DELIVERY_UNKNOWN":
                raise
            # JSON-RPC/application errors are RuntimeError subclasses. They
            # must surface instead of silently switching identities to mock.
            raise
        except (ConnectionError, OSError, TimeoutError):
            if not self.allow_fallback:
                raise
            if operation not in {"capabilities", "list_profiles"}:
                # Never move a real session, prompt or cron operation to a
                # synthetic identity. Full mock operation is explicit.
                raise
        if prompt:
            raise ConnectionError("Prompt fallback requires explicit mock mode")
        return await getattr(self.fallback, operation)(*args, **kwargs)

    async def capabilities(self):
        return await self._call("capabilities")

    async def list_profiles(self):
        return await self._call("list_profiles")

    async def create_profile(self, *, name, display_name):
        return await self._call(
            "create_profile",
            name=name,
            display_name=display_name,
        )

    async def delete_profile(self, name):
        return await self._call("delete_profile", name)

    async def transfer_profile_to(self, destination, *, name):
        target = (
            destination.real
            if isinstance(destination, FailoverProvider)
            else destination
        )
        return await self._call(
            "transfer_profile_to",
            target,
            name=name,
        )

    async def list_sessions(self):
        return await self._call("list_sessions")

    async def search_sessions(self, query, *, limit=20):
        return await self._call("search_sessions", query, limit=limit)

    async def create_session(self, *, title=None):
        return await self._call("create_session", title=title)

    async def resume_session(self, stored_session_id):
        return await self._call("resume_session", stored_session_id)

    async def history(self, route):
        return await self._call("history", route)

    async def history_readonly(self, stored_session_id):
        return await self._call("history_readonly", stored_session_id)

    async def submit_prompt(self, route, prompt, *, operation_id):
        return await self._call(
            "submit_prompt", route, prompt, operation_id=operation_id, prompt=True
        )

    async def attach_prompt_attachment(
        self,
        route,
        attachment,
        *,
        expected_runtime_generation=None,
    ):
        return await self._call(
            "attach_prompt_attachment",
            route,
            attachment,
            expected_runtime_generation=expected_runtime_generation,
        )

    async def detach_prompt_images(
        self,
        route,
        paths,
        *,
        expected_runtime_generation=None,
    ):
        return await self._call(
            "detach_prompt_images",
            route,
            paths,
            expected_runtime_generation=expected_runtime_generation,
        )

    async def interrupt(self, route):
        return await self._call("interrupt", route)

    async def respond_approval(
        self,
        route,
        request_id,
        choice,
        *,
        expected_runtime_generation=None,
    ):
        return await self._call(
            "respond_approval",
            route,
            request_id,
            choice,
            expected_runtime_generation=expected_runtime_generation,
        )

    async def respond_clarification(
        self,
        route,
        request_id,
        answer,
        *,
        question_id=None,
        expected_runtime_generation=None,
    ):
        return await self._call(
            "respond_clarification",
            route,
            request_id,
            answer,
            question_id=question_id,
            expected_runtime_generation=expected_runtime_generation,
        )

    async def delete_session(self, route):
        return await self._call("delete_session", route)

    async def list_automations(self):
        return await self._call("list_automations")

    async def create_automation(self, automation):
        return await self._call("create_automation", automation)

    async def update_automation(self, automation_id, changes):
        return await self._call("update_automation", automation_id, changes)

    async def delete_automation(self, automation_id):
        return await self._call("delete_automation", automation_id)

    async def trigger_automation(self, automation_id):
        return await self._call("trigger_automation", automation_id)

    async def list_automation_runs(self, automation_id, *, limit=100):
        return await self._call("list_automation_runs", automation_id, limit=limit)

    async def list_models(self):
        return await self._call("list_models")

    async def set_model(self, provider, model, *, confirm_expensive_model=False):
        return await self._call(
            "set_model",
            provider,
            model,
            confirm_expensive_model=confirm_expensive_model,
        )

    async def get_config(self):
        return await self._call("get_config")

    async def update_config(self, config):
        return await self._call("update_config", config)

    async def get_soul(self):
        return await self._call("get_soul")

    async def update_soul(self, content):
        return await self._call("update_soul", content)

    async def get_memory(self):
        return await self._call("get_memory")

    async def set_memory_provider(self, name):
        return await self._call("set_memory_provider", name)

    async def reset_memory(self, target):
        return await self._call("reset_memory", target)

    async def list_skills(self):
        return await self._call("list_skills")

    async def toggle_skill(self, name, enabled):
        return await self._call("toggle_skill", name, enabled)

    async def list_toolsets(self):
        return await self._call("list_toolsets")

    async def toggle_toolset(self, name, enabled):
        return await self._call("toggle_toolset", name, enabled)

    async def list_mcp_servers(self):
        return await self._call("list_mcp_servers")

    async def create_mcp_server(self, server):
        return await self._call("create_mcp_server", server)

    async def delete_mcp_server(self, name):
        return await self._call("delete_mcp_server", name)

    async def toggle_mcp_server(self, name, enabled):
        return await self._call("toggle_mcp_server", name, enabled)

    async def test_mcp_server(self, name):
        return await self._call("test_mcp_server", name)

    async def list_channels(self):
        return await self._call("list_channels")

    async def update_channel(self, name, changes):
        return await self._call("update_channel", name, changes)

    async def test_channel(self, name):
        return await self._call("test_channel", name)

    async def get_usage(self, *, days=30):
        return await self._call("get_usage", days=days)

    async def list_secrets(self):
        return await self._call("list_secrets")

    async def set_secret(self, name, value):
        return await self._call("set_secret", name, value)

    async def delete_secret(self, name):
        return await self._call("delete_secret", name)

    async def close(self):
        await self.real.close()
        await self.fallback.close()


def build_provider_pool(
    settings: Settings,
    event_sink: Callable[[Any], Awaitable[None]],
) -> ProviderPool:
    def factory(connection: ProviderConnection) -> HermesProvider:
        mock = InMemoryHermesProvider(connection, event_sink)
        if settings.provider_mode == "mock":
            return mock
        real = HermesGatewayProvider(connection, event_sink)
        if settings.provider_mode == "real":
            return real
        return FailoverProvider(real, mock, allow_fallback=settings.mock_fallback_enabled)

    return ProviderPool(factory)
