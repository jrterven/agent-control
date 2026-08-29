from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from hermes_client import (
    CapabilitySet,
    EndpointPolicy,
    EventNormalizer,
    HermesAutomation,
    HermesRunReceipt,
    HermesSessionRouter,
    JsonRpcError,
    ProviderConnection,
    ProviderPool,
    RuntimeGenerationChanged,
    SessionHistoryNotFound,
    SessionRoute,
    resolve_endpoint,
    validate_endpoint,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .eventing import EventHub
from .gateway_health import aggregate_profile_health
from .models import (
    AuditEvent,
    Automation,
    AutomationRun,
    AuthSession,
    Gateway,
    GatewayCredential,
    IdempotencyOperation,
    ProfileRef,
    RealtimeTicket,
    SessionLink,
    User,
    Workspace,
    utc_now,
)
from .providers import authoritative_provider_read
from .realtime import persist_normalized_event
from .schemas import AutomationCreate, GatewayCreate, GatewayUpdate, SessionCreate, WorkspaceCreate
from .security import SecretVault, random_token, token_hash


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


class UpstreamUnavailableError(ConnectionError):
    pass


# Every current Control operation that can change Hermes state is centralized
# here. require_capability applies the profile guard before consulting a cache
# or touching a provider, so a permissive/stale capability set cannot bypass it.
UPSTREAM_MUTATION_CAPABILITIES: frozenset[str] = frozenset(
    {
        "session.create",
        "session.resume",
        "prompt.submit",
        "session.interrupt",
        "approval.respond",
        "clarify.respond",
        "session.delete",
        "cron.create",
        "cron.update",
        "cron.delete",
        "cron.trigger",
        "models.set",
        "config.set",
        "soul.set",
        "memory.provider.set",
        "memory.reset",
        "skills.toggle",
        "toolsets.toggle",
        "mcp.create",
        "mcp.delete",
        "mcp.toggle",
        "mcp.test",
        "channels.update",
        "channels.test",
        "secrets.set",
        "secrets.delete",
    }
)

# Narrow, user-facing conversational writes. Membership here never authorizes
# destructive or administrative methods; those still require mutable_profiles.
INTERACTIVE_MUTATION_CAPABILITIES: frozenset[str] = frozenset(
    {
        "session.create",
        "session.resume",
        "prompt.submit",
        "session.interrupt",
        "approval.respond",
        "clarify.respond",
    }
)


def mutation_allowed_for_profile(
    profile_name: str,
    capability: str,
    mutable_profiles: list[str] | tuple[str, ...] | frozenset[str],
    interactive_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
) -> bool:
    if capability not in UPSTREAM_MUTATION_CAPABILITIES:
        return True
    if profile_name in mutable_profiles:
        return True
    return (
        capability in INTERACTIVE_MUTATION_CAPABILITIES
        and profile_name in interactive_profiles
    )


def require_mutable_profile(
    profile_name: str,
    capability: str,
    mutable_profiles: list[str] | tuple[str, ...] | frozenset[str],
    interactive_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
) -> None:
    if not mutation_allowed_for_profile(
        profile_name,
        capability,
        mutable_profiles,
        interactive_profiles,
    ):
        raise ConflictError(
            "Hermes mutations are not allowed for this profile by the operator"
        )


def capabilities_for_profile(
    capabilities: CapabilitySet,
    profile_name: str,
    mutable_profiles: list[str] | tuple[str, ...] | frozenset[str],
    *,
    interactive_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
    trusted_source_sha_configured: bool,
) -> CapabilitySet:
    methods = capabilities.methods
    if not trusted_source_sha_configured:
        methods = frozenset(
            method
            for method in methods
            if method not in UPSTREAM_MUTATION_CAPABILITIES
        )
    else:
        methods = frozenset(
            method
            for method in methods
            if mutation_allowed_for_profile(
                profile_name,
                method,
                mutable_profiles,
                interactive_profiles,
            )
        )
    return CapabilitySet(
        protocol=capabilities.protocol,
        version=capabilities.version,
        source_sha=capabilities.source_sha,
        methods=methods,
        features=capabilities.features,
    )


def fresh_profile_capabilities(
    profile: ProfileRef | None,
    *,
    now: datetime,
    ttl_seconds: int,
) -> dict[str, Any]:
    """Return a cached contract only inside the verification TTL."""

    if profile is None or profile.capabilities_checked_at is None:
        return {}
    checked_at = profile.capabilities_checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    else:
        checked_at = checked_at.astimezone(timezone.utc)
    current = (
        now.replace(tzinfo=timezone.utc)
        if now.tzinfo is None
        else now.astimezone(timezone.utc)
    )
    if (
        checked_at < current - timedelta(seconds=ttl_seconds)
        or checked_at > current + timedelta(seconds=5)
    ):
        return {}
    return dict(profile.capabilities or {})


@dataclass(slots=True)
class AppServices:
    settings: Settings
    vault: SecretVault
    event_hub: EventHub
    provider_pool: ProviderPool
    session_router: HermesSessionRouter
    session_factory: Any | None = None


def record_profile_health(
    db: Session,
    services: AppServices,
    profile: ProfileRef,
    *,
    status: str,
    observed_at: datetime,
) -> None:
    """Update one known route and recompute its gateway fail-closed."""

    profile.status = status
    profile.last_seen_at = observed_at
    gateway = db.get(Gateway, profile.gateway_id)
    if gateway is None or not gateway.enabled:
        return
    required_profiles = list(
        db.scalars(
            select(ProfileRef).where(ProfileRef.gateway_id == profile.gateway_id)
        ).all()
    )
    gateway.health_status = aggregate_profile_health(
        required_profiles,
        at=observed_at,
        ttl_seconds=services.settings.upstream_health_ttl_seconds,
    )
    gateway.last_health_at = observed_at


def trusted_gateway_source_sha(
    db: Session, services: AppServices, gateway_id: str
) -> str | None:
    """Return a valid operator anchor, never Gateway.source_sha diagnostics."""

    credential = db.scalar(
        select(GatewayCredential).where(GatewayCredential.gateway_id == gateway_id)
    )
    if credential is None or not credential.trusted_source_sha_ciphertext:
        return None
    try:
        value = services.vault.decrypt(
            credential.trusted_source_sha_ciphertext,
            aad=f"gateway:{gateway_id}:source-sha",
        )
    except ValueError:
        return None
    if (
        value is None
        or len(value) != 40
        or any(char not in "0123456789abcdef" for char in value)
    ):
        return None
    return value


async def require_capability(
    db: Session,
    services: AppServices,
    *,
    gateway_id: str,
    profile_name: str,
    method: str,
) -> None:
    require_mutable_profile(
        profile_name,
        method,
        services.settings.mutable_profiles,
        services.settings.interactive_profiles,
    )
    trusted_source_sha = trusted_gateway_source_sha(
        db, services, gateway_id
    )
    if (
        method in UPSTREAM_MUTATION_CAPABILITIES
        and trusted_source_sha is None
    ):
        raise ConflictError(
            "Hermes mutations require an operator-trusted full source SHA for this gateway"
        )
    profile = db.scalar(
        select(ProfileRef).where(
            ProfileRef.gateway_id == gateway_id,
            ProfileRef.profile_name == profile_name,
        )
    )
    now = utc_now()
    cached_capabilities = fresh_profile_capabilities(
        profile,
        now=now,
        ttl_seconds=services.settings.capability_ttl_seconds,
    )
    methods = set(cached_capabilities.get("methods", []))
    if not cached_capabilities or method not in methods:
        connection = await GatewayService(services).connection(db, gateway_id, profile_name)
        provider = await services.provider_pool.get(connection)
        try:
            capabilities = await authoritative_provider_read(provider, "capabilities")
        except Exception:
            if profile is not None:
                record_profile_health(
                    db,
                    services,
                    profile,
                    status="degraded",
                    observed_at=now,
                )
                profile.capabilities = {}
                profile.capabilities_checked_at = None
                db.commit()
            raise
        capabilities = capabilities_for_profile(
            capabilities,
            profile_name,
            services.settings.mutable_profiles,
            interactive_profiles=services.settings.interactive_profiles,
            trusted_source_sha_configured=trusted_source_sha is not None,
        )
        methods = set(capabilities.methods)
        if profile is not None:
            record_profile_health(
                db,
                services,
                profile,
                status="online",
                observed_at=now,
            )
            profile.capabilities = capabilities.to_dict()
            profile.capabilities_checked_at = now
            db.commit()
    if method not in methods:
        raise ConflictError(f"Hermes capability {method} is not verified for this profile")


def audit(
    db: Session,
    *,
    actor: User | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    outcome: str = "success",
    details: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    safe_details = {
        key: value
        for key, value in (details or {}).items()
        if not any(term in key.lower() for term in ("token", "secret", "password", "key"))
    }
    db.add(
        AuditEvent(
            actor_user_id=actor.id if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            outcome=outcome,
            details=safe_details,
            request_id=request_id,
        )
    )


class GatewayService:
    def __init__(self, services: AppServices) -> None:
        self.services = services

    def seed_environment_gateway(self, db: Session) -> Gateway:
        settings = self.services.settings
        configured_dashboard_token = settings.hermes_dashboard_token or None
        configured_api_key = settings.hermes_api_key or None
        gateway = db.scalar(select(Gateway).where(Gateway.env_managed.is_(True)))
        configuration_changed = False
        if gateway is None:
            gateway = Gateway(
                name=settings.default_gateway_name,
                rest_url=settings.hermes_dashboard_url,
                ws_url=settings.hermes_dashboard_ws,
                api_url=settings.hermes_api_url,
                connection_mode="tunnel",
                enabled=True,
                env_managed=True,
            )
            db.add(gateway)
            db.flush()
        else:
            configuration_changed = any(
                (
                    gateway.rest_url != settings.hermes_dashboard_url,
                    gateway.ws_url != settings.hermes_dashboard_ws,
                    gateway.api_url != settings.hermes_api_url,
                )
            )
            gateway.name = settings.default_gateway_name
            gateway.rest_url = settings.hermes_dashboard_url
            gateway.ws_url = settings.hermes_dashboard_ws
            gateway.api_url = settings.hermes_api_url
            if (
                settings.hermes_source_sha is not None
                and str(gateway.source_sha or "").lower()
                == settings.hermes_source_sha
            ):
                # Scrub values written by older releases. A successful probe
                # will restore the same value only if Hermes itself reports it.
                gateway.source_sha = None
        credential = db.scalar(
            select(GatewayCredential).where(GatewayCredential.gateway_id == gateway.id)
        )
        if credential is None:
            credential = GatewayCredential(gateway_id=gateway.id)
            db.add(credential)
            configuration_changed = configuration_changed or gateway.env_managed
        else:
            try:
                old_dashboard_token = self.services.vault.decrypt(
                    credential.dashboard_token_ciphertext,
                    aad=f"gateway:{gateway.id}:dashboard",
                )
                old_api_key = self.services.vault.decrypt(
                    credential.api_key_ciphertext,
                    aad=f"gateway:{gateway.id}:api",
                )
                old_trusted_source_sha = self.services.vault.decrypt(
                    credential.trusted_source_sha_ciphertext,
                    aad=f"gateway:{gateway.id}:source-sha",
                )
            except ValueError:
                # A credential that cannot be authenticated can never retain
                # capabilities certified under its previous connection.
                old_dashboard_token = old_api_key = old_trusted_source_sha = object()
            if (
                isinstance(old_trusted_source_sha, str)
                and str(gateway.source_sha or "").lower()
                == old_trusted_source_sha.lower()
            ):
                # Scrub a trust value copied into diagnostics by an older
                # build even when the environment variable is now absent.
                gateway.source_sha = None
            configuration_changed = configuration_changed or any(
                (
                    old_dashboard_token != configured_dashboard_token,
                    old_api_key != configured_api_key,
                    old_trusted_source_sha != settings.hermes_source_sha,
                )
            )
        credential.dashboard_token_ciphertext = (
            self.services.vault.encrypt(
                configured_dashboard_token, aad=f"gateway:{gateway.id}:dashboard"
            )
            if configured_dashboard_token
            else None
        )
        credential.api_key_ciphertext = (
            self.services.vault.encrypt(
                configured_api_key, aad=f"gateway:{gateway.id}:api"
            )
            if configured_api_key
            else None
        )
        # The environment is authoritative for this env-managed gateway.
        # Removing/emptying the setting explicitly revokes prior trust; a
        # reported Gateway.source_sha remains diagnostics and is never copied
        # into this authenticated backend value.
        credential.trusted_source_sha_ciphertext = (
            self.services.vault.encrypt(
                settings.hermes_source_sha,
                aad=f"gateway:{gateway.id}:source-sha",
            )
            if settings.hermes_source_sha is not None
            else None
        )
        for name in settings.default_profiles:
            existing = db.scalar(
                select(ProfileRef).where(
                    ProfileRef.gateway_id == gateway.id, ProfileRef.profile_name == name
                )
            )
            if existing is None:
                display = {"default": "Newton", "jarvis": "Jarvis"}.get(name, name)
                db.add(
                    ProfileRef(
                        gateway_id=gateway.id,
                        profile_name=name,
                        display_name=display,
                    )
                )
        if configuration_changed:
            for profile in db.scalars(
                select(ProfileRef).where(ProfileRef.gateway_id == gateway.id)
            ):
                profile.capabilities = {}
                profile.capabilities_checked_at = None
                profile.status = "unknown"
                profile.last_seen_at = None
            gateway.health_status = "unknown"
            gateway.last_health_at = None
            gateway.version = None
            gateway.source_sha = None
        db.commit()
        db.refresh(gateway)
        return gateway

    async def create(self, db: Session, actor: User, payload: GatewayCreate) -> Gateway:
        await validate_endpoint(
            payload.rest_url,
            self._policy(payload.connection_mode, frozenset({"http", "https"})),
        )
        await validate_endpoint(
            payload.ws_url,
            self._policy(payload.connection_mode, frozenset({"ws", "wss"})),
        )
        if payload.api_url:
            await validate_endpoint(
                payload.api_url,
                self._policy(payload.connection_mode, frozenset({"http", "https"})),
            )
        gateway = Gateway(
            name=payload.name,
            rest_url=payload.rest_url.rstrip("/"),
            ws_url=payload.ws_url,
            api_url=payload.api_url.rstrip("/") if payload.api_url else None,
            connection_mode=payload.connection_mode,
            env_managed=False,
        )
        db.add(gateway)
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise ConflictError("A gateway with that name already exists") from exc
        db.add(
            GatewayCredential(
                gateway_id=gateway.id,
                dashboard_token_ciphertext=self.services.vault.encrypt(
                    payload.dashboard_token, aad=f"gateway:{gateway.id}:dashboard"
                ),
                api_key_ciphertext=self.services.vault.encrypt(
                    payload.api_key, aad=f"gateway:{gateway.id}:api"
                ),
                trusted_source_sha_ciphertext=self.services.vault.encrypt(
                    payload.trusted_source_sha,
                    aad=f"gateway:{gateway.id}:source-sha",
                ),
            )
        )
        audit(db, actor=actor, action="gateway.create", target_type="gateway", target_id=gateway.id)
        db.commit()
        db.refresh(gateway)
        return gateway

    async def update(
        self, db: Session, actor: User, gateway: Gateway, payload: GatewayUpdate
    ) -> Gateway:
        if gateway.env_managed:
            raise ConflictError("Environment-managed gateway is read-only")
        values = payload.model_dump(exclude_unset=True)
        connection_fields = {
            "rest_url",
            "ws_url",
            "api_url",
            "connection_mode",
            "dashboard_token",
            "api_key",
            "trusted_source_sha",
        }
        connection_changed = bool(connection_fields.intersection(values))
        if values.get("rest_url", gateway.rest_url) is None or values.get("ws_url", gateway.ws_url) is None:
            raise ConflictError("Dashboard REST and WebSocket URLs cannot be cleared")
        mode = values.get("connection_mode", gateway.connection_mode)
        for field in ("rest_url", "ws_url", "api_url"):
            endpoint = values.get(field, getattr(gateway, field))
            if endpoint:
                schemes = (
                    frozenset({"ws", "wss"})
                    if field == "ws_url"
                    else frozenset({"http", "https"})
                )
                await validate_endpoint(endpoint, self._policy(mode, schemes))
        credential = db.scalar(
            select(GatewayCredential).where(GatewayCredential.gateway_id == gateway.id)
        )
        if credential is None:
            credential = GatewayCredential(gateway_id=gateway.id)
            db.add(credential)
        if "dashboard_token" in values:
            credential.dashboard_token_ciphertext = self.services.vault.encrypt(
                values.pop("dashboard_token"), aad=f"gateway:{gateway.id}:dashboard"
            )
        if "api_key" in values:
            credential.api_key_ciphertext = self.services.vault.encrypt(
                values.pop("api_key"), aad=f"gateway:{gateway.id}:api"
            )
        if "trusted_source_sha" in values:
            credential.trusted_source_sha_ciphertext = self.services.vault.encrypt(
                values.pop("trusted_source_sha"),
                aad=f"gateway:{gateway.id}:source-sha",
            )
        for key, value in values.items():
            setattr(gateway, key, value.rstrip("/") if key in {"rest_url", "api_url"} and value else value)
        if connection_changed:
            profiles = db.scalars(
                select(ProfileRef).where(ProfileRef.gateway_id == gateway.id)
            ).all()
            for profile in profiles:
                # A capability assertion belongs to the exact endpoint and
                # credential set that produced it. Never carry it across a
                # gateway reconfiguration, even within the normal TTL.
                profile.capabilities = {}
                profile.last_seen_at = None
                profile.capabilities_checked_at = None
                profile.status = "unknown"
            gateway.health_status = "unknown"
            gateway.last_health_at = None
            gateway.version = None
            gateway.source_sha = None
        audit(
            db,
            actor=actor,
            action="gateway.update",
            target_type="gateway",
            target_id=gateway.id,
        )
        db.commit()
        db.refresh(gateway)
        await self.services.provider_pool.invalidate(gateway.id)
        return gateway

    async def connection(self, db: Session, gateway_id: str, profile_name: str) -> ProviderConnection:
        gateway = db.get(Gateway, gateway_id)
        if gateway is None or not gateway.enabled:
            raise NotFoundError("Gateway not found or disabled")
        rest_endpoint = await resolve_endpoint(
            gateway.rest_url,
            self._policy(gateway.connection_mode, frozenset({"http", "https"})),
        )
        ws_endpoint = await resolve_endpoint(
            gateway.ws_url,
            self._policy(gateway.connection_mode, frozenset({"ws", "wss"})),
        )
        api_endpoint = (
            await resolve_endpoint(
                gateway.api_url,
                self._policy(gateway.connection_mode, frozenset({"http", "https"})),
            )
            if gateway.api_url
            else None
        )
        credential = db.scalar(
            select(GatewayCredential).where(GatewayCredential.gateway_id == gateway.id)
        )
        dashboard_token = api_key = None
        if credential is not None:
            dashboard_token = self.services.vault.decrypt(
                credential.dashboard_token_ciphertext, aad=f"gateway:{gateway.id}:dashboard"
            )
            api_key = self.services.vault.decrypt(
                credential.api_key_ciphertext, aad=f"gateway:{gateway.id}:api"
            )
        trusted_source_sha = trusted_gateway_source_sha(
            db, self.services, gateway.id
        )
        return ProviderConnection(
            gateway_id=gateway.id,
            profile_name=profile_name,
            rest_url=gateway.rest_url,
            ws_url=gateway.ws_url,
            api_url=gateway.api_url if profile_name == "control-dev" else None,
            dashboard_token=dashboard_token,
            api_key=api_key if profile_name == "control-dev" else None,
            rest_connect_host=rest_endpoint.addresses[0],
            ws_connect_host=ws_endpoint.addresses[0],
            api_connect_host=(
                api_endpoint.addresses[0]
                if profile_name == "control-dev" and api_endpoint is not None
                else None
            ),
            trusted_source_sha=trusted_source_sha,
        )

    async def probe(self, db: Session, actor: User, gateway_id: str, profile_name: str):
        gateway = db.get(Gateway, gateway_id)
        if gateway is None:
            raise NotFoundError("Gateway not found")
        profile = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == gateway_id,
                ProfileRef.profile_name == profile_name,
            )
        )
        connection = await self.connection(db, gateway_id, profile_name)
        provider = await self.services.provider_pool.get(connection)
        observed_at = utc_now()

        def update_health(status: str) -> None:
            # Unknown profile names are not allowed to create/control gateway
            # health. Profile discovery/configuration establishes membership.
            if profile is None:
                return
            record_profile_health(
                db,
                self.services,
                profile,
                status=status,
                observed_at=observed_at,
            )

        try:
            capabilities = capabilities_for_profile(
                await provider.capabilities(),
                profile_name,
                self.services.settings.mutable_profiles,
                interactive_profiles=self.services.settings.interactive_profiles,
                trusted_source_sha_configured=bool(connection.trusted_source_sha),
            )
            update_health("online")
            if profile is not None:
                profile.capabilities = capabilities.to_dict()
                profile.capabilities_checked_at = observed_at
            gateway.version = capabilities.version
            # A previous release could copy the write-only operator anchor
            # here. Successful discovery always replaces diagnostics with the
            # value Hermes actually reported, including clearing it when the
            # official status response omits a revision.
            gateway.source_sha = capabilities.source_sha
            outcome = "success"
        except Exception as exc:
            update_health("offline")
            if profile is not None:
                profile.capabilities = {}
                profile.capabilities_checked_at = None
            outcome = "failure"
            audit(
                db,
                actor=actor,
                action="gateway.probe",
                target_type="gateway",
                target_id=gateway.id,
                outcome=outcome,
                details={"errorType": type(exc).__name__},
            )
            db.commit()
            raise UpstreamUnavailableError("Hermes gateway is unavailable") from exc
        audit(
            db,
            actor=actor,
            action="gateway.probe",
            target_type="gateway",
            target_id=gateway.id,
            outcome=outcome,
        )
        db.commit()
        return capabilities

    def _policy(
        self,
        connection_mode: str,
        allowed_schemes: frozenset[str] = frozenset({"http", "https", "ws", "wss"}),
    ) -> EndpointPolicy:
        private = connection_mode in {"private", "tunnel"}
        if not private:
            allowed_schemes = frozenset(
                scheme for scheme in allowed_schemes if scheme in {"https", "wss"}
            )
        return EndpointPolicy(
            allow_loopback=private,
            allow_private=private,
            allowed_schemes=allowed_schemes,
        )


