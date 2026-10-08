import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from agent_control_connector.chat_modes_install import install_profile, plugin_source
from agent_control_connector.hermes_chat_policy import (
    ChatPolicy, ReadOnlyMemoryStore, VolatileSqlite, constrain_agent, current_policy, policy_scope,
    _private_upload_directory,
)
from agent_control_connector.storage import OperationLedger


def test_memory_readonly_preserves_reads_and_denies_mutation():
    original = SimpleNamespace(memory_entries=["remembered"], format_for_system_prompt=lambda target: "remembered")
    store = ReadOnlyMemoryStore(original)
    assert store.format_for_system_prompt("memory") == "remembered"
    store.memory_entries.append("not shared")
    assert original.memory_entries == ["remembered"]
    assert store.add("memory", "private") == {"success": False, "error": "CHAT_MEMORY_READ_ONLY"}
    assert store.apply_batch([])["success"] is False


def test_policy_is_scoped_to_concurrent_agent_instances():
    def apply(mode):
        policy = ChatPolicy(mode, mode)
        agent = SimpleNamespace(tools=[{"function": {"name": "memory"}}, {"function": {"name": "web_search"}}],
                                _memory_store=SimpleNamespace(memory_entries=["remembered"]), _memory_manager=None)
        with policy_scope(policy):
            constrain_agent(agent, policy)
            assert current_policy() is policy
        assert current_policy() is None
        return agent
    with ThreadPoolExecutor(max_workers=3) as executor:
        regular, readonly, temporary = list(executor.map(apply, ["memory_read_write", "memory_read_only", "temporary"]))
    assert len(regular.tools) == 2
    assert len(readonly.tools) == len(temporary.tools) == 1
    assert isinstance(readonly._memory_store, ReadOnlyMemoryStore)
    assert temporary._persist_disabled and not temporary.save_trajectories


def test_volatile_store_shared_connections_and_revocation(tmp_path):
    policy = ChatPolicy("temporary", "ac_tmp_test")
    store = VolatileSqlite(policy)
    first, second = store.connect(), store.connect()
    first.execute("CREATE TABLE messages(content TEXT)")
    first.execute("INSERT INTO messages VALUES('private canary')")
    first.commit()
    assert second.execute("SELECT content FROM messages").fetchone()[0] == "private canary"
    assert first.execute("PRAGMA database_list").fetchone()[2] == ""
    with pytest.raises(sqlite3.DatabaseError):
        second.execute("ATTACH DATABASE ? AS leaked", (str(tmp_path / "leaked.sqlite"),))
    assert not (tmp_path / "leaked.sqlite").exists()
    policy.close()
    with pytest.raises(RuntimeError, match="TEMPORARY_CHAT_ENDED"):
        store.connect()
    with pytest.raises(sqlite3.ProgrammingError):
        second.execute("SELECT 1")


def test_expiry_cannot_be_renewed_after_deadline(monkeypatch):
    policy = ChatPolicy("temporary", "ac_tmp_expired")
    monkeypatch.setattr("agent_control_connector.hermes_chat_policy.time.monotonic", lambda: policy.deadline + 1)
    with pytest.raises(RuntimeError, match="TEMPORARY_CHAT_ENDED"):
        policy.renew()


def test_private_uploads_ignore_retained_scratch_and_aliases(tmp_path, monkeypatch):
    import tempfile

    home = tmp_path / "hermes"
    profile = home / "profiles/personal"
    scratch = profile / "cache/scratch"
    scratch.mkdir(parents=True)
    alias = tmp_path / "outside-looking-alias"
    alias.symlink_to(scratch, target_is_directory=True)
    for key in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(key, str(alias))
    monkeypatch.setattr(tempfile, "tempdir", str(alias))
    policy = ChatPolicy("temporary", "ac_tmp_upload")
    directory = _private_upload_directory(home, profile)
    policy.cleanup.append(directory.cleanup)
    path = Path(directory.name) / "upload.txt"
    path.write_text("private-upload-canary")
    assert not path.resolve().is_relative_to(home.resolve())
    assert Path(directory.name).stat().st_mode & 0o777 == 0o700
    assert not list(scratch.iterdir())
    assert tempfile.tempdir == str(alias)
    policy.close()
    assert not path.exists()


def test_private_uploads_fail_closed_without_an_external_root():
    with pytest.raises(RuntimeError, match="TEMPORARY_CHAT_UPLOAD_UNAVAILABLE"):
        _private_upload_directory(Path("/"))


def test_private_operation_receipts_never_create_a_file(tmp_path):
    ledger = OperationLedger(None)
    assert ledger.reserve("private-operation", "digest")[0] == "new"
    ledger.finish("private-operation", b"private response")
    assert ledger.lookup("private-operation", "digest") == ("completed", b"private response")
    assert ledger.db.execute("PRAGMA database_list").fetchone()[2] == ""
    ledger.discard_receipts(["private-operation"])
    assert ledger.lookup("private-operation", "digest") == ("unknown", None)
    ledger.close()


def test_install_is_idempotent_and_respects_explicit_opt_out(tmp_path):
    config = {"model": "personal-model", "plugins": {"enabled": ["personal-plugin"]}}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    install_profile(tmp_path)
    first = path.read_bytes()
    install_profile(tmp_path)
    assert path.read_bytes() == first
    assert yaml.safe_load(first)["model"] == "personal-model"
    assert (tmp_path / "plugins/agent-control-chat-modes/__init__.py").read_bytes() == plugin_source()
    config["plugins"]["disabled"] = ["agent-control-chat-modes"]
    path.write_text(yaml.safe_dump(config))
    before = path.read_bytes()
    assert install_profile(tmp_path) == {"state": "disabled"}
    assert path.read_bytes() == before

    config["plugins"].pop("disabled")
    path.write_text(yaml.safe_dump(config))
    assert install_profile(tmp_path) == {"state": "disabled"}
