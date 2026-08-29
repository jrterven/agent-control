from __future__ import annotations

from fastapi.testclient import TestClient

from mock_hermes import MockHermesState, create_dashboard_app


TOKEN = "mock-dashboard-token"
AUTH = {"X-Hermes-Session-Token": TOKEN}


def rpc(client, request_id: int, method: str, params: dict | None = None):  # type: ignore[no-untyped-def]
    client.send_json(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }
    )
    return client.receive_json()


def receive_until(client, event_type: str, maximum: int = 12):  # type: ignore[no-untyped-def]
    for _ in range(maximum):
        frame = client.receive_json()
        if frame.get("method") == "event" and frame.get("params", {}).get("type") == event_type:
            return frame
    raise AssertionError(f"event not received: {event_type}")


def test_rest_requires_dashboard_token_and_lists_profiles() -> None:
    app = create_dashboard_app(MockHermesState())
    with TestClient(app) as client:
        assert client.get("/api/profiles").status_code == 401
        response = client.get("/api/profiles", headers=AUTH)

    assert response.status_code == 200
    assert [profile["name"] for profile in response.json()["profiles"]] == [
        "default",
        "jarvis",
        "control-dev",
    ]

    with TestClient(app) as client:
        status = client.get("/api/status", headers=AUTH).json()
    assert status["version"] == "0.20.6"
    assert "source_sha" not in status


def test_json_rpc_dual_identity_streaming_tools_and_replay() -> None:
    app = create_dashboard_app(MockHermesState())
    with TestClient(app) as http:
        with http.websocket_connect(f"/api/ws?token={TOKEN}") as ws:
            ready = ws.receive_json()
            assert ready["params"]["type"] == "gateway.ready"
            assert ready["params"]["payload"]["heartbeat"] is True

            created = rpc(ws, 1, "session.create", {"profile": "control-dev"})
            runtime_id = created["result"]["session_id"]
            stored_id = created["result"]["stored_session_id"]
            assert runtime_id != stored_id

            empty = rpc(ws, 2, "session.list", {"profile": "control-dev"})
            assert empty["result"]["sessions"] == []

            accepted = rpc(
                ws,
                3,
                "prompt.submit",
                {"session_id": runtime_id, "text": "usa [tool]"},
            )
            assert accepted["result"]["status"] == "streaming"
            complete = receive_until(ws, "message.complete")
            assert complete["params"]["seq"] >= 5
            assert complete["params"]["payload"]["status"] == "complete"

            sessions = rpc(ws, 4, "session.list", {"profile": "control-dev"})
            assert sessions["result"]["sessions"][0]["id"] == stored_id

            replay = rpc(
                ws,
                5,
                "session.events.since",
                {"session_id": runtime_id, "last_seen": 0},
            )
            assert replay["result"]["count"] >= 5
            assert replay["result"]["truncated"] is False

            resumed = rpc(
                ws,
                6,
                "session.resume",
                {"session_id": stored_id, "profile": "control-dev"},
            )
            assert resumed["result"]["session_id"] != runtime_id
            assert resumed["result"]["stored_session_id"] == stored_id


def test_unknown_event_and_approval_are_explicit() -> None:
    app = create_dashboard_app(MockHermesState())
    with TestClient(app) as http:
        with http.websocket_connect(f"/api/ws?token={TOKEN}") as ws:
            ws.receive_json()
            created = rpc(ws, 1, "session.create", {"profile": "control-dev"})
            runtime_id = created["result"]["session_id"]
            rpc(
                ws,
                2,
                "prompt.submit",
                {"session_id": runtime_id, "text": "[unknown-event] [approval]"},
            )
            unknown = receive_until(ws, "future.experimental.event")
            assert unknown["params"]["payload"]["safe"] is True
            approval = receive_until(ws, "approval.request")
            request_id = approval["params"]["payload"]["request_id"]
            resolved = rpc(
                ws,
                3,
                "approval.respond",
                {"session_id": runtime_id, "request_id": request_id, "choice": "deny"},
            )
            assert resolved["result"] == {"resolved": 1}
            assert approval["params"]["payload"]["choices"] == ["once", "session", "deny"]


def test_clarify_single_and_batch_match_official_rpc_contract() -> None:
    app = create_dashboard_app(MockHermesState())
    with TestClient(app) as http:
        with http.websocket_connect(f"/api/ws?token={TOKEN}") as ws:
            ws.receive_json()
            created = rpc(ws, 1, "session.create", {"profile": "control-dev"})
            runtime_id = created["result"]["session_id"]
            rpc(ws, 2, "prompt.submit", {"session_id": runtime_id, "text": "[clarify]"})
            single = receive_until(ws, "clarify.request")["params"]["payload"]
            answered = rpc(
                ws,
                3,
                "clarify.respond",
                {"request_id": single["request_id"], "answer": "A"},
            )
            assert answered["result"] == {"status": "ok", "remaining": []}
            receive_until(ws, "message.complete")

            rpc(ws, 4, "prompt.submit", {"session_id": runtime_id, "text": "[clarify-batch]"})
            batch = receive_until(ws, "clarify.request")["params"]["payload"]
            first = rpc(
                ws,
                5,
                "clarify.respond",
                {"request_id": batch["request_id"], "question_id": "q0", "answer": "dev"},
            )
            assert first["result"] == {"status": "ok", "remaining": ["q1"]}
            second = rpc(
                ws,
                6,
                "clarify.respond",
                {"request_id": batch["request_id"], "question_id": "q1", "answer": ["web"]},
            )
            assert second["result"] == {"status": "ok", "remaining": []}


def test_cron_and_missing_endpoint_scenarios() -> None:
    app = create_dashboard_app(MockHermesState())
    with TestClient(app) as client:
        created = client.post(
            "/api/cron/jobs",
            headers=AUTH,
            params={"profile": "control-dev"},
            json={"name": "Weekly review", "schedule": "0 9 * * 1"},
        )
        assert created.status_code == 200
        job_id = created.json()["id"]
        assert client.post(
            f"/api/cron/jobs/{job_id}/pause",
            headers=AUTH,
            params={"profile": "control-dev"},
        ).json()["enabled"] is False

        scenario = client.post(
            "/__mock/scenarios/missing-endpoint",
            headers=AUTH,
            json={"path": "/api/profiles"},
        )
        assert scenario.status_code == 200
        assert client.get("/api/profiles", headers=AUTH).status_code == 404


def test_provider_compatibility_aliases_and_no_invented_cron_rpc() -> None:
    app = create_dashboard_app(MockHermesState())
    with TestClient(app) as http:
        with http.websocket_connect(f"/api/ws?token={TOKEN}") as ws:
            ws.receive_json()
            created = rpc(ws, 1, "session.create", {"profile": "control-dev"})["result"]
            accepted = rpc(
                ws,
                2,
                "prompt.submit",
                {
                    "session_id": created["session_id"],
                    "stored_session_id": created["stored_session_id"],
                    "prompt": "provider alias",
                    "profile": "control-dev",
                },
            )
            assert accepted["result"]["status"] == "streaming"
            receive_until(ws, "message.complete")

            resumed = rpc(
                ws,
                3,
                "session.resume",
                {"stored_session_id": created["stored_session_id"], "profile": "control-dev"},
            )
            assert resumed["result"]["stored_session_id"] == created["stored_session_id"]

            unsupported = rpc(ws, 4, "cron.list", {"profile": "control-dev"})
            assert unsupported["error"]["code"] == -32601
