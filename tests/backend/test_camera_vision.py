from __future__ import annotations

import asyncio
import base64
import io
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, update

from hermes_control_api.connector_models import Connector
from hermes_control_api.integrations import IntegrationError
from hermes_control_api.models import AuditEvent, Gateway, IdempotencyOperation, ProfileRef, SessionLink, User, VisionObservationRecord, VisionPreference, VisionRequestReceipt, utc_now
from hermes_control_api.openai_live import OpenAIIntegrationService
from hermes_control_api.vision import OpenAIVisionClient, VisionService, vision_context
from hermes_control_api.vision_schemas import VisionAnalysisRequest, VisionFinding, VisionIntentResult


def jpeg(width=80, height=60, fmt="JPEG"):
    data = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(data, format=fmt)
    return "data:image/jpeg;base64," + base64.b64encode(data.getvalue()).decode()


def analysis_body(**overrides):
    return {"requestId": str(uuid4()), "activationId": str(uuid4()), "mode": "on_demand",
            "capturedAt": utc_now().isoformat(), "image": jpeg(), "question": "¿Qué ves?", **overrides}


class FakeVisionClient:
    def __init__(self):
        self.calls = []
        self.finding = VisionFinding(summary="Hay una taza roja privada sobre la mesa.", meaningful_change=True, scene_reset=False, uncertainties=[])
        self.failure = None

    async def infer(self, **kwargs):
        self.calls.append(kwargs)
        if self.failure:
            raise self.failure
        if kwargs["result_type"] is VisionIntentResult:
            return VisionIntentResult(intent="visual", question="Describe el objeto delante de la cámara")
        return self.finding.model_copy()


@pytest.fixture
def vision_setup(authenticated, app):
    client, csrf = authenticated
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        profile = db.scalar(select(ProfileRef).where(ProfileRef.profile_name == "default"))
        session = SessionLink(owner_id=owner.id, gateway_id=profile.gateway_id, profile_name=profile.profile_name, stored_session_id="camera-test")
        db.add(session)
        OpenAIIntegrationService(app.state.services.vault).set_api_key(db, owner, "sk-vision-test-private-key")
        db.commit()
        owner_id, session_id = owner.id, session.id
    fake = FakeVisionClient()
    app.state.vision_service.client = fake
    return client, {"X-CSRF-Token": csrf, "Idempotency-Key": "must-not-enter-generic-ledger"}, owner_id, session_id, fake


def allow_next(app, owner_id):
    with app.state.session_factory() as db:
        db.execute(update(VisionPreference).where(VisionPreference.owner_id == owner_id).values(last_analysis_at=None, last_intent_at=None))
        db.commit()


def test_preferences_validate_owner_defaults_and_csrf(authenticated, app):
    client, csrf = authenticated
    path = "/api/v1/vision/preferences"
    assert client.get(path).json() == {"modelId": "gpt-5.6-luna", "intervalSeconds": 5, "configured": False}
    payload = {"modelId": "gpt-5.6-terra", "intervalSeconds": 10}
    assert client.put(path, json=payload).status_code == 403
    assert client.put(path, json=payload, headers={"X-CSRF-Token": csrf}).json() == {**payload, "configured": False}
    for values in ({"modelId": "unapproved-model", "intervalSeconds": 5}, {"modelId": "gpt-5.6-sol", "intervalSeconds": 1}):
        assert client.put(path, json=values, headers={"X-CSRF-Token": csrf}).status_code == 422


