from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .state import MockHermesState, MockSession


DASHBOARD_TOKEN = os.getenv("MOCK_HERMES_DASHBOARD_TOKEN", "mock-dashboard-token")
API_KEY = os.getenv("MOCK_HERMES_API_KEY", "mock-api-server-key-change-me")


def _rpc_ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _profile(params: dict[str, Any]) -> str:
    return str(params.get("profile") or "default")


def _dashboard_authorized(request: Request) -> bool:
    return request.headers.get("x-hermes-session-token") == DASHBOARD_TOKEN


def _api_authorized(request: Request) -> bool:
    return request.headers.get("authorization") == f"Bearer {API_KEY}"


def _require_dashboard(request: Request) -> None:
    if not _dashboard_authorized(request):
        raise HTTPException(status_code=401, detail="invalid dashboard session token")


def _require_api(request: Request) -> None:
    if not _api_authorized(request):
        raise HTTPException(status_code=401, detail="invalid API server key")


def _install_common_middleware(app: FastAPI, state: MockHermesState) -> None:
    @app.middleware("http")
    async def mock_faults_and_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        scenario = request.headers.get("x-mock-hermes-scenario", "")
        missing = path in state.faults.missing_endpoints or (
            scenario == "missing-endpoint" and not path.startswith("/__mock")
        )
        if missing:
            response = JSONResponse({"detail": "mocked missing endpoint"}, status_code=404)
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response


