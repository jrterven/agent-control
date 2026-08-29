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
from .middleware import BodySizeLimitMiddleware, IdempotencyMiddleware, SecurityBoundaryMiddleware
from .models import Automation
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
    UpstreamUnavailableError,
)


_LOG_SECRET = re.compile(
    r"(?i)authorization\s*[:=]\s*Bearer\s+[A-Za-z0-9._~+/=-]+|Bearer\s+[A-Za-z0-9._~+/=-]+|(authorization|api[_-]?key|token|ticket|secret|password)\s*[:=]\s*[^\s,;&]+|sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}"
)


def _mark_orphans(session_factory, services: AppServices) -> int:
    with session_factory() as db:
        return AutomationService(services).mark_orphaned_local_triggers_unknown(db)


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
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "hermes_control"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, RedactingLogFilter) for item in logger.filters):
            logger.addFilter(RedactingLogFilter())


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
    async def durable_event_sink(event) -> None:
        event_hub.remember_correlation(event)
        persist_normalized_event(
            session_factory,
            event,
            gateway_health_ttl_seconds=settings.upstream_health_ttl_seconds,
        )
        await event_hub.publish(event)

    provider_pool = build_provider_pool(settings, durable_event_sink)
    service_container = AppServices(
        settings=settings,
        vault=SecretVault(settings.materialize_vault_key()),
        event_hub=event_hub,
        provider_pool=provider_pool,
        session_router=HermesSessionRouter(provider_pool),
        session_factory=session_factory,
    )
    automation_route_health = SupervisorHealth(
        stale_after_seconds=max(
            settings.automation_route_stale_seconds,
            settings.automation_route_watch_seconds * 2,
        )
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
        app.state.automation_route_watcher_task = automation_watcher
        app.state.warm_automation_routes_once = warm_automation_routes_once
        app.state.mark_orphaned_local_triggers_unknown = lambda: _mark_orphans(
            session_factory, service_container
        )
        try:
            yield
        finally:
            automation_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await automation_watcher
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
    app.state.automation_route_health = automation_route_health
    app.add_middleware(IdempotencyMiddleware)
    app.add_middleware(SecurityBoundaryMiddleware, settings=settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key", "X-Confirm-Delete"],
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.include_router(router)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError):
        return error_response(request, 404, "NOT_FOUND", str(exc))

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError):
        return error_response(request, 409, "CONFLICT", str(exc))

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