def test_analysis_is_text_only_encrypted_retry_safe_and_passive(vision_setup, app):
    client, headers, owner_id, session_id, fake = vision_setup
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    payload = analysis_body()
    assert client.post(path, json=payload).status_code == 403
    first = client.post(path, json=payload, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["published"] is True
    observation = first.json()["observation"]
    assert observation["modelId"] == "gpt-5.6-luna"
    assert observation["activationId"] == payload["activationId"]
    assert observation["sceneReset"] is True
    # UUID replay happens before rate limiting or time freshness, and never calls provider again.
    second = client.post(path, json=payload, headers=headers)
    assert second.json() == first.json()
    assert len(fake.calls) == 1
    assert client.post(path, json=analysis_body(), headers=headers).status_code == 429
    page = client.get(f"/api/v1/sessions/{session_id}/vision/observations")
    assert page.json() == {"items": [observation], "nextCursor": None}
    assert first.headers["cache-control"] == "no-store"
    assert "camera=(self)" in first.headers["permissions-policy"]
    with app.state.session_factory() as db:
        row = db.scalar(select(VisionObservationRecord))
        receipt = db.scalar(select(VisionRequestReceipt))
        assert row.payload_ciphertext.startswith("v1.")
        assert receipt.result_ciphertext.startswith("v1.")
        assert receipt.state == "completed"
        assert "taza roja privada" not in row.payload_ciphertext
        for model in (VisionObservationRecord, VisionRequestReceipt, IdempotencyOperation, AuditEvent):
            assert payload["image"] not in repr([item.__dict__ for item in db.scalars(select(model))])
        assert db.scalar(select(IdempotencyOperation).where(IdempotencyOperation.idempotency_key == headers["Idempotency-Key"])) is None
        with pytest.raises(ValueError):
            app.state.services.vault.decrypt(row.payload_ciphertext, aad=f"vision-observation:foreign:{session_id}:{row.id}")
        context = vision_context(db, app.state.services.vault, owner_id, session_id)
        assert "untrusted passive reference" in context and "taza roja" in context
        assert "capturedAt" in context
        assert vision_context(db, app.state.services.vault, "foreign", session_id) == ""
        assert db.get(SessionLink, session_id).active_turn_id is None
        db.delete(db.get(SessionLink, session_id))
        db.commit()
        assert db.scalar(select(VisionObservationRecord)) is None
        assert db.scalar(select(VisionRequestReceipt)) is None


@pytest.mark.parametrize("change", [
    {"image": "https://private.example/image.jpg"}, {"image": "data:image/jpeg;base64,!bad!"},
    {"image": jpeg(fmt="PNG")}, {"image": jpeg(1281, 1)},
    {"image": "data:image/jpeg;base64," + base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()},
    {"previousImage": jpeg(1, 1281)}, {"capturedAt": "2020-01-01T00:00:00Z"},
    {"capturedAt": "2020-01-01T00:00:00"}, {"mode": "hidden"}, {"extra": "forbidden"},
])
def test_invalid_camera_payload_never_calls_model(vision_setup, change):
    client, headers, _, session_id, fake = vision_setup
    response = client.post(f"/api/v1/sessions/{session_id}/vision/analyses", headers=headers, json=analysis_body(**change))
    assert response.status_code in (413, 422), response.text
    assert len(fake.calls) == 0
    assert "base64" not in response.text


def test_body_limit_and_two_images(vision_setup):
    client, headers, _, session_id, fake = vision_setup
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    response = client.post(path, headers=headers, json=analysis_body(image="x" * (3 * 1024 * 1024)))
    assert response.status_code == 413
    response = client.post(path, headers=headers, json=analysis_body(previousImage=jpeg()))
    assert response.status_code == 200, response.text
    image_parts = [item for item in fake.calls[0]["content"] if item["type"] == "input_image"]
    assert len(image_parts) == 2
    assert image_parts[0]["image_url"] == jpeg()
    assert image_parts[1]["image_url"] == jpeg()


def test_validation_never_echoes_images_in_values_or_unknown_keys(vision_setup, app, caplog):
    client, headers, _, session_id, fake = vision_setup
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    frame = jpeg()
    for payload in (analysis_body(privateExtra={"image": frame, "secret": "camera-private-text"}),
                    {**analysis_body(), frame: "camera-private-text"}):
        response = client.post(path, json=payload, headers=headers)
        assert response.status_code == 422
        assert response.json()["fields"] == []
        assert frame not in response.text and "camera-private-text" not in response.text
    assert frame not in caplog.text and "camera-private-text" not in caplog.text
    assert len(fake.calls) == 0
    with app.state.session_factory() as db:
        assert db.scalar(select(IdempotencyOperation).where(IdempotencyOperation.idempotency_key == headers["Idempotency-Key"])) is None


def test_cooldown_starts_after_provider_completion(vision_setup, app, monkeypatch):
    import hermes_control_api.vision as module
    client, headers, owner_id, session_id, fake = vision_setup
    now = utc_now()
    clock = [now]
    monkeypatch.setattr(module, "utc_now", lambda: clock[0])
    original = fake.infer

    async def slow_infer(**kwargs):
        clock[0] += timedelta(seconds=20)
        return await original(**kwargs)

    fake.infer = slow_infer
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    assert client.post(path, json=analysis_body(mode="continuous"), headers=headers).status_code == 200
    clock[0] += timedelta(seconds=3)
    assert client.post(path, json=analysis_body(mode="continuous"), headers=headers).status_code == 429
    assert len(fake.calls) == 1


def test_missing_key_never_claims_or_dispatches(vision_setup, app):
    client, headers, owner_id, session_id, fake = vision_setup
    with app.state.session_factory() as db:
        OpenAIIntegrationService(app.state.services.vault).delete_api_key(db, db.get(User, owner_id))
        db.commit()
    assert client.get("/api/v1/vision/preferences").json()["configured"] is False
    response = client.post(f"/api/v1/sessions/{session_id}/vision/analyses", json=analysis_body(), headers=headers)
    assert response.status_code == 409
    assert len(fake.calls) == 0
    with app.state.session_factory() as db:
        assert db.scalar(select(VisionRequestReceipt)) is None


def test_intent_uses_selected_model_without_images_or_history_writes(vision_setup, app):
    client, headers, owner_id, session_id, fake = vision_setup
    client.put("/api/v1/vision/preferences", json={"modelId": "gpt-5.6-sol", "intervalSeconds": 2}, headers=headers)
    payload = {"requestId": str(uuid4()), "text": "¿Qué tengo enfrente?", "recentContext": "El usuario abrió la cámara"}
    path = f"/api/v1/sessions/{session_id}/vision/intent"
    first = client.post(path, json=payload, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["intent"] == "visual"
    assert client.post(path, json=payload, headers=headers).json() == first.json()
    assert len(fake.calls) == 1
    assert fake.calls[0]["model_id"] == "gpt-5.6-sol"
    assert all(item["type"] == "input_text" for item in fake.calls[0]["content"])
    # Classification must not prevent the subsequent image analysis.
    assert client.post(f"/api/v1/sessions/{session_id}/vision/analyses", json=analysis_body(), headers=headers).status_code == 200
    with app.state.session_factory() as db:
        assert len(db.scalars(select(VisionObservationRecord)).all()) == 1


def test_failed_and_in_progress_receipts_never_repeat_provider(vision_setup, app):
    client, headers, owner_id, session_id, fake = vision_setup
    fake.failure = IntegrationError(status_code=504, code="VISION_PROVIDER_TIMEOUT", message="Timed out")
    payload = analysis_body()
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    assert client.post(path, json=payload, headers=headers).status_code == 504
    assert client.post(path, json=payload, headers=headers).status_code == 409
    assert len(fake.calls) == 1
    with app.state.session_factory() as db:
        receipt = db.scalar(select(VisionRequestReceipt))
        assert receipt.state == "failed" and receipt.result_ciphertext is None
        receipt.state = "in_progress"
        db.commit()
    assert client.post(path, json=payload, headers=headers).status_code == 409
    assert len(fake.calls) == 1


def test_expired_replay_keeps_tombstone_and_context_is_recent(vision_setup, app):
    client, headers, owner_id, session_id, fake = vision_setup
    payload = analysis_body()
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    assert client.post(path, json=payload, headers=headers).status_code == 200
    with app.state.session_factory() as db:
        db.scalar(select(VisionRequestReceipt)).result_expires_at = utc_now() - timedelta(seconds=1)
        db.scalar(select(VisionObservationRecord)).created_at = utc_now() - timedelta(minutes=6)
        db.commit()
        assert vision_context(db, app.state.services.vault, owner_id, session_id) == ""
    assert client.post(path, json=payload, headers=headers).status_code == 409
    allow_next(app, owner_id)
    assert client.post(path, json=analysis_body(), headers=headers).status_code == 200
    with app.state.session_factory() as db:
        expired = db.get(VisionRequestReceipt, (payload["requestId"], owner_id, session_id, "analysis"))
        assert expired.state == "expired" and expired.result_ciphertext is None
    assert len(fake.calls) == 2


@pytest.mark.parametrize("denial", ["foreign", "archived", "disabled_gateway", "revoked_connector", "disabled_profile", "missing_profile"])
def test_session_access_fails_before_provider(vision_setup, app, denial):
    client, headers, owner_id, session_id, fake = vision_setup
    with app.state.session_factory() as db:
        session = db.get(SessionLink, session_id)
        profile = db.scalar(select(ProfileRef).where(ProfileRef.gateway_id == session.gateway_id, ProfileRef.profile_name == session.profile_name))
        if denial == "foreign":
            other = User(username="foreign-camera", password_hash="unused")
            db.add(other)
            db.flush()
            session.owner_id = other.id
        elif denial == "archived":
            session.archived_at = utc_now()
        elif denial == "disabled_gateway":
            db.get(Gateway, session.gateway_id).enabled = False
        elif denial == "revoked_connector":
            db.add(Connector(owner_id=owner_id, gateway_id=session.gateway_id, name="revoked", token_hash="revoked-camera-token", profiles=[], revoked_at=utc_now()))
        elif denial == "disabled_profile":
            profile.status = "disabled"
        else:
            db.delete(profile)
        db.commit()
    for suffix, data in (("analyses", analysis_body()), ("intent", {"requestId": str(uuid4()), "text": "Mira"})):
        assert client.post(f"/api/v1/sessions/{session_id}/vision/{suffix}", json=data, headers=headers).status_code == 404
    assert client.get(f"/api/v1/sessions/{session_id}/vision/observations").status_code == 404
    assert len(fake.calls) == 0


def test_continuous_intervals_changes_cooldown_and_exact_capture(vision_setup, app):
    client, headers, owner_id, session_id, fake = vision_setup
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    activation = str(uuid4())
    first = client.post(path, json=analysis_body(mode="continuous", activationId=activation), headers=headers)
    assert first.json()["published"] is True
    # Continuous honors user's five-second cadence, not just on-demand two seconds.
    with app.state.session_factory() as db:
        db.get(VisionPreference, owner_id).last_analysis_at = utc_now() - timedelta(seconds=3)
        db.commit()
    assert client.post(path, json=analysis_body(mode="continuous", activationId=activation), headers=headers).status_code == 429
    fake.finding.meaningful_change = False
    fake.finding.scene_reset = False
    allow_next(app, owner_id)
    stable = client.post(path, json=analysis_body(mode="continuous", activationId=activation, previousImage=jpeg()), headers=headers)
    assert stable.json()["published"] is False
    with app.state.session_factory() as db:
        assert len(db.scalars(select(VisionObservationRecord)).all()) == 1
    fake.finding.meaningful_change = True
    fake.finding.summary = "Una segunda taza apareció."
    allow_next(app, owner_id)
    pending = client.post(path, json=analysis_body(mode="continuous", activationId=activation, previousImage=jpeg()), headers=headers)
    assert pending.json()["published"] is False
    assert len(client.get(f"/api/v1/sessions/{session_id}/vision/observations").json()["items"]) == 1
    # The model compares to last published, and confirms the pending change still exists.
    with app.state.session_factory() as db:
        db.execute(update(VisionObservationRecord).where(VisionObservationRecord.published_at.is_not(None)).values(published_at=utc_now() - timedelta(seconds=16)))
        db.commit()
    fake.finding.meaningful_change = True
    fake.finding.summary = "Ahora siguen dos tazas visibles."
    allow_next(app, owner_id)
    current = analysis_body(mode="continuous", activationId=activation, previousImage=jpeg())
    published = client.post(path, json=current, headers=headers).json()
    assert published["published"] is True
    assert published["observation"]["summary"] == fake.finding.summary
    assert published["observation"]["id"] != pending.json()["observation"]["id"]
    assert published["observation"]["capturedAt"].replace("Z", "+00:00") == current["capturedAt"]
    prompt = json.loads(fake.calls[-1]["content"][0]["text"])
    assert {item["published"] for item in prompt["priorObservations"]} == {True, False}
    assert "CURRENT frame" in fake.calls[-1]["instructions"]
    # Explicit user questions publish immediately within the same activation.
    allow_next(app, owner_id)
    assert client.post(path, json=analysis_body(activationId=activation), headers=headers).json()["published"] is True


def test_pending_changes_replace_and_reverted_scene_is_never_published(vision_setup, app):
    client, headers, owner_id, session_id, fake = vision_setup
    path = f"/api/v1/sessions/{session_id}/vision/analyses"
    activation = str(uuid4())
    assert client.post(path, json=analysis_body(mode="continuous", activationId=activation), headers=headers).json()["published"]
    for text in ("Una segunda taza", "Una tercera taza"):
        allow_next(app, owner_id)
        fake.finding.summary = text
        fake.finding.scene_reset = False
        assert not client.post(path, json=analysis_body(mode="continuous", activationId=activation, previousImage=jpeg()), headers=headers).json()["published"]
    with app.state.session_factory() as db:
        rows = db.scalars(select(VisionObservationRecord).where(VisionObservationRecord.published_at.is_(None))).all()
        assert len(rows) == 1
        assert "tercera" in vision_context(db, app.state.services.vault, owner_id, session_id)
        db.execute(update(VisionObservationRecord).where(VisionObservationRecord.published_at.is_not(None)).values(published_at=utc_now() - timedelta(seconds=16)))
        db.commit()
    # The current frame has reverted to what the user already saw. No stale announcement.
    fake.finding.meaningful_change = False
    fake.finding.summary = "La escena inicial vuelve a estar visible."
    allow_next(app, owner_id)
    assert not client.post(path, json=analysis_body(mode="continuous", activationId=activation, previousImage=jpeg()), headers=headers).json()["published"]
    with app.state.session_factory() as db:
        assert db.scalar(select(VisionObservationRecord).where(VisionObservationRecord.published_at.is_(None))) is None
    assert len(client.get(f"/api/v1/sessions/{session_id}/vision/observations").json()["items"]) == 1


@pytest.mark.asyncio
async def test_owner_lease_excludes_second_service_and_rechecks_access(vision_setup, app):
    _, _, owner_id, session_id, _ = vision_setup
    entered, release = asyncio.Event(), asyncio.Event()

    class PausedClient:
        async def infer(self, **kwargs):
            entered.set()
            await release.wait()
            return VisionFinding(summary="Una mesa", meaningful_change=True, scene_reset=True, uncertainties=[])

    service = VisionService(app.state.services.vault, PausedClient())
    with app.state.session_factory() as first_db, app.state.session_factory() as second_db:
        owner = first_db.get(User, owner_id)
        task = asyncio.create_task(service.analyze(first_db, owner, session_id, VisionAnalysisRequest.model_validate(analysis_body())))
        await entered.wait()
        second_service = VisionService(app.state.services.vault, PausedClient())
        with pytest.raises(IntegrationError) as failure:
            await second_service.analyze(second_db, second_db.get(User, owner_id), session_id, VisionAnalysisRequest.model_validate(analysis_body()))
        assert failure.value.code == "VISION_BUSY"
        second_db.get(SessionLink, session_id).archived_at = utc_now()
        second_db.commit()
        release.set()
        from hermes_control_api.services import NotFoundError
        with pytest.raises(NotFoundError):
            await task
        assert first_db.scalar(select(VisionObservationRecord)) is None
        assert first_db.scalar(select(VisionRequestReceipt)).state == "failed"


@pytest.mark.asyncio
async def test_openai_wire_fixed_endpoint_images_low_reasoning_schema_and_no_storage():
    captured = []

    def handler(request):
        captured.append(request)
        finding = {"summary": "Una mesa.", "meaningfulChange": True, "sceneReset": True, "uncertainties": []}
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(finding)}]}]})

    client = OpenAIVisionClient(httpx.MockTransport(handler))
    result = await client.infer(api_key="sk-test", model_id="gpt-5.6-luna", instructions="Inspect safely", content=[{"type": "input_image", "image_url": jpeg()}], result_type=VisionFinding)
    assert result.summary == "Una mesa."
    request = captured[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_output_tokens"] == 600
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"]["additionalProperties"] is False
    assert "tools" not in body and "previous_response_id" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["redirect", "unauthorized", "model_not_found", "quota", "oversize", "incomplete", "refusal", "malformed", "wrong_fields", "timeout"])
