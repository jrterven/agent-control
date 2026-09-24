from __future__ import annotations

import io
import json
import struct
import wave
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from hermes_control_api.integrations import IntegrationError
from hermes_control_api.models import AuthSession, IdempotencyOperation, User, UserIntegration, utc_now
from hermes_control_api.pyannote import PyannoteClient, identification, error
from hermes_control_api.security import hash_password
from hermes_control_api.speaker_models import SpeakerJob, SpeakerPreference, VoiceCapture, VoicePerson

CONFIG = "/api/v1/integrations/pyannote"
ROOT = "/api/v1/speaker-recognition"
SECRET = "pyannote-private-test-key"
PRINT = "opaque-biometric-do-not-log"


def audio(seconds=5, rate=16000):
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate)
        wav.writeframes(struct.pack("<h", 1024) * (rate * seconds))
    return output.getvalue()


class Provider:
    def __init__(self):
        self.calls = []
        self.output = {"voiceprint": PRINT}
        self.before_poll = None
        self.submit_error = None

    async def test(self, key): self.calls.append(("test", key))
    async def media(self, key, url): self.calls.append(("media", url)); return "https://storage.googleapis.com/upload?secret=yes"
    async def upload(self, url, data): self.calls.append(("upload", len(data)))
    async def submit(self, key, kind, payload):
        self.calls.append((kind, payload))
        if self.submit_error: raise self.submit_error
        return "provider-job"
    async def poll(self, key, identifier):
        self.calls.append(("poll", identifier))
        if self.before_poll: self.before_poll()
        return {"status": "succeeded", "output": self.output}


@pytest.fixture
def pilot(authenticated, app, monkeypatch):
    client, csrf = authenticated
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "must-not-cache"}
    provider = Provider()
    app.state.speaker_service.client = provider
    scheduled = []
    monkeypatch.setattr(app.state.speaker_service, "launch", lambda *args: scheduled.append(args))
    assert client.put(CONFIG + "/key", headers=headers, json={"apiKey": SECRET}).status_code == 200
    assert client.put(CONFIG, headers=headers, json={"enabled": True}).status_code == 200
    person_id = str(uuid4())
    assert client.put(ROOT + "/people/" + person_id, headers=headers, json={"name": "Juan Ramón", "consent": True}).status_code == 200
    return client, headers, provider, scheduled, person_id


def capture(pilot, mode="enroll"):
    client, headers, _, _, person = pilot
    identifier = str(uuid4())
    response = client.put(f"{ROOT}/captures/{identifier}", headers=headers, json={"mode": mode, "personId": person if mode == "enroll" else None})
    assert response.status_code == 201, response.text
    return identifier


def submit(pilot, cap, seconds=20, identifier=None):
    client, headers, *_ = pilot
    return client.put(f"{ROOT}/captures/{cap}/jobs/{identifier or uuid4()}", headers={**headers, "Content-Type": "audio/wav"}, content=audio(seconds))


def run(pilot, app):
    client, _, _, scheduled, _ = pilot
    client.portal.call(app.state.speaker_service.run, *scheduled[-1])


def enroll(pilot, app):
    cap = capture(pilot)
    response = submit(pilot, cap)
    assert response.status_code == 202, response.text
    run(pilot, app)
    view = pilot[0].get(f"{ROOT}/jobs/{response.json()['id']}").json()
    assert view["status"] == "succeeded", view
    with app.state.session_factory() as db:
        db.execute(SpeakerPreference.__table__.update().values(last_submission=0)); db.commit()
    return view