class ProfileService:
    def __init__(self, services: AppServices) -> None:
        self.services = services
        self.gateway_service = GatewayService(services)

    async def sync(self, db: Session, gateway_id: str, profile_name: str = "default") -> list[ProfileRef]:
        discovery_connection = await self.gateway_service.connection(
            db, gateway_id, profile_name
        )
        discovery_provider = await self.services.provider_pool.get(discovery_connection)
        discovered = await authoritative_provider_read(discovery_provider, "list_profiles")
        profiles = list({profile.name: profile for profile in discovered}.values())
        now = utc_now()
        gateway = db.get(Gateway, gateway_id)
        scoped_capabilities: dict[str, Any] = {}
        for profile in profiles:
            try:
                scoped_connection = await self.gateway_service.connection(
                    db, gateway_id, profile.name
                )
                scoped_provider = await self.services.provider_pool.get(scoped_connection)
                scoped_capabilities[profile.name] = capabilities_for_profile(
                    await authoritative_provider_read(
                        scoped_provider, "capabilities"
                    ),
                    profile.name,
                    self.services.settings.mutable_profiles,
                    interactive_profiles=self.services.settings.interactive_profiles,
                    trusted_source_sha_configured=bool(
                        scoped_connection.trusted_source_sha
                    ),
                )
            except Exception:
                # Discovery remains useful, but an unverified profile must not
                # inherit capabilities from another route or from mock fallback.
                pass
        for profile in profiles:
            display_name = {
                "default": "Newton",
                "jarvis": "Jarvis",
            }.get(profile.name, profile.display_name)
            row = db.scalar(
                select(ProfileRef).where(
                    ProfileRef.gateway_id == gateway_id,
                    ProfileRef.profile_name == profile.name,
                )
            )
            if row is None:
                row = ProfileRef(
                    gateway_id=gateway_id,
                    profile_name=profile.name,
                    display_name=display_name,
                )
                db.add(row)
            # Hermes 0.20.5 may return only the technical name. Preserve the
            # operator-confirmed aliases used throughout Agent Control.
            row.display_name = display_name
            row.model = profile.model
            capability = scoped_capabilities.get(profile.name)
            # Official 0.20.5/0.20.6 profiles.list omits status. The scoped
            # capability request is the actual per-profile liveness proof.
            row.status = "online" if capability is not None else "degraded"
            row.capabilities = capability.to_dict() if capability is not None else {}
            row.last_seen_at = now
            if capability is not None:
                row.capabilities_checked_at = now
            else:
                row.capabilities_checked_at = None

        discovered_names = {profile.name for profile in profiles}
        # Sessions intentionally disable autoflush. Persist newly discovered
        # profile membership before calculating the fail-closed gateway set.
        db.flush()
        cached_profiles = db.scalars(
            select(ProfileRef).where(ProfileRef.gateway_id == gateway_id)
        ).all()
        for cached in cached_profiles:
            if cached.profile_name not in discovered_names:
                cached.status = "offline"
                cached.capabilities = {}
                cached.capabilities_checked_at = None
                cached.last_seen_at = now

        if gateway is not None:
            gateway.health_status = aggregate_profile_health(
                cached_profiles,
                at=now,
                ttl_seconds=self.services.settings.upstream_health_ttl_seconds,
            )
            gateway.last_health_at = now
            primary_capability = scoped_capabilities.get(profile_name)
            if primary_capability is not None:
                gateway.version = primary_capability.version
                gateway.source_sha = primary_capability.source_sha
        db.commit()
        return list(
            db.scalars(select(ProfileRef).where(ProfileRef.gateway_id == gateway_id)).all()
        )


