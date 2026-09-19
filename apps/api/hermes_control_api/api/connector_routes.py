from __future__ import annotations

import asyncio
import contextlib
import hmac
import secrets
import shlex
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session
from hermes_client.compatibility import AUDITED_REVISIONS
from hermes_client.connector_protocol import FrameReader, ProtocolError, VERSION, send_message

from ..auth import aware, current_user, get_db, require_csrf
from ..connector_models import Connector, DeviceAuthorization
from ..connectors import PAIR_COOKIE, PROFILE, PairRateLimiter, binding, connector_view, normalized_code, require_pending
from ..models import AuthSession, Gateway, GatewayCredential, ProfileRef, User
from ..remote_provider import ConnectorLink
from ..security import random_token, token_hash

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])


def _ingest_image(state, connector_id: str, message: dict) -> dict:
    # A fresh DB session belongs to this worker, never the WebSocket event loop.
    from ..visual_media import get_visual_media_service
    with state.session_factory() as db:
        connector = db.get(Connector, connector_id)
        if connector is None:
            return {"id": message["id"], "status": "failed", "errorCode": "forbidden"}
        return get_visual_media_service(state.services).ingest(
            db, connector, media_id=message["id"], profile_name=message["profile"],
            stored_session_id=message["sessionId"], metadata=message["metadata"],
            content=message["content"], thumbnail=message.get("thumbnail"),
        )


async def _publish_image(state, link, connector_id: str, message: dict) -> None:
    try:
        try:
            result = await asyncio.to_thread(_ingest_image, state, connector_id, message)
        except Exception:
            # Storage/network errors are retryable; never send exception details,
            # object-store credentials or local paths back to the connector.
            result = {"id": message["id"], "status": "failed", "errorCode": "storage_unavailable"}
        with contextlib.suppress(Exception):
            await send_message(link.send_bytes, link.lock, {"v": VERSION, "type": "media.ack", **result})
    finally:
        state.cloud_mutations_inflight -= 1
        state.visual_publications_inflight -= 1


async def _accept_image(state, link, connector_id: str, message: dict, publications: set) -> None:
    if (message.get("v") != VERSION or not isinstance(message.get("id"), str)
            or not re.fullmatch(r"[a-f0-9]{32}", message["id"])
            or not isinstance(message.get("profile"), str)
            or not isinstance(message.get("sessionId"), str)
            or not 1 <= len(message["sessionId"]) <= 255
            or not isinstance(message.get("metadata"), dict)
            or not isinstance(message.get("content"), bytes)
            or (message.get("thumbnail") is not None and not isinstance(message["thumbnail"], bytes))):
        raise ProtocolError("Invalid image publication")
    error = None
    if message["profile"] not in link.profiles:
        error = "forbidden"
    elif len(message["content"]) > state.settings.visual_media_max_bytes or len(message.get("thumbnail") or b"") > state.settings.visual_media_max_bytes:
        error = "invalid_image"
    elif getattr(state, "cloud_draining", False):
        error = "draining"
    elif len(publications) >= 2 or getattr(state, "visual_publications_inflight", 0) >= 2:
        error = "storage_unavailable"
    if error:
        await send_message(link.send_bytes, link.lock, {
            "v": VERSION, "type": "media.ack", "id": message["id"], "status": "failed", "errorCode": error,
        })
        return
    # Admission and drain accounting are atomic on the event loop. Publications
    # survive a socket reconnect, so a lost ACK can be retried idempotently.
    state.cloud_mutations_inflight = getattr(state, "cloud_mutations_inflight", 0) + 1
    state.visual_publications_inflight = getattr(state, "visual_publications_inflight", 0) + 1
    task = asyncio.create_task(_publish_image(state, link, connector_id, message))
    publications.add(task)
    task.add_done_callback(publications.discard)


class DeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    profiles: list[str] = Field(min_length=1, max_length=64)
    version: str = Field(max_length=80)
    sourceSha: str = Field(pattern=r"^[a-f0-9]{40}$")
    installationKind: Literal["managed", "existing"] | None = None
    hermesVersion: str | None = Field(default=None, max_length=80)

    @field_validator("profiles")
    @classmethod
    def valid_profiles(cls, values):
        if len(set(values)) != len(values) or any(not PROFILE.fullmatch(value) for value in values):
            raise ValueError("Invalid profile names")
        return values


class CodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=20)


class ApproveRequest(CodeRequest):
    profiles: list[str] = Field(min_length=1, max_length=64)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deviceCode: str = Field(min_length=40, max_length=100)


def enabled(request):
    if request.app.state.settings.deployment_mode != "cloud":
        raise HTTPException(404, "Not found")


def rate(request, scope, maximum, window=60):
    limiter = getattr(request.app.state, "connector_pair_limiter", None)
    if limiter is None:
        limiter = PairRateLimiter()
        request.app.state.connector_pair_limiter = limiter
    limiter.check(scope, request.client.host if request.client else "unknown", maximum, window)


@router.post("/device/authorize")
def authorize(payload: DeviceRequest, request: Request):
    enabled(request)
    rate(request, "authorize", 10, 3600)
    if payload.sourceSha not in AUDITED_REVISIONS:
        raise HTTPException(422, "Hermes revision is not supported by this connector release")
    device_code = random_token()
    user_code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
    now = datetime.now(timezone.utc)
    with request.app.state.session_factory() as db:
        db.execute(delete(DeviceAuthorization).where(DeviceAuthorization.expires_at < now - timedelta(hours=1)))
        if db.scalar(select(func.count()).select_from(DeviceAuthorization)) >= 1000:
            raise HTTPException(429, "Connector pairing is temporarily busy")
        row = DeviceAuthorization(device_code_hash=token_hash(device_code), user_code_hash=token_hash(user_code),
                                  name=payload.name, profiles=payload.profiles, version=payload.version,
                                  source_sha=payload.sourceSha, expires_at=now + timedelta(minutes=10),
                                  installation_kind=payload.installationKind, hermes_version=payload.hermesVersion)
        db.add(row)
        db.commit()
    display_code = user_code[:4] + "-" + user_code[4:]
    return {"deviceCode": device_code, "userCode": display_code,
            "verificationUri": request.app.state.settings.public_base_url.rstrip("/") + "/connect",
            "expiresIn": 600, "interval": 5}


@router.post("/device/token")
def device_token(payload: TokenRequest, request: Request):
    enabled(request)
    rate(request, "token", 120)
    now = datetime.now(timezone.utc)
    with request.app.state.session_factory() as db:
        row = db.scalar(select(DeviceAuthorization).where(DeviceAuthorization.device_code_hash == token_hash(payload.deviceCode)))
        if row is None or aware(row.expires_at) <= now or row.consumed_at:
            raise HTTPException(400, "expired_token")
        if row.connector_id is None:
            return Response(status_code=428, content='{"error":"authorization_pending"}', media_type="application/json")
        consumed = db.execute(update(DeviceAuthorization).where(DeviceAuthorization.id == row.id, DeviceAuthorization.consumed_at.is_(None)).values(consumed_at=now))
        if consumed.rowcount != 1:
            raise HTTPException(400, "expired_token")
        connector = db.get(Connector, row.connector_id)
        owner = db.get(User, connector.owner_id) if connector else None
        if connector is None or connector.revoked_at or owner is None or not owner.is_active:
            raise HTTPException(400, "access_denied")
        secret = random_token()
        connector.token_hash = token_hash(secret)
        db.commit()
        return {"accessToken": secret, "tokenType": "Bearer", "connectorId": connector.id,
                "gatewayId": connector.gateway_id, "profiles": connector.profiles}


