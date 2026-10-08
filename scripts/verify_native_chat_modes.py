"""Exercise the chat policy against an installed, audited Hermes runtime.

Run with that runtime's Python (and dependencies), passing --hermes-root.
Uses an isolated temporary home; makes no model requests or user-config changes.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-root", type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.hermes_root.resolve()), str(Path(__file__).resolve().parents[1] / "packages/connector")]
    with tempfile.TemporaryDirectory(prefix="verify-chat-policy-") as directory:
        home = Path(directory)
        os.environ["HERMES_HOME"] = directory
        os.environ["HERMES_SKIP_MIGRATIONS"] = "1"
        (home / "config.yaml").write_text("plugins:\n  enabled: [agent-control-chat-modes]\nmodel:\n  provider: custom\n  base_url: http://127.0.0.1:1/v1\n  default: test-model\n")
        (home / "memories").mkdir()
        memory = home / "memories/MEMORY.md"
        memory.write_text("shared-memory-canary")
        from tui_gateway import server
        from agent_control_connector import hermes_chat_policy as policy_module
        hooks = {}
        class Context:
            def register_hook(self, name, handler):
                hooks[name] = handler
        policy_module.register(Context())
        runtime = policy_module.active_runtime
        # Agent construction is tested explicitly below; disable only the async
        # eager build to keep the RPC test deterministic and entirely offline.
        server._schedule_agent_build = lambda sid: None
        server._schedule_session_cap_enforcement = lambda: None
        rpc = lambda name, **params: server._methods[name]("smoke", params)["result"]
        assert rpc("control.chat_modes")["modes"] == ["memory_read_only", "temporary"]
        assert runtime.available_modes({"plugins": {"enabled": [policy_module.PLUGIN_NAME]}, "dashboard": {"turn_isolation": True}}) == []

        from run_agent import AIAgent
        for mode in ("memory_read_only", "temporary"):
            created = rpc("control.session.create", chat_mode=mode)
            key, sid = created["stored_session_id"], created["session_id"]
            policy = runtime.policy(key)
            with policy_module.policy_scope(policy):
                agent = AIAgent(model="test-model", provider="custom", base_url="http://127.0.0.1:1/v1",
                                api_key="unused-offline-test", session_id=key, enabled_toolsets=[],
                                quiet_mode=True, skip_context_files=True)
                assert "shared-memory-canary" in agent._memory_store.format_for_system_prompt("memory")
                assert agent._memory_store.add("memory", "unwanted-memory")["success"] is False
                agent.commit_memory_session()
                assert "unwanted-memory" not in memory.read_text()
                assert hooks["pre_llm_call"]()["context"]
                if mode == "temporary":
                    from tools.debug_helpers import DebugSession
                    debug = DebugSession("private-smoke", env_var="UNSET_CHAT_SMOKE_DEBUG")
                    debug.enabled = True
                    debug.log_call("private", {"content": "private-debug-canary"})
                    debug.save()
                    assert debug._calls == []
                    db = runtime.databases[key]
                    db.create_session(session_id=key, source="cli")
                    db.append_message(key, "user", "private-transcript-canary")
                    assert db.get_messages(key)[0]["content"] == "private-transcript-canary"
                    assert db._conn.execute("PRAGMA database_list").fetchone()[2] == ""
                    from tools import async_delegation
                    with contextlib.closing(async_delegation._connect()) as jobs:
                        assert jobs.execute("PRAGMA database_list").fetchone()[2] == ""
                    record = server._sessions[sid]
                    # Native Linux boot redirects tempfile into the retained
                    # home. Exercise that behavior on every certification OS,
                    # including a populated tempfile cache and a path alias.
                    scratch = home / "cache/scratch"
                    scratch.mkdir(parents=True, exist_ok=True)
                    scratch_alias = home / "scratch-alias"
                    scratch_alias.symlink_to(scratch, target_is_directory=True)
                    for variable in ("TMPDIR", "TMP", "TEMP"):
                        os.environ[variable] = str(scratch_alias)
                    tempfile.tempdir = str(scratch_alias)
                    staged, uploaded = server._stage_session_file_attachment(record, raw_path="", name="upload.txt",
                        data_url="data:text/plain;base64," + base64.b64encode(b"upload-canary").decode())
                    assert uploaded and staged.read_bytes() == b"upload-canary"
                    assert not staged.resolve().is_relative_to(home.resolve())
                    assert not list(scratch.rglob("upload.txt"))
                    record["agent"] = agent
                    assert rpc("control.session.background", stored_session_id=key)["tasks"] == []
                    # Exercise the real native worker/finalizer, not a fabricated
                    # ledger. Its only runner is local and performs no model calls.
                    handle = async_delegation.dispatch_async_delegation_batch(
                        goals=["private-goal-canary"], context="private-context-canary", toolsets=[],
                        role="general", model=None, session_key=key, parent_session_id=key,
                        runner=lambda: {"results": [{"task_index": 0, "status": "completed", "summary": "private-result-canary"}]})
                    assert handle["status"] == "dispatched", handle
                    delegation_id = handle["delegation_id"]
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        task = async_delegation.get_durable_delegation(delegation_id)
                        if task and task["state"] == "completed":
                            break
                        time.sleep(.01)
                    assert task and task["state"] == "completed", task
                    # Native compression publication must retain its child in RAM
                    # and attach the same restrictions before any follow-up turn.
                    child = "private-compression-child"
                    db.publish_compression_child(parent_session_id=key, child_session_id=child,
                        source="cli", messages=[{"role": "user", "content": "private-compression-canary"}],
                        require_compression_lease=False)
                    assert runtime.policy(child) is policy
                    assert db.get_messages(child)[0]["content"] == "private-compression-canary"
                    record["agent"].session_id = child
                    server._sync_session_key_after_compress(sid, record)
                    assert record["session_key"] == key
            if mode == "temporary":
                # A fresh thread has no inherited policy. Native lifecycle/poller
                # entry must bind it from the session before recovery claims open a DB.
                def deliver_from_native_scope():
                    with server._session_profile_runtime_scope(record):
                        with contextlib.closing(async_delegation._connect()) as jobs:
                            assert jobs.execute("PRAGMA database_list").fetchone()[2] == ""
                        event = {"type": "async_delegation", "delegation_id": delegation_id,
                                 "parent_session_id": key, "session_key": key}
                        claim = async_delegation.claim_event_delivery(event, "isolated-smoke")
                        assert claim
                        async_delegation.complete_event_delivery(event, claim)
                        assert async_delegation.claim_event_delivery(event, "again") is None
                with ThreadPoolExecutor(max_workers=1) as executor:
                    executor.submit(deliver_from_native_scope).result(timeout=10)
                rpc("control.session.renew", session_id=key)
                assert rpc("control.chat_modes")["activeTemporary"] == 1
                rpc("control.session.close", session_id=key)
                rpc("control.session.close", session_id=key)
                assert sid not in server._sessions and policy.closed
                assert not staged.exists()
                assert key not in runtime.databases
                assert rpc("control.chat_modes")["activeTemporary"] == 0
                assert async_delegation._persist_completion({"delegation_id": delegation_id,
                    "parent_session_id": key, "session_key": key}, {"summary": "private-late-canary"}) is False
                for path in home.rglob("*.db"):
                    with sqlite3.connect(path) as persisted:
                        dump = "\n".join(persisted.iterdump())
                        for canary in ("private-transcript-canary", "private-goal-canary", "private-result-canary",
                                       "private-compression-canary", "private-late-canary"):
                            assert canary not in dump, (canary, path)
            else:
                server._close_session_by_id(sid)
        assert memory.read_text() == "shared-memory-canary"
        print("Native chat policy verified: shared recall, blocked memory writes, volatile transcript/jobs, native worker/compression/delivery, upload cleanup, revoked lease and late completion.")


if __name__ == "__main__":
    main()
