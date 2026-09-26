"""Device pairing helpers; invitation admission is enforced by cloud login."""
from __future__ import annotations
import hashlib
import hmac
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timezone
from fastapi import HTTPException
from .auth import aware
from .connector_models import Connector, DeviceAuthorization

PROFILE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}$")
CODE = re.compile(r"^[A-Z2-9]{8}$")
PAIR_COOKIE = "hc_connector_pair"


def normalized_code(value: str) -> str:
    code = value.upper().replace("-", "").replace(" ", "")
    if not CODE.fullmatch(code):
        raise HTTPException(404, "Pairing code not found or expired")
    return code


def require_pending(row: DeviceAuthorization | None):
    if row is None or aware(row.expires_at) <= datetime.now(timezone.utc):
        raise HTTPException(404, "Pairing code not found or expired")
    if row.consumed_at is not None or row.approved_by is not None:
        raise HTTPException(409, "Pairing code was already used")
    return row


def binding(key: bytes, row_id: str, session_id: str) -> str:
    return hmac.new(key, f"connector-pair-v1:{row_id}:{session_id}".encode(), hashlib.sha256).hexdigest()


def connector_view(row: Connector, registry) -> dict:
    from .connector_updates import update_view
    return {"id": row.id, "name": row.name, "gatewayId": row.gateway_id,
            "update": update_view(row),
            "installationKind": row.installation_kind, "hermesVersion": row.hermes_version,
            "status": "revoked" if row.revoked_at else "online" if registry.online(row.gateway_id) else "offline",
            "version": row.version, "lastSeenAt": aware(row.last_seen_at).isoformat() if row.last_seen_at else None,
            "profiles": row.profiles}


class PairRateLimiter:
    def __init__(self):
        self.buckets: OrderedDict[tuple, deque] = OrderedDict()

    def check(self, scope: str, peer: str, maximum: int, window: int = 60):
        now = time.monotonic()
        key = (scope, peer)
        queue = self.buckets.setdefault(key, deque())
        self.buckets.move_to_end(key)
        while queue and queue[0] <= now - window:
            queue.popleft()
        if len(queue) >= maximum:
            raise HTTPException(429, "Too many connector requests", headers={"Retry-After": "60"})
        queue.append(now)
        while len(self.buckets) > 4096:
            self.buckets.popitem(last=False)