def _install_scenario_routes(app: FastAPI, state: MockHermesState, auth) -> None:  # type: ignore[no-untyped-def]
    @app.post("/__mock/reset")
    async def reset(request: Request) -> dict[str, Any]:
        auth(request)
        state.reset()
        return {"status": "reset", "epoch": state.replay_epoch}

    @app.post("/__mock/scenarios/{name}")
    async def scenario(name: str, request: Request, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        auth(request)
        enabled = bool(payload.get("enabled", True))
        if name == "epoch":
            return {"scenario": name, "enabled": enabled, "epoch": state.bump_epoch()}
        if name == "missing-endpoint":
            endpoint = str(payload.get("path") or "")
            if not endpoint.startswith("/"):
                raise HTTPException(status_code=422, detail="path must start with /")
            if enabled:
                state.faults.missing_endpoints.add(endpoint)
            else:
                state.faults.missing_endpoints.discard(endpoint)
            return {"scenario": name, "enabled": enabled, "path": endpoint}
        try:
            state.set_scenario(name, enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"scenario": name, "enabled": enabled}

    @app.get("/__mock/state")
    async def state_snapshot(request: Request) -> dict[str, Any]:
        auth(request)
        return {
            "epoch": state.replay_epoch,
            "profiles": state.profile_payloads(),
            "sessions": [state.session_payload(session) for session in state.sessions_by_stored.values()],
            "cron_jobs": list(state.cron_jobs.values()),
            "runs": list(state.runs.values()),
            "faults": {
                "disconnect_after_submit": state.faults.disconnect_after_submit,
                "unknown_event": state.faults.unknown_event,
                "force_replay_truncated": state.faults.force_replay_truncated,
                "missing_endpoints": sorted(state.faults.missing_endpoints),
                "stream_delay_ms": state.faults.stream_delay_ms,
            },
        }


def create_dashboard_app(state: MockHermesState | None = None) -> FastAPI:
    state = state or MockHermesState()
    app = FastAPI(title="Mock Hermes Dashboard", version="0.20.6-mock")
    app.state.hermes = state
    _install_common_middleware(app, state)
    _install_scenario_routes(app, state, _require_dashboard)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        _require_dashboard(request)
        return {
            "status": "ok",
            "service": "hermes-serve",
            "version": "0.20.6",
            "git_sha": "9978706e9303dbf990d90e744b131361449d73b9",
            "replay_epoch": state.replay_epoch,
        }

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        _require_dashboard(request)
        return {
            "status": "ok",
            "version": "0.20.6",
            "replay_epoch": state.replay_epoch,
        }

    @app.get("/api/profiles")
    async def profiles(request: Request) -> dict[str, Any]:
        _require_dashboard(request)
        return {"profiles": state.profile_payloads()}

    @app.get("/api/model/options")
    async def model_options(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_dashboard(request)
        admin = state.admin_profile(profile)
        return {
            "provider": admin["model"]["provider"],
            "model": admin["model"]["model"],
            "providers": [
                {
                    "slug": "mock",
                    "name": "Mock",
                    "configured": True,
                    "models": ["mock-model", "mock-model-small"],
                }
            ],
        }

    @app.post("/api/model/set")
    async def set_model(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        admin = state.admin_profile(target)
        provider = str(payload.get("provider") or "").strip()
        model = str(payload.get("model") or "").strip()
        if not provider or not model:
            raise HTTPException(status_code=422, detail="provider and model are required")
        admin["model"] = {"provider": provider, "model": model}
        admin["config"]["model"] = {"provider": provider, "default": model}
        return {"ok": True, "scope": "main", "provider": provider, "model": model}

    @app.get("/api/config")
    async def get_config(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_dashboard(request)
        return dict(state.admin_profile(profile)["config"])

    @app.put("/api/config")
    async def put_config(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        config = payload.get("config")
        if not isinstance(config, dict):
            raise HTTPException(status_code=422, detail="config must be an object")
        state.admin_profile(target)["config"].update(config)
        return {"ok": True}

    @app.get("/api/profiles/{profile}/soul")
    async def get_soul(profile: str, request: Request) -> dict[str, Any]:
        _require_dashboard(request)
        content = str(state.admin_profile(profile)["soul"])
        return {"content": content, "exists": bool(content)}

    @app.put("/api/profiles/{profile}/soul")
    async def put_soul(
        profile: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        _require_dashboard(request)
        content = payload.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=422, detail="content must be a string")
        state.admin_profile(profile)["soul"] = content
        return {"ok": True}

    @app.get("/api/memory")
    async def get_memory(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_dashboard(request)
        admin = state.admin_profile(profile)
        return {
            "active": admin["memory_provider"],
            "providers": [
                {"name": "", "label": "Built-in", "available": True},
                {"name": "mock-memory", "label": "Mock Memory", "available": True},
            ],
            "builtin_files": dict(admin["memory_sizes"]),
        }

    @app.put("/api/memory/provider")
    async def put_memory_provider(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        provider = str(payload.get("provider") or "")
        if provider not in {"", "mock-memory"}:
            raise HTTPException(status_code=404, detail="memory provider not found")
        state.admin_profile(target)["memory_provider"] = provider
        return {"ok": True, "active": provider}

    @app.post("/api/memory/reset")
    async def reset_memory(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target_profile = str(payload.get("profile") or profile)
        target = str(payload.get("target") or "all")
        if target not in {"all", "memory", "user"}:
            raise HTTPException(status_code=422, detail="invalid reset target")
        sizes = state.admin_profile(target_profile)["memory_sizes"]
        deleted: list[str] = []
        for key, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
            if target in {"all", key} and sizes[key]:
                sizes[key] = 0
                deleted.append(filename)
        return {"ok": True, "deleted": deleted}

    @app.get("/api/skills")
    async def get_skills(request: Request, profile: str = "default") -> list[dict[str, Any]]:
        _require_dashboard(request)
        return list(state.admin_profile(profile)["skills"].values())

    @app.put("/api/skills/toggle")
    async def put_skill_toggle(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        name = str(payload.get("name") or "")
        skill = state.admin_profile(target)["skills"].get(name)
        if skill is None:
            raise HTTPException(status_code=404, detail="skill not found")
        skill["enabled"] = bool(payload.get("enabled"))
        return {"ok": True, "name": name, "enabled": skill["enabled"]}

    @app.get("/api/tools/toolsets")
    async def get_toolsets(request: Request, profile: str = "default") -> list[dict[str, Any]]:
        _require_dashboard(request)
        return list(state.admin_profile(profile)["toolsets"].values())

    @app.put("/api/tools/toolsets/{name}")
    async def put_toolset(
        name: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        toolset = state.admin_profile(target)["toolsets"].get(name)
        if toolset is None:
            raise HTTPException(status_code=404, detail="toolset not found")
        toolset["enabled"] = bool(payload.get("enabled"))
        return {"ok": True, "name": name, "enabled": toolset["enabled"]}

    @app.get("/api/mcp/servers")
    async def get_mcp_servers(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_dashboard(request)
        return {"servers": list(state.admin_profile(profile)["mcp"].values())}

    @app.post("/api/mcp/servers", status_code=201)
    async def add_mcp_server(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        servers = state.admin_profile(target)["mcp"]
        if name in servers:
            raise HTTPException(status_code=409, detail="MCP server already exists")
        env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
        server = {
            "name": name,
            "url": payload.get("url"),
            "command": payload.get("command"),
            "args": list(payload.get("args") or []),
            "enabled": bool(payload.get("enabled", True)),
            "configured": True,
            "env": {
                str(key): {"configured": bool(value)} for key, value in env.items()
            },
            "auth": (
                {"configured": True}
                if payload.get("auth") or payload.get("bearer_token")
                else None
            ),
        }
        servers[name] = server
        return dict(server)

    @app.put("/api/mcp/servers/{name}/enabled")
    async def put_mcp_enabled(
        name: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        server = state.admin_profile(target)["mcp"].get(name)
        if server is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        server["enabled"] = bool(payload.get("enabled"))
        return {"ok": True, "name": name, "enabled": server["enabled"]}

    @app.delete("/api/mcp/servers/{name}")
    async def remove_mcp_server(
        name: str,
        request: Request,
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        if state.admin_profile(profile)["mcp"].pop(name, None) is None:
            raise HTTPException(status_code=404, detail="MCP server not found")
        return {"ok": True}

    @app.post("/api/mcp/servers/{name}/test")
    async def test_mcp_server(
        name: str,
        request: Request,
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        if name not in state.admin_profile(profile)["mcp"]:
            raise HTTPException(status_code=404, detail="MCP server not found")
        return {"ok": True, "name": name, "tools": ["mock_echo", "mock_status"]}

    @app.get("/api/messaging/platforms")
    async def get_channels(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_dashboard(request)
        return {
            "platforms": list(state.admin_profile(profile)["channels"].values())
        }

    @app.put("/api/messaging/platforms/{name}")
    async def put_channel(
        name: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        channel = state.admin_profile(target)["channels"].get(name)
        if channel is None:
            raise HTTPException(status_code=404, detail="channel not found")
        if payload.get("enabled") is not None:
            channel["enabled"] = bool(payload["enabled"])
        env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
        cleared = {str(item) for item in payload.get("clear_env") or []}
        for field in channel["env_vars"]:
            key = str(field["key"])
            if key in env:
                field["is_set"] = bool(str(env[key]).strip())
            if key in cleared:
                field["is_set"] = False
        channel["configured"] = all(
            not field.get("required") or field.get("is_set")
            for field in channel["env_vars"]
        )
        channel["state"] = (
            "ready" if channel["enabled"] and channel["configured"] else "disabled"
        )
        return {"ok": True, "platform": name}

    @app.post("/api/messaging/platforms/{name}/test")
    async def test_channel(
        name: str,
        request: Request,
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        channel = state.admin_profile(profile)["channels"].get(name)
        if channel is None:
            raise HTTPException(status_code=404, detail="channel not found")
        return {
            "ok": bool(channel["enabled"] and channel["configured"]),
            "state": channel["state"],
            "platform": name,
        }

    @app.get("/api/analytics/usage")
    async def get_usage(
        request: Request,
        profile: str = "default",
        days: int = 30,
    ) -> dict[str, Any]:
        _require_dashboard(request)
        state.admin_profile(profile)
        profile_sessions = [
            item for item in state.sessions_by_stored.values() if item.profile == profile
        ]
        return {
            "period_days": max(1, min(days, 365)),
            "totals": {
                "total_input": sum(len(message.content.split()) for session in profile_sessions for message in session.messages if message.role == "user"),
                "total_output": sum(len(message.content.split()) for session in profile_sessions for message in session.messages if message.role == "assistant"),
                "total_sessions": len(profile_sessions),
            },
            "daily": [],
            "by_model": [],
        }

    @app.get("/api/env")
    async def get_secrets(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_dashboard(request)
        secrets = state.admin_profile(profile)["secrets"]
        return {
            name: {
                "is_set": name in secrets,
                "description": "Write-only mock secret",
                "category": "provider",
                "is_password": True,
                "advanced": False,
                "channel_managed": False,
            }
            for name in sorted(set(secrets) | {"OPENAI_API_KEY", "OPENROUTER_API_KEY"})
        }

    @app.put("/api/env")
    async def put_secret(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        name = str(payload.get("key") or "")
        value = payload.get("value")
        if not name or not isinstance(value, str) or not value:
            raise HTTPException(status_code=422, detail="key and value are required")
        state.admin_profile(target)["secrets"][name] = value
        return {"ok": True, "key": name}

    @app.delete("/api/env")
    async def remove_secret(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        target = str(payload.get("profile") or profile)
        name = str(payload.get("key") or "")
        found = state.admin_profile(target)["secrets"].pop(name, None) is not None
        if not found:
            raise HTTPException(status_code=404, detail="secret not found")
        return {"ok": True, "key": name, "found": True}

    @app.get("/api/sessions")
    async def sessions(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_dashboard(request)
        if not state.profile_exists(profile):
            raise HTTPException(status_code=404, detail="profile not found")
        return {"sessions": [state.list_payload(item) for item in state.list_sessions(profile)]}

    @app.get("/api/sessions/{stored_session_id}/messages")
    async def messages(
        stored_session_id: str,
        request: Request,
        profile: str | None = None,
    ) -> dict[str, Any]:
        _require_dashboard(request)
        try:
            session = state.session_for_stored(stored_session_id, profile)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return {
            "session_id": stored_session_id,
            "messages": [asdict(message) for message in session.messages],
        }

    @app.delete("/api/sessions/{stored_session_id}", status_code=204)
    async def delete_session(
        stored_session_id: str,
        request: Request,
        profile: str | None = None,
    ) -> None:
        _require_dashboard(request)
        try:
            session = state.session_for_stored(stored_session_id, profile)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        state.runtime_to_stored.pop(session.runtime_session_id, None)
        state.sessions_by_stored.pop(session.stored_session_id, None)

    @app.get("/api/cron/jobs")
    async def cron_jobs(request: Request, profile: str = "all") -> list[dict[str, Any]]:
        _require_dashboard(request)
        return [
            dict(job)
            for job in state.cron_jobs.values()
            if profile == "all" or job["profile"] == profile
        ]

    @app.post("/api/cron/jobs")
    async def create_cron(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_dashboard(request)
        try:
            return state.create_cron_job(profile, payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/cron/jobs/{job_id}")
    async def update_cron(
        job_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str | None = None,
    ) -> dict[str, Any]:
        _require_dashboard(request)
        job = state.cron_jobs.get(job_id)
        if job is None or (profile is not None and job["profile"] != profile):
            raise HTTPException(status_code=404, detail="cron job not found")
        updates = payload.get("updates")
        if not isinstance(updates, dict):
            raise HTTPException(status_code=422, detail="updates must be an object")
        for key in ("name", "prompt"):
            if key in updates:
                job[key] = updates[key]
        if "schedule" in updates:
            schedule_text = str(updates["schedule"])
            job["schedule"] = {
                "kind": "cron",
                "expr": schedule_text,
                "display": schedule_text,
            }
            job["schedule_display"] = schedule_text
            job["next_run_at"] = "2026-08-31T15:00:00Z"
        return dict(job)

    @app.post("/api/cron/jobs/{job_id}/pause")
    async def pause_cron(
        job_id: str, request: Request, profile: str | None = None
    ) -> dict[str, Any]:
        _require_dashboard(request)
        job = state.cron_jobs.get(job_id)
        if job is None or (profile is not None and job["profile"] != profile):
            raise HTTPException(status_code=404, detail="cron job not found")
        job["enabled"] = False
        job["state"] = "paused"
        return dict(job)

    @app.post("/api/cron/jobs/{job_id}/resume")
    async def resume_cron(
        job_id: str, request: Request, profile: str | None = None
    ) -> dict[str, Any]:
        _require_dashboard(request)
        job = state.cron_jobs.get(job_id)
        if job is None or (profile is not None and job["profile"] != profile):
            raise HTTPException(status_code=404, detail="cron job not found")
        job["enabled"] = True
        job["state"] = "scheduled"
        return dict(job)

    @app.post("/api/cron/jobs/{job_id}/trigger")
    async def trigger_cron(
        job_id: str, request: Request, profile: str | None = None
    ) -> dict[str, Any]:
        _require_dashboard(request)
        job = state.cron_jobs.get(job_id)
        if job is None or (profile is not None and job["profile"] != profile):
            raise HTTPException(status_code=404, detail="cron job not found")
        session = state.create_session(
            str(job["profile"]), f"Ejecución · {job['name']}"
        )
        state.add_message(session, "user", str(job.get("prompt") or ""))
        state.add_message(session, "assistant", "Ejecución determinista completada")
        started_at = state.now()
        ended_at = state.now()
        state.cron_runs.setdefault(job_id, []).insert(
            0,
            {
                "id": session.stored_session_id,
                "title": session.title,
                "source": "cron",
                "profile": job["profile"],
                "started_at": started_at,
                "ended_at": ended_at,
                "last_active": ended_at,
                "is_active": False,
                "archived": False,
            },
        )
        job["last_run_at"] = ended_at
        job["last_status"] = "success"
        job["state"] = "scheduled" if job["enabled"] else "paused"
        return dict(job)

    @app.get("/api/cron/jobs/{job_id}/runs")
    async def cron_job_runs(
        job_id: str,
        request: Request,
        profile: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        _require_dashboard(request)
        job = state.cron_jobs.get(job_id)
        if job is None or (profile is not None and job["profile"] != profile):
            raise HTTPException(status_code=404, detail="cron job not found")
        bounded_limit = max(1, min(limit, 100))
        return {
            "runs": list(state.cron_runs.get(job_id, []))[:bounded_limit],
            "limit": bounded_limit,
        }

    @app.delete("/api/cron/jobs/{job_id}")
    async def delete_cron(
        job_id: str, request: Request, profile: str | None = None
    ) -> dict[str, bool]:
        _require_dashboard(request)
        job = state.cron_jobs.get(job_id)
        if job is None or (profile is not None and job["profile"] != profile):
            raise HTTPException(status_code=404, detail="cron job not found")
        state.cron_jobs.pop(job_id, None)
        return {"ok": True}

    @app.websocket("/api/ws")
    async def rpc_websocket(websocket: WebSocket) -> None:
        token = websocket.query_params.get("token") or websocket.headers.get("x-hermes-session-token")
        if token != DASHBOARD_TOKEN:
            await websocket.close(code=4401, reason="invalid dashboard session token")
            return
        await websocket.accept()
        await websocket.send_json(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    "payload": {
                        "skin": {"name": "mock-dark"},
                        "change_events": True,
                        "heartbeat": True,
                        "replay_epoch": state.replay_epoch,
                    },
                },
            }
        )
        stream_tasks: set[asyncio.Task[None]] = set()
        try:
            while True:
                request = await websocket.receive_json()
                request_id = request.get("id")
                method = request.get("method")
                params = request.get("params") if isinstance(request.get("params"), dict) else {}
                response, stream = _dispatch_rpc(state, request_id, str(method or ""), params)
                await websocket.send_json(response)
                if method == "session.interrupt" and "result" in response:
                    try:
                        interrupted = state.session_for_runtime(str(params.get("session_id") or ""))
                        await websocket.send_json(
                            state.emit(
                                interrupted,
                                "message.complete",
                                {"text": "", "status": "interrupted"},
                            )
                        )
                    except KeyError:
                        pass
                if method in {"approval.respond", "clarify.respond"} and "result" in response:
                    try:
                        resolved = stream or state.session_for_runtime(str(params.get("session_id") or ""))
                        if not resolved.running:
                            await websocket.send_json(
                                state.emit(
                                    resolved,
                                    "message.complete",
                                    {
                                        "text": resolved.messages[-1].content if resolved.messages else "",
                                        "status": "complete",
                                    },
                                )
                            )
                    except KeyError:
                        pass
                if stream is not None and method == "prompt.submit":
                    task = asyncio.create_task(_stream_prompt(state, websocket, stream, params))
                    stream_tasks.add(task)
                    task.add_done_callback(stream_tasks.discard)
        except WebSocketDisconnect:
            pass
        finally:
            for task in stream_tasks:
                task.cancel()

    return app


def _dispatch_rpc(
    state: MockHermesState,
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any], MockSession | None]:
    try:
        if method in {"gateway.ping", "ping"}:
            return _rpc_ok(request_id, {"pong": True, "epoch": state.replay_epoch}), None
        if method == "profiles.list":
            return _rpc_ok(request_id, {"profiles": state.profile_payloads()}), None
        if method == "session.create":
            session = state.create_session(_profile(params), str(params.get("title") or ""))
            return _rpc_ok(request_id, state.session_payload(session)), None
        if method == "session.list":
            profile = _profile(params)
            if not state.profile_exists(profile):
                return _rpc_error(request_id, 4007, "profile not found"), None
            return _rpc_ok(
                request_id,
                {"sessions": [state.list_payload(item) for item in state.list_sessions(profile)]},
            ), None
        if method == "session.resume":
            stored_id = str(params.get("session_id") or params.get("stored_session_id") or "")
            session = state.resume_session(stored_id, _profile(params))
            return _rpc_ok(request_id, {**state.session_payload(session), "resumed": session.stored_session_id}), None
        if method == "session.history":
            session = state.session_for_runtime(str(params.get("session_id") or ""))
            return _rpc_ok(
                request_id,
                {"messages": [asdict(message) for message in session.messages], "count": len(session.messages)},
            ), None
        if method == "session.status":
            session = state.session_for_runtime(str(params.get("session_id") or ""))
            return _rpc_ok(
                request_id,
                {"running": session.running, "session_id": session.runtime_session_id},
            ), None
        if method == "session.events.since":
            session = state.session_for_runtime(str(params.get("session_id") or ""))
            try:
                last_seen = int(params.get("last_seen", 0))
            except (TypeError, ValueError):
                return _rpc_error(request_id, -32602, "invalid params: last_seen must be an integer"), None
            return _rpc_ok(request_id, state.events_since(session, last_seen)), None
        if method == "session.interrupt":
            session = state.session_for_runtime(str(params.get("session_id") or ""))
            was_running = session.running
            session.running = False
            return _rpc_ok(request_id, {"interrupted": was_running, "status": "idle"}), None
        if method == "prompt.submit":
            session = state.session_for_runtime(str(params.get("session_id") or ""))
            if session.running:
                return _rpc_error(request_id, 4090, "session already has an active turn"), None
            text = params.get("text", params.get("prompt"))
            if not isinstance(text, str) or not text.strip():
                return _rpc_error(request_id, -32602, "text required"), None
            state.add_message(session, "user", text)
            session.running = True
            return _rpc_ok(request_id, {"status": "streaming"}), session
        if method == "approval.respond":
            session = state.session_for_runtime(str(params.get("session_id") or ""))
            request_key = str(params.get("request_id") or "")
            if request_key != session.pending_approval_id:
                return _rpc_error(request_id, 4008, "approval request not found"), None
            choice = str(params.get("choice") or "deny")
            if choice not in {"once", "session", "always", "deny"}:
                return _rpc_error(request_id, -32602, "invalid approval choice"), None
            session.pending_approval_id = None
            session.pending_approval = None
            session.running = False
            text = "Aprobación aceptada por el mock." if choice != "deny" else "Aprobación rechazada por el mock."
            state.add_message(session, "assistant", text)
            return _rpc_ok(request_id, {"resolved": 1}), session
        if method == "clarify.respond":
            # Official clarify.respond identifies the process-global pending
            # request; session_id is deliberately not part of its contract.
            request_key = str(params.get("request_id") or "")
            session = next(
                (
                    item
                    for item in state.sessions_by_stored.values()
                    if item.pending_clarify
                    and item.pending_clarify.get("request_id") == request_key
                ),
                None,
            )
            if session is None:
                return _rpc_ok(request_id, {"status": "expired"}), None
            pending = session.pending_clarify or {}
            questions = pending.get("questions")
            if isinstance(questions, list):
                question_id = str(params.get("question_id") or "")
                valid_ids = {
                    str(item.get("qid"))
                    for item in questions
                    if isinstance(item, dict) and item.get("qid")
                }
                if question_id not in valid_ids:
                    return _rpc_error(request_id, 4002, "unknown question_id"), None
                session.clarify_answers[question_id] = params.get("answer")
                remaining = sorted(valid_ids - set(session.clarify_answers))
                if remaining:
                    return _rpc_ok(request_id, {"status": "ok", "remaining": remaining}), None
            session.pending_clarify = None
            session.clarify_answers.clear()
            session.running = False
            state.add_message(session, "assistant", "Aclaración recibida por el mock.")
            return _rpc_ok(request_id, {"status": "ok", "remaining": []}), session
        if method == "session.events.stats":
            return _rpc_ok(
                request_id,
                {
                    "epoch": state.replay_epoch,
                    "sessions": len(state.sessions_by_stored),
                    "buffer_size": state.replay_buffer_size,
                },
            ), None
        if method == "mock.epoch.bump":
            return _rpc_ok(request_id, {"epoch": state.bump_epoch()}), None
        return _rpc_error(request_id, -32601, f"method not found: {method}"), None
    except KeyError as exc:
        return _rpc_error(request_id, 4007, str(exc).strip("'")), None


async def _stream_prompt(
    state: MockHermesState,
    websocket: WebSocket,
    session: MockSession,
    params: dict[str, Any],
) -> None:
    text = str(params.get("text", params.get("prompt")) or "")
    delay = max(0, state.faults.stream_delay_ms) / 1000

    async def send(event_type: str, payload: dict[str, Any]) -> None:
        frame = state.emit(session, event_type, payload)
        if params.get("request_id"):
            frame["correlation_id"] = str(params["request_id"])
        await websocket.send_json(frame)
        if delay:
            await asyncio.sleep(delay)

    try:
        await send("message.start", {"role": "assistant"})
        await send("message.delta", {"text": "Respuesta "})
        if state.faults.disconnect_after_submit or "[disconnect]" in text:
            await websocket.close(code=1012, reason="mock disconnect after accepted prompt")
            return
        if not session.running:
            await send("message.complete", {"text": "", "status": "interrupted"})
            return
        if state.faults.unknown_event or "[unknown-event]" in text:
            await send("future.experimental.event", {"safe": True, "value": "unknown-event"})
        if "[tool]" in text:
            tool_id = state._next("tool")
            await send("tool.start", {"tool_id": tool_id, "name": "mock_search", "args_text": "{}"})
            await send("tool.progress", {"name": "mock_search", "preview": "resultado determinista"})
            await send(
                "tool.complete",
                {"tool_id": tool_id, "name": "mock_search", "summary": "1 resultado", "duration_s": 0.01},
            )
        if "[approval]" in text:
            approval_id = state._next("approval")
            session.pending_approval_id = approval_id
            session.pending_approval = {
                "request_id": approval_id,
                "command": "mock-safe-command",
                "description": "Aprobación determinista solicitada por el mock",
                "allow_permanent": False,
                "allow_session": True,
                "choices": ["once", "session", "deny"],
            }
            await send(
                "approval.request",
                dict(session.pending_approval),
            )
            return
        if "[clarify-batch]" in text:
            clarify_id = state._next("clarify")
            session.pending_clarify = {
                "request_id": clarify_id,
                "questions": [
                    {"qid": "q0", "question": "¿Entorno?", "choices": ["dev", "prod"], "multi_select": False},
                    {"qid": "q1", "question": "¿Herramientas?", "choices": ["web", "terminal"], "multi_select": True},
                ],
            }
            await send("clarify.request", dict(session.pending_clarify))
            return
        if "[clarify]" in text:
            clarify_id = state._next("clarify")
            session.pending_clarify = {
                "request_id": clarify_id,
                "question": "¿Qué opción prefieres?",
                "choices": ["A", "B"],
            }
            await send("clarify.request", dict(session.pending_clarify))
            return
        response_text = f"determinista del perfil {session.profile}: {text.strip()}"
        await send("message.delta", {"text": response_text})
        state.add_message(session, "assistant", f"Respuesta {response_text}")
        session.running = False
        await send(
            "message.complete",
            {
                "text": f"Respuesta {response_text}",
                "status": "complete",
                "usage": {"input_tokens": len(text.split()), "output_tokens": len(response_text.split())},
            },
        )
    except (RuntimeError, WebSocketDisconnect):
        # A disconnect intentionally leaves running=True: clients must reconcile
        # the accepted-but-ambiguous prompt instead of resubmitting it blindly.
        return


def create_api_app(state: MockHermesState | None = None) -> FastAPI:
    state = state or MockHermesState()
    app = FastAPI(title="Mock Hermes API Server", version="0.20.6-mock")
    app.state.hermes = state
    _install_common_middleware(app, state)
    _install_scenario_routes(app, state, _require_api)

    def check_profile(profile: str) -> None:
        if not state.profile_exists(profile):
            raise HTTPException(status_code=404, detail="profile not found")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "api-server", "version": "0.20.6"}

    @app.get("/health/detailed")
    async def health_detailed(request: Request) -> dict[str, Any]:
        _require_api(request)
        return {
            "status": "ok",
            "profiles": [profile.name for profile in state.PROFILES],
            "active_sessions": sum(session.running for session in state.sessions_by_stored.values()),
        }

    @app.get("/v1/capabilities")
    @app.get("/p/{profile}/v1/capabilities")
    async def capabilities(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_api(request)
        check_profile(profile)
        return {
            "object": "hermes.api_server.capabilities",
            "platform": "hermes-agent",
            "model": next(item.model for item in state.PROFILES if item.name == profile),
            "features": {
                "chat_completions": True,
                "chat_completions_streaming": True,
                "run_submission": True,
                "run_status": True,
                "run_events_sse": True,
                "run_stop": True,
                "run_approval_response": True,
                "session_resources": True,
                "session_chat": True,
                "session_chat_streaming": True,
                "skills_api": True,
            },
            "endpoints": {
                "chat_completions": {"method": "POST", "path": "/v1/chat/completions"},
                "runs": {"method": "POST", "path": "/v1/runs"},
                "run_status": {"method": "GET", "path": "/v1/runs/{run_id}"},
                "run_events": {"method": "GET", "path": "/v1/runs/{run_id}/events"},
                "run_approval": {"method": "POST", "path": "/v1/runs/{run_id}/approval"},
                "run_stop": {"method": "POST", "path": "/v1/runs/{run_id}/stop"},
                "sessions": {"method": "GET", "path": "/api/sessions"},
                "session_create": {"method": "POST", "path": "/api/sessions"},
                "session": {"method": "GET", "path": "/api/sessions/{session_id}"},
                "session_delete": {"method": "DELETE", "path": "/api/sessions/{session_id}"},
                "session_messages": {"method": "GET", "path": "/api/sessions/{session_id}/messages"},
                "session_chat": {"method": "POST", "path": "/api/sessions/{session_id}/chat"},
                "session_chat_stream": {"method": "POST", "path": "/api/sessions/{session_id}/chat/stream"},
            },
        }

    @app.get("/v1/models")
    @app.get("/p/{profile}/v1/models")
    async def models(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_api(request)
        check_profile(profile)
        model = next(item.model for item in state.PROFILES if item.name == profile)
        return {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "hermes"}]}

    @app.post("/v1/chat/completions")
    @app.post("/p/{profile}/v1/chat/completions")
    async def chat_completions(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ):
        _require_api(request)
        check_profile(profile)
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=422, detail="messages must be a non-empty list")
        text = str(messages[-1].get("content") or "") if isinstance(messages[-1], dict) else ""
        completion_id = state._next("chatcmpl")
        model = str(payload.get("model") or next(item.model for item in state.PROFILES if item.name == profile))
        answer = f"Respuesta determinista del perfil {profile}: {text}"
        if bool(payload.get("stream")):
            return StreamingResponse(
                _chat_sse(completion_id, model, answer),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": 1787884800,
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": len(text.split()),
                "completion_tokens": len(answer.split()),
                "total_tokens": len(text.split()) + len(answer.split()),
            },
        }

    @app.post("/v1/responses")
    @app.post("/p/{profile}/v1/responses")
    async def responses(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ):
        _require_api(request)
        check_profile(profile)
        raw_input = payload.get("input", "")
        if isinstance(raw_input, list):
            text = " ".join(
                str(item.get("content") or "") if isinstance(item, dict) else str(item)
                for item in raw_input
            )
        else:
            text = str(raw_input or "")
        response_id = state._next("response")
        answer = f"Respuesta determinista del perfil {profile}: {text}"
        if bool(payload.get("stream")):
            return StreamingResponse(
                _responses_sse(response_id, answer, disconnect="[disconnect]" in text),
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )
        return {
            "id": response_id,
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": answer}],
                }
            ],
        }

    @app.post("/api/sessions", status_code=201)
    @app.post("/p/{profile}/api/sessions", status_code=201)
    async def create_persistent_session(
        request: Request,
        payload: dict[str, Any] = Body(default={}),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_api(request)
        check_profile(profile)
        session = state.create_session(profile, str(payload.get("title") or ""))
        session.persisted = True
        return {"id": session.stored_session_id, "title": session.title, "profile": profile}

    @app.get("/api/sessions")
    @app.get("/p/{profile}/api/sessions")
    async def list_persistent_sessions(request: Request, profile: str = "default") -> dict[str, Any]:
        _require_api(request)
        check_profile(profile)
        return {"data": [state.list_payload(item) for item in state.list_sessions(profile)]}

    @app.get("/api/sessions/{stored_session_id}/messages")
    @app.get("/p/{profile}/api/sessions/{stored_session_id}/messages")
    async def persistent_messages(
        stored_session_id: str,
        request: Request,
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_api(request)
        try:
            session = state.session_for_stored(stored_session_id, profile)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        return {"data": [asdict(message) for message in session.messages]}

    @app.post("/api/sessions/{stored_session_id}/chat")
    @app.post("/p/{profile}/api/sessions/{stored_session_id}/chat")
    async def persistent_chat(
        stored_session_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_api(request)
        try:
            session = state.session_for_stored(stored_session_id, profile)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        text = str(payload.get("message", payload.get("input", "")) or "")
        if not text:
            raise HTTPException(status_code=422, detail="message required")
        state.add_message(session, "user", text)
        answer = f"Respuesta determinista del perfil {profile}: {text}"
        message = state.add_message(session, "assistant", answer)
        return {"session_id": stored_session_id, "message": asdict(message)}

    @app.post("/api/sessions/{stored_session_id}/chat/stream")
    @app.post("/p/{profile}/api/sessions/{stored_session_id}/chat/stream")
    async def persistent_chat_stream(
        stored_session_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> StreamingResponse:
        _require_api(request)
        try:
            session = state.session_for_stored(stored_session_id, profile)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="session not found") from exc
        text = str(payload.get("message", payload.get("input", "")) or "")
        if not text:
            raise HTTPException(status_code=422, detail="message required")
        state.add_message(session, "user", text)
        answer = f"Respuesta determinista del perfil {profile}: {text}"
        state.add_message(session, "assistant", answer)
        return StreamingResponse(
            _session_chat_sse(stored_session_id, answer, disconnect="[disconnect]" in text),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/v1/runs", status_code=202)
    @app.post("/p/{profile}/v1/runs", status_code=202)
    async def create_run(
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_api(request)
        check_profile(profile)
        run = state.create_run(profile, payload)
        return {key: value for key, value in run.items() if key != "events"}

    @app.get("/v1/runs/{run_id}")
    @app.get("/p/{profile}/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request, profile: str = "default") -> dict[str, Any]:
        _require_api(request)
        run = state.runs.get(run_id)
        if run is None or run["profile"] != profile:
            raise HTTPException(status_code=404, detail="run not found")
        return {key: value for key, value in run.items() if key != "events"}

    @app.get("/v1/runs/{run_id}/events")
    @app.get("/p/{profile}/v1/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request, profile: str = "default") -> StreamingResponse:
        _require_api(request)
        run = state.runs.get(run_id)
        if run is None or run["profile"] != profile:
            raise HTTPException(status_code=404, detail="run not found")
        return StreamingResponse(_run_sse(run), media_type="text/event-stream")

    @app.post("/v1/runs/{run_id}/approval")
    @app.post("/p/{profile}/v1/runs/{run_id}/approval")
    async def approve_run(
        run_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
        profile: str = "default",
    ) -> dict[str, Any]:
        _require_api(request)
        run = state.runs.get(run_id)
        if run is None or run["profile"] != profile:
            raise HTTPException(status_code=404, detail="run not found")
        if run["status"] != "requires_action":
            raise HTTPException(status_code=409, detail="run is not awaiting approval")
        if str(payload.get("request_id") or "") != run["approval_id"]:
            raise HTTPException(status_code=404, detail="approval request not found")
        approved = bool(payload.get("approved", payload.get("allow", False)))
        run["status"] = "completed" if approved else "cancelled"
        run["output"] = (
            f"Respuesta determinista del perfil {profile}: {run['input']}" if approved else None
        )
        run["events"].append(
            {
                "type": f"run.{run['status']}",
                "run_id": run_id,
                "output": run["output"],
            }
        )
        return {"id": run_id, "status": run["status"], "approved": approved}

    @app.post("/v1/runs/{run_id}/stop")
    @app.post("/p/{profile}/v1/runs/{run_id}/stop")
    async def stop_run(run_id: str, request: Request, profile: str = "default") -> dict[str, Any]:
        _require_api(request)
        run = state.runs.get(run_id)
        if run is None or run["profile"] != profile:
            raise HTTPException(status_code=404, detail="run not found")
        run["status"] = "cancelled"
        run["events"].append({"type": "run.cancelled", "run_id": run_id})
        return {"id": run_id, "status": "cancelled"}

    return app


async def _chat_sse(completion_id: str, model: str, answer: str) -> AsyncIterator[str]:
    parts = ["Respuesta ", answer.removeprefix("Respuesta ")]
    for index, part in enumerate(parts):
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": 1787884800,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": part} if index == 0 else {"content": part},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
        await asyncio.sleep(0)
    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": 1787884800,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final, separators=(',', ':'))}\n\n"
    yield "data: [DONE]\n\n"


async def _responses_sse(response_id: str, answer: str, *, disconnect: bool) -> AsyncIterator[str]:
    created = {"type": "response.created", "response": {"id": response_id, "status": "in_progress"}}
    yield f"event: response.created\ndata: {json.dumps(created, separators=(',', ':'))}\n\n"
    delta = {"type": "response.output_text.delta", "response_id": response_id, "delta": answer}
    yield f"event: response.output_text.delta\ndata: {json.dumps(delta, separators=(',', ':'))}\n\n"
    if disconnect:
        return
    complete = {"type": "response.completed", "response": {"id": response_id, "status": "completed"}}
    yield f"event: response.completed\ndata: {json.dumps(complete, separators=(',', ':'))}\n\n"


async def _session_chat_sse(session_id: str, answer: str, *, disconnect: bool) -> AsyncIterator[str]:
    delta = {"type": "message.delta", "session_id": session_id, "delta": answer}
    yield f"event: message.delta\ndata: {json.dumps(delta, separators=(',', ':'))}\n\n"
    if disconnect:
        return
    complete = {"type": "message.complete", "session_id": session_id, "text": answer}
    yield f"event: message.complete\ndata: {json.dumps(complete, separators=(',', ':'))}\n\n"


async def _run_sse(run: dict[str, Any]) -> AsyncIterator[str]:
    for index, event in enumerate(run["events"], start=1):
        yield f"id: {index}\nevent: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        await asyncio.sleep(0)
