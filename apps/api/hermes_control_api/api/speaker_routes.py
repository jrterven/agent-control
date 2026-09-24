"""Authenticated pilot endpoints; neither voice evidence nor feedback grants access."""
from __future__ import annotations

import hashlib
import math
import time
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..auth import current_auth_session, current_user, get_db, require_csrf
from ..models import AuthSession, SessionLink, User, UserIntegration
from ..pyannote import MODEL, error, invalidate, key_row, preference, wav_duration
from ..schemas import ApiModel
from ..speaker_models import SpeakerJob, SpeakerPreference, VoiceCapture, VoicePerson

router = APIRouter(tags=["speaker recognition pilot"])
CONFIG = "/api/v1/integrations/pyannote"
ROOT = "/api/v1/speaker-recognition"


class SettingsMutation(ApiModel):
    enabled: bool
    window_seconds: Literal[5, 10, 20] = 5


class KeyMutation(ApiModel):
    api_key: SecretStr


class PersonMutation(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    consent: Literal[True]

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Invalid name")
        return value


class CaptureMutation(ApiModel):
    mode: Literal["live", "dictation", "enroll", "test"]
    session_id: str | None = Field(default=None, max_length=100)
    person_id: UUID | None = None


class FeedbackMutation(ApiModel):
    feedback: Literal["correct", "incorrect"]
    expected_person_id: UUID | None = None


class DeliveryMutation(ApiModel):
    upload_ms: int = Field(ge=0, le=180000)
    delivery_ms: int = Field(ge=0, le=180000)


def service(request):
    return request.app.state.speaker_service


def owned(db, model, identifier, owner_id):
    row = db.get(model, str(identifier))
    if row is None or row.owner_id != owner_id:
        raise error("NOT_FOUND", 404)
    return row


def configuration(db, owner_id):
    pref = preference(db, owner_id)
    ready = db.scalar(select(func.count()).select_from(VoicePerson).where(
        VoicePerson.owner_id == owner_id, VoicePerson.voiceprint_ciphertext.is_not(None)))
    tested_recognition = db.scalar(select(SpeakerJob.id).where(
        SpeakerJob.owner_id == owner_id, SpeakerJob.kind == "identify", SpeakerJob.status == "succeeded",
        SpeakerJob.generation == pref.generation).limit(1)) is not None
    result = {"provider": "pyannote", "model": MODEL, "configured": key_row(db, owner_id) is not None,
              "enabled": pref.enabled, "windowSeconds": pref.window_seconds, "generation": pref.generation,
              "connectionTested": pref.tested, "recognitionTested": tested_recognition, "readyPeople": ready}
    db.commit()
    return result


@router.get(CONFIG)
def get_configuration(owner: User = Depends(current_user), db: Session = Depends(get_db)):
    return configuration(db, owner.id)


@router.put(CONFIG)
def set_configuration(payload: SettingsMutation, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    pref = preference(db, auth.user_id)
    if payload.enabled and key_row(db, auth.user_id) is None:
        raise error("NOT_CONFIGURED")
    if (pref.enabled, pref.window_seconds) != (payload.enabled, payload.window_seconds):
        invalidate(db, pref)
    pref.enabled, pref.window_seconds = payload.enabled, payload.window_seconds
    db.commit()
    return configuration(db, auth.user_id)


@router.put(CONFIG + "/key")
def set_key(payload: KeyMutation, request: Request, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    key = payload.api_key.get_secret_value().strip()
    if not 8 <= len(key) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise error("INVALID_KEY", 422)
    pref = preference(db, auth.user_id)
    invalidate(db, pref)
    pref.tested = False
    row = key_row(db, auth.user_id)
    if row is None:
        row = UserIntegration(owner_id=auth.user_id, provider="pyannote")
        db.add(row)
    row.api_key_ciphertext = service(request).vault.encrypt(key, aad=f"user-integration:{auth.user_id}:pyannote:api-key")
    db.commit()
    return configuration(db, auth.user_id)


@router.delete(CONFIG + "/key", status_code=204)
def delete_key(auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    pref = preference(db, auth.user_id)
    invalidate(db, pref)
    pref.enabled = pref.tested = False
    row = key_row(db, auth.user_id)
    if row:
        db.delete(row)
    db.commit()
    return Response(status_code=204)


@router.post(CONFIG + "/test")
async def test_connection(request: Request, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    pref = preference(db, auth.user_id)
    generation = pref.generation
    key = service(request).key(db, auth.user_id)
    owner_id = auth.user_id
    request.app.state.transcription_token_limiter.consume(owner_id)
    db.commit()
    await service(request).client.test(key)
    db.expire_all()
    pref = preference(db, owner_id)
    if pref.generation != generation:
        raise error("CAPTURE_INVALIDATED")
    pref.tested = True
    db.commit()
    return {"ok": True, "connectionTested": True}


def person_view(person):
    return {"id": person.id, "name": person.name, "ready": bool(person.voiceprint_ciphertext),
            "provider": person.provider, "model": person.model, "consent": person.consent}


@router.get(ROOT + "/people")
def list_people(owner: User = Depends(current_user), db: Session = Depends(get_db)):
    return {"items": [person_view(p) for p in db.scalars(select(VoicePerson).where(VoicePerson.owner_id == owner.id).order_by(VoicePerson.created_at))]}


@router.put(ROOT + "/people/{person_id}")
def save_person(person_id: UUID, payload: PersonMutation, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    pref = preference(db, auth.user_id)
    # Serialize catalog changes with capture/job reservation on PostgreSQL.
    db.scalar(select(SpeakerPreference).where(SpeakerPreference.owner_id == auth.user_id).with_for_update())
    person = db.get(VoicePerson, str(person_id))
    if person is not None and person.owner_id != auth.user_id:
        raise error("NOT_FOUND", 404)
    if person is None:
        if db.scalar(select(func.count()).select_from(VoicePerson).where(VoicePerson.owner_id == auth.user_id)) >= 50:
            raise error("PEOPLE_LIMIT")
        person = VoicePerson(id=str(person_id), owner_id=auth.user_id, name=payload.name, consent=True)
        db.add(person)
    else:
        person.name = payload.name
    invalidate(db, pref)
    db.commit()
    return person_view(person)


@router.delete(ROOT + "/people/{person_id}", status_code=204)
def delete_person(person_id: UUID, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    person = owned(db, VoicePerson, person_id, auth.user_id)
    invalidate(db, preference(db, auth.user_id))
    db.delete(person)
    db.commit()
    return Response(status_code=204)


@router.put(ROOT + "/captures/{capture_id}", status_code=201)
def start_capture(capture_id: UUID, payload: CaptureMutation, request: Request,
                  auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    pref = preference(db, auth.user_id)
    db.scalar(select(SpeakerPreference).where(SpeakerPreference.owner_id == auth.user_id).with_for_update())
    if not pref.enabled or key_row(db, auth.user_id) is None:
        raise error("DISABLED")
    session_id = payload.session_id
    if session_id:
        if session_id.startswith("tmp_"):
            # Recognition has its own consented pilot history, but it must not
            # retain an identifier or transcript from a temporary conversation.
            if not request.app.state.services.temporary_chats.owned(session_id, auth.user_id, request.headers.get("X-Temporary-Chat")):
                raise error("NOT_FOUND", 404)
            session_id = None
        elif not db.scalar(select(SessionLink.id).where(SessionLink.id == session_id, SessionLink.owner_id == auth.user_id, SessionLink.archived_at.is_(None))):
            raise error("NOT_FOUND", 404)
    if payload.mode == "enroll":
        if payload.person_id is None:
            raise error("PERSON_REQUIRED", 422)
        owned(db, VoicePerson, payload.person_id, auth.user_id)
    existing = db.get(VoiceCapture, str(capture_id))
    if existing:
        if (existing.owner_id != auth.user_id or existing.auth_session_id != auth.id
            or existing.mode != payload.mode or existing.session_id != session_id
            or existing.person_id != (str(payload.person_id) if payload.person_id else None)):
            raise error("CAPTURE_CONFLICT")
        if not existing.active or existing.generation != pref.generation:
            raise error("CAPTURE_INVALIDATED")
        return {"id": existing.id, "generation": existing.generation}
    db.execute(update(VoiceCapture).where(VoiceCapture.owner_id == auth.user_id).values(active=False))
    capture = VoiceCapture(id=str(capture_id), owner_id=auth.user_id, auth_session_id=auth.id,
                           generation=pref.generation, mode=payload.mode, session_id=session_id,
                           person_id=str(payload.person_id) if payload.person_id else None)
    db.add(capture)
    db.commit()
    return {"id": capture.id, "generation": capture.generation}


@router.delete(ROOT + "/captures/{capture_id}", status_code=204)
def stop_capture(capture_id: UUID, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    capture = owned(db, VoiceCapture, capture_id, auth.user_id)
    capture.active = False
    db.commit()
    return Response(status_code=204)


@router.put(ROOT + "/captures/{capture_id}/jobs/{job_id}", status_code=202)
async def submit_job(capture_id: UUID, job_id: UUID, request: Request,
                     auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    capture = owned(db, VoiceCapture, capture_id, auth.user_id)
    if request.headers.get("content-type", "").split(";")[0] != "audio/wav":
        raise error("INVALID_AUDIO", 415)
    audio = await request.body()
    duration = wav_duration(audio)
    fingerprint = hashlib.sha256(audio).hexdigest()
    existing = db.get(SpeakerJob, str(job_id))
    if existing:
        if existing.owner_id != auth.user_id or existing.capture_id != capture.id or existing.fingerprint != fingerprint:
            raise error("JOB_CONFLICT")
        return service(request).job_view(db, existing)
    pref = preference(db, auth.user_id)
    if not pref.enabled or capture.auth_session_id != auth.id or not capture.active or pref.generation != capture.generation:
        raise error("CAPTURE_INVALIDATED")
    kind = "enroll" if capture.mode == "enroll" else "identify"
    if kind == "enroll" and not 19 <= duration <= 30:
        raise error("ENROLLMENT_TOO_SHORT", 422)
    if kind == "identify" and abs(duration - pref.window_seconds) > .1:
        raise error("INVALID_WINDOW", 422)
    if kind == "identify" and not db.scalar(select(VoicePerson.id).where(VoicePerson.owner_id == auth.user_id, VoicePerson.voiceprint_ciphertext.is_not(None)).limit(1)):
        raise error("NO_VOICES")
    now = time.time()
    reservation = db.execute(update(SpeakerPreference).where(
        SpeakerPreference.owner_id == auth.user_id, SpeakerPreference.enabled.is_(True),
        SpeakerPreference.generation == capture.generation, SpeakerPreference.busy_job_id.is_(None),
        SpeakerPreference.last_submission <= now - 10,
    ).values(busy_job_id=str(job_id), last_submission=now))
    if reservation.rowcount != 1:
        raise error("BUSY", 429)
    job = SpeakerJob(id=str(job_id), owner_id=auth.user_id, capture_id=capture.id,
                     generation=pref.generation, kind=kind, fingerprint=fingerprint, duration=duration)
    db.add(job)
    db.commit()
    view = service(request).job_view(db, job)
    service(request).launch(job.id, auth.id, audio)
    return view


@router.get(ROOT + "/jobs/{job_id}")
def get_job(job_id: UUID, request: Request, auth: AuthSession = Depends(current_auth_session), db: Session = Depends(get_db)):
    job = owned(db, SpeakerJob, job_id, auth.user_id)
    capture = db.get(VoiceCapture, job.capture_id)
    if capture.auth_session_id != auth.id:
        raise error("CAPTURE_INVALIDATED")
    return service(request).job_view(db, job)


@router.put(ROOT + "/jobs/{job_id}/feedback")
def feedback(job_id: UUID, payload: FeedbackMutation, request: Request,
             auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    job = owned(db, SpeakerJob, job_id, auth.user_id)
    if job.status != "succeeded" or job.kind != "identify":
        raise error("NO_OBSERVATION")
    if payload.expected_person_id:
        owned(db, VoicePerson, payload.expected_person_id, auth.user_id)
    job.feedback = payload.feedback
    job.expected_person_id = str(payload.expected_person_id) if payload.expected_person_id else None
    db.commit()
    return {"ok": True}


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo))


@router.put(ROOT + "/jobs/{job_id}/delivery", status_code=204)
def delivery(job_id: UUID, payload: DeliveryMutation, auth: AuthSession = Depends(require_csrf), db: Session = Depends(get_db)):
    job = owned(db, SpeakerJob, job_id, auth.user_id)
    if job.status == "succeeded":
        job.timings = {**job.timings, "browserUploadMs": payload.upload_ms, "deliveryMs": payload.delivery_ms}
        db.commit()
    return Response(status_code=204)


@router.get(ROOT + "/metrics")
def metrics(request: Request, owner: User = Depends(current_user), db: Session = Depends(get_db)):
    jobs = list(db.scalars(select(SpeakerJob).where(SpeakerJob.owner_id == owner.id).order_by(SpeakerJob.created_at.desc()).limit(10000)))
    views = [service(request).job_view(db, job, historical=True) for job in jobs]
    def summarize(items):
        labeled = [i for i in items if i["feedback"]]
        return {"jobs": len(items), "succeeded": sum(i["status"] == "succeeded" for i in items),
                "reviewed": len(labeled), "correct": sum(i["feedback"] == "correct" for i in labeled),
                "falseMatches": sum(i["feedback"] == "incorrect" and i["result"] is not None and i["result"]["state"] == "recognized" for i in items),
                "unknown": sum(i["result"] is not None and i["result"]["state"] == "unknown" for i in items),
                "inconclusive": sum(i["result"] is not None and i["result"]["state"] == "inconclusive" for i in items),
                "latencyMs": {key: {"p50": percentile([i["timings"][key] for i in items if key in i["timings"] and i["status"] == "succeeded"], .5),
                                    "p95": percentile([i["timings"][key] for i in items if key in i["timings"] and i["status"] == "succeeded"], .95)}
                              for key in ("uploadMs", "providerAndPollingMs", "pollingHttpMs", "browserUploadMs", "deliveryMs", "totalMs")}}
    identify = [v for v in views if v["kind"] == "identify"]
    return {"model": MODEL, "threshold": 70, "minimumMargin": 10, "authorizes": False,
            "sampleLimit": 10000, "summary": summarize(identify),
            "windows": {str(s): summarize([v for v in identify if abs(v["duration"] - s) < .1]) for s in (5, 10, 20)},
            "estimatedBillableSeconds": sum(j.billed_seconds for j in jobs),
            "voiceprintsCreated": sum(j.voiceprints_created for j in jobs),
            "unresolvedCharges": sum(j.submission_attempted and j.status != "succeeded"
                                     and j.error_code not in {"PYANNOTE_JOB_FAILED", "PYANNOTE_CREDENTIAL_REJECTED", "PYANNOTE_QUOTA_EXCEEDED", "PYANNOTE_RATE_LIMITED"}
                                     and not j.billed_seconds and not j.voiceprints_created for j in jobs),
            "recent": views[:50]}
