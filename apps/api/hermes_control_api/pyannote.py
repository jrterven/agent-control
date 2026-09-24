"""Bounded BYOK pyannote jobs. Observations never grant authority."""
from __future__ import annotations

import asyncio
import io
import json
import math
import re
import time
import wave
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .auth import aware
from .integrations import IntegrationError
from .models import AuthSession, User, UserIntegration, new_id, utc_now
from .speaker_models import SpeakerJob, SpeakerPreference, VoiceCapture, VoicePerson

MODEL = "precision-3"
ACTIVE = {"pending", "uploading", "running"}
MAX_AUDIO = 960_128


def error(code: str, status: int = 409):
    return IntegrationError(status_code=status, code=f"PYANNOTE_{code}", message=f"Speaker recognition: {code.lower().replace('_', ' ')}")


def preference(db, owner_id: str):
    row = db.get(SpeakerPreference, owner_id)
    if row is None:
        insert = postgresql_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        db.execute(insert(SpeakerPreference).values(owner_id=owner_id).on_conflict_do_nothing(index_elements=["owner_id"]))
        row = db.get(SpeakerPreference, owner_id)
    return row


def key_row(db, owner_id):
    return db.scalar(select(UserIntegration).where(UserIntegration.owner_id == owner_id, UserIntegration.provider == "pyannote"))


def invalidate(db, pref):
    pref.generation = new_id()
    db.execute(update(VoiceCapture).where(VoiceCapture.owner_id == pref.owner_id).values(active=False))
    # Keep the lease until its worker exits, so disabling/re-enabling cannot
    # overlap local uploads or job creation requests. Already accepted upstream
    # work may continue after invalidation; its charge remains uncertain.


def wav_duration(audio: bytes) -> float:
    if len(audio) > MAX_AUDIO:
        raise error("AUDIO_TOO_LARGE", 413)
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getcomptype()) != (1, 2, 16000, "NONE"):
                raise ValueError()
            frames = wav.getnframes()
            if not 16000 <= frames <= 480000 or len(wav.readframes(frames)) != frames * 2:
                raise ValueError()
            return frames / 16000
    except (wave.Error, EOFError, ValueError):
        raise error("INVALID_AUDIO", 422) from None


class PyannoteClient:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client

    async def _request(self, method, path, key=None, *, payload=None, audio=None):
        url = f"https://api.pyannote.ai/v1/{path}" if key is not None else path
        headers = {"Accept-Encoding": "identity"}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        if audio is not None:
            headers["Content-Type"] = "application/octet-stream"
        async def run(client):
            async with client.stream(method, url, headers=headers, json=payload, content=audio, follow_redirects=False) as response:
                if response.status_code in (401, 403):
                    raise error("CREDENTIAL_REJECTED", 422)
                if response.status_code == 402:
                    raise error("QUOTA_EXCEEDED", 402)
                if response.status_code == 429:
                    try:
                        delay = min(120, max(1, int(response.headers.get("Retry-After", "10"))))
                    except ValueError:
                        delay = 10
                    exc = error("RATE_LIMITED", 429)
                    exc.retry_after = delay
                    raise exc
                if not 200 <= response.status_code < 300:
                    raise error("PROVIDER_REJECTED", 422 if response.status_code < 500 else 502)
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > 2 * 1024 * 1024:
                        raise error("INVALID_RESPONSE", 502)
                if audio is not None:
                    return {}
                try:
                    result = json.loads(data)
                    if not isinstance(result, dict):
                        raise ValueError()
                    return result
                except (ValueError, TypeError):
                    raise error("INVALID_RESPONSE", 502) from None
        try:
            if self.client is not None:
                return await run(self.client)
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                return await run(client)
        except httpx.HTTPError:
            raise error("DELIVERY_UNKNOWN", 503) from None

    async def test(self, key):
        result = await self._request("GET", "test", key)
        if result.get("status") not in {"OK", "ok", "success"}:
            raise error("INVALID_RESPONSE", 502)

    async def media(self, key, media_id):
        result = await self._request("POST", "media/input", key, payload={"url": media_id})
        url = result.get("url")
        try:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            trusted = host == "storage.googleapis.com" or any(host.endswith(suffix) for suffix in (".amazonaws.com", ".r2.cloudflarestorage.com", ".pyannote.ai"))
            if not trusted or parsed.scheme != "https" or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
                raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise error("INVALID_UPLOAD_URL", 502) from None
        return url

    async def upload(self, url, audio):
        # Deliberately no provider Authorization header on the signed storage URL.
        await self._request("PUT", url, audio=audio)

    async def submit(self, key, kind, payload):
        try:
            result = await self._request("POST", "voiceprint" if kind == "enroll" else "identify", key, payload=payload)
        except IntegrationError as exc:
            if exc.status_code >= 500:
                raise error("DELIVERY_UNKNOWN", 503) from None
            raise
        job_id = result.get("jobId")
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id):
            raise error("DELIVERY_UNKNOWN", 502)
        return job_id

    async def poll(self, key, job_id):
        return await self._request("GET", f"jobs/{job_id}", key)


