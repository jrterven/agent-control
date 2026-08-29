from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hermes_client import EventNormalizer, InMemoryHermesProvider, NormalizedEvent
from hermes_control_api.models import Gateway, SessionLink, User

from .conftest import mutation_headers


def _gateway_id(client) -> str:
    return client.get("/api/v1/gateways").json()[0]["id"]


def _create_session(client, csrf: str, key: str) -> dict:
    response = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, key),
        json={
            "gatewayId": _gateway_id(client),
            "profileName": "control-dev",
            "title": key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _pending_request(client, app, session_id: str, csrf: str, marker: str, key: str):
    response = client.post(
        f"/api/v1/sessions/{session_id}/prompts",
        headers=mutation_headers(csrf, key),
        json={"content": marker},
    )
    assert response.status_code == 202, response.text
    client.portal.call(asyncio.sleep, 0.01)
    matches = [
        claim
        for claim in app.state.services.event_hub._interactions.values()
        if claim.stored_session_id
    ]
    assert matches
    return matches[-1]


def test_approval_and_clarification_routes_are_owner_bound_and_idempotent(
    authenticated, app
):
    client, csrf = authenticated
    first = _create_session(client, csrf, "interaction-first")
    second = _create_session(client, csrf, "interaction-second")

    approval = _pending_request(
        client, app, first["id"], csrf, "[approval]", "approval-prompt"
    )
    crossed = client.post(
        f"/api/v1/sessions/{second['id']}/approvals/{approval.request_id}/respond",
        headers=mutation_headers(csrf, "approval-crossed"),
        json={"choice": "deny"},
    )
    assert crossed.status_code == 409
    assert crossed.json()["code"] == "CONFLICT"

    headers = mutation_headers(csrf, "approval-response")
    approved = client.post(
        f"/api/v1/sessions/{first['id']}/approvals/{approval.request_id}/respond",
        headers=headers,
        json={"choice": "once"},
    )
    replay = client.post(
        f"/api/v1/sessions/{first['id']}/approvals/{approval.request_id}/respond",
        headers=headers,
        json={"choice": "once"},
    )
    assert approved.status_code == replay.status_code == 200
    assert approved.json() == replay.json() == {
        "requestId": approval.request_id,
        "resolved": 1,
        "status": "resolved",
    }

    clarification = _pending_request(
        client, app, first["id"], csrf, "[clarify]", "clarify-prompt"
    )
    answered = client.post(
        f"/api/v1/sessions/{first['id']}/clarifications/{clarification.request_id}/respond",
        headers=mutation_headers(csrf, "clarify-response"),
        json={"answer": "A"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json() == {
        "requestId": clarification.request_id,
        "status": "ok",
        "remaining": [],
    }


@pytest.mark.parametrize("profile_name", ["default", "jarvis"])
@pytest.mark.parametrize("kind", ["approvals", "clarifications"])
def test_human_gate_routes_respect_an_operator_read_only_override(
    authenticated, app, profile_name: str, kind: str
):
    client, csrf = authenticated
    app.state.services.settings.mutable_profiles = ["control-dev"]
    app.state.services.settings.interactive_profiles = []
    with app.state.session_factory() as db:
        gateway = db.query(Gateway).one()
        actor = db.query(User).filter(User.username == "admin").one()
        row = SessionLink(
            owner_id=actor.id,
            gateway_id=gateway.id,
            profile_name=profile_name,
            stored_session_id=f"{profile_name}-{kind}",
            title="read only",
        )
        db.add(row)
        db.commit()
        session_id = row.id

    body = {"choice": "deny"} if kind == "approvals" else {"answer": "no"}
    response = client.post(
        f"/api/v1/sessions/{session_id}/{kind}/request-1/respond",
        headers=mutation_headers(csrf, f"blocked-{profile_name}-{kind}"),
        json=body,
    )
    assert response.status_code == 409
    assert "operator" in response.json()["message"]


def test_human_gate_routes_require_csrf_and_validate_bounded_payloads(authenticated):
    client, csrf = authenticated
    session = _create_session(client, csrf, "interaction-validation")
    missing_csrf = client.post(
        f"/api/v1/sessions/{session['id']}/approvals/request/respond",
        headers={"Idempotency-Key": "missing-csrf"},
        json={"choice": "deny"},
    )
    invalid_choice = client.post(
        f"/api/v1/sessions/{session['id']}/approvals/request/respond",
        headers=mutation_headers(csrf, "invalid-choice"),
        json={"choice": "yes"},
    )
    oversized = client.post(
        f"/api/v1/sessions/{session['id']}/clarifications/request/respond",
        headers=mutation_headers(csrf, "oversized-answer"),
        json={"answer": "x" * 10_001},
    )
    assert missing_csrf.status_code == 403
    assert invalid_choice.status_code == 422
    assert oversized.status_code == 422


def test_ambiguous_approval_response_is_not_retried(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "ambiguous-approval")
    approval = _pending_request(
        client,
        app,
        session["id"],
        csrf,
        "[approval]",
        "ambiguous-approval-prompt",
    )
    dispatch = AsyncMock(side_effect=RuntimeError("MUTATION_DELIVERY_UNKNOWN"))
    monkeypatch.setattr(InMemoryHermesProvider, "respond_approval", dispatch)
    headers = mutation_headers(csrf, "ambiguous-approval-response")

    first = client.post(
        f"/api/v1/sessions/{session['id']}/approvals/{approval.request_id}/respond",
        headers=headers,
        json={"choice": "once"},
    )
    replay = client.post(
        f"/api/v1/sessions/{session['id']}/approvals/{approval.request_id}/respond",
        headers=headers,
        json={"choice": "once"},
    )
    different_key = client.post(
        f"/api/v1/sessions/{session['id']}/approvals/{approval.request_id}/respond",
        headers=mutation_headers(csrf, "ambiguous-approval-response-new-key"),
        json={"choice": "once"},
    )

    assert first.status_code == replay.status_code == 409
    assert first.json()["code"] == "MUTATION_DELIVERY_UNKNOWN"
    assert replay.json()["code"] == "MUTATION_DELIVERY_UNKNOWN"
    assert different_key.status_code == 409
    assert different_key.json()["code"] == "CONFLICT"
    dispatch.assert_awaited_once()


def test_ambiguous_clarification_response_is_not_retried(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "ambiguous-clarification")
    clarification = _pending_request(
        client,
        app,
        session["id"],
        csrf,
        "[clarify]",
        "ambiguous-clarification-prompt",
    )
    dispatch = AsyncMock(side_effect=RuntimeError("MUTATION_DELIVERY_UNKNOWN"))
    monkeypatch.setattr(InMemoryHermesProvider, "respond_clarification", dispatch)
    headers = mutation_headers(csrf, "ambiguous-clarification-response")
    path = (
        f"/api/v1/sessions/{session['id']}/clarifications/"
        f"{clarification.request_id}/respond"
    )

    first = client.post(path, headers=headers, json={"answer": "A"})
    replay = client.post(path, headers=headers, json={"answer": "A"})
    different_key = client.post(
        path,
        headers=mutation_headers(csrf, "ambiguous-clarification-new-key"),
        json={"answer": "A"},
    )

    assert first.status_code == replay.status_code == 409
    assert first.json()["code"] == "MUTATION_DELIVERY_UNKNOWN"
    assert replay.json()["code"] == "MUTATION_DELIVERY_UNKNOWN"
    assert different_key.status_code == 409
    assert different_key.json()["code"] == "CONFLICT"
    dispatch.assert_awaited_once()


def test_batch_clarification_claim_removes_answered_question_ids(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    session = _create_session(client, csrf, "batch-claim")
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        event = NormalizedEvent.create(
            type="clarify.request",
            gateway_id=row.gateway_id,
            profile_name=row.profile_name,
            stored_session_id=row.stored_session_id,
            runtime_session_id=row.runtime_session_id,
            runtime_generation=row.runtime_generation,
            data={
                "request_id": "clarify-batch",
                "questions": [
                    {"qid": "q0", "question": "First"},
                    {"qid": "q1", "question": "Second"},
                ],
            },
        )
    client.portal.call(app.state.services.event_hub.publish, event)
    dispatch = AsyncMock(
        side_effect=[
            {"status": "ok", "remaining": ["q1"]},
            {"status": "ok", "remaining": []},
        ]
    )
    monkeypatch.setattr(
        InMemoryHermesProvider, "respond_clarification", dispatch
    )

    first = client.post(
        f"/api/v1/sessions/{session['id']}/clarifications/clarify-batch/respond",
        headers=mutation_headers(csrf, "batch-q0"),
        json={"questionId": "q0", "answer": "one"},
    )
    duplicate_question = client.post(
        f"/api/v1/sessions/{session['id']}/clarifications/clarify-batch/respond",
        headers=mutation_headers(csrf, "batch-q0-again"),
        json={"questionId": "q0", "answer": "again"},
    )
    second = client.post(
        f"/api/v1/sessions/{session['id']}/clarifications/clarify-batch/respond",
        headers=mutation_headers(csrf, "batch-q1"),
        json={"questionId": "q1", "answer": "two"},
    )

    assert first.status_code == 200, first.text
    assert first.json()["remaining"] == ["q1"]
    assert duplicate_question.status_code == 409
    assert second.status_code == 200, second.text
    assert second.json()["remaining"] == []
    assert dispatch.await_count == 2


def test_interaction_normalizer_preserves_official_shape_and_redacts_values():
    event = EventNormalizer(gateway_id="g", profile_name="control-dev").normalize(
        {
            "method": "event",
            "params": {
                "type": "approval.request",
                "session_id": "runtime",
                "payload": {
                    "request_id": "approval-1",
                    "command": "curl -H 'Authorization: Bearer super-secret-token-value'",
                    "description": "Run command",
                    "choices": ["once", "session", "always", "deny", "invented"],
                    "allow_session": True,
                    "allow_permanent": False,
                    "pattern_keys": ["echo *"],
                    "arbitrary": "drop me",
                },
            },
        }
    )
    assert event.data["choices"] == ["once", "session", "always", "deny"]
    assert event.data["allow_session"] is True
    assert "super-secret-token-value" not in event.data["command"]
    assert "arbitrary" not in event.data

    clarify = EventNormalizer(gateway_id="g", profile_name="control-dev").normalize(
        {
            "method": "event",
            "params": {
                "type": "clarify.request",
                "session_id": "runtime",
                "payload": {
                    "request_id": "clarify-1",
                    "questions": [
                        {
                            "qid": "q0",
                            "question": "¿Entorno?",
                            "choices": ["dev", "prod"],
                            "multi_select": False,
                        }
                    ],
                    "answers": {"q0": "dev"},
                },
            },
        }
    )
    assert clarify.data["questions"][0] == {
        "qid": "q0",
        "question": "¿Entorno?",
        "choices": ["dev", "prod"],
        "multi_select": False,
    }
    assert clarify.data["answers"] == {"q0": "dev"}
