"""Cloud operations: bounded aggregate metrics and a conservative release drain."""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import contextlib
import json
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from .auth import current_admin, issue_session, require_csrf, SESSION_COOKIE
from .connector_models import Connector
from .models import AuthSession, AutomationRun, SessionLink, User, utc_now
from .services import GatewayService

router = APIRouter(prefix="/api/v1/platform", tags=["platform"])
ACTIVE = frozenset({"pending", "queued", "accepted", "starting", "streaming", "running", "working", "waiting", "awaiting_approval", "awaiting_clarification"})


class CloudMetrics:
    def __init__(self):
        self.started_at = time.monotonic()
        self.requests = defaultdict(lambda: {"count": 0, "errors": 0, "seconds": 0.0})

    def record(self, route: str, status: int, seconds: float) -> None:
        if route not in self.requests and len(self.requests) >= 256:
            route = "other"
        row = self.requests[route]
        row["count"] += 1
        row["errors"] += int(status >= 400)
        row["seconds"] += seconds

    def snapshot(self) -> dict:
        return {"uptimeSeconds": int(time.monotonic() - self.started_at),
                "requests": {route: {**row, "seconds": round(row["seconds"], 3)} for route, row in self.requests.items()}}


class CloudOperationsMiddleware:
    def __init__(self, app, state, metrics: CloudMetrics):
        self.app, self.state, self.metrics = app, state, metrics
        self.state.cloud_mutations_inflight = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if self.state.cloud_draining and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"} and path not in {"/api/v1/platform/resume", "/api/v1/platform/drain"}:
            payload = b'{"code":"RELEASE_MAINTENANCE","message":"A release is being installed. Keep your draft and try again shortly.","retryable":true}'
            await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store"), (b"retry-after", b"15")]})
            await send({"type": "http.response.body", "body": payload})
            return
        mutation = scope["method"] in {"POST", "PUT", "PATCH", "DELETE"} and path not in {
            "/api/v1/platform/drain", "/api/v1/platform/resume",
        }
        if mutation:
            self.state.cloud_mutations_inflight += 1
        started = time.monotonic()
        status = 500
        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
        try:
            await self.app(scope, receive, capture)
        finally:
            if mutation:
                self.state.cloud_mutations_inflight -= 1
            # Only route templates, never a path, query, user id or request body.
            template = getattr(scope.get("route"), "path", "unmatched")
            self.metrics.record(template, status, time.monotonic() - started)


def cloud_only(request: Request):
    if request.app.state.settings.deployment_mode != "cloud":
        raise HTTPException(404, "Not found")


@router.get("/status", dependencies=[Depends(current_admin)])
def platform_status(request: Request):
    cloud_only(request)
    registry = request.app.state.connector_registry
    with request.app.state.session_factory() as db:
        users = db.scalar(select(func.count()).select_from(User))
        connectors = db.scalar(select(func.count()).select_from(Connector).where(Connector.revoked_at.is_(None)))
    return {"users": users, "connectors": connectors,
            "onlineConnectors": sum(link.online for link in registry.links.values()),
            "pendingOperations": sum(len(link.pending) for link in registry.links.values()),
            "draining": request.app.state.cloud_draining,
            "pendingMutations": getattr(request.app.state, "cloud_mutations_inflight", 0),
            "api": request.app.state.cloud_metrics.snapshot()}


