from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pywebpush import WebPushException, webpush
from sqlalchemy import select

from .models import ProfileRef, PushSubscription, SessionLink, Workspace
from .security import SecretVault


_P256_ORDER = int(
    "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551",
    16,
)
_PUSH_ENDPOINT_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "push.services.mozilla.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
        "webpush.push.apple.com",
    }
)
_PUSH_ENDPOINT_SUFFIXES = (".notify.windows.com", ".wns.windows.com")
_SUPPORTED_LOCALES = frozenset({"de", "en", "es", "fr", "pt"})


@dataclass(frozen=True, slots=True)
class ChatCompletionNotification:
    owner_id: str
    session_id: str
    title: str
    workspace_name: str | None
    profile_name: str
    status: str
    occurred_at: str


def completion_for_session(
    db,
    session: SessionLink,
    *,
    status: str,
    occurred_at: datetime | None = None,
) -> ChatCompletionNotification:
    profile = db.scalar(
        select(ProfileRef).where(
            ProfileRef.gateway_id == session.gateway_id,
            ProfileRef.profile_name == session.profile_name,
        )
    )
    workspace = db.get(Workspace, session.workspace_id) if session.workspace_id else None
    observed = occurred_at or datetime.now(timezone.utc)
    return ChatCompletionNotification(
        owner_id=session.owner_id,
        session_id=session.id,
        title=session.display_title or session.title or "Conversation",
        workspace_name=workspace.name if workspace is not None else None,
        profile_name=profile.display_name if profile is not None else session.profile_name,
        status=status,
        occurred_at=observed.isoformat(),
    )


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def derive_vapid_key_pair(master_key: bytes) -> tuple[str, str]:
    """Derive a stable, domain-separated VAPID pair from the Control vault key."""

    material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"agent-control:web-push:v1",
    ).derive(master_key)
    scalar = (int.from_bytes(material, "big") % (_P256_ORDER - 1)) + 1
    private = ec.derive_private_key(scalar, ec.SECP256R1())
    private_raw = scalar.to_bytes(32, "big")
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(private_raw), _b64url(public_raw)