@router.get("")
def list_connectors(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    enabled(request)
    rows = db.scalars(select(Connector).where(Connector.owner_id == user.id).order_by(Connector.created_at)).all()
    base = request.app.state.settings.public_base_url.rstrip("/")
    return {"items": [connector_view(row, request.app.state.connector_registry) for row in rows],
            "installCommand": f"curl --proto '=https' --tlsv1.2 -fsSL {shlex.quote(base + '/downloads/connector/install.sh')} | sh -s -- --server {shlex.quote(base)}"}


@router.post("/pair/inspect")
def inspect_pair(payload: CodeRequest, request: Request, response: Response,
                 auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    enabled(request)
    rate(request, "inspect", 20)
    code = normalized_code(payload.code)
    row = require_pending(db.scalar(select(DeviceAuthorization).where(DeviceAuthorization.user_code_hash == token_hash(code))))
    response.set_cookie(PAIR_COOKIE, binding(request.app.state.services.vault.key, row.id, auth.id),
                        max_age=600, httponly=True, secure=request.app.state.settings.secure_cookies,
                        samesite="strict", path="/api/v1/connectors/pair")
    return {"code": code[:4] + "-" + code[4:], "name": row.name, "profiles": row.profiles,
            "expiresAt": aware(row.expires_at).isoformat()}


@router.post("/pair/approve")
def approve_pair(payload: ApproveRequest, request: Request, response: Response,
                 auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    enabled(request)
    rate(request, "approve", 20)
    row = require_pending(db.scalar(select(DeviceAuthorization).where(DeviceAuthorization.user_code_hash == token_hash(normalized_code(payload.code)))))
    expected = binding(request.app.state.services.vault.key, row.id, auth.id)
    if not hmac.compare_digest(expected, request.cookies.get(PAIR_COOKIE, "")):
        raise HTTPException(403, "Inspect this pairing code in this browser before approving")
    if len(set(payload.profiles)) != len(payload.profiles) or not set(payload.profiles) <= set(row.profiles):
        raise HTTPException(422, "Select profiles advertised by this device")
    if db.scalar(select(func.count()).select_from(Connector).where(Connector.owner_id == auth.user_id, Connector.revoked_at.is_(None))) >= 10:
        raise HTTPException(409, "Maximum of ten connected devices reached")
    claimed = db.execute(update(DeviceAuthorization).where(DeviceAuthorization.id == row.id, DeviceAuthorization.approved_by.is_(None), DeviceAuthorization.consumed_at.is_(None)).values(approved_by=auth.user_id))
    if claimed.rowcount != 1:
        raise HTTPException(409, "Pairing code was already used")
    gateway_id = str(uuid4())
    gateway = Gateway(id=gateway_id, name=f"{row.name[:75]} ({gateway_id})", owner_id=auth.user_id,
                      transport_kind="connector", rest_url=f"connector://{gateway_id}", ws_url=f"connector://{gateway_id}",
                      connection_mode="private", health_status="offline", enabled=True, env_managed=False)
    db.add(gateway)
    db.flush()
    db.add(GatewayCredential(gateway_id=gateway.id,
        trusted_source_sha_ciphertext=request.app.state.services.vault.encrypt(row.source_sha, aad=f"gateway:{gateway.id}:source-sha")))
    connector = Connector(owner_id=auth.user_id, gateway_id=gateway_id, name=row.name,
                          profiles=payload.profiles, version=row.version, token_hash=token_hash(random_token()),
                          installation_kind=row.installation_kind, hermes_version=row.hermes_version)
    db.add(connector)
    db.flush()
    row.connector_id = connector.id
    for profile in payload.profiles:
        db.add(ProfileRef(gateway_id=gateway_id, profile_name=profile, display_name=profile, status="offline"))
    db.commit()
    response.delete_cookie(PAIR_COOKIE, path="/api/v1/connectors/pair")
    return connector_view(connector, request.app.state.connector_registry)


@router.delete("/{connector_id}")
async def revoke(connector_id: str, request: Request, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    enabled(request)
    row = db.scalar(select(Connector).where(Connector.id == connector_id, Connector.owner_id == auth.user_id))
    if row is None:
        raise HTTPException(404, "Connector not found")
    row.revoked_at = datetime.now(timezone.utc)
    gateway = db.get(Gateway, row.gateway_id)
    if gateway:
        gateway.enabled = False
        gateway.health_status = "offline"
    db.commit()
    await request.app.state.connector_registry.disconnect(row.gateway_id)
    return {"ok": True}


@router.websocket("/ws")
async def connector_socket(websocket: WebSocket):
    if websocket.app.state.settings.deployment_mode != "cloud" or websocket.headers.get("origin"):
        await websocket.close(code=1008)
        return
    authorization = websocket.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or len(authorization) > 200:
        await websocket.close(code=1008)
        return
    with websocket.app.state.session_factory() as db:
        row = db.scalar(select(Connector).join(User, User.id == Connector.owner_id).where(
            Connector.token_hash == token_hash(authorization[7:]), Connector.revoked_at.is_(None), User.is_active.is_(True)))
        if row is None:
            await websocket.close(code=1008)
            return
        connector_id, gateway_id, profiles = row.id, row.gateway_id, frozenset(row.profiles)
    await websocket.accept()
    reader = FrameReader()
    link = ConnectorLink(gateway_id, profiles, websocket.send_bytes, lambda: websocket.close(code=1001))
    registry = websocket.app.state.connector_registry
    publications: set[asyncio.Task] = set()

    async def prepare_create(name):
        if not isinstance(name, str) or not PROFILE.fullmatch(name):
            raise ValueError("Invalid new profile name")
        with websocket.app.state.session_factory() as db:
            current = db.get(Connector, connector_id)
            if current is None or current.revoked_at or len(current.profiles) >= 64:
                raise ConnectionError("Connector profile creation is unavailable")
            if name not in current.profiles:
                # The explicit owner request grants this future name before dispatch.
                # Local runtime still rejects any already existing, unshared profile.
                current.profiles = [*current.profiles, name]
                db.commit()
            link.profiles = frozenset(current.profiles)

    link.prepare_create = prepare_create
    await registry.register(link)
    try:
        from ..visual_media import get_visual_media_service
        media = get_visual_media_service(websocket.app.state.services)
        settings = websocket.app.state.settings
        await send_message(link.send_bytes, link.lock, {"v": VERSION, "type": "welcome", "gatewayId": gateway_id, "profiles": sorted(profiles),
            "capabilities": {"visualMediaV1": media.configured, "backgroundTasksV1": True, "profileTransferV2": True},
            "visualMediaLimits": {
                "maxBytes": settings.visual_media_max_bytes,
                "maxPixels": settings.visual_media_max_pixels,
                "maxImagesPerGallery": settings.visual_media_max_images_per_gallery,
                "maxImagesPerResponse": settings.visual_media_max_images_per_response,
            }})
        while True:
            raw = await asyncio.wait_for(websocket.receive_bytes(), 45)
            message = reader.feed(raw)
            if message is None:
                continue
            # Check persisted revocation/owner activation on every complete message.
            with websocket.app.state.session_factory() as db:
                current = db.get(Connector, connector_id)
                owner = db.get(User, current.owner_id) if current else None
                gateway = db.get(Gateway, gateway_id)
                if current is None or current.revoked_at or owner is None or not owner.is_active or gateway is None or not gateway.enabled:
                    break
                if current.last_seen_at is None or (datetime.now(timezone.utc) - aware(current.last_seen_at)).total_seconds() >= 10:
                    current.last_seen_at = datetime.now(timezone.utc)
                    db.commit()
            if message.get("type") == "media.publish":
                await _accept_image(websocket.app.state, link, connector_id, message, publications)
            else:
                await registry.receive(link, message)
    except (WebSocketDisconnect, TimeoutError, ProtocolError, ValueError, RuntimeError):
        pass
    finally:
        # Do not cancel a to_thread upload halfway through committing immutable
        # objects. The durable sender will retry if the disconnected ACK is lost.
        if publications:
            await asyncio.shield(asyncio.gather(*publications, return_exceptions=True))
        with contextlib.suppress(Exception):
            await registry.disconnect(gateway_id, link)