class WorkspaceService:
    def create(self, db: Session, actor: User, payload: WorkspaceCreate) -> Workspace:
        row = Workspace(owner_id=actor.id, **payload.model_dump())
        db.add(row)
        db.flush()
        audit(db, actor=actor, action="workspace.create", target_type="workspace", target_id=row.id)
        db.commit()
        db.refresh(row)
        return row

    def owned(self, db: Session, actor: User, workspace_id: str) -> Workspace:
        row = db.scalar(
            select(Workspace).where(Workspace.id == workspace_id, Workspace.owner_id == actor.id)
        )
        if row is None:
            raise NotFoundError("Workspace not found")
        return row


class SearchService:
    """Federated, owner-scoped search over Control metadata and Hermes FTS.

    Message text is never copied into Control's database. Each authoritative
    Hermes result is intersected with the caller's existing SessionLink rows
    before any excerpt is returned, including compression-lineage matches.
    """

    _MAX_ROUTES = 16

    def __init__(self, services: AppServices) -> None:
        self.services = services
        self.gateways = GatewayService(services)

    @staticmethod
    def _compact(value: Any, maximum: int = 240) -> str:
        normalized = " ".join(str(value or "").split())
        if len(normalized) <= maximum:
            return normalized
        return normalized[: maximum - 1].rstrip() + "…"

    async def search(
        self,
        db: Session,
        actor: User,
        *,
        query: str,
        kind: str = "all",
        limit: int = 50,
    ) -> dict[str, Any]:
        needle = " ".join(query.split()).casefold()
        safe_limit = max(1, min(int(limit), 100))
        owned_sessions = list(
            db.scalars(
                select(SessionLink)
                .where(SessionLink.owner_id == actor.id)
                .order_by(SessionLink.updated_at.desc())
            ).all()
        )
        profiles = {
            (row.gateway_id, row.profile_name): row.display_name
            for row in db.scalars(select(ProfileRef)).all()
        }
        items: list[dict[str, str]] = []
        seen: set[str] = set()

        def add(item: dict[str, str]) -> None:
            if item["id"] in seen or len(items) >= safe_limit:
                return
            seen.add(item["id"])
            items.append(item)

        if kind in {"all", "session"}:
            for row in owned_sessions:
                haystack = f"{row.title or ''} {row.stored_session_id}".casefold()
                if needle not in haystack:
                    continue
                display_name = profiles.get(
                    (row.gateway_id, row.profile_name), row.profile_name
                )
                add(
                    {
                        "id": f"session:{row.id}",
                        "targetId": row.id,
                        "kind": "session",
                        "title": self._compact(row.title or "Conversación de Hermes", 200),
                        "excerpt": "Conversación persistente en Hermes",
                        "meta": f"{display_name} · {row.updated_at.isoformat()}",
                    }
                )

        if kind in {"all", "workspace"}:
            workspaces = db.scalars(
                select(Workspace)
                .where(Workspace.owner_id == actor.id)
                .order_by(Workspace.updated_at.desc())
            ).all()
            for row in workspaces:
                if needle not in f"{row.name} {row.description or ''}".casefold():
                    continue
                add(
                    {
                        "id": f"workspace:{row.id}",
                        "targetId": row.id,
                        "kind": "workspace",
                        "title": self._compact(row.name, 200),
                        "excerpt": self._compact(
                            row.description or "Workspace de Agent Control"
                        ),
                        "meta": f"Workspace · {row.updated_at.isoformat()}",
                    }
                )

        if kind in {"all", "automation"}:
            automations = db.scalars(
                select(Automation)
                .where(Automation.owner_id == actor.id)
                .order_by(Automation.updated_at.desc())
            ).all()
            for row in automations:
                haystack = (
                    f"{row.name} {row.schedule} {row.timezone} {row.prompt}"
                ).casefold()
                if needle not in haystack:
                    continue
                add(
                    {
                        "id": f"automation:{row.id}",
                        "targetId": row.id,
                        "kind": "automation",
                        "title": self._compact(row.name, 200),
                        "excerpt": self._compact(
                            f"{row.schedule} · {row.timezone}"
                        ),
                        "meta": (
                            "Automatización activa"
                            if row.enabled
                            else "Automatización pausada"
                        ),
                    }
                )

        partial = False
        if kind in {"all", "message", "session"} and len(items) < safe_limit:
            grouped: dict[tuple[str, str], list[SessionLink]] = {}
            for row in owned_sessions:
                grouped.setdefault((row.gateway_id, row.profile_name), []).append(row)
            route_groups = list(grouped.items())
            if len(route_groups) > self._MAX_ROUTES:
                partial = True
                route_groups = route_groups[: self._MAX_ROUTES]

            calls: list[tuple[tuple[str, str], Any]] = []
            for (gateway_id, profile_name), rows in route_groups:
                try:
                    await require_capability(
                        db,
                        self.services,
                        gateway_id=gateway_id,
                        profile_name=profile_name,
                        method="session.search",
                    )
                    connection = await self.gateways.connection(
                        db, gateway_id, profile_name
                    )
                    provider = await self.services.provider_pool.get(connection)
                    calls.append(
                        (
                            (gateway_id, profile_name),
                            provider.search_sessions(query, limit=safe_limit),
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    partial = True

            if calls:
                upstream = await asyncio.gather(
                    *(call for _, call in calls), return_exceptions=True
                )
                for ((gateway_id, profile_name), _), outcome in zip(
                    calls, upstream, strict=True
                ):
                    if isinstance(outcome, BaseException):
                        partial = True
                        continue
                    owned = {
                        row.stored_session_id: row
                        for row in grouped[(gateway_id, profile_name)]
                    }
                    display_name = profiles.get(
                        (gateway_id, profile_name), profile_name
                    )
                    normalizer = EventNormalizer(
                        gateway_id=gateway_id, profile_name=profile_name
                    )
                    for hit in outcome:
                        row = owned.get(hit.stored_session_id)
                        if row is None and hit.lineage_root:
                            row = owned.get(hit.lineage_root)
                        if row is None:
                            # A search endpoint indexes every Hermes session;
                            # Control may expose only sessions linked to this user.
                            continue
                        safe = normalizer.sanitize_data(
                            {"role": hit.role, "content": hit.snippet}
                        )
                        if not isinstance(safe, dict) or safe.get("omitted"):
                            continue
                        excerpt = self._compact(safe.get("content") or "", 240)
                        result_kind = "message" if hit.role else "session"
                        if kind != "all" and result_kind != kind:
                            continue
                        digest = hashlib.sha256(
                            f"{row.id}\0{result_kind}\0{excerpt}".encode("utf-8")
                        ).hexdigest()[:20]
                        add(
                            {
                                "id": f"{result_kind}:{digest}",
                                "targetId": row.id,
                                "kind": result_kind,
                                "title": self._compact(
                                    row.title
                                    or hit.title
                                    or "Conversación de Hermes",
                                    200,
                                ),
                                "excerpt": excerpt or "Coincidencia en Hermes",
                                "meta": f"{display_name} · {row.updated_at.isoformat()}",
                            }
                        )

        return {"items": items[:safe_limit], "partial": partial}


class SessionService:
    def __init__(self, services: AppServices) -> None:
        self.services = services
        self.gateways = GatewayService(services)

    async def create(self, db: Session, actor: User, payload: SessionCreate) -> SessionLink:
        gateway_id = payload.gateway_id
        profile_name = payload.profile_name
        if payload.profile_id:
            profile = db.get(ProfileRef, payload.profile_id)
            if profile is None:
                raise NotFoundError("Profile not found")
            gateway_id = profile.gateway_id
            profile_name = profile.profile_name
        assert gateway_id is not None and profile_name is not None
        await require_capability(
            db,
            self.services,
            gateway_id=gateway_id,
            profile_name=profile_name,
            method="session.create",
        )
        if payload.workspace_id:
            WorkspaceService().owned(db, actor, payload.workspace_id)
        connection = await self.gateways.connection(db, gateway_id, profile_name)
        provider = await self.services.provider_pool.get(connection)
        session = await provider.create_session(title=payload.title)
        self.services.session_router.mark_runtime(
            SessionRoute(
                gateway_id=gateway_id,
                profile_name=profile_name,
                stored_session_id=session.stored_session_id,
                runtime_session_id=session.runtime_session_id,
            ),
            generation=provider.runtime_generation,
        )
        row = SessionLink(
            owner_id=actor.id,
            gateway_id=gateway_id,
            workspace_id=payload.workspace_id,
            profile_name=profile_name,
            stored_session_id=session.stored_session_id,
            runtime_session_id=session.runtime_session_id,
            title=session.title,
            status=session.status,
            initial_history_pending=True,
        )
        self._assign_runtime(
            db, row, session.runtime_session_id, provider.runtime_generation
        )
        db.add(row)
        db.flush()
        audit(db, actor=actor, action="session.create", target_type="session", target_id=row.id)
        db.commit()
        db.refresh(row)
        return row

    async def sync(
        self,
        db: Session,
        actor: User,
        *,
        gateway_id: str,
        profile_name: str,
        workspace_id: str | None,
    ) -> list[SessionLink]:
        await require_capability(
            db,
            self.services,
            gateway_id=gateway_id,
            profile_name=profile_name,
            method="session.list",
        )
        if workspace_id:
            WorkspaceService().owned(db, actor, workspace_id)
        connection = await self.gateways.connection(db, gateway_id, profile_name)
        provider = await self.services.provider_pool.get(connection)
        upstream = await provider.list_sessions()
        inventory_complete = bool(
            getattr(provider, "session_inventory_complete", False)
        )
        upstream_ids = {session.stored_session_id for session in upstream}
        runtime_owners: dict[str, str] = {}
        for session in upstream:
            if not session.runtime_session_id:
                continue
            previous = runtime_owners.get(session.runtime_session_id)
            if previous is not None and previous != session.stored_session_id:
                raise ConflictError("Hermes returned a colliding runtime session identity")
            runtime_owners[session.runtime_session_id] = session.stored_session_id
        synchronized: list[SessionLink] = []
        for session in upstream:
            row = db.scalar(
                select(SessionLink).where(
                    SessionLink.gateway_id == gateway_id,
                    SessionLink.profile_name == profile_name,
                    SessionLink.stored_session_id == session.stored_session_id,
                )
            )
            was_missing = bool(row is not None and row.status == "missing")
            if row is None:
                row = SessionLink(
                    owner_id=actor.id,
                    gateway_id=gateway_id,
                    workspace_id=workspace_id,
                    profile_name=profile_name,
                    stored_session_id=session.stored_session_id,
                )
                db.add(row)
            elif row.owner_id != actor.id:
                # Future multi-user installations must never acquire an
                # existing session merely by discovering its upstream id.
                continue
            if session.runtime_session_id:
                self.services.session_router.mark_runtime(
                    SessionRoute(
                        gateway_id,
                        profile_name,
                        session.stored_session_id,
                        session.runtime_session_id,
                    ),
                    generation=provider.runtime_generation,
                )
            self._assign_runtime(
                db, row, session.runtime_session_id, provider.runtime_generation
            )
            row.title = session.title
            row.status = session.status
            # A row returned by Hermes' durable inventory is no longer the
            # special unpersisted result of Control's session.create call.
            row.initial_history_pending = False
            if was_missing:
                # Preserve explicit Control archives. Only rows previously
                # archived by negative Hermes reconciliation are restored.
                row.archived_at = None
            synchronized.append(row)
        missing_count = 0
        if inventory_complete:
            now = utc_now()
            local_rows = db.scalars(
                select(SessionLink).where(
                    SessionLink.owner_id == actor.id,
                    SessionLink.gateway_id == gateway_id,
                    SessionLink.profile_name == profile_name,
                    SessionLink.archived_at.is_(None),
                )
            ).all()
            for row in local_rows:
                if row.stored_session_id in upstream_ids:
                    continue
                if row.initial_history_pending:
                    # Official session.create is lazy-persisted on first prompt.
                    # Its expected absence is not evidence of upstream deletion.
                    continue
                row.archived_at = now
                row.status = "missing"
                row.runtime_session_id = None
                row.runtime_generation = None
                row.last_sequence = 0
                row.replay_epoch = None
                missing_count += 1
        audit(
            db,
            actor=actor,
            action="session.sync",
            target_type="gateway",
            target_id=gateway_id,
            details={
                "profile": profile_name,
                "count": len(synchronized),
                "missing": missing_count,
                "inventoryComplete": inventory_complete,
            },
        )
        db.commit()
        for row in synchronized:
            db.refresh(row)
        return synchronized

    def owned(self, db: Session, actor: User, session_id: str) -> SessionLink:
        row = db.scalar(
            select(SessionLink).where(SessionLink.id == session_id, SessionLink.owner_id == actor.id)
        )
        if row is None:
            raise NotFoundError("Session not found")
        return row

    async def resume(self, db: Session, actor: User, row: SessionLink) -> SessionLink:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="session.resume",
        )
        connection = await self.gateways.connection(db, row.gateway_id, row.profile_name)
        route = self._route(row)
        routed, resumed = await self.services.session_router.ensure_runtime(route, connection)
        if resumed is not None:
            provider = await self.services.provider_pool.get(connection)
            self._assign_runtime(
                db, row, routed.runtime_session_id, provider.runtime_generation
            )
            row.status = resumed.status
            row.updated_at = utc_now()
            audit(db, actor=actor, action="session.resume", target_type="session", target_id=row.id)
            db.commit()
            db.refresh(row)
        return row

    async def history(self, db: Session, actor: User, row: SessionLink) -> list[dict[str, Any]]:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="session.history",
        )
        connection = await self.gateways.connection(db, row.gateway_id, row.profile_name)
        try:
            _, history = await self.services.session_router.history(
                self._route(row), connection
            )
        except SessionHistoryNotFound:
            if not row.initial_history_pending:
                raise
            # A locally created, not-yet-prompted Hermes runtime has no durable
            # row by contract. Its authoritative transcript is therefore empty.
            history = []
        self._reconcile_active_prompt_from_history(db, row, history)
        db.commit()
        sanitized = EventNormalizer(
            gateway_id=row.gateway_id, profile_name=row.profile_name
        ).sanitize_data(history)
        return list(sanitized)

    async def submit(
        self,
        db: Session,
        actor: User,
        row: SessionLink,
        prompt: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="prompt.submit",
        )
        scope = f"session:{row.id}:prompt"
        existing = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.user_id == actor.id,
                IdempotencyOperation.scope == scope,
                IdempotencyOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return dict(existing.response_json)
        # The audited Hermes event contract is session-scoped and does not
        # echo a prompt request id.  Keeping exactly one unresolved prompt per
        # session makes a fresh terminal message event safely correlatable
        # without inventing upstream fields or ever retrying a prompt.
        active_operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.user_id == actor.id,
                IdempotencyOperation.scope == scope,
                IdempotencyOperation.status.in_(
                    ("pending", "accepted", "streaming", "delivery_unknown")
                ),
            )
        )
        if active_operation is not None:
            raise ConflictError(
                "This session already has an unresolved prompt; wait for its "
                "terminal event or reconcile history before sending another"
            )
        operation = IdempotencyOperation(
            user_id=actor.id,
            scope=scope,
            idempotency_key=idempotency_key,
            status="pending",
            response_json={
                "operationId": idempotency_key,
                "status": "pending",
                # Recovery metadata only.  Prompt text remains in Hermes and
                # is never copied into Control's SQLite database.
                "_promptHash": self._prompt_digest(prompt),
            },
        )
        db.add(operation)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(
                select(IdempotencyOperation).where(
                    IdempotencyOperation.user_id == actor.id,
                    IdempotencyOperation.scope == scope,
                    IdempotencyOperation.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                raise
            return dict(existing.response_json)
        connection = await self.gateways.connection(db, row.gateway_id, row.profile_name)
        try:
            await require_capability(
                db,
                self.services,
                gateway_id=row.gateway_id,
                profile_name=row.profile_name,
                method="session.history",
            )
            initial_empty_boundary = False
            try:
                _, baseline_history = await self.services.session_router.history(
                    self._route(row), connection
                )
            except SessionHistoryNotFound:
                if not row.initial_history_pending:
                    raise
                baseline_history = []
                initial_empty_boundary = True
            operation.response_json = {
                **dict(operation.response_json or {}),
                "_historyCount": len(baseline_history),
                **(
                    {"_historyBoundary": "control-created-empty"}
                    if initial_empty_boundary
                    else {}
                ),
            }
            # Consume the exception before dispatch and persist that fact with
            # the boundary. A later 404 can never inherit this authorization,
            # including after a crash or an ambiguous first mutation.
            row.initial_history_pending = False
            db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            db.refresh(operation)
            operation.status = "failed"
            operation.response_json = {
                **dict(operation.response_json or {}),
                "operationId": idempotency_key,
                "status": "failed",
            }
            audit(
                db,
                actor=actor,
                action="prompt.submit",
                target_type="session",
                target_id=row.id,
                outcome="failed",
                details={"errorType": type(exc).__name__, "stage": "history_boundary"},
            )
            db.commit()
            raise ConflictError(
                "Prompt was not sent because Control could not establish a "
                "durable history boundary"
            ) from exc
        try:
            routed, receipt = await self.services.session_router.submit_prompt(
                route=self._route(row),
                connection=connection,
                prompt=prompt,
                idempotency_key=idempotency_key,
                operation_id=idempotency_key,
            )
            # A very fast upstream can emit its terminal event before this
            # request commits the accepted receipt. Refresh so the durable
            # event sink always wins that race.
            db.refresh(operation)
            db.refresh(row)
            provider = await self.services.provider_pool.get(connection)
            self._assign_runtime(
                db, row, routed.runtime_session_id, provider.runtime_generation
            )
            terminal_operation = operation.status in {"completed", "failed", "interrupted"}
            if row.status not in {"ready", "error", "interrupted"}:
                row.status = "streaming"
            response = {
                "operationId": receipt.operation_id,
                "status": (
                    operation.status
                    if terminal_operation
                    else receipt.status
                ),
                "acceptedAt": receipt.accepted_at.isoformat(),
            }
            if not terminal_operation:
                operation.status = receipt.status
            operation.response_json = {
                **dict(operation.response_json or {}),
                **response,
            }
            audit(db, actor=actor, action="prompt.submit", target_type="session", target_id=row.id)
            db.commit()
            return response
        except asyncio.CancelledError:
            db.refresh(operation)
            db.refresh(row)
            if operation.status not in {"completed", "failed", "interrupted"}:
                operation.status = "delivery_unknown"
                operation.response_json = {
                    **dict(operation.response_json or {}),
                    "operationId": idempotency_key,
                    "status": "delivery_unknown",
                }
            audit(
                db,
                actor=actor,
                action="prompt.submit",
                target_type="session",
                target_id=row.id,
                outcome="delivery_unknown",
                details={"errorType": "CancelledError"},
            )
            db.commit()
            raise
        except Exception as exc:
            db.refresh(operation)
            db.refresh(row)
            if operation.status in {"completed", "failed", "interrupted"}:
                return dict(operation.response_json or {
                    "operationId": idempotency_key,
                    "status": operation.status,
                })
            ambiguous = str(exc) == "PROMPT_DELIVERY_UNKNOWN"
            operation.status = "delivery_unknown" if ambiguous else "failed"
            operation.response_json = {
                **dict(operation.response_json or {}),
                "operationId": idempotency_key,
                "status": operation.status,
            }
            audit(
                db,
                actor=actor,
                action="prompt.submit",
                target_type="session",
                target_id=row.id,
                outcome=operation.status,
                details={"errorType": type(exc).__name__},
            )
            db.commit()
            if ambiguous:
                raise ConflictError(
                    "Prompt delivery is unknown; reconcile history before sending again"
                ) from exc
            raise

    @staticmethod
    def _prompt_digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _reconcile_active_prompt_from_history(
        cls,
        db: Session,
        row: SessionLink,
        history: list[dict[str, Any]],
    ) -> None:
        """Confirm a missed terminal event from authoritative Hermes history.

        The official event stream has no prompt request id.  The baseline
        message count and one-way prompt digest let Control recognize its one
        active turn without storing transcript text or guessing from an old
        assistant message.  Anything ambiguous remains unresolved and is
        never resent automatically.
        """

        active = list(
            db.scalars(
                select(IdempotencyOperation)
                .where(
                    IdempotencyOperation.user_id == row.owner_id,
                    IdempotencyOperation.scope == f"session:{row.id}:prompt",
                    IdempotencyOperation.status.in_(
                        ("pending", "accepted", "streaming", "delivery_unknown")
                    ),
                )
                .order_by(IdempotencyOperation.created_at)
                .limit(2)
            ).all()
        )
        if len(active) != 1:
            return
        operation = active[0]
        metadata = dict(operation.response_json or {})
        baseline = metadata.get("_historyCount")
        prompt_hash = metadata.get("_promptHash")
        if (
            not isinstance(baseline, int)
            or baseline < 0
            or not isinstance(prompt_hash, str)
            or len(prompt_hash) != 64
            or len(history) <= baseline
        ):
            return
        prompt_index: int | None = None
        for index, message in enumerate(history[baseline:], start=baseline):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = message.get("text", message.get("content"))
            if isinstance(text, str) and cls._prompt_digest(text) == prompt_hash:
                prompt_index = index
                break
        if prompt_index is None:
            return
        completed = any(
            isinstance(message, dict) and message.get("role") == "assistant"
            for message in history[prompt_index + 1 :]
        )
        if not completed:
            return
        operation.status = "completed"
        operation.response_json = {
            **metadata,
            "operationId": operation.idempotency_key,
            "status": "completed",
        }
        row.status = "ready"

    async def interrupt(self, db: Session, actor: User, row: SessionLink) -> None:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="session.interrupt",
        )
        connection = await self.gateways.connection(db, row.gateway_id, row.profile_name)
        routed = await self.services.session_router.interrupt(
            self._route(row), connection
        )
        provider = await self.services.provider_pool.get(connection)
        self._assign_runtime(
            db, row, routed.runtime_session_id, provider.runtime_generation
        )
        row.status = "interrupted"
        active_operations = db.scalars(
            select(IdempotencyOperation).where(
                IdempotencyOperation.user_id == actor.id,
                IdempotencyOperation.scope == f"session:{row.id}:prompt",
                IdempotencyOperation.status.in_(("pending", "accepted", "streaming")),
            )
        ).all()
        for operation in active_operations:
            operation.status = "interrupted"
            operation.response_json = {
                **dict(operation.response_json or {}),
                "operationId": operation.idempotency_key,
                "status": "interrupted",
            }
        audit(db, actor=actor, action="session.interrupt", target_type="session", target_id=row.id)
        db.commit()

    async def respond_approval(
        self,
        db: Session,
        actor: User,
        row: SessionLink,
        *,
        request_id: str,
        choice: str,
    ) -> dict[str, Any]:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="approval.respond",
        )
        connection = await self.gateways.connection(
            db, row.gateway_id, row.profile_name
        )
        routed, resumed = await self.services.session_router.ensure_runtime(
            self._route(row), connection
        )
        provider = await self.services.provider_pool.get(connection)
        if resumed is not None:
            self._assign_runtime(
                db, row, routed.runtime_session_id, provider.runtime_generation
            )
            db.commit()
        interaction_claim = self.services.event_hub.take_interaction(
            kind="approval",
            request_id=request_id,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            stored_session_id=row.stored_session_id,
            runtime_session_id=routed.runtime_session_id,
            runtime_generation=provider.runtime_generation,
        )
        if interaction_claim is None:
            raise ConflictError(
                "Approval request is not pending for this Control session"
            )
        try:
            result = await provider.respond_approval(
                routed,
                request_id,
                choice,
                expected_runtime_generation=provider.runtime_generation,
            )
        except RuntimeGenerationChanged as exc:
            self.services.event_hub.restore_interaction(interaction_claim)
            raise ConflictError(
                "Hermes reconnected before the approval response; wait for the pending request to reappear"
            ) from exc
        except JsonRpcError as exc:
            if exc.code in {4001, 4008, 4009}:
                self.services.event_hub.forget_interaction(
                    gateway_id=row.gateway_id,
                    profile_name=row.profile_name,
                    kind="approval",
                    request_id=request_id,
                )
            raise ConflictError("Hermes rejected the approval response") from exc
        except RuntimeError as exc:
            if str(exc) == "MUTATION_DELIVERY_UNKNOWN":
                raise UpstreamUnavailableError(
                    "Approval response outcome is unknown; wait for realtime reconciliation"
                ) from exc
            raise
        if int(result.get("resolved") or 0) < 1:
            self.services.event_hub.forget_interaction(
                gateway_id=row.gateway_id,
                profile_name=row.profile_name,
                kind="approval",
                request_id=request_id,
            )
            raise ConflictError("Approval request is no longer pending")
        audit(
            db,
            actor=actor,
            action="approval.respond",
            target_type="session",
            target_id=row.id,
            details={"choice": choice},
        )
        db.commit()
        return {
            "requestId": request_id,
            "resolved": int(result["resolved"]),
            "status": "resolved",
        }

    async def respond_clarification(
        self,
        db: Session,
        actor: User,
        row: SessionLink,
        *,
        request_id: str,
        answer: str | list[str],
        question_id: str | None,
    ) -> dict[str, Any]:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="clarify.respond",
        )
        connection = await self.gateways.connection(
            db, row.gateway_id, row.profile_name
        )
        routed, resumed = await self.services.session_router.ensure_runtime(
            self._route(row), connection
        )
        provider = await self.services.provider_pool.get(connection)
        if resumed is not None:
            self._assign_runtime(
                db, row, routed.runtime_session_id, provider.runtime_generation
            )
            db.commit()
        interaction_claim = self.services.event_hub.take_interaction(
            kind="clarification",
            request_id=request_id,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            stored_session_id=row.stored_session_id,
            runtime_session_id=routed.runtime_session_id,
            runtime_generation=provider.runtime_generation,
            question_id=question_id,
        )
        if interaction_claim is None:
            raise ConflictError(
                "Clarification request is not pending for this Control session"
            )
        try:
            result = await provider.respond_clarification(
                routed,
                request_id,
                answer,
                question_id=question_id,
                expected_runtime_generation=provider.runtime_generation,
            )
        except RuntimeGenerationChanged as exc:
            self.services.event_hub.restore_interaction(interaction_claim)
            raise ConflictError(
                "Hermes reconnected before the clarification response; wait for the pending request to reappear"
            ) from exc
        except JsonRpcError as exc:
            if exc.code in {4001, 4008, 4009}:
                self.services.event_hub.forget_interaction(
                    gateway_id=row.gateway_id,
                    profile_name=row.profile_name,
                    kind="clarification",
                    request_id=request_id,
                )
            raise ConflictError("Hermes rejected the clarification response") from exc
        except RuntimeError as exc:
            if str(exc) == "MUTATION_DELIVERY_UNKNOWN":
                raise UpstreamUnavailableError(
                    "Clarification response outcome is unknown; wait for realtime reconciliation"
                ) from exc
            raise
        status = str(result.get("status") or "")
        remaining = list(result.get("remaining") or [])
        if status == "expired" or not remaining:
            self.services.event_hub.forget_interaction(
                gateway_id=row.gateway_id,
                profile_name=row.profile_name,
                kind="clarification",
                request_id=request_id,
            )
        elif not self.services.event_hub.restrict_clarification_questions(
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            request_id=request_id,
            remaining_question_ids=remaining,
        ):
            raise ConflictError(
                "Hermes clarification response did not match the pending batch"
            )
        audit(
            db,
            actor=actor,
            action="clarify.respond",
            target_type="session",
            target_id=row.id,
            details={
                "questionId": question_id,
                "answerType": "multiple" if isinstance(answer, list) else "text",
            },
        )
        db.commit()
        return {
            "requestId": request_id,
            "status": status,
            "remaining": remaining,
        }

    async def delete_from_hermes(self, db: Session, actor: User, row: SessionLink) -> None:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="session.delete",
        )
        connection = await self.gateways.connection(db, row.gateway_id, row.profile_name)
        provider = await self.services.provider_pool.get(connection)
        await provider.delete_session(self._route(row))
        audit(
            db,
            actor=actor,
            action="session.delete_upstream",
            target_type="session",
            target_id=row.id,
        )
        db.delete(row)
        db.commit()

    @staticmethod
    def _route(row: SessionLink) -> SessionRoute:
        return SessionRoute(
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            stored_session_id=row.stored_session_id,
            runtime_session_id=row.runtime_session_id,
        )

    @staticmethod
    def _assign_runtime(
        db: Session,
        row: SessionLink,
        runtime_session_id: str | None,
        runtime_generation: str | None,
    ) -> None:
        if runtime_session_id:
            collision = db.scalar(
                select(SessionLink).where(
                    SessionLink.gateway_id == row.gateway_id,
                    SessionLink.profile_name == row.profile_name,
                    SessionLink.runtime_session_id == runtime_session_id,
                    SessionLink.id != row.id,
                )
            )
            if collision is not None:
                if collision.runtime_generation == runtime_generation:
                    raise ConflictError(
                        "Runtime session id collides inside the active Hermes generation"
                    )
                collision.runtime_session_id = None
                collision.runtime_generation = None
        next_generation = runtime_generation if runtime_session_id else None
        if (
            row.runtime_session_id != runtime_session_id
            or row.runtime_generation != next_generation
        ):
            # Hermes sequence numbers are scoped to one ephemeral runtime sid.
            # A cold resume starts again at seq=1 even when the gateway replay
            # epoch is unchanged, so the previous runtime watermark must not
            # suppress the resumed session's events.
            row.last_sequence = 0
            row.replay_epoch = None
        row.runtime_session_id = runtime_session_id
        row.runtime_generation = next_generation


