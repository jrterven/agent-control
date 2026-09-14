from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field, model_validator
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..auth import current_user, get_db, require_csrf
from ..models import AuthSession, LiveTranscript, SessionLink, User, utc_now
from ..schemas import ApiModel
from ..services import NotFoundError

router = APIRouter(prefix="/api/v1/sessions", tags=["live voice"])


class TranscriptFragment(ApiModel):
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=48_000)
    start: float = Field(ge=0, le=86_400_000, allow_inf_nan=False)
    end: float = Field(ge=0, le=86_400_000, allow_inf_nan=False)
    order: int = Field(ge=0, strict=True)


class TranscriptSnapshot(ApiModel):
    fragments: list[TranscriptFragment] = Field(min_length=1, max_length=20_000)

    @model_validator(mode="after")
    def bounded_ordered_fragments(self):
        if sum(len(part.text.encode("utf-16-le")) // 2 for part in self.fragments) > 48_000:
            raise ValueError("Transcript is too long")
        offset = getattr(self, "offset", 0)
        if any(part.end < part.start or part.order != offset + i for i, part in enumerate(self.fragments)):
            raise ValueError("Invalid transcript sequence")
        return self


class TranscriptAppend(TranscriptSnapshot):
    offset: int = Field(default=0, ge=0, le=20_000, strict=True)


class TranscriptView(TranscriptSnapshot):
    id: str
    created_at: datetime


class TranscriptPage(ApiModel):
    items: list[TranscriptView]
    next_cursor: str | None = None


def _owned_session(db: Session, owner: User, session_id: str) -> None:
    if db.scalar(select(SessionLink.id).where(
        SessionLink.id == session_id, SessionLink.owner_id == owner.id,
        SessionLink.archived_at.is_(None),
    )) is None:
        raise NotFoundError("The selected conversation is unavailable")


def _aad(owner_id: str, session_id: str, call_id: str) -> str:
    return f"live-transcript:{owner_id}:{session_id}:{call_id}"


def _snapshot(request: Request, row: LiveTranscript) -> TranscriptSnapshot:
    return TranscriptSnapshot.model_validate_json(request.app.state.services.vault.decrypt(
        row.payload_ciphertext, aad=_aad(row.owner_id, row.session_link_id, row.id),
    ))


@router.get("/{session_id}/live-transcripts", response_model=TranscriptPage)
def list_transcripts(
    session_id: str, request: Request, before: UUID | None = None,
    owner: User = Depends(current_user), db: Session = Depends(get_db),
) -> TranscriptPage:
    _owned_session(db, owner, session_id)
    query = select(LiveTranscript).where(LiveTranscript.owner_id == owner.id, LiveTranscript.session_link_id == session_id)
    if before:
        cursor = db.get(LiveTranscript, str(before))
        if cursor is None or cursor.owner_id != owner.id or cursor.session_link_id != session_id:
            raise NotFoundError("Transcript is unavailable")
        query = query.where(or_(LiveTranscript.created_at < cursor.created_at, and_(
            LiveTranscript.created_at == cursor.created_at, LiveTranscript.id < cursor.id,
        )))
    rows = db.scalars(query.order_by(LiveTranscript.created_at.desc(), LiveTranscript.id.desc()).limit(11)).all()
    page = rows[:10]
    return TranscriptPage(
        items=[TranscriptView(id=row.id, created_at=row.created_at.replace(tzinfo=timezone.utc), fragments=_snapshot(request, row).fragments) for row in reversed(page)],
        next_cursor=page[-1].id if len(rows) > 10 else None,
    )


@router.put("/{session_id}/live-transcripts/{call_id}", status_code=204)
def save_transcript(
    session_id: str, call_id: UUID, payload: TranscriptAppend, request: Request,
    auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db),
) -> Response:
    # This is passive history storage: no Hermes requests, model calls or task dispatch.
    # The call UUID and append-only revision make retries safe without a plaintext ledger.
    _owned_session(db, auth.user, session_id)
    identifier = str(call_id)
    aad = _aad(auth.user.id, session_id, identifier)
    if payload.offset == 0:
        initial = TranscriptSnapshot(fragments=payload.fragments)
        ciphertext = request.app.state.services.vault.encrypt(initial.model_dump_json(), aad=aad)
        insert = {"sqlite": sqlite_insert, "postgresql": postgresql_insert}[db.get_bind().dialect.name]
        db.execute(insert(LiveTranscript).values(
            id=identifier, owner_id=auth.user.id, session_link_id=session_id,
            revision=len(initial.fragments), payload_ciphertext=ciphertext,
            created_at=utc_now(), updated_at=utc_now(),
        ).on_conflict_do_nothing(index_elements=["id"]))
    row = db.get(LiveTranscript, identifier)
    if row is None:
        if payload.offset == 0:
            raise NotFoundError("Transcript is unavailable")
        raise HTTPException(409, "The beginning of this transcript has not been saved")
    if row.owner_id != auth.user.id or row.session_link_id != session_id:
        raise NotFoundError("Transcript is unavailable")
    if payload.offset > row.revision:
        raise HTTPException(409, "Transcript fragments are missing")
    previous = _snapshot(request, row)
    common = min(row.revision - payload.offset, len(payload.fragments))
    if previous.fragments[payload.offset:payload.offset + common] != payload.fragments[:common]:
        raise HTTPException(409, "Transcript fragments cannot be replaced")
    if common < len(payload.fragments):
        try:
            merged = TranscriptSnapshot(fragments=previous.fragments + payload.fragments[common:])
        except ValueError:
            raise HTTPException(422, "Transcript exceeds the call limit") from None
        ciphertext = request.app.state.services.vault.encrypt(merged.model_dump_json(), aad=aad)
        result = db.execute(update(LiveTranscript).where(
            LiveTranscript.id == identifier, LiveTranscript.revision == row.revision,
        ).values(revision=len(merged.fragments), payload_ciphertext=ciphertext, updated_at=utc_now()))
        if result.rowcount != 1:
            raise HTTPException(409, "Retry the transcript snapshot")
    db.commit()
    return Response(status_code=204)
