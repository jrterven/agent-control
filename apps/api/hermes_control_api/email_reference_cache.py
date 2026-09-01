from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from hermes_client import EmailReferenceCandidate, email_reference_candidates
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import EmailReferenceCache, SessionLink, utc_now
from .security import SecretVault


CACHE_TTL = timedelta(days=7)
CACHE_MAX_PER_SESSION = 512


def reference_id(
    vault: SecretVault,
    row: SessionLink,
    reference: EmailReferenceCandidate,
) -> str:
    payload = (
        b"hermes-control.email-reference-id.v1\0"
        + row.id.encode("utf-8")
        + b"\0"
        + bytes.fromhex(reference.fingerprint)
    )
    return hmac.new(vault.key, payload, hashlib.sha256).hexdigest()[:32]


def cache_aad(row: SessionLink, opaque_id: str) -> str:
    return f"email-reference-cache:{row.owner_id}:{row.id}:{opaque_id}"


def purge_expired(
    db: Session,
    *,
    now: datetime | None = None,
) -> int:
    result = db.execute(
        delete(EmailReferenceCache).where(
            EmailReferenceCache.expires_at <= (now or utc_now())
        ).execution_options(synchronize_session="fetch")
    )
    return int(result.rowcount or 0)


def cache_references(
    db: Session,
    vault: SecretVault,
    row: SessionLink,
    references: list[EmailReferenceCandidate],
    *,
    candidate_limit: int = 256,
    retain_only: bool = False,
) -> None:
    """Incrementally cache validated refs with a fixed, non-sliding TTL."""

    now = utc_now()
    purge_expired(db, now=now)
    existing = {
        cached.reference_id: cached
        for cached in db.scalars(
            select(EmailReferenceCache).where(
                EmailReferenceCache.owner_id == row.owner_id,
                EmailReferenceCache.session_link_id == row.id,
            )
        ).all()
    }
    prioritized = references[:candidate_limit]
    desired_ids = {
        reference_id(vault, row, reference) for reference in prioritized
    }
    if retain_only:
        for opaque_id, cached in tuple(existing.items()):
            if opaque_id not in desired_ids:
                db.delete(cached)
                existing.pop(opaque_id, None)
    seen: set[str] = set()
    # Callers provide priority order (newest first). Insert lower-priority
    # candidates first so the global cap evicts them before recent cards.
    for reference in reversed(prioritized):
        opaque_id = reference_id(vault, row, reference)
        if opaque_id in seen or opaque_id in existing:
            continue
        seen.add(opaque_id)
        plaintext = json.dumps(
            reference.private_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        ciphertext = vault.encrypt(plaintext, aad=cache_aad(row, opaque_id))
        if not ciphertext:
            continue
        cached = EmailReferenceCache(
            owner_id=row.owner_id,
            session_link_id=row.id,
            reference_id=opaque_id,
            payload_ciphertext=ciphertext,
            expires_at=now + CACHE_TTL,
        )
        db.add(cached)
        existing[opaque_id] = cached

    db.flush()
    overflow = list(
        db.scalars(
            select(EmailReferenceCache)
            .where(
                EmailReferenceCache.owner_id == row.owner_id,
                EmailReferenceCache.session_link_id == row.id,
            )
            .order_by(
                EmailReferenceCache.created_at.desc(),
                EmailReferenceCache.id.desc(),
            )
            .offset(CACHE_MAX_PER_SESSION)
        ).all()
    )
    for cached in overflow:
        db.delete(cached)


def cached_reference(
    db: Session,
    vault: SecretVault,
    row: SessionLink,
    opaque_id: str,
) -> EmailReferenceCandidate | None:
    cached = db.scalar(
        select(EmailReferenceCache).where(
            EmailReferenceCache.owner_id == row.owner_id,
            EmailReferenceCache.session_link_id == row.id,
            EmailReferenceCache.reference_id == opaque_id,
        )
    )
    if cached is None:
        return None
    expires_at = cached.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= utc_now():
        db.delete(cached)
        return None
    try:
        plaintext = vault.decrypt(
            cached.payload_ciphertext,
            aad=cache_aad(row, opaque_id),
        )
        decoded = json.loads(plaintext or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        db.delete(cached)
        return None
    candidates = email_reference_candidates(decoded)
    if len(candidates) != 1:
        db.delete(cached)
        return None
    reference = candidates[0]
    if not hmac.compare_digest(reference_id(vault, row, reference), opaque_id):
        db.delete(cached)
        return None
    return reference
