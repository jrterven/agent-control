from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..auth import (
    SESSION_COOKIE,
    authenticate,
    csrf_for_session_token,
    current_admin,
    current_auth_session,
    current_user,
    get_db,
    issue_session,
    require_csrf,
    require_idempotency,
)
from ..eventing import SubscriptionLimitError
from ..gateway_health import gateway_health_state, profile_health_state
from ..integrations import (
    ELEVENLABS_PROVIDER,
    SCRIBE_REALTIME_MODEL_ID,
    UserIntegrationService,
)
from ..realtime import terminal_status
from ..models import AuditEvent, Automation, AutomationRun, AuthSession, Gateway, GatewayCredential, IdempotencyOperation, ProfileRef, RealtimeTicket, SessionLink, User, Workspace
from ..schemas import (
    AuthView,
    ApprovalResponseRequest,
    ApprovalResponseView,
    AuditView,
    AutomationCreate,
    AutomationRunView,
    AutomationUpdate,
    AutomationView,
    CapabilitiesView,
    ClarificationResponseRequest,
    ClarificationResponseView,
    GatewayCreate,
    GatewayUpdate,
    GatewayView,
    LoginRequest,
    OperationView,
    ProfileCreate,
    ProfileCreateView,
    ProfileView,
    PromptRequest,
    SearchResponse,
    SessionCreate,
    SessionSyncRequest,
    SessionUpdate,
    SessionView,
    TicketView,
    WorkspaceCreate,
    WorkspaceUpdate,
    WorkspaceView,
)
from ..services import (
    AutomationService,
    GatewayService,
    ProfileService,
    SearchService,
    SessionService,
    TicketService,
    WorkspaceService,
    UPSTREAM_MUTATION_CAPABILITIES,
    audit,
    capabilities_for_profile,
    fresh_profile_capabilities,
    mutation_allowed_for_profile,
    trusted_gateway_source_sha,
)


router = APIRouter(prefix="/api/v1")


def services(request: Request):
    return request.app.state.services


def public_capability_set(
    capability: dict[str, Any] | None,
    *,
    profile_name: str | None = None,
    mutable_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
    interactive_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
    trusted_source_sha_configured: bool = False,
) -> dict[str, Any]:
    """Return the exact, non-secret fields the UI may use for control gating."""

    raw = capability or {}
    methods = {
        value
        for value in raw.get("methods", [])
        if isinstance(value, str) and 0 < len(value) <= 200
    }
    if profile_name is not None:
        if not trusted_source_sha_configured:
            methods.difference_update(UPSTREAM_MUTATION_CAPABILITIES)
        else:
            methods = {
                method
                for method in methods
                if mutation_allowed_for_profile(
                    profile_name,
                    method,
                    mutable_profiles,
                    interactive_profiles,
                )
            }
    features = sorted(
        {
            value
            for value in raw.get("features", [])
            if isinstance(value, str) and 0 < len(value) <= 200
        }
    )[:256]

    def bounded_optional(*names: str, maximum: int) -> str | None:
        value = next((raw.get(name) for name in names if raw.get(name) is not None), None)
        return value if isinstance(value, str) and len(value) <= maximum else None

    return {
        "protocol": bounded_optional("protocol", maximum=120),
        "version": bounded_optional("version", maximum=120),
        "sourceSha": bounded_optional("sourceSha", "source_sha", maximum=128),
        "methods": sorted(methods)[:512],
        "features": features,
    }


def public_profile_mutable(
    capability: dict[str, Any] | None,
    *,
    profile_name: str,
    mutable_profiles: list[str] | tuple[str, ...] | frozenset[str],
    interactive_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
    trusted_source_sha_configured: bool,
) -> bool:
    """Expose mutation eligibility only after all three safety gates pass."""

    if not trusted_source_sha_configured:
        return False
    projected = public_capability_set(
        capability,
        profile_name=profile_name,
        mutable_profiles=mutable_profiles,
        interactive_profiles=interactive_profiles,
        trusted_source_sha_configured=True,
    )
    return bool(set(projected["methods"]) & UPSTREAM_MUTATION_CAPABILITIES)


def gateway_capability_set(
    db: Session,
    gateway_id: str,
    mutable_profiles: list[str],
    *,
    interactive_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
    capability_ttl_seconds: int,
    trusted_source_sha_configured: bool,
) -> dict[str, Any]:
    """Conservative gateway summary; profile views remain authoritative.

    Methods/features are intersected so this aggregate can never certify a
    control that is unavailable on one profile. The active profile's exact
    ``capabilitySet`` must be used for profile-scoped operations.
    """

    rows = list(
        db.scalars(select(ProfileRef).where(ProfileRef.gateway_id == gateway_id)).all()
    )
    observed_at = datetime.now(timezone.utc)
    sets = [
        public_capability_set(
            fresh_profile_capabilities(
                row,
                now=observed_at,
                ttl_seconds=capability_ttl_seconds,
            ),
            profile_name=row.profile_name,
            mutable_profiles=mutable_profiles,
            interactive_profiles=interactive_profiles,
            trusted_source_sha_configured=trusted_source_sha_configured,
        )
        for row in rows
    ]
    if not sets:
        return public_capability_set(None)
    methods = set(sets[0]["methods"])
    features = set(sets[0]["features"])
    for item in sets[1:]:
        methods.intersection_update(item["methods"])
        features.intersection_update(item["features"])
    protocols = {item["protocol"] for item in sets if item["protocol"] is not None}
    versions = {item["version"] for item in sets if item["version"] is not None}
    revisions = {item["sourceSha"] for item in sets if item["sourceSha"] is not None}
    return {
        "protocol": next(iter(protocols)) if len(protocols) == 1 else None,
        "version": next(iter(versions)) if len(versions) == 1 else None,
        "sourceSha": next(iter(revisions)) if len(revisions) == 1 else None,
        "methods": sorted(methods),
        "features": sorted(features),
    }


