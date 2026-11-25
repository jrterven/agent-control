"""Offline end-to-end certification of the native Hermes gateway and Control.

Run with a managed runtime's Python, passing --runtime-root. All homes, tokens,
model requests and processes belong to this temporary test. The model is a
deterministic loopback OpenAI-compatible stub; no provider account is used.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = Path(__file__).resolve().parents[1]


def native_server(root: Path, port: int) -> None:
    sys.path.insert(0, str(root / "hermes"))
    from tui_gateway import server, server_requests
    results = {}
    release_delegations = threading.Event()

    # A test-only entry point to the actual native server-request machinery.
    # It never executes a command, changes approval policy or reads credentials.
    @server.method("control.test.approval")
    def approval(rid, params):
        sid = params["session_id"]
        assert sid in server._sessions
        def question():
            results[sid] = server_requests.send("approval", sid, {
                "request_id": "offline-approval", "command": "offline assertion; no command is executed",
                "description": "Native approval round trip", "choices": ["once", "deny"],
                "allow_session": False, "allow_permanent": False}, timeout=30)
        threading.Thread(target=question, daemon=True).start()
        return server._ok(rid, {"started": True})

    @server.method("control.test.result")
    def result(rid, params):
        return server._ok(rid, {"answer": results.get(params["session_id"])})

    @server.method("control.test.delegate")
    def dispatch(rid, params):
        from tools.async_delegation import dispatch_async_delegation_batch

        sid = params["session_id"]
        session = server._sessions[sid]
        key = session["session_key"]
        label = params["label"]

        def runner():
            assert release_delegations.wait(25), "The isolated test did not release its delegates"
            return {"results": [{"task_index": 0, "status": "completed", "summary": label}]}

        with server._session_profile_runtime_scope(session):
            result = dispatch_async_delegation_batch(goals=[label], context=None, toolsets=[], role="general", model=None,
                session_key=key, parent_session_id=key, origin_ui_session_id=sid, origin_session_id=key, runner=runner)
        return server._ok(rid, result)

    @server.method("control.test.release-delegates")
    def release(rid, params):
        release_delegations.set()
        return server._ok(rid, {"released": True})

    from hermes_cli.main import main
    sys.argv = ["hermes", "serve", "--host", "127.0.0.1", "--port", str(port), "--isolated"]
    main()


class ModelHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "offline-test", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        messages = request.get("messages", [])
        user_index = next((index for index in range(len(messages) - 1, -1, -1)
                           if messages[index].get("role") == "user"), -1)
        prompt = str(messages[user_index].get("content", "")) if user_index >= 0 else ""
        has_tool = any(item.get("role") == "tool" for item in messages[user_index + 1:])
        tool = "SMOKE_CLARIFY" in prompt and not has_tool
        slow = "SMOKE_SLOW" in prompt
        content = "HERMES_DELEGATE_DELIVERED" if "SMOKE_DELEGATE_RESULT_" in prompt else "HERMES_NATIVE_SMOKE_OK"
        call = {"id": "call_smoke_clarify", "type": "function", "function": {"name": "clarify", "arguments": json.dumps({"questions": [
            {"question": "Choose two colors", "choices": ["Blue", "Green"], "multi_select": True},
            {"question": "Enter a short label"}]})}}
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            def chunk(delta, finish=None):
                data = {"id": "chatcmpl-offline", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": "offline-test", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
                self.wfile.flush()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                chunk({"role": "assistant"})
                if tool:
                    chunk({"tool_calls": [{"index": 0, **call}]})
                    chunk({}, "tool_calls")
                else:
                    for text in (["HERMES_"] + ["NATIVE_"] * 15 + ["SMOKE_OK"] if slow else [content]):
                        if slow:
                            time.sleep(0.25)
                        chunk({"content": text})
                    chunk({}, "stop")
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            self.close_connection = True
        else:
            message = {"role": "assistant", "content": None if tool else content}
            if tool:
                message["tool_calls"] = [call]
            result = {"id": "chatcmpl-offline", "object": "chat.completion", "created": int(time.time()),
                "model": "offline-test", "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool else "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}}
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


async def exercise(url: str, token: str, source_sha: str, version: str, work: Path) -> list[str]:
    from hermes_client import HermesGatewayProvider, HermesAutomation, ProviderConnection, SessionRoute
    events = []
    async def sink(event):
        events.append(event)
    provider = HermesGatewayProvider(ProviderConnection(gateway_id="offline-certification", profile_name="default",
        rest_url=url, ws_url=url.replace("http://", "ws://") + "/api/ws", dashboard_token=token,
        trusted_source_sha=source_sha), sink)
    checks = []
    def passed(name):
        checks.append(name)
        print(json.dumps({"passed": name}), flush=True)
    async def event_after(kind, start=0, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            match = next((event for event in events[start:] if event.type == kind), None)
            if match is not None:
                return match
            failure = next((event for event in events[start:] if event.type in {"message.error", "error"}), None)
            if failure:
                raise AssertionError(f"Native error before {kind}: {failure.data}")
            await asyncio.sleep(0.05)
        raise AssertionError(f"No {kind}; observed event types: {sorted({event.type for event in events[start:]})}")
    try:
        caps = await provider.capabilities()
        assert caps.version == version and {"prompt.submit", "clarify.respond", "cron.create"}.issubset(caps.methods)
        assert {"session.mode.temporary", "session.mode.memory_read_only"} <= caps.features
        passed("authenticated-native-capabilities-and-strict-handshake")
        session = await provider.create_session(title="Offline native certification")
        route = SessionRoute("offline-certification", "default", session.stored_session_id, session.runtime_session_id)
        start = len(events)
        receipt = await provider.submit_prompt(route, "SMOKE_CHAT", operation_id="offline-chat")
        assert receipt.status == "streaming"
        await event_after("message.complete", start)
        history = await provider.history_readonly(route.stored_session_id)
        assert any("HERMES_NATIVE_SMOKE_OK" in str(row.get("content", "")) for row in history)
        assert any(event.type == "message.delta" for event in events[start:])
        await provider.history(route)
        passed("native-chat-streaming-and-rpc-rest-history")

        start = len(events)
        await provider.submit_prompt(route, "SMOKE_CLARIFY", operation_id="offline-clarify")
        question = await event_after("clarify.request", start)
        assert len(question.data["questions"]) == 2
        request_id = question.data["request_id"]
        qids = [item["qid"] for item in question.data["questions"]]
        answer = await provider.respond_clarification(route, request_id, ["Blue", "Green"], question_id=qids[0])
        assert answer["remaining"] == [qids[1]]
        # Drop the actual WS while the native agent waits, restore from native
        # open_requests, and prove the first answer is not offered twice.
        previous_generation = provider.runtime_generation
        await provider.rpc.close()
        await provider._ensure_connected()
        assert provider.runtime_generation != previous_generation
        resumed = await provider.resume_session(route.stored_session_id)
        route = SessionRoute("offline-certification", "default", resumed.stored_session_id, resumed.runtime_session_id)
        latest = next(event for event in reversed(events) if event.type == "clarify.request")
        assert [item["qid"] for item in latest.data["questions"]] == [qids[1]]
        await provider.respond_clarification(route, request_id, "offline-label", question_id=qids[1])
        await event_after("message.complete", start)
        history = await provider.history_readonly(route.stored_session_id)
        assert "offline-label" in json.dumps(history) and "Blue" in json.dumps(history)
        passed("real-clarify-tool-multi-select-locks-reconnect-and-completion")

        start = len(events)
        await provider.rpc.request("control.test.approval", {"session_id": route.runtime_session_id})
        approval = await event_after("approval.request", start)
        assert await provider.respond_approval(route, approval.data["request_id"], "once") == {"resolved": 1}
        for _ in range(50):
            outcome = await provider.rpc.request("control.test.result", {"session_id": route.runtime_session_id})
            if outcome.get("answer"):
                break
            await asyncio.sleep(0.05)
        assert outcome["answer"] == {"choice": "once"}
        passed("native-approval-request-answer-round-trip-no-command-executed")

        start = len(events)
        await provider.submit_prompt(route, "SMOKE_SLOW", operation_id="offline-interrupt")
        await event_after("message.delta", start)
        await provider.interrupt(route)
        await event_after("message.complete", start)
        passed("native-interrupt-during-stream")

        automation = await provider.create_automation(HermesAutomation(automation_id="", name="Offline certification paused",
            schedule="0 12 * * MON", timezone="UTC", enabled=False, prompt="Never execute this paused test"))
        assert automation.enabled is False
        edited = await provider.update_automation(automation.automation_id, {"name": "Offline edited", "schedule": "0 13 * * TUE"})
        assert edited.enabled is False
        await provider.delete_automation(automation.automation_id)
        assert all(row.automation_id != automation.automation_id for row in await provider.list_automations())
        passed("native-atomic-paused-cron-create-edit-delete")

        private = await provider.create_session(chat_mode="temporary")
        private_route = SessionRoute("offline-certification", "default", private.stored_session_id, private.runtime_session_id)
        start = len(events)
        await provider.submit_prompt(private_route, "OFFLINE_PRIVATE_CANARY", operation_id="offline-private")
        await event_after("message.complete", start)
        assert "HERMES_NATIVE_SMOKE_OK" in json.dumps(await provider.history(private_route))
        await provider.renew_temporary_session(private_route)
        await provider.close_temporary_session(private_route)
        assert private.stored_session_id not in {row.stored_session_id for row in await provider.list_sessions()}
        assert all(b"OFFLINE_PRIVATE_CANARY" not in path.read_bytes()
                   for path in (work / "hermes-home").rglob("*") if path.is_file())
        passed("temporary-chat-actual-turn-renewal-close-and-no-persisted-transcript")

        profile = await provider.create_profile(name="offline-source", display_name="Offline source")
        assert profile.name == "offline-source"
        archive = work / "transfer.tar.gz"
        await provider.export_profile_archive_to(profile.name, archive)
        imported = await provider.import_profile_archive_from("offline-destination", archive)
        assert {profile.name, imported.name} <= {item.name for item in await provider.list_profiles()}
        await provider.delete_profile(imported.name)
        await provider.delete_profile(profile.name)
        assert {profile.name, imported.name}.isdisjoint({item.name for item in await provider.list_profiles()})
        passed("native-profile-create-export-import-and-delete")
        await verify_background_delivery(provider, events, work / "hermes-home")
        passed("native-background-completion-once-in-each-owning-profile")
        return checks
    finally:
        await provider.close()


async def verify_background_delivery(provider, events, home):
    from agent_control_connector.background_tasks import snapshot
    from dataclasses import replace
    from hermes_client import HermesGatewayProvider, SessionRoute

    peer_events = []
    async def peer_sink(event):
        peer_events.append(event)
    peer = HermesGatewayProvider(replace(provider.connection, profile_name="offline-worker"), peer_sink)
    try:
        scopes = [(provider, events, home), (peer, peer_events, home / "profiles/offline-worker")]
        delegations = []
        for agent, received, profile_home in scopes:
            session = await agent.create_session(title="Offline scoped delegation")
            profile_name = agent.connection.profile_name
            stored_id = session.stored_session_id
            route = SessionRoute(agent.connection.gateway_id, profile_name, stored_id, session.runtime_session_id)
            await agent.submit_prompt(route, "SMOKE_CHAT", operation_id="offline-prepare-delegation")
            for _ in range(240):
                if any(event.type == "message.complete" and event.stored_session_id == stored_id for event in received):
                    break
                await asyncio.sleep(.05)
            else:
                raise AssertionError(f"Test parent did not complete its foreground turn in {profile_name}")
            label = "SMOKE_DELEGATE_RESULT_" + profile_name
            dispatched = await agent.rpc.request("control.test.delegate", {"session_id": route.runtime_session_id, "label": label})
            assert dispatched["status"] == "dispatched", dispatched
            observed = snapshot(profile_home, stored_id)
            assert observed["complete"] and observed["activeCount"] == 1, observed
            delegations.append((agent, received, profile_home, stored_id, label, dispatched["delegation_id"]))
        await provider.rpc.request("control.test.release-delegates", {})
        for agent, received, profile_home, stored_id, label, task_id in delegations:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                observed = snapshot(profile_home, stored_id)
                completions = [event for event in received if event.type == "message.complete" and event.stored_session_id == stored_id]
                if any(task["id"] == task_id and task["deliveryState"] == "delivered" for task in observed["tasks"]) and len(completions) == 2:
                    break
                await asyncio.sleep(.1)
            else:
                raise AssertionError(f"Native completion not delivered to {agent.connection.profile_name}: {observed}")
            transcript = await agent.history_readonly(stored_id)
            assert sum(row.get("content") == "HERMES_DELEGATE_DELIVERED" for row in transcript) == 1
            assert label in json.dumps(transcript)
            other = next(item[4] for item in delegations if item[4] != label)
            assert other not in json.dumps(transcript)
    finally:
        await peer.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--server", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.runtime_root.resolve()
    if args.server is not None:
        native_server(root, args.server)
        return
    sys.path.insert(0, str(REPO / "packages/hermes-client"))
    sys.path.insert(0, str(REPO / "packages/connector"))
    from agent_control_connector import background_install, chat_modes_install, media_install
    from hermes_client.compatibility import CONTRACTS, SERVER_REQUEST_REVISIONS
    stamp = json.loads((root / "hermes/install-stamp.json").read_text())
    source_sha = stamp["commit"]
    if source_sha not in SERVER_REQUEST_REVISIONS or stamp["displayVersion"] != CONTRACTS[source_sha].version:
        raise ValueError("Native certification requires an audited runtime")
    version = CONTRACTS[source_sha].version
    with tempfile.TemporaryDirectory(prefix="verify-native-gateway-") as temporary:
        work = Path(temporary)
        home = work / "hermes-home"
        home.mkdir()
        model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
        threading.Thread(target=model.serve_forever, daemon=True).start()
        config = {"model": {"provider": "custom", "default": "offline-test", "base_url": f"http://127.0.0.1:{model.server_port}/v1",
                           "api_key": "offline-placeholder", "api_mode": "chat_completions"},
                  "timezone": "UTC", "security": {"allow_lazy_installs": False},
                  "agent": {"max_turns": 6}, "tools": {"enabled_toolsets": ["terminal", "file", "clarify"]},
                  "platform_toolsets": {"cli": ["terminal", "file", "clarify"]},
                  "display": {"streaming": True}, "dashboard": {"turn_isolation": False},
                  "memory": {"enabled": False}, "plugins": {"enabled": []}}
        (home / "config.yaml").write_text(json.dumps(config))
        chat_modes_install.install_profile(home, source_sha)
        background_install.install_profile(home, source_sha)
        media_install.install_profile(home)
        peer_home = home / "profiles/offline-worker"
        peer_home.mkdir(parents=True)
        (peer_home / "config.yaml").write_text(json.dumps(config))
        background_install.install_profile(peer_home, source_sha)
        token = secrets.token_urlsafe(32)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        env = {"HOME": str(work), "HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "HERMES_DASHBOARD_SESSION_TOKEN": token, "HERMES_DISABLE_LAZY_INSTALLS": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "HERMES_SKIP_MIGRATIONS": "1",
               "PYTHONPATH": os.pathsep.join((str(REPO / "packages/hermes-client"), str(REPO / "packages/connector"),
                                               str(root / "hermes"), str(root / "connector")))}
        log = work / "server.log"
        with log.open("w") as output:
            process = subprocess.Popen([str(root / "python/bin/python3"), "-s", "-B", str(Path(__file__).resolve()),
                "--runtime-root", str(root), "--server", str(port)], env=env, cwd=home, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                import urllib.request
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 60
                url = f"http://127.0.0.1:{port}"
                while True:
                    if process.poll() is not None:
                        raise AssertionError(f"Native server exited: {process.returncode}")
                    try:
                        request = urllib.request.Request(url + "/api/profiles", headers={"X-Hermes-Session-Token": token})
                        with opener.open(request, timeout=2) as response:
                            assert json.load(response)["profiles"]
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise AssertionError("Native server did not become ready")
                        time.sleep(0.1)
                assert background_install.probe_profile(home, source_sha)["state"] == "ready"
                marker = json.loads((home / ".agent-control/background/runtime.json").read_text())
                assert marker["profileDeliveryMode"] == "native-tui"
                assert media_install.probe_profile(home)["state"] == "ready"
                checks = asyncio.run(exercise(url, token, source_sha, version, work))
                print(json.dumps({"ok": True, "hermes": version, "checks": checks}), flush=True)
            except BaseException:
                # This log contains only isolated fake credentials/prompts.
                tail = log.read_text(errors="replace")[-10_000:].replace(token, "[test-token]")
                print(tail, file=sys.stderr)
                raise
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                model.shutdown()
                model.server_close()


if __name__ == "__main__":
    main()