def test_enrollment_encrypted_key_write_only_and_no_http_cache(pilot, app):
    client, headers, provider, _, person_id = pilot
    response = client.post(CONFIG + "/test", headers=headers)
    assert response.json() == {"ok": True, "connectionTested": True}
    assert provider.calls == [("test", SECRET)]
    config = client.get(CONFIG).json()
    assert config["connectionTested"] and not config["recognitionTested"]
    view = enroll(pilot, app)
    assert view["result"]["state"] == "enrolled"
    assert not view["authorizes"]
    assert provider.calls[-2][1]["model"] == "precision-3"
    with app.state.session_factory() as db:
        person = db.get(VoicePerson, person_id)
        key = db.scalar(select(UserIntegration).where(UserIntegration.provider == "pyannote"))
        assert PRINT not in person.voiceprint_ciphertext and person.voiceprint_ciphertext.startswith("v1.")
        assert SECRET not in key.api_key_ciphertext
        assert app.state.speaker_service.key(db, key.owner_id) == SECRET
        assert not list(db.scalars(select(IdempotencyOperation).where(IdempotencyOperation.scope.contains("pyannote"))))
        assert not list(db.scalars(select(IdempotencyOperation).where(IdempotencyOperation.scope.contains("speaker-recognition"))))
    for path in (CONFIG, ROOT + "/people", ROOT + "/metrics", ROOT + "/jobs/" + view["id"]):
        response = client.get(path)
        assert SECRET not in response.text and PRINT not in response.text
        assert response.headers["cache-control"] == "no-store"


def output(person, score=90, second=None):
    scores = {person: score}
    if second: scores[second] = score - 5
    return {"identification": [{"start": 0, "end": 5, "speaker": person, "diarizationSpeaker": "SPEAKER_00"}],
            "voiceprints": [{"speaker": "SPEAKER_00", "match": person, "confidence": scores}]}


def test_identification_labels_billing_feedback_and_report(pilot, app):
    client, headers, provider, _, person = pilot
    enroll(pilot, app)
    provider.output = output(person)
    cap = capture(pilot, "dictation")
    receipt = submit(pilot, cap, 5).json()
    run(pilot, app)
    view = client.get(ROOT + "/jobs/" + receipt["id"]).json()
    assert view["result"]["name"] == "Juan Ramón" and view["billedSeconds"] == 20
    payload = [call[1] for call in provider.calls if call[0] == "identify"][0]
    assert payload["voiceprints"] == [{"label": person, "voiceprint": PRINT}]
    assert payload["matching"] == {"threshold": 70, "exclusive": True}
    assert "confidence" not in payload and "Juan" not in json.dumps(payload)
    assert client.put(f"{ROOT}/jobs/{receipt['id']}/feedback", headers=headers, json={"feedback": "incorrect"}).status_code == 200
    report = client.get(ROOT + "/metrics").json()
    assert report["summary"]["falseMatches"] == 1
    assert report["windows"]["5"]["reviewed"] == 1
    assert report["estimatedBillableSeconds"] == 20 and report["voiceprintsCreated"] == 1
    assert report["summary"]["latencyMs"]["totalMs"]["p95"] is not None
    assert client.get(CONFIG).json()["recognitionTested"]


@pytest.mark.parametrize("mutation", ["key", "disable", "delete-person", "close", "logout"])
def test_results_after_revocation_are_discarded(pilot, app, mutation):
    client, headers, provider, _, person = pilot
    cap = capture(pilot)
    receipt = submit(pilot, cap).json()
    def revoke():
        # Called while the provider's successful response is in flight.
        with app.state.session_factory() as db:
            from hermes_control_api.pyannote import invalidate
            pref = db.scalar(select(SpeakerPreference))
            if mutation == "close": db.get(VoiceCapture, cap).active = False
            elif mutation == "logout": db.scalar(select(AuthSession)).revoked_at = utc_now()
            else:
                invalidate(db, pref)
                if mutation == "disable": pref.enabled = False
                if mutation == "delete-person": db.delete(db.get(VoicePerson, person))
            db.commit()
    provider.before_poll = revoke
    run(pilot, app)
    with app.state.session_factory() as db:
        job = db.get(SpeakerJob, receipt["id"])
        assert job.status == "invalidated" and job.result_ciphertext is None
        assert job.voiceprints_created == 1  # Known cost survives local invalidation.
        p = db.get(VoicePerson, person)
        assert p is None or p.voiceprint_ciphertext is None
        assert db.scalar(select(SpeakerPreference)).busy_job_id is None


