from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import numpy as np
import pytest
from sqlalchemy import delete, select

from hermes_control_api.models import (LiveTranscript, SemanticFragment, SemanticIndexState,
    SemanticPreference, SessionLink, User, utc_now)
from hermes_control_api.openai_live import OpenAIIntegrationService
from hermes_control_api.semantic_search import (DIMENSIONS, EmbeddingsClient, SemanticSearch,
    chunks, encoding, failure, public_text)

from .conftest import mutation_headers
from .test_api_sessions import create_session

SUPERVISOR_RUN = SemanticSearch.run


@pytest.mark.asyncio
async def test_supervisor_stays_dormant_before_opt_in(app):
    service = app.state.semantic_search
    service.tick = AsyncMock()
    task = asyncio.create_task(SUPERVISOR_RUN(service))
    try:
        await asyncio.sleep(0.01)
        service.tick.assert_not_called()
        service.activated = True
        await asyncio.sleep(2.05)
        service.tick.assert_awaited_once()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_persisted_activation_wakes_restarted_supervisor(semantic, app):
    _, _, service, _ = semantic
    restarted = SemanticSearch(app.state.services)
    assert not restarted.activated
    restarted.initialize()
    assert service.activated and restarted.activated


@pytest.fixture(autouse=True)
def controlled_worker(monkeypatch):
    async def idle(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(SemanticSearch, "run", idle)


class FakeEmbeddings:
    def __init__(self):
        self.inputs = []

    async def embed(self, key, texts):
        self.inputs.extend(texts)
        result = []
        for text in texts:
            vector = np.zeros(DIMENSIONS, dtype=np.float32)
            text = text.lower()
            index = 0 if any(word in text for word in ("bicicleta", "pedalear", "bicycle")) else 1 if "océano" in text else 2
            vector[index] = 1
            result.append(vector)
        return result


@pytest.fixture
def semantic(authenticated, app):
    client, csrf = authenticated
    service = app.state.semantic_search
    service.client = FakeEmbeddings()
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        owner_id = owner.id
        OpenAIIntegrationService(service.vault).set_api_key(db, owner, "sk-unit-test-semantic-key")
        db.commit()
    response = client.put("/api/v1/search/semantic/settings", json={"enabled": True}, headers=mutation_headers(csrf))
    assert response.status_code == 200, response.text
    return client, csrf, service, owner_id


def add_history(client, app, session_id, rows):
    async def insert():
        from hermes_control_api.services import GatewayService
        with app.state.session_factory() as db:
            row = db.get(SessionLink, session_id)
            provider = await app.state.services.provider_pool.get(await GatewayService(app.state.services).connection(db, row.gateway_id, row.profile_name))
            provider._messages[row.stored_session_id].extend(rows)
            row.last_sequence += 1
            db.commit()
    client.portal.call(insert)


def drain(client, service, owner_id, maximum=150):
    for _ in range(maximum):
        client.portal.call(service.tick)
        status = service.status(owner_id)
        if status["pending"] == 0:
            return
        assert not status["failed"], status
    pytest.fail(f"Index did not finish: {service.status(owner_id)}")


def test_semantic_concepts_live_encryption_cache_and_lexical_compatibility(semantic, app):
    client, csrf, service, owner = semantic
    session = create_session(client, csrf, "control-dev", "semantic-source")
    add_history(client, app, session["id"], [{"role": "user", "content": "Mi bicicleta es azul."},
        {"role": "assistant", "content": "Puedes recorrer el camino."}])
    with app.state.session_factory() as db:
        identifier = str(uuid4())
        payload = {"fragments": [{"role": "user", "text": "Exploramos el océano.", "start": 0, "end": 1, "order": 0}]}
        db.add(LiveTranscript(id=identifier, owner_id=owner, session_link_id=session["id"], revision=1,
            payload_ciphertext=service.vault.encrypt(json.dumps(payload), aad=f"live-transcript:{owner}:{session['id']}:{identifier}")))
        db.commit()
    drain(client, service, owner)
    lexical = client.get("/api/v1/search", params={"q": "pedalear"}).json()
    assert lexical["items"] == []
    before = len(service.client.inputs)
    response = client.get("/api/v1/search", params={"q": "pedalear", "mode": "semantic"})
    assert response.status_code == 200, response.text
    assert response.json()["partial"] is False
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["targetId"] == session["id"]
    assert "bicicleta" in response.json()["items"][0]["excerpt"]
    client.get("/api/v1/search", params={"q": "pedalear", "mode": "semantic"})
    assert len(service.client.inputs) == before + 1
    voice = client.get("/api/v1/search", params={"q": "océano", "mode": "semantic"}).json()
    assert voice["items"][0]["source"] == "live"
    with app.state.session_factory() as db:
        rows = list(db.scalars(select(SemanticFragment)))
        assert rows and all(row.payload_ciphertext.startswith("v1.") for row in rows)
        assert all("bicicleta" not in row.payload_ciphertext and "océano" not in row.payload_ciphertext for row in rows)


def test_paginated_backfill_includes_messages_beyond_5000_and_survives_restart(semantic, app):
    client, csrf, service, owner = semantic
    session = create_session(client, csrf, "control-dev", "long-source")
    add_history(client, app, session["id"], [{"role": "system", "content": "internal"} for _ in range(5001)]
        + [{"role": "user", "content": "Última bicicleta del historial."}])
    client.portal.call(service.tick)
    with app.state.session_factory() as db:
        state = db.get(SemanticIndexState, session["id"])
        assert state.history_offset == 100
        assert state.active_generation is None
    restarted = SemanticSearch(app.state.services, service.client)
    app.state.semantic_search = restarted
    drain(client, restarted, owner)
    response = client.get("/api/v1/search", params={"q": "pedalear", "mode": "semantic"})
    assert "Última bicicleta" in response.json()["items"][0]["excerpt"]
    assert "internal" not in " ".join(service.client.inputs)


def test_reindex_reuses_embeddings_and_atomic_publication(semantic, app):
    client, csrf, service, owner = semantic
    session = create_session(client, csrf, "control-dev", "source-stable")
    add_history(client, app, session["id"], [{"role": "user", "content": "Mi bicicleta."}])
    drain(client, service, owner)
    before = len(service.client.inputs)
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        row.display_title = "Renamed title"
        db.commit()
    client.portal.call(service.tick)
    result = client.get("/api/v1/search", params={"q": "pedalear", "mode": "semantic"}).json()
    assert result["partial"] is True
    assert "bicicleta" in result["items"][0]["excerpt"]
    drain(client, service, owner)
    # Existing body and query are reused; only the renamed title is embedded.
    assert len(service.client.inputs) == before + 2
    assert service.status(owner)["indexed"] == 1


def test_history_overlap_survives_page_checkpoint_and_restart(semantic, app):
    client, csrf, service, owner = semantic
    session = create_session(client, csrf, "control-dev", "page-boundary")
    add_history(client, app, session["id"], [{"role": "system", "content": "internal"}] * 99
        + [{"role": "user", "content": "La bicicleta de mi hermana"},
           {"role": "assistant", "content": "necesita frenos nuevos."}])
    client.portal.call(service.tick)
    with app.state.session_factory() as db:
        checkpoint = db.get(SemanticIndexState, session["id"])
        assert checkpoint.history_tail_ciphertext.startswith("v1.")
        assert "bicicleta" not in checkpoint.history_tail_ciphertext
    restarted = SemanticSearch(app.state.services, service.client)
    drain(client, restarted, owner)
    assert any("bicicleta" in text and "frenos" in text for text in service.client.inputs)


def test_disabled_missing_key_csrf_and_owner_cache_isolation(authenticated, app):
    client, csrf = authenticated
    response = client.get("/api/v1/search", params={"q": "hello", "mode": "semantic"})
    assert response.status_code == 409
    assert client.put("/api/v1/search/semantic/settings", json={"enabled": True}).status_code == 403
    response = client.put("/api/v1/search/semantic/settings", json={"enabled": True}, headers=mutation_headers(csrf))
    assert response.status_code == 409
    assert client.get("/api/v1/search/semantic/status").json()["enabled"] is False


def test_ownership_deletion_and_temporary_exclusion(semantic, app):
    client, csrf, service, owner = semantic
    owned = create_session(client, csrf, "control-dev", "mine")
    add_history(client, app, owned["id"], [{"role": "user", "content": "bicicleta privada"}])
    drain(client, service, owner)
    with app.state.session_factory() as db:
        original = db.get(SessionLink, owned["id"])
        other = User(username="other-semantic", password_hash="unused")
        db.add(other); db.flush()
        db.add(SessionLink(owner_id=other.id, gateway_id=original.gateway_id, profile_name=original.profile_name,
            stored_session_id="other-history", title="other private"))
        db.add(SessionLink(owner_id=owner, gateway_id=original.gateway_id, profile_name=original.profile_name,
            stored_session_id="ac_tmp_excluded", title="temporary private", chat_mode="temporary"))
        db.commit()
        other_id = other.id
        db.add(SemanticPreference(owner_id=other_id, enabled=True))
        OpenAIIntegrationService(service.vault).set_api_key(db, other, "sk-other-unit-test-key")
        db.commit()
    service.reconcile()
    assert service.status(owner)["total"] == 1
    assert service.rank(other_id, np.eye(1, DIMENSIONS, dtype=np.float32)[0], 20)["items"] == []
    before = len(service.client.inputs)
    client.portal.call(service.query_vector, owner, "pedalear")
    client.portal.call(service.query_vector, other_id, "pedalear")
    assert len(service.client.inputs) == before + 2
    with app.state.session_factory() as db:
        assert len(list(db.scalars(select(SemanticIndexState).where(SemanticIndexState.owner_id == owner)))) == 1
        db.get(SessionLink, owned["id"]).display_title = "will be deleted"
        db.commit()
    service.reconcile()
    job = service.claim()
    if job.session_id != owned["id"]:
        job = service.claim()
    with app.state.session_factory() as db:
        db.execute(delete(SessionLink).where(SessionLink.id == owned["id"]))
        db.commit()
    assert service.save_fragments(job, "text", []) is False
    with app.state.session_factory() as db:
        assert list(db.scalars(select(SemanticFragment))) == []


def test_pause_and_quota_block_worker_without_blocking_lexical(semantic, app):
    client, csrf, service, owner = semantic
    session = create_session(client, csrf, "control-dev", "testquota")
    add_history(client, app, session["id"], [{"role": "user", "content": "bike"}])
    service.client.embed = AsyncMock(side_effect=failure("SEMANTIC_QUOTA", 429))
    client.portal.call(service.tick)
    assert service.status(owner)["state"] == "blocked"
    assert service.claim() is None
    assert client.get("/api/v1/search", params={"q": "testquota"}).status_code == 200
    response = client.put("/api/v1/search/semantic/settings", json={"enabled": False}, headers=mutation_headers(csrf, "disable-semantic"))
    assert response.json()["enabled"] is False
    assert client.get("/api/v1/search", params={"q": "bike", "mode": "semantic"}).status_code == 409


def test_chunk_bounds_public_projection_and_reasoning_exclusion():
    text = "palabra " * 1800
    parts = list(chunks(text))
    assert len(parts) >= 3 and all(len(encoding().encode(part)) <= 700 for part in parts)
    projected = public_text([
        {"role": "system", "content": "system-secret"},
        {"role": "tool", "content": "tool-secret"},
        {"role": "assistant", "channel": "analysis", "content": "reason-secret"},
        {"role": "user", "display_kind": "async_delegation_complete", "content": "internal-secret"},
        {"role": "assistant", "content": "Visible answer", "codex_reasoning_items": "secret"},
    ], "gateway", "default")
    assert projected == "assistant: Visible answer"


@pytest.mark.asyncio
async def test_embeddings_transport_validates_dimensions_and_errors():
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1]}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(Exception) as caught:
            await EmbeddingsClient(http).embed("secret-never-browser", ["query"])
        assert caught.value.code == "SEMANTIC_INVALID_RESPONSE"
    assert str(calls[0].url) == "https://api.openai.com/v1/embeddings"
    assert json.loads(calls[0].content)["dimensions"] == DIMENSIONS