def gateway_view(
    db: Session, row: Gateway, app_services
) -> GatewayView:
    credential = db.scalar(
        select(GatewayCredential).where(GatewayCredential.gateway_id == row.id)
    )
    trusted = trusted_gateway_source_sha(db, app_services, row.id) is not None
    public_health = gateway_health_state(
        row,
        at=datetime.now(timezone.utc),
        ttl_seconds=app_services.settings.upstream_health_ttl_seconds,
    )
    return GatewayView(
        id=row.id,
        name=row.name,
        connection_mode=row.connection_mode,
        enabled=row.enabled,
        env_managed=row.env_managed,
        health_status=public_health,
        last_health_at=row.last_health_at,
        version=row.version,
        source_sha=row.source_sha,
        has_dashboard_token=bool(credential and credential.dashboard_token_ciphertext),
        has_api_key=bool(credential and credential.api_key_ciphertext),
        has_trusted_source_sha=trusted,
        capability_set=gateway_capability_set(
            db,
            row.id,
            app_services.settings.mutable_profiles,
            interactive_profiles=app_services.settings.interactive_profiles,
            capability_ttl_seconds=app_services.settings.capability_ttl_seconds,
            trusted_source_sha_configured=trusted,
        ),
    )


def session_view(db: Session, row: SessionLink) -> SessionView:
    profile_id = db.scalar(
        select(ProfileRef.id).where(
            ProfileRef.gateway_id == row.gateway_id,
            ProfileRef.profile_name == row.profile_name,
        )
    )
    return SessionView.model_validate(row).model_copy(
        update={
            "profile_id": profile_id,
            # A rename is intentionally a Control-side label. Hermes remains
            # the source of truth for the canonical title and conversation.
            "title": row.display_title or row.title,
        }
    )


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": request.app.state.services.settings.app_name,
        "environment": request.app.state.services.settings.environment,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/ready", response_model=None)
def readiness(request: Request) -> Any:
    """Process readiness plus a non-secret cached upstream summary.

    Hermes outages degrade the product but must not restart Control: offline
    shell and drafts remain useful. A database failure returns 503 because the
    authentication, routing and audit boundaries cannot operate safely.
    """

    try:
        with request.app.state.session_factory() as db:
            db.execute(select(1)).scalar_one()
            gateways = list(db.scalars(select(Gateway)).all())
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "database": "unavailable",
                "upstream": "unknown",
                "time": datetime.now(timezone.utc).isoformat(),
            },
        )
    now = datetime.now(timezone.utc)
    ttl_seconds = request.app.state.services.settings.upstream_health_ttl_seconds

    states = [
        gateway_health_state(row, at=now, ttl_seconds=ttl_seconds)
        for row in gateways
        if row.enabled
    ]
    upstream = (
        "online"
        if states and all(state == "online" for state in states)
        else "offline"
        if states and all(state == "offline" for state in states)
        else "stale"
        if states and all(state == "stale" for state in states)
        else "degraded"
        if any(state in {"online", "degraded", "offline", "stale"} for state in states)
        else "unknown"
    )
    checked_times = []
    for row in gateways:
        if not row.enabled:
            continue
        checked_at = row.last_health_at
        if checked_at is None:
            continue
        checked_times.append(
            checked_at.replace(tzinfo=timezone.utc)
            if checked_at.tzinfo is None
            else checked_at.astimezone(timezone.utc)
        )
    last_health = max(checked_times, default=None)
    watcher = getattr(request.app.state, "automation_route_health", None)
    watcher_snapshot = watcher.snapshot(at=now) if watcher is not None else {
        "status": "unknown"
    }
    watcher_status = str(watcher_snapshot["status"])
    capability_watcher = getattr(request.app.state, "capability_refresh_health", None)
    capability_watcher_snapshot = (
        capability_watcher.snapshot(at=now)
        if capability_watcher is not None
        else {"status": "unknown"}
    )
    capability_watcher_status = str(capability_watcher_snapshot["status"])
    return {
        "status": (
            "degraded"
            if watcher_status in {"failed", "stale", "unknown"}
            or capability_watcher_status in {"failed", "stale", "unknown"}
            else "ready"
        ),
        "database": "ready",
        "upstream": upstream,
        "enabledGateways": len(states),
        "staleGateways": sum(state == "stale" for state in states),
        "upstreamHealthTtlSeconds": ttl_seconds,
        "lastUpstreamCheckAt": last_health.isoformat() if last_health else None,
        "automationRoutes": watcher_status,
        "automationRouteWatcher": watcher_snapshot,
        "capabilityRefresh": capability_watcher_status,
        "capabilityRefreshWatcher": capability_watcher_snapshot,
        "time": now.isoformat(),
    }


@router.post("/auth/login", response_model=AuthView)
def login(
    payload: LoginRequest,
    response: Response,
    request: Request,
    db: Session = Depends(get_db),
) -> AuthView:
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    settings = request.app.state.services.settings
    token, csrf, _ = issue_session(db, user, ttl_hours=settings.session_ttl_hours)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )
    audit(db, actor=user, action="auth.login")
    db.commit()
    return AuthView(
        id=user.id,
        name=user.username,
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        csrf_token=csrf,
    )


@router.post("/auth/logout", status_code=204)
def logout(
    response: Response,
    auth: AuthSession = Depends(require_csrf),
    db: Session = Depends(get_db),
) -> Response:
    now = datetime.now(timezone.utc)
    auth.revoked_at = now
    for ticket in db.scalars(
        select(RealtimeTicket).where(RealtimeTicket.auth_session_id == auth.id)
    ):
        ticket.used_at = ticket.used_at or now
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/auth/me", response_model=AuthView)
def me(
    request: Request,
    auth: AuthSession = Depends(current_auth_session),
) -> AuthView:
    csrf = csrf_for_session_token(request.cookies[SESSION_COOKIE])
    user = auth.user
    return AuthView(
        id=user.id,
        name=user.username,
        user_id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        csrf_token=csrf,
    )


@router.get("/auth/csrf", response_model=AuthView)
def rotate_csrf(
    request: Request,
    auth: AuthSession = Depends(current_auth_session),
) -> AuthView:
    csrf = csrf_for_session_token(request.cookies[SESSION_COOKIE])
    return AuthView(
        id=auth.user.id,
        name=auth.user.username,
        user_id=auth.user.id,
        username=auth.user.username,
        is_admin=auth.user.is_admin,
        csrf_token=csrf,
    )


