from __future__ import annotations

from fastapi.testclient import TestClient

from mock_hermes import MockHermesState, create_api_app


KEY = "mock-api-server-key-change-me"
AUTH = {"Authorization": f"Bearer {KEY}"}


def test_health_is_probeable_but_detailed_health_requires_key() -> None:
    app = create_api_app(MockHermesState())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/detailed").status_code == 401
        assert client.get("/health/detailed", headers=AUTH).status_code == 200


def test_profile_prefixed_chat_completion_and_sse() -> None:
    app = create_api_app(MockHermesState())
    payload = {
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Hola"}],
    }
    with TestClient(app) as client:
        response = client.post("/p/control-dev/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200
        assert "perfil control-dev" in response.json()["choices"][0]["message"]["content"]

        stream = client.post(
            "/p/control-dev/v1/chat/completions",
            headers=AUTH,
            json={**payload, "stream": True},
        )
        assert stream.status_code == 200
        assert "data: [DONE]" in stream.text
        assert "chat.completion.chunk" in stream.text


def test_persistent_session_surface_and_missing_profile() -> None:
    state = MockHermesState()
    app = create_api_app(state)
    with TestClient(app) as client:
        created = client.post(
            "/p/control-dev/api/sessions",
            headers=AUTH,
            json={"title": "Fallback session"},
        )
        assert created.status_code == 201
        stored_id = created.json()["id"]

        listed = client.get("/p/control-dev/api/sessions", headers=AUTH)
        assert listed.json()["data"][0]["id"] == stored_id
        assert client.get("/p/not-real/v1/models", headers=AUTH).status_code == 404


def test_run_sse_approval_and_stop_contract() -> None:
    app = create_api_app(MockHermesState())
    with TestClient(app) as client:
        created = client.post(
            "/p/control-dev/v1/runs",
            headers=AUTH,
            json={"input": "hazlo [approval]"},
        )
        assert created.status_code == 202
        run = created.json()
        assert run["status"] == "requires_action"

        events = client.get(f"/p/control-dev/v1/runs/{run['id']}/events", headers=AUTH)
        assert "event: approval.request" in events.text

        approved = client.post(
            f"/p/control-dev/v1/runs/{run['id']}/approval",
            headers=AUTH,
            json={"request_id": run["approval_id"], "approved": True},
        )
        assert approved.json()["status"] == "completed"

        stopped = client.post(f"/p/control-dev/v1/runs/{run['id']}/stop", headers=AUTH)
        assert stopped.json()["status"] == "cancelled"


def test_responses_stream_can_end_without_terminal_event_for_disconnect_fault() -> None:
    app = create_api_app(MockHermesState())
    with TestClient(app) as client:
        response = client.post(
            "/p/control-dev/v1/responses",
            headers=AUTH,
            json={"input": "simulate [disconnect]", "stream": True},
        )
        assert response.status_code == 200
        assert "response.output_text.delta" in response.text
        assert "response.completed" not in response.text
