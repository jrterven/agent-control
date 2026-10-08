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
        content = "HERMES_NATIVE_SMOKE_OK"
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


async def exercise(url: str, token: str) -> list[str]:
    import httpx
    from hermes_client import HermesGatewayProvider, HermesAutomation, ProviderConnection, SessionRoute
    from hermes_client.compatibility import HERMES_0216_SHA
    events = []
    async def sink(event):
        events.append(event)
    provider = HermesGatewayProvider(ProviderConnection(gateway_id="offline-certification", profile_name="default",
        rest_url=url, ws_url=url.replace("http://", "ws://") + "/api/ws", dashboard_token=token,
        trusted_source_sha=HERMES_0216_SHA), sink)
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
        assert caps.version == "0.21.6" and {"prompt.submit", "clarify.respond", "cron.create"}.issubset(caps.methods)
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
        return checks
    finally:
        await provider.close()


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
    from hermes_client.compatibility import HERMES_0216_SHA
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
        chat_modes_install.install_profile(home, HERMES_0216_SHA)
        background_install.install_profile(home, HERMES_0216_SHA)
        media_install.install_profile(home)
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
                assert background_install.probe_profile(home, HERMES_0216_SHA)["state"] == "ready"
                marker = json.loads((home / ".agent-control/background/runtime.json").read_text())
                assert marker["profileDeliveryMode"] == "native-tui"
                assert media_install.probe_profile(home)["state"] == "ready"
                checks = asyncio.run(exercise(url, token))
                print(json.dumps({"ok": True, "hermes": "0.21.6", "checks": checks}), flush=True)
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