class AutomationService:
    def __init__(self, services: AppServices) -> None:
        self.services = services
        self.gateways = GatewayService(services)

    async def sync(
        self,
        db: Session,
        actor: User,
        *,
        gateway_id: str,
        profile_name: str,
    ) -> list[Automation]:
        await require_capability(
            db,
            self.services,
            gateway_id=gateway_id,
            profile_name=profile_name,
            method="cron.list",
        )
        connection = await self.gateways.connection(db, gateway_id, profile_name)
        provider = await self.services.provider_pool.get(connection)
        upstream = await provider.list_automations()
        local_rows = list(
            db.scalars(
                select(Automation).where(
                    Automation.owner_id == actor.id,
                    Automation.gateway_id == gateway_id,
                    Automation.profile_name == profile_name,
                )
            ).all()
        )
        local_by_upstream_id = {
            row.hermes_automation_id: row
            for row in local_rows
            if row.hermes_automation_id
        }
        upstream_ids = {item.automation_id for item in upstream}
        removed = 0
        for upstream_id, row in local_by_upstream_id.items():
            if upstream_id not in upstream_ids:
                # Hermes owns cron.  A reference deleted through Hermes' own
                # CLI/dashboard must disappear from Control on the next sync
                # instead of lingering as an actionable ghost row.
                db.delete(row)
                removed += 1
        synchronized: list[Automation] = []
        for item in upstream:
            row = local_by_upstream_id.get(item.automation_id)
            if row is None:
                row = db.scalar(
                    select(Automation).where(
                        Automation.gateway_id == gateway_id,
                        Automation.profile_name == profile_name,
                        Automation.hermes_automation_id == item.automation_id,
                    )
                )
            if row is not None and row.owner_id != actor.id:
                continue
            if row is None:
                row = Automation(
                    owner_id=actor.id,
                    gateway_id=gateway_id,
                    profile_name=profile_name,
                    hermes_automation_id=item.automation_id,
                    name=item.name,
                    schedule=item.schedule,
                    timezone=item.timezone,
                    prompt=item.prompt,
                    enabled=item.enabled,
                    next_runs=[],
                )
                db.add(row)
            row.name = item.name
            row.schedule = item.schedule
            row.timezone = item.timezone
            row.prompt = item.prompt
            row.enabled = item.enabled
            row.next_runs = [value.isoformat() for value in item.next_runs[:5]]
            synchronized.append(row)
        audit(
            db,
            actor=actor,
            action="automation.sync",
            target_type="gateway",
            target_id=gateway_id,
            details={
                "profile": profile_name,
                "count": len(synchronized),
                "removed": removed,
            },
        )
        db.commit()
        for row in synchronized:
            db.refresh(row)
        return synchronized

    async def create(self, db: Session, actor: User, payload: AutomationCreate) -> Automation:
        await require_capability(
            db,
            self.services,
            gateway_id=payload.gateway_id,
            profile_name=payload.profile_name,
            method="cron.create",
        )
        connection = await self.gateways.connection(db, payload.gateway_id, payload.profile_name)
        provider = await self.services.provider_pool.get(connection)
        upstream = await provider.create_automation(
            HermesAutomation(
                automation_id="",
                name=payload.name,
                schedule=payload.schedule,
                timezone=payload.timezone,
                enabled=payload.enabled,
                prompt=payload.prompt,
            )
        )
        row = Automation(
            owner_id=actor.id,
            gateway_id=payload.gateway_id,
            profile_name=payload.profile_name,
            hermes_automation_id=upstream.automation_id,
            name=upstream.name,
            schedule=upstream.schedule,
            timezone=upstream.timezone,
            prompt=upstream.prompt,
            enabled=upstream.enabled,
            next_runs=[value.isoformat() for value in upstream.next_runs[:5]],
        )
        db.add(row)
        db.flush()
        audit(db, actor=actor, action="automation.create", target_type="automation", target_id=row.id)
        db.commit()
        db.refresh(row)
        return row

    def owned(self, db: Session, actor: User, automation_id: str) -> Automation:
        row = db.scalar(
            select(Automation).where(
                Automation.id == automation_id, Automation.owner_id == actor.id
            )
        )
        if row is None:
            raise NotFoundError("Automation not found")
        return row

    async def update(
        self,
        db: Session,
        actor: User,
        row: Automation,
        changes: dict[str, Any],
        *,
        audit_action: str = "automation.update",
    ) -> Automation:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="cron.update",
        )
        if not row.hermes_automation_id:
            raise ConflictError("Automation has no verified Hermes identity")
        connection = await self.gateways.connection(db, row.gateway_id, row.profile_name)
        provider = await self.services.provider_pool.get(connection)
        upstream = await provider.update_automation(row.hermes_automation_id, changes)
        row.name = upstream.name
        row.schedule = upstream.schedule
        row.timezone = upstream.timezone
        row.prompt = upstream.prompt
        row.enabled = upstream.enabled
        row.next_runs = [value.isoformat() for value in upstream.next_runs[:5]]
        audit(db, actor=actor, action=audit_action, target_type="automation", target_id=row.id)
        db.commit()
        db.refresh(row)
        return row

    async def set_enabled(
        self,
        db: Session,
        actor: User,
        row: Automation,
        *,
        enabled: bool,
    ) -> Automation:
        return await self.update(
            db,
            actor,
            row,
            {"enabled": enabled},
            audit_action="automation.resume" if enabled else "automation.pause",
        )

    async def delete(self, db: Session, actor: User, row: Automation) -> None:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="cron.delete",
        )
        if not row.hermes_automation_id:
            raise ConflictError("Automation has no verified Hermes identity")
        connection = await self.gateways.connection(db, row.gateway_id, row.profile_name)
        provider = await self.services.provider_pool.get(connection)
        await provider.delete_automation(row.hermes_automation_id)
        audit(db, actor=actor, action="automation.delete", target_type="automation", target_id=row.id)
        db.delete(row)
        db.commit()

    async def enqueue_trigger(
        self, db: Session, actor: User, row: Automation
    ) -> AutomationRun:
        await require_capability(
            db,
            self.services,
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            method="cron.trigger",
        )
        if not row.hermes_automation_id:
            raise ConflictError("Automation has no verified Hermes identity")
        run = AutomationRun(automation_id=row.id, status="queued")
        db.add(run)
        db.flush()
        audit(
            db,
            actor=actor,
            action="automation.trigger.accepted",
            target_type="automation_run",
            target_id=run.id,
            details={"automationId": row.id},
        )
        db.commit()
        db.refresh(run)
        return run

    def _link_receipt_session(
        self,
        db: Session,
        row: Automation,
        receipt: HermesRunReceipt,
        provider: Any,
    ) -> SessionLink | None:
        linked_session: SessionLink | None = None
        if receipt.stored_session_id:
            linked_session = db.scalar(
                select(SessionLink).where(
                    SessionLink.gateway_id == row.gateway_id,
                    SessionLink.profile_name == row.profile_name,
                    SessionLink.stored_session_id == receipt.stored_session_id,
                )
            )
            if linked_session is not None and linked_session.owner_id != row.owner_id:
                raise ConflictError("Automation session identity belongs to another user")
            if linked_session is None:
                linked_session = SessionLink(
                    owner_id=row.owner_id,
                    gateway_id=row.gateway_id,
                    profile_name=row.profile_name,
                    stored_session_id=receipt.stored_session_id,
                    title=f"Ejecución · {row.name}",
                    status=(
                        "ready"
                        if receipt.status in {"completed", "failed", "cancelled", "interrupted"}
                        else "streaming"
                    ),
                )
                SessionService._assign_runtime(
                    db,
                    linked_session,
                    receipt.runtime_session_id,
                    provider.runtime_generation,
                )
                db.add(linked_session)
                db.flush()
            elif receipt.runtime_session_id:
                SessionService._assign_runtime(
                    db,
                    linked_session,
                    receipt.runtime_session_id,
                    provider.runtime_generation,
                )
            if receipt.runtime_session_id:
                self.services.session_router.mark_runtime(
                    SessionRoute(
                        row.gateway_id,
                        row.profile_name,
                        receipt.stored_session_id,
                        receipt.runtime_session_id,
                    ),
                    generation=provider.runtime_generation,
                )
        return linked_session

    def _apply_run_receipt(
        self,
        db: Session,
        row: Automation,
        run: AutomationRun,
        receipt: HermesRunReceipt,
        provider: Any,
    ) -> AutomationRun:
        linked_session = self._link_receipt_session(db, row, receipt, provider)
        terminal = receipt.status in {"completed", "failed", "cancelled", "interrupted"}
        now = utc_now()
        if receipt.run_id:
            duplicate = db.scalar(
                select(AutomationRun).where(
                    AutomationRun.automation_id == row.id,
                    AutomationRun.hermes_run_id == receipt.run_id,
                    AutomationRun.id != run.id,
                )
            )
            if duplicate is not None:
                if linked_session is None and duplicate.session_link_id:
                    linked_session = db.get(SessionLink, duplicate.session_link_id)
                db.delete(duplicate)
                db.flush()
        run.hermes_run_id = receipt.run_id
        run.status = receipt.status
        run.session_link_id = linked_session.id if linked_session else None
        run.started_at = run.started_at or receipt.started_at or (
            now if receipt.status != "queued" else None
        )
        run.finished_at = receipt.finished_at or (now if terminal else None)
        run.error_summary = None
        if (
            self.services.session_factory is not None
            and receipt.run_id
            and row.hermes_automation_id
        ):
            for event in self.services.event_hub.correlated_run_events(
                gateway_id=row.gateway_id,
                profile_name=row.profile_name,
                run_id=receipt.run_id,
                automation_id=row.hermes_automation_id,
            ):
                persist_normalized_event(self.services.session_factory, event)
        return run

    async def execute_queued_trigger(self, run_id: str) -> None:
        """Execute one accepted manual trigger without holding the HTTP request."""

        if self.services.session_factory is None:
            return
        with self.services.session_factory() as db:
            run = db.get(AutomationRun, run_id)
            if run is None or run.status != "queued":
                return
            row = db.get(Automation, run.automation_id)
            if row is None or not row.hermes_automation_id:
                run.status = "failed"
                run.finished_at = utc_now()
                run.error_summary = "Automation is no longer available"
                db.commit()
                return
            try:
                await require_capability(
                    db,
                    self.services,
                    gateway_id=row.gateway_id,
                    profile_name=row.profile_name,
                    method="cron.trigger",
                )
                connection = await self.gateways.connection(
                    db, row.gateway_id, row.profile_name
                )
                provider = await self.services.provider_pool.get(connection)
                # Persist dispatch state before the long synchronous Hermes
                # call. A process crash can then be recovered as unknown
                # without replaying a possibly accepted trigger.
                run.status = "running"
                run.started_at = run.started_at or utc_now()
                db.commit()
                receipt = await provider.trigger_automation(row.hermes_automation_id)
                self._apply_run_receipt(db, row, run, receipt, provider)
                audit(
                    db,
                    actor=db.get(User, row.owner_id),
                    action="automation.trigger.completed",
                    target_type="automation_run",
                    target_id=run.id,
                    details={"automationId": row.id},
                )
                db.commit()

            except Exception as exc:
                db.rollback()
                run = db.get(AutomationRun, run_id)
                if run is None:
                    return
                ambiguous = isinstance(exc, RuntimeError) and str(exc) == "MUTATION_DELIVERY_UNKNOWN"
                run.status = "unknown" if ambiguous else "failed"
                run.finished_at = utc_now()
                run.error_summary = (
                    "Hermes did not confirm whether the run completed; Control will not retry it."
                    if ambiguous
                    else "Hermes rejected or could not complete the run."
                )
                db.commit()

    @staticmethod
    def mark_orphaned_local_triggers_unknown(db: Session) -> int:
        """Close local dispatches left behind by a previous process.

        A row with no Hermes run id cannot be retried safely: Hermes may have
        accepted the trigger before Control crashed. Authoritative `/runs`
        receipts are synchronized separately.
        """

        orphaned = list(
            db.scalars(
                select(AutomationRun).where(
                    AutomationRun.hermes_run_id.is_(None),
                    AutomationRun.status.in_(("queued", "running")),
                )
            ).all()
        )
        now = utc_now()
        for run in orphaned:
            run.status = "unknown"
            run.finished_at = now
            run.error_summary = (
                "Control restarted before Hermes confirmed this trigger; it "
                "was not retried. Authoritative Hermes runs are synchronized separately."
            )
        if orphaned:
            db.commit()
        return len(orphaned)

    async def reconcile_upstream_runs(
        self,
        db: Session,
        row: Automation,
        *,
        provider: Any | None = None,
    ) -> list[AutomationRun]:
        """Import authoritative Hermes cron sessions, including unattended runs."""

        if not row.hermes_automation_id:
            return []
        if provider is None:
            connection = await self.gateways.connection(
                db, row.gateway_id, row.profile_name
            )
            provider = await self.services.provider_pool.get(connection)
        receipts = await provider.list_automation_runs(
            row.hermes_automation_id, limit=100
        )
        synchronized: list[AutomationRun] = []
        for receipt in receipts:
            if not receipt.run_id:
                continue
            run = db.scalar(
                select(AutomationRun).where(
                    AutomationRun.automation_id == row.id,
                    AutomationRun.hermes_run_id == receipt.run_id,
                )
            )
            if run is None:
                run = AutomationRun(
                    automation_id=row.id,
                    hermes_run_id=receipt.run_id,
                    status=receipt.status,
                    created_at=receipt.started_at or utc_now(),
                )
                db.add(run)
                db.flush()
            self._apply_run_receipt(db, row, run, receipt, provider)
            synchronized.append(run)
        db.commit()
        return synchronized

    def runs(
        self,
        db: Session,
        actor: User,
        *,
        automation_id: str | None = None,
        limit: int = 100,
    ) -> list[AutomationRun]:
        if automation_id is not None:
            self.owned(db, actor, automation_id)
        statement = (
            select(AutomationRun)
            .join(Automation, Automation.id == AutomationRun.automation_id)
            .where(Automation.owner_id == actor.id)
        )
        if automation_id is not None:
            statement = statement.where(AutomationRun.automation_id == automation_id)
        return list(
            db.scalars(
                statement.order_by(AutomationRun.created_at.desc()).limit(limit)
            ).all()
        )

    def mark_run_read(
        self,
        db: Session,
        actor: User,
        automation_run_id: str,
    ) -> AutomationRun:
        run = db.scalar(
            select(AutomationRun)
            .join(Automation, Automation.id == AutomationRun.automation_id)
            .where(
                AutomationRun.id == automation_run_id,
                Automation.owner_id == actor.id,
            )
        )
        if run is None:
            raise NotFoundError("Automation run not found")
        if run.read_at is None:
            run.read_at = utc_now()
            db.commit()
            db.refresh(run)
        return run

    def link_run_session(
        self,
        db: Session,
        actor: User,
        *,
        automation_run_id: str,
        session_link_id: str,
    ) -> AutomationRun:
        """Correlate a Hermes-reported run session without crossing owner/routes."""

        run = db.scalar(
            select(AutomationRun)
            .join(Automation, Automation.id == AutomationRun.automation_id)
            .where(
                AutomationRun.id == automation_run_id,
                Automation.owner_id == actor.id,
            )
        )
        session = db.scalar(
            select(SessionLink).where(
                SessionLink.id == session_link_id,
                SessionLink.owner_id == actor.id,
            )
        )
        if run is None or session is None:
            raise NotFoundError("Automation run or session not found")
        automation = db.get(Automation, run.automation_id)
        if automation is None:
            raise NotFoundError("Automation not found")
        if (
            automation.gateway_id != session.gateway_id
            or automation.profile_name != session.profile_name
        ):
            raise ConflictError("Automation run and session routes do not match")
        run.session_link_id = session.id
        audit(
            db,
            actor=actor,
            action="automation_run.link_session",
            target_type="automation_run",
            target_id=run.id,
            details={"sessionId": session.id},
        )
        db.commit()
        db.refresh(run)
        return run