def identification(output, people, duration):
    """Identity matching confidence differs from deprecated diarization confidence."""
    if not isinstance(output, dict):
        raise error("INVALID_RESPONSE", 502)
    raw = output.get("identification", [])
    scores = output.get("voiceprints", [])
    if not isinstance(raw, list) or len(raw) > 500 or not isinstance(scores, list) or len(scores) > 50:
        raise error("INVALID_RESPONSE", 502)
    by_speaker = {}
    for item in scores:
        if not isinstance(item, dict) or not isinstance(item.get("speaker"), str) or not re.fullmatch(r"SPEAKER_\d{1,3}", item["speaker"]):
            raise error("INVALID_RESPONSE", 502)
        confidence = item.get("confidence", {})
        if not isinstance(confidence, dict):
            raise error("INVALID_RESPONSE", 502)
        ranked = sorted(((person, float(score)) for person, score in confidence.items()
                         if person in people and isinstance(score, (int, float)) and not isinstance(score, bool)
                         and math.isfinite(score) and 0 <= score <= 100), key=lambda x: x[1], reverse=True)
        best = ranked[0] if ranked else (None, 0)
        margin = best[1] - (ranked[1][1] if len(ranked) > 1 else 0)
        state = "unknown" if ranked and best[1] < 70 else "inconclusive"
        person = None
        if best[1] >= 70 and margin >= 10 and item.get("match") == best[0]:
            state, person = "recognized", best[0]
        by_speaker[item["speaker"]] = {"state": state, "personId": person, "score": best[1], "margin": margin}
    segments = []
    for item in raw:
        if not isinstance(item, dict):
            raise error("INVALID_RESPONSE", 502)
        start, end = item.get("start"), item.get("end")
        if not all(isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n) for n in (start, end)) or not 0 <= start < end <= duration + .25:
            raise error("INVALID_RESPONSE", 502)
        speaker = item.get("diarizationSpeaker", item.get("speaker"))
        if not isinstance(speaker, str) or not re.fullmatch(r"SPEAKER_\d{1,3}", speaker):
            raise error("INVALID_RESPONSE", 502)
        decision = by_speaker.get(speaker, {"state": "inconclusive", "personId": None, "score": None, "margin": None})
        segments.append({"start": start, "end": end, "speaker": str(speaker)[:100], **decision})
    speakers = {s["speaker"] for s in segments}
    state = "multiple" if len(speakers) > 1 else segments[0]["state"] if segments else "inconclusive"
    person = segments[0]["personId"] if state == "recognized" else None
    return {"state": state, "personId": person, "segments": segments, "authorizes": False}