def test_duplicate_window_is_deduplicated_and_no_backlog(pilot, app):
    client, _, _, scheduled, _ = pilot
    cap = capture(pilot)
    identifier = str(uuid4())
    first = submit(pilot, cap, identifier=identifier)
    assert first.status_code == 202
    assert submit(pilot, cap, identifier=identifier).status_code == 202
    assert len(scheduled) == 1
    assert submit(pilot, cap).status_code == 429
    assert submit(pilot, cap, 21, identifier).status_code == 409
    run(pilot, app)
    assert submit(pilot, cap).status_code == 429  # Minimum ten seconds, even after success.


def test_uncertain_create_is_never_retried(pilot, app):
    client, _, provider, scheduled, _ = pilot
    provider.submit_error = error("DELIVERY_UNKNOWN", 503)
    cap = capture(pilot)
    identifier = str(uuid4())
    submit(pilot, cap, identifier=identifier)
    run(pilot, app)
    assert client.get(f"{ROOT}/jobs/{identifier}").json()["status"] == "unknown"
    assert submit(pilot, cap, identifier=identifier).status_code == 202
    assert len(scheduled) == 1 and len([c for c in provider.calls if c[0] == "enroll"]) == 1


def test_csrf_validation_and_cross_user_isolation(pilot, app):
    client, headers, _, _, person = pilot
    cap = capture(pilot)
    job = submit(pilot, cap).json()["id"]
    assert client.put(CONFIG, json={"enabled": False}).status_code == 403
    assert client.put(CONFIG + "/key", headers=headers, json={"apiKey": SECRET, SECRET: SECRET}).status_code == 422
    response = client.put(CONFIG + "/key", headers=headers, json={"apiKey": []})
    assert response.status_code == 422 and SECRET not in response.text
    with app.state.session_factory() as db:
        db.add(User(username="other", password_hash=hash_password("another strong password")))
        db.commit()
    response = client.post("/api/v1/auth/login", json={"username": "other", "password": "another strong password"})
    other = {"X-CSRF-Token": response.json()["csrfToken"]}
    assert client.get(CONFIG).json()["configured"] is False
    assert client.get(ROOT + "/people").json()["items"] == []
    assert client.get(ROOT + "/metrics").json()["summary"]["jobs"] == 0
    assert client.get(ROOT + "/jobs/" + job).status_code == 404
    assert client.delete(ROOT + "/people/" + person, headers=other).status_code == 404
    assert client.delete(ROOT + "/captures/" + cap, headers=other).status_code == 404


def test_key_replacement_preserves_voiceprints_without_creating_work(pilot, app):
    client, headers, provider, _, person = pilot
    enroll(pilot, app)
    calls = len(provider.calls)
    assert client.put(CONFIG + "/key", headers=headers, json={"apiKey": "replacement-key"}).status_code == 200
    assert client.get(ROOT + "/people").json()["items"][0]["ready"]
    assert len(provider.calls) == calls
    assert not client.get(CONFIG).json()["connectionTested"]


def test_audio_validation_and_size_bound(pilot):
    client, headers, *_ = pilot
    cap = capture(pilot)
    path = f"{ROOT}/captures/{cap}/jobs/{uuid4()}"
    headers = {**headers, "Content-Type": "audio/wav"}
    assert client.put(path, headers=headers, content=audio(20, 8000)).status_code == 422
    assert client.put(path, headers=headers, content=audio(20)[:-2]).status_code == 422
    assert client.put(path, headers=headers, content=b"a" * 960129).status_code == 413
    assert client.put(path, headers=headers, content=audio(5)).status_code == 422


def test_threshold_margin_multispeaker_and_invalid_scores():
    assert identification(output("a", 69), {"a": 1}, 5)["state"] == "unknown"
    assert identification(output("a", 90, "b"), {"a": 1, "b": 1}, 5)["state"] == "inconclusive"
    mixed = output("a")
    mixed["identification"].append({"start": 2, "end": 4, "diarizationSpeaker": "SPEAKER_01"})
    result = identification(mixed, {"a": 1}, 5)
    assert result["state"] == "multiple" and result["personId"] is None and len(result["segments"]) == 2
    mixed["identification"][0]["end"] = float("nan")
    with pytest.raises(IntegrationError): identification(mixed, {"a": 1}, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(401, "CREDENTIAL_REJECTED"), (402, "QUOTA_EXCEEDED"), (429, "RATE_LIMITED"), (500, "PROVIDER_REJECTED")])
