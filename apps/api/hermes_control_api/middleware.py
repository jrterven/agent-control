from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from .auth import SESSION_COOKIE, resolve_session
from .config import Settings
from .models import IdempotencyOperation
from .security import constant_time_hash_matches


class BodySizeLimitMiddleware:
    """Buffer and replay request bodies with a strict, bounded ceiling.

    Reading from the ASGI receive channel here prevents chunked requests from
    bypassing Content-Length checks without ever accumulating more than the
    configured limit plus the server's current chunk.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        messages: list[dict] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                messages.append(message)
                if not message.get("more_body", False):
                    break
                continue
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                request_id = next(
                    (
                        value.decode("latin-1")
                        for key, value in scope.get("headers", [])
                        if key.lower() == b"x-request-id"
                    ),
                    uuid4().hex,
                )
                content = json.dumps(
                    {
                        "code": "REQUEST_TOO_LARGE",
                        "message": "Request body is too large",
                        "requestId": request_id,
                        "retryable": False,
                    },
                    separators=(",", ":"),
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(content)).encode()),
                            (b"cache-control", b"no-store"),
                            (b"connection", b"close"),
                            (b"x-content-type-options", b"nosniff"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": content})
                return
            messages.append(message)
            if not message.get("more_body", False):
                break

        position = 0

        async def replay_receive():
            nonlocal position
            if position < len(messages):
                message = messages[position]
                position += 1
                return message
            # Streaming responses keep listening for a client disconnect after
            # the request body has been consumed. Delegate to the original
            # channel at that point instead of inventing endless request-body
            # messages, which Starlette correctly treats as a protocol error.
            return await receive()

        await self.app(scope, replay_receive, send)


class IdempotencyMiddleware(BaseHTTPMiddleware):
    """Reserve and replay authenticated mutation responses.

    A reservation is committed before the endpoint runs, so concurrent retries
    cannot execute the mutation twice. Any server failure after reservation is
    retained as delivery-unknown; releasing it could duplicate an upstream side
    effect that completed before Control lost the response.
    """

    async def dispatch(self, request: Request, call_next):
        normalized_path = request.url.path.rstrip("/") or "/"
        if (
            request.method not in {"POST", "PUT", "PATCH", "DELETE"}
            or not request.url.path.startswith("/api/v1/")
            or request.url.path.startswith("/api/v1/auth/")
            or normalized_path
            in {
                "/api/v1/realtime/tickets",
                "/api/v1/realtime/transcription-token",
                "/api/v1/realtime/speech-token",
                "/api/v1/integrations/elevenlabs/speech",
            }
        ):
            return await call_next(request)
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key or len(key) > 200:
            return await call_next(request)

        with request.app.state.session_factory() as db:
            auth = resolve_session(db, request.cookies.get(SESSION_COOKIE))
            csrf = request.headers.get("X-CSRF-Token")
            if auth is None or not csrf or not constant_time_hash_matches(auth.csrf_hash, csrf):
                return await call_next(request)
            body = await request.body()
            request_hash = hashlib.sha256(
                request.method.encode()
                + b"\0"
                + request.url.path.encode()
                + b"?"
                + request.url.query.encode()
                + b"\0"
                + body
                + b"\0confirm-delete:"
                + request.headers.get("X-Confirm-Delete", "").encode()
            ).hexdigest()
            scope = f"http:{request.method}:{hashlib.sha256((request.url.path + '?' + request.url.query).encode()).hexdigest()}"
            existing = db.scalar(
                select(IdempotencyOperation).where(
                    IdempotencyOperation.user_id == auth.user_id,
                    IdempotencyOperation.scope == scope,
                    IdempotencyOperation.idempotency_key == key,
                )
            )
            if existing is not None:
                stored = dict(existing.response_json or {})
                if stored.get("requestHash") != request_hash:
                    return JSONResponse(
                        status_code=409,
                        content={"code": "IDEMPOTENCY_CONFLICT", "message": "Idempotency-Key was already used with another request", "retryable": False},
                    )
                if existing.status == "in_progress":
                    return JSONResponse(
                        status_code=409,
                        content={"code": "IDEMPOTENCY_IN_PROGRESS", "message": "The original mutation is still in progress", "retryable": True},
                    )
                headers = {"X-Idempotent-Replay": "true"}
                if stored.get("contentType"):
                    headers["Content-Type"] = str(stored["contentType"])
                return Response(
                    content=str(stored.get("body") or "").encode(),
                    status_code=int(stored.get("statusCode") or 200),
                    headers=headers,
                )

            operation = IdempotencyOperation(
                user_id=auth.user_id,
                scope=scope,
                idempotency_key=key,
                status="in_progress",
                response_json={"requestHash": request_hash},
            )
            db.add(operation)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return JSONResponse(
                    status_code=409,
                    content={"code": "IDEMPOTENCY_IN_PROGRESS", "message": "A concurrent request owns this Idempotency-Key", "retryable": True},
                )
            operation_id = operation.id

        try:
            response = await call_next(request)
            response_body = b"".join([chunk async for chunk in response.body_iterator])
        except asyncio.CancelledError:
            self._mark_delivery_unknown(request, operation_id, request_hash)
            raise
        except Exception:
            self._mark_delivery_unknown(request, operation_id, request_hash)
            raise

        with request.app.state.session_factory() as db:
            row = db.get(IdempotencyOperation, operation_id)
            if row is not None:
                if response.status_code >= 500:
                    row.status = "delivery_unknown"
                    row.response_json = self._delivery_unknown_record(request_hash)
                else:
                    row.status = "complete"
                    row.response_json = {
                        "requestHash": request_hash,
                        "statusCode": response.status_code,
                        "contentType": response.headers.get("content-type"),
                        "body": response_body.decode("utf-8", errors="replace"),
                    }
                db.commit()
        if response.status_code >= 500:
            response_body = json.dumps(
                {
                    "code": "MUTATION_DELIVERY_UNKNOWN",
                    "message": "Mutation outcome is unknown; reconcile before retrying",
                    "retryable": False,
                },
                separators=(",", ":"),
            ).encode()
            response.status_code = 409
            response.headers["content-type"] = "application/json"
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=response_body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
            background=response.background,
        )

    @staticmethod
    def _delivery_unknown_record(request_hash: str) -> dict[str, object]:
        return {
            "requestHash": request_hash,
            "statusCode": 409,
            "contentType": "application/json",
            "body": json.dumps(
                {
                    "code": "MUTATION_DELIVERY_UNKNOWN",
                    "message": "Mutation outcome is unknown; reconcile before retrying",
                    "retryable": False,
                },
                separators=(",", ":"),
            ),
        }

    @classmethod
    def _mark_delivery_unknown(
        cls, request: Request, operation_id: str, request_hash: str
    ) -> None:
        with request.app.state.session_factory() as db:
            row = db.get(IdempotencyOperation, operation_id)
            if row is not None:
                row.status = "delivery_unknown"
                row.response_json = cls._delivery_unknown_record(request_hash)
                db.commit()


class SecurityBoundaryMiddleware(BaseHTTPMiddleware):
    _LOGIN_WINDOW_SECONDS = 60
    _LOGIN_ATTEMPT_LIMIT = 10

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self._login_attempts: dict[str, deque[float]] = defaultdict(deque)

    def _login_rate_limited(self, peer: str, now: float) -> bool:
        """Limit both the direct peer and the whole single-admin endpoint.

        Uvicorn may rewrite ``scope['client']`` from a trusted proxy header
        before Starlette creates ``Request``. The global bucket is deliberate:
        a loopback process cannot evade Argon2 throttling by rotating a forged
        X-Forwarded-For value, even if proxy-header handling is accidentally
        re-enabled later.
        """

        cutoff = now - self._LOGIN_WINDOW_SECONDS
        keys = ("global", f"peer:{peer}")
        queues = [self._login_attempts[key] for key in keys]
        for queue in queues:
            while queue and queue[0] < cutoff:
                queue.popleft()
        if any(len(queue) >= self._LOGIN_ATTEMPT_LIMIT for queue in queues):
            return True
        for queue in queues:
            queue.append(now)
        if len(self._login_attempts) > 4_096:
            for key in list(self._login_attempts):
                queue = self._login_attempts[key]
                while queue and queue[0] < cutoff:
                    queue.popleft()
                if not queue:
                    del self._login_attempts[key]
        return False

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = request_id
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > self.settings.max_request_bytes:
                    return self._error(413, "REQUEST_TOO_LARGE", "Request body is too large", request_id)
            except ValueError:
                return self._error(400, "INVALID_CONTENT_LENGTH", "Invalid content length", request_id)

        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin and origin not in self.settings.allowed_origins:
                return self._error(403, "ORIGIN_REJECTED", "Request origin is not allowed", request_id)
            if not origin and self.settings.environment == "production":
                return self._error(403, "ORIGIN_REQUIRED", "Request origin is required", request_id)

        if request.url.path.endswith("/auth/login"):
            # Proxy headers are disabled in the supported launch command. The
            # global bucket below remains a fail-safe against a future
            # accidental trust of caller-controlled forwarding headers.
            peer = request.client.host if request.client else "unknown"
            now = time.monotonic()
            if self._login_rate_limited(peer, now):
                return self._error(429, "RATE_LIMITED", "Too many login attempts", request_id)

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(self), geolocation=()"
        )
        path = request.url.path
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif path == "/sw.js" or path.endswith(".webmanifest") or path.endswith(".html") or "." not in path.rsplit("/", 1)[-1]:
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
        response.headers[
            "Content-Security-Policy"
        ] = "default-src 'self'; connect-src 'self' wss://api.elevenlabs.io; media-src 'self' blob:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    @staticmethod
    def _error(status: int, code: str, message: str, request_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={
                "code": code,
                "message": message,
                "requestId": request_id,
                "retryable": status in {429, 502, 503, 504},
            },
        )