@router.get("/bootstrap")
def bootstrap(
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Mobile-shell projection; canonical resources remain independently addressable."""
    gateways = list(db.scalars(select(Gateway).order_by(Gateway.created_at)).all())
    app_services = services(request)
    integration_configuration = UserIntegrationService(
        app_services.vault
    ).configuration(db, user)
    mutable_profiles = app_services.settings.mutable_profiles
    interactive_profiles = app_services.settings.interactive_profiles
    capability_observed_at = datetime.now(timezone.utc)
    trusted_gateways = {
        row.id: trusted_gateway_source_sha(db, app_services, row.id) is not None
        for row in gateways
    }
    profiles = list(db.scalars(select(ProfileRef).order_by(ProfileRef.display_name)).all())
    fresh_capabilities = {
        row.id: fresh_profile_capabilities(
            row,
            now=capability_observed_at,
            ttl_seconds=app_services.settings.capability_ttl_seconds,
        )
        for row in profiles
    }
    workspaces = list(
        db.scalars(
            select(Workspace)
            .where(Workspace.owner_id == user.id, Workspace.archived_at.is_(None))
            .order_by(Workspace.updated_at.desc())
        ).all()
    )
    sessions = list(
        db.scalars(
            select(SessionLink)
            .where(SessionLink.owner_id == user.id, SessionLink.archived_at.is_(None))
            .order_by(SessionLink.updated_at.desc())
        ).all()
    )
    automations = list(
        db.scalars(
            select(Automation)
            .where(Automation.owner_id == user.id)
            .order_by(Automation.updated_at.desc())
        ).all()
    )
    profile_by_route = {(row.gateway_id, row.profile_name): row for row in profiles}
    profile_capabilities = {
        row.id: public_capability_flags(
            fresh_capabilities[row.id],
            profile_name=row.profile_name,
            mutable_profiles=mutable_profiles,
            interactive_profiles=interactive_profiles,
            trusted_source_sha_configured=trusted_gateways.get(
                row.gateway_id, False
            ),
        )
        for row in profiles
    }
    gateway_capabilities: dict[str, dict[str, bool]] = {}
    for profile in profiles:
        flags = profile_capabilities[profile.id]
        aggregate = gateway_capabilities.setdefault(
            profile.gateway_id, {name: False for name in flags}
        )
        for name, enabled in flags.items():
            aggregate[name] = aggregate[name] or enabled
    bootstrap_now = datetime.now(timezone.utc)

    def bootstrap_gateway_status(row: Gateway) -> str:
        health = gateway_health_state(
            row,
            at=bootstrap_now,
            ttl_seconds=app_services.settings.upstream_health_ttl_seconds,
        )
        return {
            "online": "connected",
            "degraded": "degraded",
            "offline": "offline",
            "stale": "offline",
            "unknown": "offline",
        }[health]

    return {
        "features": {
            "dictation": {
                "available": integration_configuration["configured"],
                "provider": ELEVENLABS_PROVIDER,
                "modelId": SCRIBE_REALTIME_MODEL_ID,
            },
            "speech": {
                "available": integration_configuration["speech_available"],
                "provider": ELEVENLABS_PROVIDER,
                "modelId": integration_configuration["tts_model_id"],
                "voiceId": integration_configuration["voice_id"],
                "voiceName": integration_configuration["voice_name"],
            },
        },
        "gateways": [
            {
                "id": row.id,
                "name": row.name,
                "location": "Túnel privado" if row.connection_mode == "tunnel" else row.connection_mode,
                "status": bootstrap_gateway_status(row),
                "version": row.version or "desconocida",
                # Diagnostics reported by Hermes, never the operator-supplied
                # write-only trust anchor.
                "sha": row.source_sha,
                "envManaged": row.env_managed,
                "hasTrustedSourceSha": trusted_gateways.get(row.id, False),
                "capabilities": gateway_capabilities.get(
                    row.id,
                    {
                        "realtime": False,
                        "sessions": False,
                        "prompts": False,
                        "interrupt": False,
                        "cron": False,
                        "profiles": False,
                        "config": False,
                        "memory": False,
                    },
                ),
                "capabilitySet": gateway_capability_set(
                    db,
                    row.id,
                    mutable_profiles,
                    interactive_profiles=interactive_profiles,
                    capability_ttl_seconds=app_services.settings.capability_ttl_seconds,
                    trusted_source_sha_configured=trusted_gateways.get(
                        row.id, False
                    ),
                ),
            }
            for row in gateways
        ],
        "profiles": [
            {
                "id": row.id,
                "gatewayId": row.gateway_id,
                "technicalName": row.profile_name,
                "displayName": row.display_name,
                "model": row.model or "sin detectar",
                "status": (
                    "ready"
                    if profile_health_state(
                        row,
                        at=bootstrap_now,
                        ttl_seconds=app_services.settings.upstream_health_ttl_seconds,
                    ) == "online"
                    else "offline"
                ),
                "mutable": (
                    public_profile_mutable(
                        fresh_capabilities[row.id],
                        profile_name=row.profile_name,
                        mutable_profiles=mutable_profiles,
                        interactive_profiles=interactive_profiles,
                        trusted_source_sha_configured=trusted_gateways.get(
                            row.gateway_id, False
                        ),
                    )
                ),
                "capabilities": profile_capabilities[row.id],
                "capabilitySet": public_capability_set(
                    fresh_capabilities[row.id],
                    profile_name=row.profile_name,
                    mutable_profiles=mutable_profiles,
                    interactive_profiles=interactive_profiles,
                    trusted_source_sha_configured=trusted_gateways.get(
                        row.gateway_id, False
                    ),
                ),
            }
            for row in profiles
        ],
        "workspaces": [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description or "",
                "sessionCount": db.scalar(
                    select(func.count(SessionLink.id)).where(
                        SessionLink.workspace_id == row.id, SessionLink.archived_at.is_(None)
                    )
                )
                or 0,
                "updatedAt": row.updated_at.isoformat(),
            }
            for row in workspaces
        ],
        "sessions": [
            {
                "id": row.id,
                "storedSessionId": row.stored_session_id,
                "runtimeSessionId": row.runtime_session_id,
                "workspaceId": row.workspace_id,
                "profileId": (
                    profile_by_route.get((row.gateway_id, row.profile_name)).id
                    if profile_by_route.get((row.gateway_id, row.profile_name))
                    else None
                ),
                "title": row.display_title or row.title or "Conversación",
                "preview": "",
                "updatedAt": row.updated_at.isoformat(),
                "unread": False,
                "archived": row.archived_at is not None,
            }
            for row in sessions
        ],
        "automations": [
            {
                "id": row.id,
                "gatewayId": row.gateway_id,
                "profileName": row.profile_name,
                "hermesAutomationId": row.hermes_automation_id,
                "name": row.name,
                "schedule": row.schedule,
                "timezone": row.timezone,
                "profileId": (
                    profile_by_route.get((row.gateway_id, row.profile_name)).id
                    if profile_by_route.get((row.gateway_id, row.profile_name))
                    else None
                ),
                "prompt": row.prompt,
                "enabled": row.enabled,
                "nextRun": row.next_runs[0] if row.next_runs else "",
                "nextRuns": row.next_runs,
                "lastStatus": "idle",
                "updatedAt": row.updated_at.isoformat(),
            }
            for row in automations
        ],
    }


def public_capability_flags(
    capability: dict[str, Any] | None,
    *,
    profile_name: str | None = None,
    mutable_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
    interactive_profiles: list[str] | tuple[str, ...] | frozenset[str] = (),
    trusted_source_sha_configured: bool = False,
) -> dict[str, bool]:
    raw = public_capability_set(
        capability,
        profile_name=profile_name,
        mutable_profiles=mutable_profiles,
        interactive_profiles=interactive_profiles,
        trusted_source_sha_configured=trusted_source_sha_configured,
    )
    methods = set(raw["methods"])
    features = set(raw["features"])
    return {
        "realtime": "gateway.ping" in methods,
        "sessions": {"session.list", "session.create", "session.history"}.issubset(methods),
        "prompts": "prompt.submit" in methods,
        "interrupt": "session.interrupt" in methods,
        "approvals": "approval.respond" in methods,
        "clarifications": "clarify.respond" in methods,
        "cron": "cron.list" in methods,
        "cronCreate": "cron.create" in methods,
        "cronUpdate": "cron.update" in methods,
        "cronDelete": "cron.delete" in methods,
        "cronTrigger": "cron.trigger" in methods,
        "profiles": "profiles.list" in methods or "profiles" in features,
        "profileCreate": "profiles.create" in methods,
        "config": bool(methods & {"config.get", "config.set", "models.list", "commands.catalog"}),
        "memory": any(value.startswith("memory.") for value in methods),
    }


@router.get("/gateways", response_model=list[GatewayView])
def list_gateways(
    request: Request,
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[GatewayView]:
    rows = db.scalars(select(Gateway).order_by(Gateway.created_at)).all()
    app_services = services(request)
    return [gateway_view(db, row, app_services) for row in rows]


@router.post("/gateways", response_model=GatewayView, status_code=201)
async def create_gateway(
    payload: GatewayCreate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> GatewayView:
    row = await GatewayService(services(request)).create(db, user, payload)
    return gateway_view(db, row, services(request))


@router.patch("/gateways/{gateway_id}", response_model=GatewayView)
async def update_gateway(
    gateway_id: str,
    payload: GatewayUpdate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> GatewayView:
    row = db.get(Gateway, gateway_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    row = await GatewayService(services(request)).update(db, user, row, payload)
    return gateway_view(db, row, services(request))


@router.delete("/gateways/{gateway_id}", status_code=204)
async def delete_gateway(
    gateway_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> Response:
    row = db.get(Gateway, gateway_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Gateway not found")
    if row.env_managed:
        raise HTTPException(status_code=409, detail="Environment-managed gateway cannot be deleted")
    has_sessions = db.scalar(
        select(SessionLink.id).where(SessionLink.gateway_id == gateway_id).limit(1)
    )
    has_automations = db.scalar(
        select(Automation.id).where(Automation.gateway_id == gateway_id).limit(1)
    )
    if has_sessions or has_automations:
        raise HTTPException(status_code=409, detail="Archive linked data before deleting gateway")
    audit(db, actor=user, action="gateway.delete", target_type="gateway", target_id=row.id)
    db.delete(row)
    db.commit()
    await services(request).provider_pool.invalidate(gateway_id)
    return Response(status_code=204)


@router.post("/gateways/{gateway_id}/probe", response_model=CapabilitiesView)
async def probe_gateway(
    gateway_id: str,
    request: Request,
    profile_name: str = Query(default="default", alias="profileName"),
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> CapabilitiesView:
    capability = await GatewayService(services(request)).probe(db, user, gateway_id, profile_name)
    return CapabilitiesView(
        gateway_id=gateway_id,
        profile_name=profile_name,
        protocol=capability.protocol,
        version=capability.version,
        source_sha=capability.source_sha,
        methods=sorted(capability.methods),
        features=sorted(capability.features),
    )


@router.get("/diagnostics/capabilities", response_model=CapabilitiesView)
async def read_capabilities(
    request: Request,
    gateway_id: str = Query(alias="gatewayId"),
    profile_name: str = Query(default="default", alias="profileName"),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> CapabilitiesView:
    gateway_service = GatewayService(services(request))
    connection = await gateway_service.connection(db, gateway_id, profile_name)
    provider = await services(request).provider_pool.get(connection)
    capability = capabilities_for_profile(
        await provider.capabilities(),
        profile_name,
        services(request).settings.mutable_profiles,
        interactive_profiles=services(request).settings.interactive_profiles,
        trusted_source_sha_configured=bool(connection.trusted_source_sha),
    )
    return CapabilitiesView(
        gateway_id=gateway_id,
        profile_name=profile_name,
        protocol=capability.protocol,
        version=capability.version,
        source_sha=capability.source_sha,
        methods=sorted(capability.methods),
        features=sorted(capability.features),
    )


@router.get("/profiles", response_model=list[ProfileView])
async def list_profiles(
    request: Request,
    gateway_id: str = Query(alias="gatewayId"),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[ProfileView]:
    rows = db.scalars(
        select(ProfileRef)
        .where(ProfileRef.gateway_id == gateway_id)
        .order_by(ProfileRef.display_name)
    ).all()
    app_services = services(request)
    mutable_profiles = app_services.settings.mutable_profiles
    interactive_profiles = app_services.settings.interactive_profiles
    trusted = trusted_gateway_source_sha(db, app_services, gateway_id) is not None
    observed_at = datetime.now(timezone.utc)
    fresh_capabilities = {
        row.id: fresh_profile_capabilities(
            row,
            now=observed_at,
            ttl_seconds=app_services.settings.capability_ttl_seconds,
        )
        for row in rows
    }
    return [
        ProfileView(
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            display_name=row.display_name,
            status=profile_health_state(
                row,
                at=observed_at,
                ttl_seconds=app_services.settings.upstream_health_ttl_seconds,
            ),
            model=row.model,
            mutable=public_profile_mutable(
                fresh_capabilities[row.id],
                profile_name=row.profile_name,
                mutable_profiles=mutable_profiles,
                interactive_profiles=interactive_profiles,
                trusted_source_sha_configured=trusted,
            ),
            capabilities=public_capability_set(
                fresh_capabilities[row.id],
                profile_name=row.profile_name,
                mutable_profiles=mutable_profiles,
                interactive_profiles=interactive_profiles,
                trusted_source_sha_configured=trusted,
            ),
            capability_set=public_capability_set(
                fresh_capabilities[row.id],
                profile_name=row.profile_name,
                mutable_profiles=mutable_profiles,
                interactive_profiles=interactive_profiles,
                trusted_source_sha_configured=trusted,
            ),
        )
        for row in rows
    ]


@router.post("/profiles", response_model=ProfileCreateView, status_code=201)
async def create_profile(
    payload: ProfileCreate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> ProfileCreateView:
    app_services = services(request)
    row = await ProfileService(app_services).create(db, user, payload)
    trusted = trusted_gateway_source_sha(
        db, app_services, row.gateway_id
    ) is not None
    observed_at = datetime.now(timezone.utc)
    capability = fresh_profile_capabilities(
        row,
        now=observed_at,
        ttl_seconds=app_services.settings.capability_ttl_seconds,
    )
    capability_set = public_capability_set(
        capability,
        profile_name=row.profile_name,
        mutable_profiles=app_services.settings.mutable_profiles,
        interactive_profiles=app_services.settings.interactive_profiles,
        trusted_source_sha_configured=trusted,
    )
    health = profile_health_state(
        row,
        at=observed_at,
        ttl_seconds=app_services.settings.upstream_health_ttl_seconds,
    )
    return ProfileCreateView(
        id=row.id,
        gateway_id=row.gateway_id,
        technical_name=row.profile_name,
        display_name=row.display_name,
        model=row.model or "sin detectar",
        status="ready" if health == "online" else "offline",
        mutable=public_profile_mutable(
            capability,
            profile_name=row.profile_name,
            mutable_profiles=app_services.settings.mutable_profiles,
            interactive_profiles=app_services.settings.interactive_profiles,
            trusted_source_sha_configured=trusted,
        ),
        capabilities=public_capability_flags(
            capability,
            profile_name=row.profile_name,
            mutable_profiles=app_services.settings.mutable_profiles,
            interactive_profiles=app_services.settings.interactive_profiles,
            trusted_source_sha_configured=trusted,
        ),
        capability_set=capability_set,
    )


@router.post("/profiles/refresh", response_model=list[ProfileView])
async def refresh_profiles(
    request: Request,
    gateway_id: str = Query(alias="gatewayId"),
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    ___: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> list[ProfileView]:
    app_services = services(request)
    rows = await ProfileService(app_services).sync(db, gateway_id)
    mutable_profiles = app_services.settings.mutable_profiles
    interactive_profiles = app_services.settings.interactive_profiles
    trusted = trusted_gateway_source_sha(db, app_services, gateway_id) is not None
    observed_at = datetime.now(timezone.utc)
    fresh_capabilities = {
        row.id: fresh_profile_capabilities(
            row,
            now=observed_at,
            ttl_seconds=app_services.settings.capability_ttl_seconds,
        )
        for row in rows
    }
    return [
        ProfileView(
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            display_name=row.display_name,
            status=profile_health_state(
                row,
                at=observed_at,
                ttl_seconds=app_services.settings.upstream_health_ttl_seconds,
            ),
            model=row.model,
            mutable=public_profile_mutable(
                fresh_capabilities[row.id],
                profile_name=row.profile_name,
                mutable_profiles=mutable_profiles,
                interactive_profiles=interactive_profiles,
                trusted_source_sha_configured=trusted,
            ),
            capabilities=public_capability_set(
                fresh_capabilities[row.id],
                profile_name=row.profile_name,
                mutable_profiles=mutable_profiles,
                interactive_profiles=interactive_profiles,
                trusted_source_sha_configured=trusted,
            ),
            capability_set=public_capability_set(
                fresh_capabilities[row.id],
                profile_name=row.profile_name,
                mutable_profiles=mutable_profiles,
                interactive_profiles=interactive_profiles,
                trusted_source_sha_configured=trusted,
            ),
        )
        for row in rows
    ]


@router.get("/workspaces", response_model=list[WorkspaceView])
def list_workspaces(
    include_archived: bool = Query(default=False, alias="includeArchived"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[Workspace]:
    query = select(Workspace).where(Workspace.owner_id == user.id)
    if not include_archived:
        query = query.where(Workspace.archived_at.is_(None))
    return list(db.scalars(query.order_by(Workspace.updated_at.desc())).all())


@router.post("/workspaces", response_model=WorkspaceView, status_code=201)
def create_workspace(
    payload: WorkspaceCreate,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Workspace:
    return WorkspaceService().create(db, user, payload)


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceView)
def update_workspace(
    workspace_id: str,
    payload: WorkspaceUpdate,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Workspace:
    row = WorkspaceService().owned(db, user, workspace_id)
    values = payload.model_dump(exclude_unset=True)
    archived = values.pop("archived", None)
    for key, value in values.items():
        setattr(row, key, value)
    if archived is not None:
        row.archived_at = datetime.now(timezone.utc) if archived else None
    audit(db, actor=user, action="workspace.update", target_type="workspace", target_id=row.id)
    db.commit()
    db.refresh(row)
    return row


@router.get("/sessions", response_model=list[SessionView])
def list_sessions(
    gateway_id: str | None = Query(default=None, alias="gatewayId"),
    profile_name: str | None = Query(default=None, alias="profileName"),
    workspace_id: str | None = Query(default=None, alias="workspaceId"),
    include_archived: bool = Query(default=False, alias="includeArchived"),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[SessionView]:
    query = select(SessionLink).where(SessionLink.owner_id == user.id)
    if gateway_id:
        query = query.where(SessionLink.gateway_id == gateway_id)
    if profile_name:
        query = query.where(SessionLink.profile_name == profile_name)
    if workspace_id:
        query = query.where(SessionLink.workspace_id == workspace_id)
    if not include_archived:
        query = query.where(SessionLink.archived_at.is_(None))
    return [
        session_view(db, row)
        for row in db.scalars(query.order_by(SessionLink.updated_at.desc())).all()
    ]


@router.get("/search", response_model=SearchResponse)
async def global_search(
    request: Request,
    q: str = Query(min_length=2, max_length=200),
    kind: str = Query(
        default="all",
        pattern="^(all|session|message|workspace|automation)$",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return await SearchService(services(request)).search(
        db, user, query=q, kind=kind, limit=limit
    )


@router.post("/sessions", response_model=SessionView, status_code=201)
async def create_session(
    payload: SessionCreate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SessionView:
    row = await SessionService(services(request)).create(db, user, payload)
    return session_view(db, row)


@router.post("/sessions/sync", response_model=list[SessionView])
async def sync_sessions(
    payload: SessionSyncRequest,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> list[SessionView]:
    rows = await SessionService(services(request)).sync(
        db,
        user,
        gateway_id=payload.gateway_id,
        profile_name=payload.profile_name,
        workspace_id=payload.workspace_id,
    )
    return [session_view(db, row) for row in rows]


@router.patch("/sessions/{session_id}", response_model=SessionView)
def update_session(
    session_id: str,
    payload: SessionUpdate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SessionView:
    row = SessionService(services(request)).owned(db, user, session_id)
    values = payload.model_dump(exclude_unset=True)
    if "workspace_id" in values and values["workspace_id"]:
        WorkspaceService().owned(db, user, values["workspace_id"])
    archived = values.pop("archived", None)
    for key, value in values.items():
        setattr(row, key, value)
    if archived is not None:
        row.archived_at = datetime.now(timezone.utc) if archived else None
    action = "session.rename" if "display_title" in values else "session.update"
    audit(db, actor=user, action=action, target_type="session", target_id=row.id)
    db.commit()
    db.refresh(row)
    return session_view(db, row)


@router.post("/sessions/{session_id}/resume", response_model=SessionView)
async def resume_session(
    session_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> SessionView:
    service = SessionService(services(request))
    row = await service.resume(db, user, service.owned(db, user, session_id))
    return session_view(db, row)


@router.get("/sessions/{session_id}/messages")
async def session_history(
    session_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = SessionService(services(request))
    row = service.owned(db, user, session_id)
    return {"items": await service.history(db, user, row)}


@router.get("/sessions/{session_id}/media/{media_id}")
async def session_media(
    session_id: str,
    media_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Stream one history-bound voice note without exposing its host path."""

    service = SessionService(services(request))
    row = service.owned(db, user, session_id)
    asset = await service.media(db, user, row, media_id)
    return FileResponse(
        asset.path,
        media_type=asset.media_type,
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="voice-note{asset.path.suffix.lower()}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/sessions/{session_id}/export")
async def export_session(
    session_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download a bounded, sanitized projection of Hermes-owned history."""

    service = SessionService(services(request))
    row = service.owned(db, user, session_id)
    history = await service.history(db, user, row)
    byte_budget = 10 * 1024 * 1024
    exported: list[dict[str, Any]] = []
    used = 0
    truncated = len(history) >= 5_000
    for item in history:
        if not isinstance(item, dict):
            continue
        encoded = json.dumps(
            item, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode("utf-8")
        if used + len(encoded) > byte_budget:
            truncated = True
            break
        exported.append(item)
        used += len(encoded)
    payload = {
        "format": "hermes-control.session-export.v1",
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "sourceOfTruth": "Hermes",
        "session": {
            "controlSessionId": row.id,
            "gatewayId": row.gateway_id,
            "profileName": row.profile_name,
            "storedSessionId": row.stored_session_id,
            "title": row.title,
        },
        "messages": exported,
        "truncated": truncated,
        "redaction": "Secrets, private paths and reasoning fields are omitted.",
    }
    audit(
        db,
        actor=user,
        action="session.export",
        target_type="session",
        target_id=row.id,
        details={"messageCount": len(exported), "truncated": truncated},
    )
    db.commit()
    return Response(
        content=json.dumps(payload, ensure_ascii=False, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="hermes-session-{row.id}.json"'
            ),
            "Cache-Control": "no-store",
        },
    )


@router.post("/sessions/{session_id}/prompts", response_model=OperationView, status_code=202)
async def submit_prompt(
    session_id: str,
    payload: PromptRequest,
    request: Request,
    idempotency_key: str = Depends(require_idempotency),
    _: AuthSession = Depends(require_csrf),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = SessionService(services(request))
    row = service.owned(db, user, session_id)
    return await service.submit(db, user, row, payload.content, idempotency_key)


@router.get("/sessions/{session_id}/operations/{operation_id}", response_model=OperationView)
def prompt_operation(
    session_id: str,
    operation_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> OperationView:
    row = SessionService(services(request)).owned(db, user, session_id)
    operation = db.scalar(
        select(IdempotencyOperation).where(
            IdempotencyOperation.user_id == user.id,
            IdempotencyOperation.scope == f"session:{row.id}:prompt",
            IdempotencyOperation.idempotency_key == operation_id,
        )
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Prompt operation not found")
    response = dict(operation.response_json or {})
    return OperationView(
        operation_id=str(response.get("operationId") or operation.idempotency_key),
        status=operation.status,
        accepted_at=response.get("acceptedAt"),
    )


@router.post("/sessions/{session_id}/interrupt", status_code=204)
async def interrupt_session(
    session_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    service = SessionService(services(request))
    await service.interrupt(db, user, service.owned(db, user, session_id))
    return Response(status_code=204)


@router.post(
    "/sessions/{session_id}/approvals/{request_id}/respond",
    response_model=ApprovalResponseView,
)
async def respond_to_approval(
    payload: ApprovalResponseRequest,
    request: Request,
    session_id: str = Path(min_length=1, max_length=36),
    request_id: str = Path(min_length=1, max_length=200),
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = SessionService(services(request))
    return await service.respond_approval(
        db,
        user,
        service.owned(db, user, session_id),
        request_id=request_id,
        choice=payload.choice,
    )


@router.post(
    "/sessions/{session_id}/clarifications/{request_id}/respond",
    response_model=ClarificationResponseView,
)
async def respond_to_clarification(
    payload: ClarificationResponseRequest,
    request: Request,
    session_id: str = Path(min_length=1, max_length=36),
    request_id: str = Path(min_length=1, max_length=200),
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    service = SessionService(services(request))
    return await service.respond_clarification(
        db,
        user,
        service.owned(db, user, session_id),
        request_id=request_id,
        answer=payload.answer,
        question_id=payload.question_id,
    )


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session_from_hermes(
    session_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    service = SessionService(services(request))
    row = service.owned(db, user, session_id)
    if request.headers.get("X-Confirm-Delete") != row.stored_session_id:
        raise HTTPException(status_code=409, detail="Exact stored session id confirmation required")
    await service.delete_from_hermes(db, user, row)
    return Response(status_code=204)


@router.get("/automations", response_model=list[AutomationView])
def list_automations(
    user: User = Depends(current_user), db: Session = Depends(get_db)
) -> list[Automation]:
    return list(
        db.scalars(
            select(Automation)
            .where(Automation.owner_id == user.id)
            .order_by(Automation.updated_at.desc())
        ).all()
    )


@router.post("/automations/sync", response_model=list[AutomationView])
async def sync_automations(
    request: Request,
    gateway_id: str = Query(alias="gatewayId"),
    profile_name: str = Query(alias="profileName"),
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_admin),
    db: Session = Depends(get_db),
) -> list[Automation]:
    return await AutomationService(services(request)).sync(
        db,
        user,
        gateway_id=gateway_id,
        profile_name=profile_name,
    )


@router.post("/automations", response_model=AutomationView, status_code=201)
async def create_automation(
    payload: AutomationCreate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Automation:
    return await AutomationService(services(request)).create(db, user, payload)


@router.patch("/automations/{automation_id}", response_model=AutomationView)
async def update_automation(
    automation_id: str,
    payload: AutomationUpdate,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Automation:
    service = AutomationService(services(request))
    row = service.owned(db, user, automation_id)
    return await service.update(db, user, row, payload.model_dump(exclude_unset=True))


@router.post("/automations/{automation_id}/pause", response_model=AutomationView)
async def pause_automation(
    automation_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Automation:
    service = AutomationService(services(request))
    return await service.set_enabled(
        db,
        user,
        service.owned(db, user, automation_id),
        enabled=False,
    )


@router.post("/automations/{automation_id}/resume", response_model=AutomationView)
async def resume_automation(
    automation_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Automation:
    service = AutomationService(services(request))
    return await service.set_enabled(
        db,
        user,
        service.owned(db, user, automation_id),
        enabled=True,
    )


@router.delete("/automations/{automation_id}", status_code=204)
async def delete_automation(
    automation_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> Response:
    service = AutomationService(services(request))
    await service.delete(db, user, service.owned(db, user, automation_id))
    return Response(status_code=204)


@router.post("/automations/{automation_id}/trigger", response_model=OperationView, status_code=202)
async def trigger_automation(
    automation_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> OperationView:
    service = AutomationService(services(request))
    run = await service.enqueue_trigger(
        db, user, service.owned(db, user, automation_id)
    )
    background_tasks.add_task(service.execute_queued_trigger, run.id)
    return OperationView(
        operation_id=run.id,
        status=run.status,
        accepted_at=run.created_at,
    )


@router.get("/automation-runs", response_model=list[AutomationRunView])
def list_automation_runs(
    request: Request,
    automation_id: str | None = Query(default=None, alias="automationId"),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[AutomationRun]:
    return AutomationService(services(request)).runs(
        db,
        user,
        automation_id=automation_id,
        limit=limit,
    )


@router.get(
    "/automations/{automation_id}/runs",
    response_model=list[AutomationRunView],
)
def list_automation_runs_for_automation(
    automation_id: str,
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[AutomationRun]:
    return AutomationService(services(request)).runs(
        db,
        user,
        automation_id=automation_id,
        limit=limit,
    )


@router.post(
    "/automation-runs/{automation_run_id}/read",
    response_model=AutomationRunView,
)
def mark_automation_run_read(
    automation_run_id: str,
    request: Request,
    _: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> AutomationRun:
    return AutomationService(services(request)).mark_run_read(
        db,
        user,
        automation_run_id,
    )


@router.post("/realtime/tickets", response_model=TicketView, status_code=201)
def issue_realtime_ticket(
    request: Request,
    auth: AuthSession = Depends(require_csrf),
    __: str = Depends(require_idempotency),
    db: Session = Depends(get_db),
) -> TicketView:
    token, row = TicketService(services(request)).issue(db, auth)
    return TicketView(ticket=token, expires_at=row.expires_at)


@router.get("/audit", response_model=list[AuditView])
def list_audit_events(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
) -> list[AuditEvent]:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access required")
    return list(
        db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)).all()
    )


def bind_owned_realtime_event(
    session_factory,
    *,
    user_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Bind an upstream event only when every supplied route identity agrees."""

    stored_id = payload.get("storedSessionId")
    runtime_id = payload.get("runtimeSessionId")
    runtime_generation = payload.get("_runtimeGeneration")
    route_identity = payload.get("_routeIdentity")
    event_type = str(payload.get("type") or "")
    global_event = event_type in {
        "control.connection",
        "gateway.health",
        "gateway.pong",
        "gateway.status",
    }
    if route_identity == "gateway":
        if not global_event:
            return None
        bound = dict(payload)
        bound.pop("_routeIdentity", None)
        bound.pop("_runtimeGeneration", None)
        return bound
    session_scoped = bool(stored_id or runtime_id or route_identity) or event_type.startswith(
        ("message.", "tool.", "approval.", "clarify.", "session.")
    )
    if not session_scoped:
        if not global_event:
            return None
        bound = dict(payload)
        bound.pop("_routeIdentity", None)
        bound.pop("_runtimeGeneration", None)
        return bound
    if not stored_id and not runtime_id and not route_identity:
        return None
    if runtime_id and (
        not isinstance(runtime_generation, str)
        or not runtime_generation
        or len(runtime_generation) > 96
    ):
        return None
    if (
        route_identity
        and not stored_id
        and not runtime_id
        and not runtime_generation
        and event_type != "control.reconcile"
    ):
        return None
    with session_factory() as db:
        identity_clause = []
        if stored_id:
            identity_clause.append(SessionLink.stored_session_id == stored_id)
        if runtime_id:
            identity_clause.append(SessionLink.runtime_session_id == runtime_id)
            identity_clause.append(SessionLink.runtime_generation == runtime_generation)
        if route_identity:
            if event_type == "control.reconcile" and not runtime_generation:
                identity_clause.append(
                    or_(
                        SessionLink.stored_session_id == str(route_identity),
                        SessionLink.runtime_session_id == str(route_identity),
                    )
                )
            else:
                identity_clause.append(
                    or_(
                        SessionLink.stored_session_id == str(route_identity),
                        (
                            (SessionLink.runtime_session_id == str(route_identity))
                            & (SessionLink.runtime_generation == runtime_generation)
                        ),
                    )
                )
        matches = list(
            db.scalars(
                select(SessionLink)
                .where(
                    SessionLink.gateway_id == payload.get("gatewayId"),
                    SessionLink.profile_name == payload.get("profileName"),
                    *identity_clause,
                )
                .limit(2)
            ).all()
        )
        if len(matches) != 1 or matches[0].owner_id != user_id:
            return None
        control_session = matches[0]
        sequence = payload.get("seq")
        incoming_epoch = (
            str(payload["replayEpoch"])
            if payload.get("replayEpoch") is not None
            else None
        )
        if incoming_epoch is not None and len(incoming_epoch) > 100:
            return None
        if sequence is not None and (
            not isinstance(sequence, int)
            or not 0 <= sequence <= 9_223_372_036_854_775_807
        ):
            return None
        epoch_changed = bool(
            incoming_epoch
            and control_session.replay_epoch
            and incoming_epoch != control_session.replay_epoch
        )
        if epoch_changed:
            control_session.last_sequence = sequence if isinstance(sequence, int) else 0
        elif isinstance(sequence, int):
            control_session.last_sequence = max(control_session.last_sequence, sequence)
        if incoming_epoch:
            control_session.replay_epoch = incoming_epoch
        resolved_terminal = terminal_status(
            event_type,
            payload.get("data") if isinstance(payload.get("data"), dict) else None,
        )
        if resolved_terminal:
            control_session.status = "ready" if resolved_terminal in {"completed", "interrupted"} else "error"
            correlation_id = payload.get("correlationId")
            if correlation_id:
                operation = db.scalar(
                    select(IdempotencyOperation).where(
                        IdempotencyOperation.user_id == user_id,
                        IdempotencyOperation.scope == f"session:{control_session.id}:prompt",
                        IdempotencyOperation.idempotency_key == str(correlation_id),
                    )
                )
                if operation is not None:
                    operation.status = resolved_terminal
                    operation.response_json = {
                        **dict(operation.response_json or {}),
                        "operationId": str(correlation_id),
                        "status": resolved_terminal,
                    }
        db.commit()
        bound = dict(payload)
        bound.pop("_routeIdentity", None)
        bound.pop("_runtimeGeneration", None)
        bound["controlSessionId"] = control_session.id
        bound["sessionId"] = control_session.id
        bound["storedSessionId"] = control_session.stored_session_id
        bound["runtimeSessionId"] = control_session.runtime_session_id
        return bound


@router.websocket("/realtime")
async def realtime_socket(websocket: WebSocket) -> None:
    origin = websocket.headers.get("origin")
    settings = websocket.app.state.services.settings
    if not origin and settings.environment == "production":
        await websocket.close(code=4403)
        return
    if origin and origin not in settings.allowed_origins:
        await websocket.close(code=4403)
        return
    ticket = websocket.query_params.get("ticket", "")
    with websocket.app.state.session_factory() as db:
        row = TicketService(websocket.app.state.services).consume(db, ticket)
    if row is None:
        await websocket.close(code=4401)
        return
    try:
        subscription = await websocket.app.state.services.event_hub.subscribe(row.user_id)
    except SubscriptionLimitError:
        await websocket.close(code=4429)
        return
    try:
        await websocket.accept()
    except Exception:
        await websocket.app.state.services.event_hub.unsubscribe(subscription)
        raise
    inbound_times: deque[float] = deque()

    def bind_owned_session(payload: dict[str, Any]) -> dict[str, Any] | None:
        return bind_owned_realtime_event(
            websocket.app.state.session_factory,
            user_id=row.user_id,
            payload=payload,
        )

    def auth_session_is_active() -> bool:
        with websocket.app.state.session_factory() as db:
            auth_session = db.get(AuthSession, row.auth_session_id)
            if auth_session is None or auth_session.revoked_at is not None:
                return False
            expires_at = auth_session.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            return expires_at > datetime.now(timezone.utc) and auth_session.user.is_active

    try:
        encoded_cursors = websocket.query_params.get("cursors", "")
        if encoded_cursors and len(encoded_cursors) <= 16_384:
            try:
                parsed_cursors = json.loads(encoded_cursors)
                if isinstance(parsed_cursors, dict):
                    replay, reconciliations = websocket.app.state.services.event_hub.replay_since(
                        {str(key): value for key, value in parsed_cursors.items() if isinstance(value, dict)}
                    )
                    for replay_payload in [*reconciliations, *replay]:
                        if not auth_session_is_active():
                            await websocket.close(code=4401)
                            return
                        bound = bind_owned_session(replay_payload)
                        if bound is not None:
                            await websocket.send_json(bound)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        while True:
            if not auth_session_is_active():
                await websocket.close(code=4401)
                break
            event_task = asyncio.create_task(
                websocket.app.state.services.event_hub.next_event(subscription)
            )
            # Read text first so an authenticated browser frame is bounded
            # before JSON parsing. The process-level Uvicorn limit is configured
            # to the same value; this check also protects alternate ASGI hosts.
            inbound_task = asyncio.create_task(websocket.receive_text())
            done, pending = await asyncio.wait(
                {event_task, inbound_task}, timeout=15, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if not done:
                if not auth_session_is_active():
                    await websocket.close(code=4401)
                    break
                await websocket.send_json({"type": "control.heartbeat"})
                continue
            frame_text: str | None = None
            payload: dict[str, Any] | None = None
            if inbound_task in done:
                try:
                    frame_text = inbound_task.result()
                except WebSocketDisconnect:
                    break
            if event_task in done:
                payload = event_task.result()
            if not auth_session_is_active():
                await websocket.close(code=4401)
                break
            if inbound_task in done:
                now = time.monotonic()
                while inbound_times and inbound_times[0] < now - 60:
                    inbound_times.popleft()
                inbound_times.append(now)
                if len(inbound_times) > 20:
                    await websocket.close(code=4408)
                    break
                assert frame_text is not None
                if len(frame_text.encode("utf-8")) > settings.ws_max_inbound_bytes:
                    await websocket.close(code=4409)
                    break
                try:
                    frame = json.loads(frame_text)
                except (json.JSONDecodeError, UnicodeError):
                    await websocket.close(code=4400)
                    break
                if (
                    not isinstance(frame, dict)
                    or set(frame) - {"type", "at"}
                    or frame.get("type") != "ping"
                    or isinstance(frame.get("at"), bool)
                    or not isinstance(frame.get("at"), (int, float))
                    or abs(frame["at"]) > 9_223_372_036_854_775_807
                ):
                    await websocket.close(code=4400)
                    break
                await websocket.send_json({"type": "control.pong", "at": frame.get("at")})
            if event_task in done:
                assert payload is not None
                bound = bind_owned_session(payload)
                if bound is not None:
                    await websocket.send_json(bound)
    except WebSocketDisconnect:
        pass
    finally:
        await websocket.app.state.services.event_hub.unsubscribe(subscription)
