from __future__ import annotations

import base64

import pytest

from hermes_client import (
    CapabilitySet,
    HermesProfile,
    HermesSessionRouter,
    ProviderConnection,
    ProviderPool,
)
from hermes_control_api.config import Settings
from hermes_control_api.database import Base, build_engine, build_session_factory
from hermes_control_api.eventing import EventHub
from hermes_control_api.models import Gateway, ProfileRef
from hermes_control_api.providers import FailoverProvider, authoritative_provider_read
from hermes_control_api.security import SecretVault
from hermes_control_api.services import AppServices, ProfileService


class ScopedProbeProvider:
    def __init__(self, connection: ProviderConnection, calls: list[tuple[str, str]]) -> None:
        self.connection = connection
        self.calls = calls

    @property
    def runtime_generation(self) -> int:
        return 1

    async def list_profiles(self):
        self.calls.append(("profiles", self.connection.profile_name))
        assert self.connection.profile_name == "default"
        return [
            # Official 0.20.5/0.20.6 profiles.list does not carry status.
            HermesProfile("default", "default", "unknown", "model-newton"),
            HermesProfile("jarvis", "jarvis", "unknown", "model-jarvis"),
            HermesProfile("control-dev", "Control Dev", "unknown", "model-dev"),
        ]

    async def capabilities(self):
        profile = self.connection.profile_name
        self.calls.append(("capabilities", profile))
        if profile == "jarvis":
            raise OSError("profile-scoped socket unavailable")
        return CapabilitySet(
            version=f"version-{profile}",
            source_sha=f"sha-{profile}",
            methods=frozenset({f"profile.{profile}"}),
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_profile_sync_probes_capabilities_per_gateway_profile_route():
    settings = Settings(
        environment="test",
        database_url="sqlite://",
        vault_key_b64=base64.urlsafe_b64encode(b"p" * 32).decode("ascii"),
        provider_mode="real",
    )
    engine = build_engine(settings)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)
    calls: list[tuple[str, str]] = []
    pool = ProviderPool(lambda connection: ScopedProbeProvider(connection, calls))
    services = AppServices(
        settings=settings,
        vault=SecretVault(settings.materialize_vault_key()),
        event_hub=EventHub(),
        provider_pool=pool,
        session_router=HermesSessionRouter(pool),
    )
    try:
        with factory() as db:
            gateway = Gateway(
                name="Scoped gateway",
                rest_url="http://127.0.0.1:19119",
                ws_url="ws://127.0.0.1:19119/api/ws",
                connection_mode="tunnel",
                enabled=True,
            )
            db.add(gateway)
            db.commit()
            db.refresh(gateway)

            rows = await ProfileService(services).sync(db, gateway.id)
            by_name = {row.profile_name: row for row in rows}

            assert calls == [
                ("profiles", "default"),
                ("capabilities", "default"),
                ("capabilities", "jarvis"),
                ("capabilities", "control-dev"),
            ]
            assert by_name["default"].capabilities["methods"] == ["profile.default"]
            assert by_name["control-dev"].capabilities["methods"] == [
                "profile.control-dev"
            ]
            assert by_name["jarvis"].capabilities == {}
            assert by_name["jarvis"].status == "degraded"
            assert by_name["default"].status == "online"
            assert by_name["control-dev"].status == "online"
            assert by_name["default"].display_name == "Newton"
            assert by_name["jarvis"].display_name == "Jarvis"
            assert db.get(Gateway, gateway.id).health_status == "degraded"
    finally:
        await pool.close()
        engine.dispose()


class FailingAuthoritativeProvider:
    def __init__(self) -> None:
        self.connection = ProviderConnection(
            "gateway", "default", "http://127.0.0.1", "ws://127.0.0.1/api/ws"
        )
        self.capability_calls = 0

    async def capabilities(self):
        self.capability_calls += 1
        raise OSError("real provider unavailable")


class DangerousFallbackProvider:
    def __init__(self, connection: ProviderConnection) -> None:
        self.connection = connection
        self.capability_calls = 0

    async def capabilities(self):
        self.capability_calls += 1
        return CapabilitySet(
            version="synthetic",
            methods=frozenset({"must.not.be.certified"}),
        )


@pytest.mark.asyncio
async def test_authoritative_probe_never_certifies_in_memory_fallback():
    real = FailingAuthoritativeProvider()
    fallback = DangerousFallbackProvider(real.connection)
    provider = FailoverProvider(real, fallback, allow_fallback=True)  # type: ignore[arg-type]

    with pytest.raises(OSError, match="real provider unavailable"):
        await authoritative_provider_read(provider, "capabilities")

    assert real.capability_calls == 1
    assert fallback.capability_calls == 0
    assert provider.active is real