class SpeakerService:
    def __init__(self, factory, vault):
        self.factory, self.vault = factory, vault
        self.client = PyannoteClient()
        self.tasks: dict[str, asyncio.Task] = {}
        self.poll_delay = 1.0
        self.timeout_seconds = 120.0

    def initialize(self):
        with self.factory() as db:
            db.execute(update(SpeakerJob).where(SpeakerJob.status.in_(ACTIVE)).values(status="unknown", error_code="PYANNOTE_PROCESS_RESTARTED"))
            db.execute(update(SpeakerPreference).values(busy_job_id=None))
            db.execute(update(VoiceCapture).values(active=False))
            db.commit()

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def key(self, db, owner_id):
        row = key_row(db, owner_id)
        if row is None:
            raise error("NOT_CONFIGURED")
        try:
            return self.vault.decrypt(row.api_key_ciphertext, aad=f"user-integration:{owner_id}:pyannote:api-key")
        except ValueError:
            raise error("SECRET_UNAVAILABLE", 503) from None

    def _valid(self, db, job, auth_id):
        if job is None:
            raise error("CAPTURE_INVALIDATED")
        pref = db.scalar(select(SpeakerPreference).where(SpeakerPreference.owner_id == job.owner_id).with_for_update().execution_options(populate_existing=True))
        capture = db.scalar(select(VoiceCapture).where(VoiceCapture.id == job.capture_id).with_for_update().execution_options(populate_existing=True))
        owner = db.get(User, job.owner_id)
        auth = db.get(AuthSession, auth_id)
        if (not owner or not owner.is_active or not auth or auth.revoked_at is not None
            or auth.user_id != job.owner_id or aware(auth.expires_at) <= utc_now()
            or not pref or not pref.enabled or pref.generation != job.generation
            or not capture or capture.auth_session_id != auth_id or not capture.active or capture.generation != pref.generation):
            raise error("CAPTURE_INVALIDATED")
        return pref, capture

    def checkpoint(self, job_id, auth_id):
        with self.factory() as db:
            job = db.get(SpeakerJob, job_id)
            self._valid(db, job, auth_id)

    def launch(self, job_id, auth_id, audio):
        task = asyncio.create_task(self.run(job_id, auth_id, audio), name="speaker-recognition")
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    async def run(self, job_id, auth_id, audio):
        start = time.monotonic()
        timings = {}
        outcome, code = "failed", None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                with self.factory() as db:
                    job = db.get(SpeakerJob, job_id)
                    _, capture = self._valid(db, job, auth_id)
                    key = self.key(db, job.owner_id)
                    people = {p.id: p for p in db.scalars(select(VoicePerson).where(VoicePerson.owner_id == job.owner_id, VoicePerson.provider == "pyannote", VoicePerson.model == MODEL, VoicePerson.voiceprint_ciphertext.is_not(None)))}
                    payload = {"url": f"media://voice/{job.id}.wav", "model": MODEL}
                    person_id, kind, duration, owner_id = capture.person_id, job.kind, job.duration, job.owner_id
                    if kind == "identify":
                        if not people:
                            raise error("NO_VOICES")
                        payload.update(voiceprints=[{"label": p.id, "voiceprint": self.vault.decrypt(p.voiceprint_ciphertext, aad=f"voiceprint:{owner_id}:{p.id}:{p.model}")} for p in people.values()], matching={"threshold": 70, "exclusive": True})
                    job.status = "uploading"
                    db.commit()
                url = await self.client.media(key, payload["url"])
                self.checkpoint(job_id, auth_id)
                await self.client.upload(url, audio)
                audio = b""
                timings["uploadMs"] = round((time.monotonic() - start) * 1000)
                self.checkpoint(job_id, auth_id)
                with self.factory() as db:
                    job = db.get(SpeakerJob, job_id)
                    self._valid(db, job, auth_id)
                    job.submission_attempted = True
                    db.commit()
                provider_id = await self.client.submit(key, kind, payload)
                # Persist accepted ID even if cancellation raced its response;
                # this record must never create the same upstream job again.
                with self.factory() as db:
                    job = db.get(SpeakerJob, job_id)
                    job.provider_job_id = provider_id
                    job.status = "running"
                    db.commit()
                polling_start = time.monotonic()
                poll_ms = 0
                delay = self.poll_delay
                while True:
                    self.checkpoint(job_id, auth_id)
                    try:
                        poll_start = time.monotonic()
                        result = await self.client.poll(key, provider_id)
                        poll_ms += round((time.monotonic() - poll_start) * 1000)
                    except IntegrationError as exc:
                        if exc.code != "PYANNOTE_RATE_LIMITED":
                            raise
                        await asyncio.sleep(exc.retry_after or 10)
                        continue
                    state = result.get("status")
                    if state == "succeeded":
                        break
                    if state in {"failed", "canceled"}:
                        raise error("JOB_FAILED", 502)
                    if state not in {"pending", "created", "running"}:
                        raise error("INVALID_RESPONSE", 502)
                    await asyncio.sleep(delay)
                    delay = min(5, delay * 1.5)
                timings["providerAndPollingMs"] = round((time.monotonic() - polling_start) * 1000)
                timings["pollingHttpMs"] = poll_ms
                with self.factory() as db:
                    job = db.get(SpeakerJob, job_id)
                    job.billed_seconds = max(20, duration) if kind == "identify" else 0
                    job.voiceprints_created = int(kind == "enroll")
                    db.commit()
                    self._valid(db, job, auth_id)
                    output = result.get("output", {})
                    if kind == "enroll":
                        voiceprint = output.get("voiceprint") if isinstance(output, dict) else None
                        if not isinstance(voiceprint, str) or not 1 <= len(voiceprint) <= 262144:
                            raise error("INVALID_RESPONSE", 502)
                        person = db.get(VoicePerson, person_id)
                        if not person or person.owner_id != owner_id:
                            raise error("CAPTURE_INVALIDATED")
                        person.voiceprint_ciphertext = self.vault.encrypt(voiceprint, aad=f"voiceprint:{owner_id}:{person.id}:{MODEL}")
                        decision = {"state": "enrolled", "personId": person.id, "segments": [], "authorizes": False}
                    else:
                        decision = identification(output, people, duration)
                    job.result_ciphertext = self.vault.encrypt(json.dumps(decision), aad=f"speaker-result:{owner_id}:{job.id}")
                    db.commit()
                outcome = "succeeded"
        except asyncio.CancelledError:
            outcome, code = "unknown", "PYANNOTE_INTERRUPTED"
        except TimeoutError:
            outcome, code = "unknown", "PYANNOTE_TIMEOUT"
        except IntegrationError as exc:
            code = exc.code
            outcome = "invalidated" if code == "PYANNOTE_CAPTURE_INVALIDATED" else "unknown" if code == "PYANNOTE_DELIVERY_UNKNOWN" else "failed"
        except Exception:
            code = "PYANNOTE_PROCESSING_FAILED"
        finally:
            audio = b""
            timings["totalMs"] = round((time.monotonic() - start) * 1000)
            with self.factory() as db:
                job = db.get(SpeakerJob, job_id)
                if job:
                    job.status, job.error_code, job.timings = outcome, code, timings
                    db.execute(update(SpeakerPreference).where(SpeakerPreference.owner_id == job.owner_id, SpeakerPreference.busy_job_id == job.id).values(busy_job_id=None))
                    db.commit()

    def job_view(self, db, job, *, historical=False):
        result = None
        pref = db.get(SpeakerPreference, job.owner_id)
        capture = db.get(VoiceCapture, job.capture_id)
        valid = pref and pref.enabled and pref.generation == job.generation and capture and capture.active
        if job.result_ciphertext and (valid or historical):
            try:
                result = json.loads(self.vault.decrypt(job.result_ciphertext, aad=f"speaker-result:{job.owner_id}:{job.id}"))
                for part in [result, *result.get("segments", [])]:
                    person = db.get(VoicePerson, part.get("personId")) if part.get("personId") else None
                    part["name"] = person.name if person and person.owner_id == job.owner_id else None
                    if part.get("personId") and not part["name"]:
                        part.update(state="inconclusive", personId=None)
            except (ValueError, TypeError):
                result = None
        return {"id": job.id, "captureId": job.capture_id, "kind": job.kind,
                "status": job.status if valid or historical else "invalidated", "result": result,
                "duration": job.duration, "timings": job.timings, "billedSeconds": job.billed_seconds,
                "errorCode": job.error_code, "feedback": job.feedback, "expectedPersonId": job.expected_person_id,
                "createdAt": aware(job.created_at).isoformat(), "authorizes": False}