async def test_openai_failures_are_bounded_sanitized_and_never_retried(case):
    calls = []

    def handler(request):
        calls.append(request)
        if case == "timeout":
            raise httpx.ReadTimeout("private request text", request=request)
        if case == "redirect":
            return httpx.Response(302, headers={"location": "https://evil.example"})
        if case == "unauthorized":
            return httpx.Response(401, text="private request text")
        if case == "model_not_found":
            return httpx.Response(404, json={"error": {"code": "model_not_found", "message": "private request text"}})
        if case == "quota":
            return httpx.Response(429, text="private request text")
        if case == "oversize":
            return httpx.Response(200, content=b"x" * (32 * 1024 + 1))
        if case == "incomplete":
            return httpx.Response(200, json={"status": "incomplete"})
        if case == "refusal":
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "private request text"}]}]})
        if case == "wrong_fields":
            return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]})
        return httpx.Response(200, text="private request text")

    client = OpenAIVisionClient(httpx.MockTransport(handler))
    with pytest.raises(IntegrationError) as error:
        await client.infer(api_key="sk-test", model_id="gpt-5.6-luna", instructions="Inspect", content=[], result_type=VisionFinding)
    assert len(calls) == 1
    assert "private request text" not in str(error.value)
    assert error.value.retryable is False
    if case == "model_not_found":
        assert error.value.code == "VISION_PROVIDER_ACCESS"