async def test_provider_failures_are_sanitized(status, code):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status, text=SECRET + PRINT))) as http:
        client = PyannoteClient(http)
        with pytest.raises(IntegrationError) as failure:
            await client.test(SECRET)
        assert failure.value.code == "PYANNOTE_" + code
        assert SECRET not in str(failure.value) and PRINT not in str(failure.value)


@pytest.mark.asyncio
async def test_upload_credentials_urls_and_bounded_response():
    calls = []
    def handle(req):
        calls.append(req)
        if req.method == "PUT": return httpx.Response(200)
        return httpx.Response(201, json={"url": "https://storage.googleapis.com/upload?X-Amz-Signature=private"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = PyannoteClient(http)
        url = await client.media(SECRET, "media://voice/test.wav")
        await client.upload(url, audio())
    assert calls[0].headers["Authorization"] == "Bearer " + SECRET
    assert "authorization" not in calls[1].headers
    for malicious in ("http://127.0.0.1/", "https://example.com/", "https://storage.googleapis.com.evil.org/", "https://user:pass@storage.googleapis.com/"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"url": malicious}))) as http:
            with pytest.raises(IntegrationError): await PyannoteClient(http).media(SECRET, "media://test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="x" * (2 * 1024 * 1024 + 1)))) as http:
        with pytest.raises(IntegrationError): await PyannoteClient(http).test(SECRET)


def test_bounded_timeout_and_restart_release_the_lease(pilot, app):
    import asyncio
    client, _, provider, _, _ = pilot
    cap = capture(pilot)
    receipt = submit(pilot, cap).json()
    async def slow(*args): await asyncio.sleep(10)
    provider.poll = slow
    app.state.speaker_service.timeout_seconds = .02
    run(pilot, app)
    assert client.get(f"{ROOT}/jobs/{receipt['id']}").json()["errorCode"] == "PYANNOTE_TIMEOUT"
    assert client.get(ROOT + "/metrics").json()["unresolvedCharges"] == 1
    with app.state.session_factory() as db:
        assert db.scalar(select(SpeakerPreference)).busy_job_id is None
        db.get(SpeakerJob, receipt["id"]).status = "running"
        db.scalar(select(SpeakerPreference)).busy_job_id = receipt["id"]
        db.commit()
    app.state.speaker_service.initialize()
    with app.state.session_factory() as db:
        assert not db.get(VoiceCapture, cap).active
        assert db.get(SpeakerJob, receipt["id"]).status == "unknown"
        assert db.scalar(select(SpeakerPreference)).busy_job_id is None


@pytest.mark.asyncio
async def test_connection_contract_and_invalid_create_receipt():
    for payload, valid in [({"status": "OK"}, True), ({"status": "success"}, True), ({"status": "failed"}, False), ({}, False)]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as http:
            if valid: await PyannoteClient(http).test(SECRET)
            else:
                with pytest.raises(IntegrationError): await PyannoteClient(http).test(SECRET)
    for response in (httpx.Response(200, json={}), httpx.Response(500, text=SECRET)):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
            with pytest.raises(IntegrationError) as failure:
                await PyannoteClient(http).submit(SECRET, "enroll", {"model": "precision-3"})
            assert failure.value.code == "PYANNOTE_DELIVERY_UNKNOWN"


def test_raw_provider_labels_never_reach_public_observations():
    payload = output("a")
    payload["identification"][0]["diarizationSpeaker"] = SECRET
    with pytest.raises(IntegrationError): identification(payload, {"a": 1}, 5)
    from hermes_control_api.main import _redact_log
    assert "signed-secret" not in _redact_log('HTTP Request: PUT https://storage.googleapis.com/file?signature=signed-secret "200 OK"')