def test_search_text_is_removed_from_structured_access_logs():
    import logging
    from hermes_control_api.main import RedactingLogFilter
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/api/v1/search?q=private%20question&mode=semantic", "1.1", 200), None)
    assert RedactingLogFilter().filter(record)
    assert "private" not in record.getMessage() and "[REDACTED]" in record.getMessage()


def test_old_connector_reports_update_without_requesting_history(semantic, app, monkeypatch):
    from hermes_client import CapabilitySet
    from hermes_control_api.models import Gateway
    from hermes_control_api.services import GatewayService
    from types import SimpleNamespace
    client, csrf, service, owner = semantic
    session = create_session(client, csrf, "control-dev", "old-connector")
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        db.get(Gateway, row.gateway_id).transport_kind = "connector"
        db.commit()
    provider = SimpleNamespace(capabilities=AsyncMock(return_value=CapabilitySet()), history_page=AsyncMock())
    monkeypatch.setattr(GatewayService, "connection", AsyncMock(return_value=object()))
    monkeypatch.setattr(service.services.provider_pool, "get", AsyncMock(return_value=provider))
    client.portal.call(service.tick)
    assert service.status(owner)["errorCode"] == "SEMANTIC_CONNECTOR_UPDATE"
    assert not provider.history_page.called


def test_disconnection_keeps_published_results_and_reports_partial(semantic, app, monkeypatch):
    from hermes_client import InMemoryHermesProvider
    client, csrf, service, owner = semantic
    session = create_session(client, csrf, "control-dev", "offline-source")
    add_history(client, app, session["id"], [{"role": "user", "content": "bicicleta"}])
    drain(client, service, owner)
    with app.state.session_factory() as db:
        db.get(SemanticIndexState, session["id"]).checked_at = utc_now() - timedelta(minutes=10)
        db.commit()
    monkeypatch.setattr(InMemoryHermesProvider, "history_page", AsyncMock(side_effect=ConnectionError("offline")))
    client.portal.call(service.tick)
    assert service.status(owner)["errorCode"] == "SEMANTIC_CONNECTION"
    result = client.get("/api/v1/search", params={"q": "pedalear", "mode": "semantic"}).json()
    assert result["partial"] is True and result["items"][0]["targetId"] == session["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [("insufficient_quota", "SEMANTIC_QUOTA"), ("rate_limit_exceeded", "SEMANTIC_RATE_LIMIT")])
async def test_embedding_errors_distinguish_quota_from_retryable_limits(code, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"error": {"code": code}}))) as http:
        with pytest.raises(Exception) as caught:
            await EmbeddingsClient(http).embed("test-key", ["query"])
        assert caught.value.code == expected