def push_endpoint_allowed(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or len(endpoint) > 2_048
    ):
        return False
    return host in _PUSH_ENDPOINT_HOSTS or any(
        host.endswith(suffix) for suffix in _PUSH_ENDPOINT_SUFFIXES
    )


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def encrypted_subscription(
    vault: SecretVault,
    *,
    owner_id: str,
    endpoint: str,
    p256dh: str,
    auth: str,
    locale: str,
) -> tuple[str, str]:
    digest = endpoint_hash(endpoint)
    payload = json.dumps(
        {
            "endpoint": endpoint,
            "keys": {"p256dh": p256dh, "auth": auth},
            "locale": locale if locale in _SUPPORTED_LOCALES else "en",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    envelope = vault.encrypt(
        payload,
        aad=f"push-subscription:{owner_id}:{digest}",
    )
    if envelope is None:  # pragma: no cover - payload is never empty
        raise ValueError("Push subscription could not be encrypted")
    return digest, envelope


def decrypted_subscription(
    vault: SecretVault,
    row: PushSubscription,
) -> dict[str, object]:
    payload = vault.decrypt(
        row.subscription_ciphertext,
        aad=f"push-subscription:{row.owner_id}:{row.endpoint_hash}",
    )
    if payload is None:
        raise ValueError("Push subscription is empty")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Push subscription is invalid")
    return value


def _localized_payload(
    completion: ChatCompletionNotification,
    locale: str,
) -> dict[str, object]:
    language = locale if locale in _SUPPORTED_LOCALES else "en"
    labels = {
        "de": {
            "completed": "Aufgabe abgeschlossen",
            "failed": "Aufgabe fehlgeschlagen",
            "interrupted": "Aufgabe unterbrochen",
            "workspace": "Kein Workspace",
        },
        "en": {
            "completed": "Task completed",
            "failed": "Task failed",
            "interrupted": "Task interrupted",
            "workspace": "No workspace",
        },
        "es": {
            "completed": "Tarea terminada",
            "failed": "La tarea falló",
            "interrupted": "Tarea interrumpida",
            "workspace": "Sin espacio de trabajo",
        },
        "fr": {
            "completed": "Tâche terminée",
            "failed": "Échec de la tâche",
            "interrupted": "Tâche interrompue",
            "workspace": "Aucun espace de travail",
        },
        "pt": {
            "completed": "Tarefa concluída",
            "failed": "Falha na tarefa",
            "interrupted": "Tarefa interrompida",
            "workspace": "Sem espaço de trabalho",
        },
    }[language]
    status = completion.status if completion.status in labels else "completed"
    workspace = completion.workspace_name or labels["workspace"]
    return {
        "title": f"{completion.profile_name} · {labels[status]}",
        "body": f"{completion.title} · {workspace}",
        "tag": f"agent-control-session-{completion.session_id}",
        "data": {
            "sessionId": completion.session_id,
            "url": f"/chats?session={completion.session_id}",
            "occurredAt": completion.occurred_at,
        },
    }


class PushNotificationService:
    def __init__(
        self,
        *,
        session_factory,
        vault: SecretVault,
        vapid_subject: str,
    ) -> None:
        self.session_factory = session_factory
        self.vault = vault
        self.vapid_subject = vapid_subject
        self.private_key, self.public_key = derive_vapid_key_pair(vault.key)
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, completion: ChatCompletionNotification | None) -> None:
        if completion is None:
            return
        task = asyncio.create_task(
            self.send_completion(completion),
            name=f"push-notification-{completion.session_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def close(self) -> None:
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=10)
        for task in pending:
            task.cancel()
        for task in (*done, *pending):
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def send_completion(self, completion: ChatCompletionNotification) -> None:
        with self.session_factory() as db:
            rows = list(
                db.scalars(
                    select(PushSubscription)
                    .where(
                        PushSubscription.owner_id == completion.owner_id,
                        PushSubscription.disabled_at.is_(None),
                    )
                    .order_by(PushSubscription.updated_at.desc())
                    .limit(8)
                ).all()
            )
            subscriptions: list[tuple[str, dict[str, object]]] = []
            invalid_rows: list[str] = []
            for row in rows:
                try:
                    subscriptions.append((row.id, decrypted_subscription(self.vault, row)))
                except (ValueError, json.JSONDecodeError):
                    invalid_rows.append(row.id)
        for row_id in invalid_rows:
            await self._record_failure(row_id, permanent=True)
        for row_id, subscription in subscriptions:
            locale = str(subscription.pop("locale", "en"))
            endpoint = str(subscription.get("endpoint") or "")
            if not push_endpoint_allowed(endpoint):
                await self._record_failure(row_id, permanent=True)
                continue
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=subscription,
                    data=json.dumps(
                        _localized_payload(completion, locale),
                        separators=(",", ":"),
                    ),
                    vapid_private_key=self.private_key,
                    vapid_claims={"sub": self.vapid_subject},
                    ttl=3_600,
                    timeout=8,
                )
            except WebPushException as exc:
                status_code = getattr(exc.response, "status_code", None)
                await self._record_failure(
                    row_id,
                    permanent=status_code in {404, 410},
                )
            except Exception:
                await self._record_failure(row_id, permanent=False)
            else:
                with self.session_factory() as db:
                    row = db.get(PushSubscription, row_id)
                    if row is not None:
                        row.failure_count = 0
                        row.last_success_at = datetime.now(timezone.utc)
                        db.commit()

    async def _record_failure(self, row_id: str, *, permanent: bool) -> None:
        with self.session_factory() as db:
            row = db.get(PushSubscription, row_id)
            if row is None:
                return
            row.failure_count += 1
            if permanent or row.failure_count >= 5:
                row.disabled_at = datetime.now(timezone.utc)
            db.commit()