@router.post("/drain", dependencies=[Depends(current_admin), Depends(require_csrf)])
async def drain(request: Request):
    cloud_only(request)
    state = request.app.state
    generation = getattr(state, "cloud_drain_generation", 0) + 1
    state.cloud_drain_generation = generation
    state.cloud_draining = True
    try:
        registry = state.connector_registry
        if getattr(state, "cloud_mutations_inflight", 0) or any(link.pending for link in registry.links.values()):
            raise HTTPException(409, "Wait for in-flight operations before deploying")
        # Bound the whole preflight, not only individual profile requests. A
        # failed scan must release maintenance even when many devices are slow.
        async with asyncio.timeout(60):
            with state.session_factory() as db:
                if db.scalar(select(AutomationRun.id).where(AutomationRun.status.in_(("queued", "running"))).limit(1)):
                    raise HTTPException(409, "An automation has an unresolved dispatch")
                connectors = db.scalars(select(Connector).where(Connector.revoked_at.is_(None))).all()
                for connector in connectors:
                    if not registry.online(connector.gateway_id):
                        active = db.scalar(select(SessionLink.id).where(
                            SessionLink.gateway_id == connector.gateway_id,
                            SessionLink.status.in_(ACTIVE),
                        ).limit(1))
                        if active:
                            raise HTTPException(409, "An offline computer has unresolved work")
                        snapshots = list(db.scalars(select(SessionLink.background_tasks).where(
                            SessionLink.gateway_id == connector.gateway_id).limit(10_001)))
                        if len(snapshots) > 10_000 or any(item and (
                            item.get("complete") is not True
                            or type(item.get("activeCount")) is not int or item.get("activeCount") != 0
                            or type(item.get("pendingDeliveryCount")) is not int or item.get("pendingDeliveryCount") != 0
                            or any(task.get("state") in {"queued", "running", "unknown"}
                                for task in item.get("items", []))) for item in snapshots
                        ):
                            raise HTTPException(409, "An offline computer has unresolved background tasks")
                        continue
                    for profile in connector.profiles:
                        connection = await GatewayService(state.services).connection(db, connector.gateway_id, profile)
                        provider = await state.services.provider_pool.get(connection)
                        link = registry.get(connector.gateway_id)
                        if profile in getattr(link, "background_task_profiles", set()):
                            tasks = await asyncio.wait_for(provider.list_background_tasks(), 15)
                            if (tasks.get("complete") is not True or type(tasks.get("activeCount")) is not int
                                    or type(tasks.get("pendingDeliveryCount")) is not int
                                    or tasks.get("activeCount") != 0 or tasks.get("pendingDeliveryCount") != 0):
                                raise HTTPException(409, "A computer has active or uncertain background tasks")
                            if tasks.get("retired") is True:
                                # New connectors prove the exact native
                                # deletion tombstone and an empty retired DB.
                                # Querying this absent profile could recreate it.
                                continue
                        sessions = await asyncio.wait_for(provider.list_sessions(), 15)
                        if not provider.session_inventory_complete or any(session.status in ACTIVE for session in sessions):
                            raise HTTPException(409, "A computer has active or uncertain work")
        if generation != state.cloud_drain_generation or not state.cloud_draining:
            raise HTTPException(409, "The release preflight was canceled; do not restart")
        if getattr(state, "cloud_mutations_inflight", 0) or any(link.pending for link in registry.links.values()):
            raise HTTPException(409, "Background work is in flight; retry the release preflight")
        return {"safeToRestart": True, "draining": True}
    except BaseException as exc:
        # An older scan must never resume a newer operator's drain.
        if state.cloud_drain_generation == generation:
            state.cloud_draining = False
        if isinstance(exc, TimeoutError):
            raise HTTPException(409, "Computer availability could not be verified before the deadline") from None
        if isinstance(exc, ConnectionError):
            raise HTTPException(409, "A computer disconnected during the release preflight") from None
        raise


@router.post("/resume", dependencies=[Depends(current_admin), Depends(require_csrf)])
def resume(request: Request):
    cloud_only(request)
    request.app.state.cloud_drain_generation = getattr(request.app.state, "cloud_drain_generation", 0) + 1
    request.app.state.cloud_draining = False
    return {"draining": False}


def main() -> None:
    """Container-local operator CLI; temporary session is always revoked."""
    import httpx
    from .config import get_settings
    from .database import build_engine, build_session_factory
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "drain", "resume"])
    args = parser.parse_args()
    settings = get_settings()
    if settings.deployment_mode != "cloud":
        raise SystemExit("Cloud mode required")
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    session_id = None
    try:
        with factory() as db:
            admin = db.scalar(select(User).where(User.is_admin.is_(True), User.is_active.is_(True)).limit(1))
            if admin is None:
                raise SystemExit("Grant an existing beta user platform administration before using release controls")
            token, csrf, auth = issue_session(db, admin, ttl_hours=1)
            session_id = auth.id
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=120, trust_env=False,
                          cookies={SESSION_COOKIE: token}, headers={"Origin": settings.public_base_url,
                          "X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex}) as client:
            path = f"/api/v1/platform/{args.command}"
            result = client.get(path) if args.command == "status" else client.post(path)
            if result.status_code != 200:
                raise SystemExit(f"Release control refused (HTTP {result.status_code}); no restart is authorized")
            print(json.dumps(result.json(), sort_keys=True))
    finally:
        if session_id is not None:
            with factory() as db:
                auth = db.get(AuthSession, session_id)
                if auth:
                    auth.revoked_at = utc_now()
                    db.commit()
        engine.dispose()


if __name__ == "__main__":
    main()
