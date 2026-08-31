from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import re
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
import httpx
from hermes_client import JsonRpcError, RouteMismatchError, UnsafeEndpointError
from hermes_client.limits import UpstreamPayloadError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .api import router
from .config import Settings, get_settings
from .database import Base, build_engine, build_session_factory
from .eventing import EventHub
from .integrations import (
    ElevenLabsSpeechClient,
    ElevenLabsScribeClient,
    IntegrationError,
    SpeechRateLimiter,
    TranscriptionTokenLimiter,
)
from .middleware import BodySizeLimitMiddleware, IdempotencyMiddleware, SecurityBoundaryMiddleware
from .models import Automation, Gateway
from .notifications import PushNotificationService
from .providers import build_provider_pool
from .realtime import persist_normalized_event
from .security import SecretVault
from .supervision import SupervisorHealth, supervise_periodic
from .services import (
    AppServices,
    AutomationService,
    ConflictError,
    GatewayService,
    HermesSessionRouter,
    NotFoundError,
    ProfileService,
    UpstreamUnavailableError,
)


_LOG_SECRET = re.compile(
    r"(?i)authorization\s*[:=]\s*Bearer\s+[A-Za-z0-9._~+/=-]+"
    r"|Bearer\s+[A-Za-z0-9._~+/=-]+"
    r"|(authorization|api[_-]?key|token|ticket|secret|password)\s*[:=]\s*[^\s,;&]+"
    r"|(?<![A-Za-z0-9._~-])(?:sk[-_][A-Za-z0-9][A-Za-z0-9._~-]{11,}"
    r"|sutkn_[A-Za-z0-9][A-Za-z0-9._~-]{7,})(?![A-Za-z0-9._~-])"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
)


def _mark_orphans(session_factory, services: AppServices) -> int:
    with session_factory() as db:
        return AutomationService(services).mark_orphaned_local_triggers_unknown(db)


def _find_sibling_release_asset(static_root: Path, spa_path: str) -> Path | None:
    """Find a missing hashed asset in another immutable release, if applicable."""

    if not spa_path.startswith("assets/"):
        return None
    relative_asset = Path(spa_path.removeprefix("assets/"))
    if (
        not relative_asset.parts
        or relative_asset.is_absolute()
        or any(part in {"", ".", ".."} for part in relative_asset.parts)
    ):
        return None

    try:
        current_release = static_root.parents[2]
        releases_root = static_root.parents[3]
    except IndexError:
        return None
    if (
        releases_root.name != "releases"
        or static_root != current_release / "apps" / "api" / "static"
    ):
        return None

    try:
        release_entries = [
            entry
            for entry in releases_root.iterdir()
            if entry != current_release and not entry.is_symlink() and entry.is_dir()
        ]
    except OSError:
        return None

    def installed_at(entry: Path) -> int:
        try:
            return entry.stat().st_mtime_ns
        except OSError:
            return -1

    for release_entry in sorted(release_entries, key=installed_at, reverse=True):
        try:
            release_root = release_entry.resolve()
            assets_root = (release_root / "apps" / "api" / "static" / "assets").resolve()
        except OSError:
            continue
        if release_root not in assets_root.parents or not assets_root.is_dir():
            continue
        try:
            candidate = (assets_root / relative_asset).resolve()
        except OSError:
            continue
        if assets_root not in candidate.parents:
            continue
        if candidate.is_file():
            return candidate
    return None


class RedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("uvicorn"):
            # Uvicorn formatters read structured args directly. Keep tuples
            # intact while redacting string members; flattening them either
            # crashes access logging or leaves literal % placeholders.
            if isinstance(record.msg, str):
                record.msg = _LOG_SECRET.sub("[REDACTED]", record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(
                    _LOG_SECRET.sub("[REDACTED]", value) if isinstance(value, str) else value
                    for value in record.args
                )
            return True
        message = record.getMessage()
        record.msg = _LOG_SECRET.sub("[REDACTED]", message)
        record.args = ()
        return True


def configure_redacted_logging() -> None:
    names = (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "hermes_control",
        "hermes_control.provider",
        "hermes_control.supervision",
        "httpx",
        "httpcore",
    )
    handlers: set[logging.Handler] = set(logging.getLogger().handlers)
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(item, RedactingLogFilter) for item in logger.filters):
            logger.addFilter(RedactingLogFilter())
        handlers.update(logger.handlers)
    # Filters on ancestor loggers do not run for propagated child records.
    # Applying the same structure-preserving filter to installed handlers
    # closes that gap without changing Uvicorn's logger hierarchy or format.
    for handler in handlers:
        if not any(isinstance(item, RedactingLogFilter) for item in handler.filters):
            handler.addFilter(RedactingLogFilter())


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_redacted_logging()
    settings = settings or get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    event_hub = EventHub(
        queue_size=settings.ws_queue_size,
        max_subscriptions=settings.ws_max_connections,
        max_subscriptions_per_user=settings.ws_max_connections_per_user,
    )
    vault = SecretVault(settings.materialize_vault_key())
    push_notification_service = PushNotificationService(
        session_factory=session_factory,
        vault=vault,
        vapid_subject=settings.push_vapid_subject,
    )

    async def durable_event_sink(event) -> None:
        event_hub.remember_correlation(event)
        completion = persist_normalized_event(
            session_factory,
            event,
            gateway_health_ttl_seconds=settings.upstream_health_ttl_seconds,
        )
        await event_hub.publish(event)
        push_notification_service.schedule(completion)

    provider_pool = build_provider_pool(settings, durable_event_sink)
    service_container = AppServices(
        settings=settings,
        vault=vault,
        event_hub=event_hub,
        provider_pool=provider_pool,
        session_router=HermesSessionRouter(provider_pool),
        session_factory=session_factory,
        push_notifications=push_notification_service,
    )
    automation_route_health = SupervisorHealth(
        stale_after_seconds=max(
            settings.automation_route_stale_seconds,
            settings.automation_route_watch_seconds * 2,
        )
    )
    capability_refresh_interval = min(
        settings.capability_refresh_seconds,
        max(2.5, settings.capability_ttl_seconds / 2),
    )
    capability_refresh_health = SupervisorHealth(
        stale_after_seconds=max(
            settings.capability_ttl_seconds,
            capability_refresh_interval * 3,
        )
    )

    async def warm_capabilities_once() -> None:
        """Renew profile-scoped capability proofs without a browser mutation.

        Capability assertions deliberately expire quickly. Keeping their
        renewal in the backend prevents a long-lived PWA from becoming stuck
        in read-only mode while preserving the fail-closed TTL when Hermes or
        its private tunnel is actually unavailable.
        """

        with session_factory() as db:
            gateway_ids = list(
                db.scalars(select(Gateway.id).where(Gateway.enabled.is_(True))).all()
            )
        for gateway_id in gateway_ids:
            try:
                with session_factory() as db:
                    await ProfileService(service_container).sync(db, gateway_id)
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError:
                # A local database error invalidates the supervisor pass and
                # must remain visible through its health state.
                raise
            except Exception:
                # One unavailable gateway must not stop refreshes for the
                # other independently configured gateways.
                continue

    async def supervise_capabilities() -> None:
        await supervise_periodic(
            warm_capabilities_once,
            health=capability_refresh_health,
            interval_seconds=capability_refresh_interval,
        )

    async def warm_automation_routes_once() -> None:
        """Reconcile authoritative cron sessions even with no browser open.

        Official Hermes cron execution is represented by ordinary persisted
        sessions returned from ``/api/cron/jobs/{id}/runs``; it does not emit
        the invented ``run.*`` websocket events older mocks exposed. Polling
        this bounded history links unattended runs without copying messages.
        Failures stay isolated and the outer watcher retries later.
        """

        with session_factory() as db:
            rows = list(db.scalars(select(Automation)).all())
            automation_service = AutomationService(service_container)
            providers: dict[tuple[str, str], object] = {}
            for row in rows:
                try:
                    route = (row.gateway_id, row.profile_name)
                    provider = providers.get(route)
                    if provider is None:
                        connection = await GatewayService(service_container).connection(
                            db, row.gateway_id, row.profile_name
                        )
                        provider = await provider_pool.get(connection)
                        providers[route] = provider
                    await automation_service.reconcile_upstream_runs(
                        db, row, provider=provider
                    )
                except asyncio.CancelledError:
                    raise
                except SQLAlchemyError:
                    # A route-specific Hermes failure is isolated below, but
                    # local metadata failures make the whole watcher result
                    # untrustworthy and must reach its health supervisor.
                    db.rollback()
                    raise
                except Exception:
                    db.rollback()
                    continue

    async def supervise_automation_routes() -> None:
        await supervise_periodic(
            warm_automation_routes_once,
            health=automation_route_health,
            interval_seconds=settings.automation_route_watch_seconds,
        )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.create_schema_on_start:
            Base.metadata.create_all(engine)
        with session_factory() as db:
            GatewayService(service_container).seed_environment_gateway(db)
            # This runs before requests are accepted, so these dispatch rows
            # belong to a previous process and must never be retried.
            AutomationService(service_container).mark_orphaned_local_triggers_unknown(
                db
            )
        automation_watcher = asyncio.create_task(
            supervise_automation_routes(), name="hermes-automation-route-watcher"
        )
        capability_watcher = asyncio.create_task(
            supervise_capabilities(), name="hermes-capability-refresh-watcher"
        )
        app.state.automation_route_watcher_task = automation_watcher
        app.state.capability_refresh_watcher_task = capability_watcher
        app.state.warm_automation_routes_once = warm_automation_routes_once
        app.state.warm_capabilities_once = warm_capabilities_once
        app.state.mark_orphaned_local_triggers_unknown = lambda: _mark_orphans(
            session_factory, service_container
        )
        try:
            yield
        finally:
            automation_watcher.cancel()
            capability_watcher.cancel()
            for watcher in (automation_watcher, capability_watcher):
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
            await push_notification_service.close()
            await provider_pool.close()
            engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url="/api/docs" if settings.environment != "production" else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.environment != "production" else None,
        lifespan=lifespan,
    )
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.services = service_container
    app.state.push_notification_service = push_notification_service
    app.state.elevenlabs_scribe_client = ElevenLabsScribeClient()
    app.state.elevenlabs_speech_client = ElevenLabsSpeechClient()
    app.state.transcription_token_limiter = TranscriptionTokenLimiter(
        limit=settings.transcription_token_rate_limit,
        window_seconds=settings.transcription_token_rate_window_seconds,
    )
    app.state.speech_rate_limiter = SpeechRateLimiter(
        limit=settings.speech_rate_limit,
        window_seconds=settings.speech_rate_window_seconds,
    )
    app.state.automation_route_health = automation_route_health
    app.state.capability_refresh_health = capability_refresh_health
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(SecurityBoundaryMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "X-Confirm-Delete"],
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        attachment_max_bytes=settings.prompt_attachment_request_max_bytes,
    )
    app.include_router(router)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError):
        return error_response(request, 404, "NOT_FOUND", str(exc))

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError):
        return error_response(request, 409, "CONFLICT", str(exc))

    @app.exception_handler(IntegrationError)
    async def integration_error(request: Request, exc: IntegrationError):
        response = error_response(
            request,
            exc.status_code,
            exc.code,
            exc.public_message,
            retryable=exc.retryable,
        )
        if exc.retry_after is not None:
            response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(RouteMismatchError)
    async def route_mismatch(request: Request, exc: RouteMismatchError):
        return error_response(request, 409, "ROUTE_MISMATCH", "Hermes session identity did not match")

    @app.exception_handler(UnsafeEndpointError)
    async def unsafe_endpoint(request: Request, exc: UnsafeEndpointError):
        return error_response(request, 400, "UNSAFE_ENDPOINT", "Gateway endpoint is not allowed")

    @app.exception_handler(UpstreamPayloadError)
    async def upstream_payload_error(request: Request, exc: UpstreamPayloadError):
        return error_response(request, 502, "HERMES_PAYLOAD_REJECTED", "Hermes returned an invalid response")

    @app.exception_handler(UpstreamUnavailableError)
    async def upstream_unavailable(request: Request, exc: UpstreamUnavailableError):
        return error_response(request, 503, "HERMES_UNAVAILABLE", str(exc), retryable=True)

    @app.exception_handler(JsonRpcError)
    async def upstream_rpc_error(request: Request, exc: JsonRpcError):
        return error_response(request, 502, "HERMES_PROTOCOL_ERROR", "Hermes rejected the protocol operation", retryable=False)

    @app.exception_handler(httpx.TransportError)
    async def upstream_http_transport_error(request: Request, exc: httpx.TransportError):
        return error_response(request, 503, "HERMES_UNAVAILABLE", "Hermes transport is unavailable", retryable=True)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_http_error(request: Request, exc: httpx.HTTPError):
        return error_response(request, 502, "HERMES_HTTP_ERROR", "Hermes HTTP transport failed", retryable=True)

    @app.exception_handler(ConnectionError)
    @app.exception_handler(TimeoutError)
    @app.exception_handler(OSError)
    async def upstream_transport_error(request: Request, exc: Exception):
        return error_response(request, 503, "HERMES_UNAVAILABLE", "Hermes transport is unavailable", retryable=True)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "requestId": getattr(request.state, "request_id", None),
                "retryable": False,
                "fields": [
                    {"path": ".".join(str(item) for item in error["loc"]), "type": error["type"]}
                    for error in exc.errors()
                ],
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logging.getLogger("hermes_control").error(
            "request_failed request_id=%s error_type=%s",
            getattr(request.state, "request_id", "unknown"),
            type(exc).__name__,
        )
        return error_response(request, 500, "INTERNAL_ERROR", "An internal error occurred")

    # The production image places the Vite bundle here. API fallthrough is
    # registered first so an unknown API URL can never be mistaken for a SPA
    # route and cached as HTML.
    static_dir = (
        Path(settings.static_dir).expanduser().resolve()
        if settings.static_dir
        else Path(__file__).resolve().parents[1] / "static"
    )
    if (static_dir / "index.html").is_file():
        @app.api_route(
            "/api/{unknown_path:path}",
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            include_in_schema=False,
        )
        async def unknown_api(request: Request, unknown_path: str):
            return error_response(request, 404, "NOT_FOUND", "API endpoint not found")

        static_root = static_dir.resolve()

        @app.get("/{spa_path:path}", include_in_schema=False)
        async def spa(spa_path: str):
            candidate = (static_root / spa_path).resolve()
            if candidate != static_root and static_root not in candidate.parents:
                return JSONResponse(status_code=404, content={"code": "NOT_FOUND", "message": "File not found"})
            if candidate.is_file():
                return FileResponse(candidate, media_type=mimetypes.guess_type(candidate.name)[0])
            sibling_asset = _find_sibling_release_asset(static_root, spa_path)
            if sibling_asset is not None:
                return FileResponse(
                    sibling_asset,
                    media_type=mimetypes.guess_type(sibling_asset.name)[0],
                )
            last_segment = spa_path.rsplit("/", 1)[-1]
            if spa_path == "assets" or spa_path.startswith("assets/") or "." in last_segment:
                return JSONResponse(status_code=404, content={"code": "NOT_FOUND", "message": "File not found"})
            return FileResponse(static_root / "index.html", media_type="text/html")
    return app


def error_response(
    request: Request, status: int, code: str, message: str, *, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "requestId": getattr(request.state, "request_id", None),
            "retryable": retryable,
        },
    )


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "hermes_control_api.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        ws_max_size=settings.ws_max_inbound_bytes,
    )
