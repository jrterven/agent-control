from pathlib import Path
import sqlite3
import subprocess

import pytest

from deploy.managed import migrate_hermes_state as migration
from deploy.managed.verify_state_migration import seed_database


@pytest.mark.parametrize("field,value", [("schema", 29), ("messages", 1), ("fts", "0"), ("ftsRawMessages", False)])
def test_known_savepoint_retry_requires_every_original_state_guard(field, value):
    before = {"schema": 30, "messages": 0, "fts": "2", "ftsRawMessages": True}
    after = {"schema": 31, "messages": 0, "fts": "3", "ftsAligned": True}
    source = Path("/audited")
    result = subprocess.CompletedProcess([], 1, b"", b'/audited/hermes_state_schema.py in _migrate_misaligned_fts_source\nsqlite3.OperationalError: no such savepoint: fts_align_empty\n\nsqlite3.OperationalError: no such savepoint: fts_align_empty\n')
    assert migration.known_empty_failure(result, before, after, source)
    before[field] = value
    assert not migration.known_empty_failure(result, before, after, source)


def test_savepoint_wrapper_never_hides_an_unknown_original_error():
    before = {"schema": 30, "messages": 0, "fts": "2", "ftsRawMessages": True}
    after = {"schema": 31, "messages": 0, "fts": "3", "ftsAligned": True}
    result = subprocess.CompletedProcess([], 1, b"", b'/audited/hermes_state_schema.py in _migrate_misaligned_fts_source\nsqlite3.OperationalError: database or disk is full\n\nsqlite3.OperationalError: no such savepoint: fts_align_empty\n')
    assert not migration.known_empty_failure(result, before, after, Path("/audited"))


def test_rehearsal_refuses_modified_original_values_before_retry(tmp_path, monkeypatch):
    home = tmp_path / "input"
    seed_database(home)
    calls = []
    def alter(_python, _source, target, _isolated):
        calls.append(1)
        with sqlite3.connect(target) as db:
            db.execute("UPDATE sessions SET title='unexpected data change'")
        return subprocess.CompletedProcess([], 1, b"", b"sqlite3.OperationalError: no such savepoint: fts_align_empty")
    monkeypatch.setattr(migration, "open_database", alter)
    with pytest.raises(ValueError, match="original rows"):
        migration.migrate_directory(home, tmp_path / "evidence", Path("/python"), Path("/source"))
    assert calls == [1]
    with sqlite3.connect(home / "state.db") as db:
        assert db.execute("SELECT title FROM sessions").fetchone() == ("Preserve fixture session",)
    assert not (tmp_path / "evidence/verification.json").exists()


def test_rehearsal_refuses_foreign_key_damage_without_running_upstream(tmp_path, monkeypatch):
    home = tmp_path / "input"
    path = seed_database(home)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO messages(session_id,role,timestamp) VALUES('missing','user',1)")
    monkeypatch.setattr(migration, "open_database", lambda *_: pytest.fail("Corrupt input must not run upstream"))
    with pytest.raises(ValueError, match="foreign-key"):
        migration.migrate_directory(home, tmp_path / "evidence", Path("/python"), Path("/source"))


@pytest.mark.parametrize("name", ["state.db", "state.db-wal"])
def test_rehearsal_refuses_database_and_sidecar_symlinks(tmp_path, name):
    home = tmp_path / "input"
    home.mkdir()
    (home / name).symlink_to(tmp_path / "foreign")
    with pytest.raises(ValueError, match="symlink"):
        migration.databases(home)