class TicketService:
    def __init__(self, services: AppServices) -> None:
        self.services = services

    def issue(self, db: Session, auth_session: AuthSession) -> tuple[str, RealtimeTicket]:
        now = datetime.now(timezone.utc)
        # Keep at most one outstanding bearer per authenticated browser
        # session. A retry rotates the ticket instead of replaying a consumed
        # secret or persisting its plaintext in the idempotency ledger.
        for existing in db.scalars(
            select(RealtimeTicket).where(
                RealtimeTicket.auth_session_id == auth_session.id,
                RealtimeTicket.used_at.is_(None),
            )
        ):
            existing.used_at = now
        token = random_token()
        row = RealtimeTicket(
            token_hash=token_hash(token),
            user_id=auth_session.user_id,
            auth_session_id=auth_session.id,
            expires_at=now
            + timedelta(seconds=self.services.settings.realtime_ticket_ttl_seconds),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return token, row

    def consume(self, db: Session, token: str) -> RealtimeTicket | None:
        row = db.scalar(
            select(RealtimeTicket).where(RealtimeTicket.token_hash == token_hash(token))
        )
        now = datetime.now(timezone.utc)
        if row is None or row.used_at is not None:
            return None
        auth_session = db.get(AuthSession, row.auth_session_id)
        if (
            auth_session is None
            or auth_session.revoked_at is not None
            or (auth_session.expires_at if auth_session.expires_at.tzinfo else auth_session.expires_at.replace(tzinfo=timezone.utc)) <= now
            or not auth_session.user.is_active
        ):
            return None
        expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
        if expires <= now:
            return None
        # Atomic enough for SQLite's serialized writer; conditional UPDATE is
        # supplied by the single-worker production contract.
        row.used_at = now
        db.commit()
        db.refresh(row)
        return row
