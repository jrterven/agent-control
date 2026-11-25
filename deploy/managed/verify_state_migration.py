"""Native regression for the audited Hermes migrations; disposable data only."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deploy.managed import migrate_hermes_state as migration


def seed_database(home: Path, *, marker: str | None = "2", nonempty: bool = False) -> Path:
    home.mkdir(mode=0o700, parents=True)
    path = home / "state.db"
    database = sqlite3.connect(path)
    try:
        database.executescript(Path(__file__).with_name("fixtures").joinpath("hermes-state-v30.sql").read_text())
        database.execute("INSERT INTO schema_version(version) VALUES(30)")
        if marker is not None:
            database.execute("INSERT INTO state_meta(key,value) VALUES('fts_storage_version',?)", (marker,))
        database.execute("INSERT INTO sessions(id,source,started_at,title) VALUES('fixture-session','cli',1234567890,'Preserve fixture session')")
        if nonempty:
            for role, content in (("user", "Preserve the complete original text"), ("tool", "fixture tool output " * 1_000)):
                database.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES('fixture-session',?,?,1234567890)", (role, content))
        database.commit()
    finally:
        database.close()
    return path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_0234(root: Path, output: Path) -> dict:
    python, source = root / "python/bin/python3", root / "hermes"
    migration.private_directory(output)
    home = output / "schema-31"
    original = seed_database(home, nonempty=True)
    profile_db = seed_database(home / "profiles/lab")
    for path in (original, profile_db):
        with closing(sqlite3.connect(path)) as database, database:
            database.executescript("""
                UPDATE schema_version SET version=31;
                INSERT INTO state_meta(key,value) VALUES('message_uid_backfill','1');
                INSERT INTO sessions(id,source,started_at,parent_session_id,model_config)
                    VALUES('delegate-session','subagent',1234567891,'fixture-session','{"model":"fixture"}');
            """)
    before = [digest(path) for path in (original, profile_db)]
    records = migration.migrate_directory(home, output / "rehearsal", python, source, revision=migration.HERMES_0234_SHA)
    assert [digest(path) for path in (original, profile_db)] == before
    assert [(row["schema"], row["preservedRows"]["messages"]) for row in records] == [(33, 2), (33, 0)]
    migration.migrate_directory(home, output / "offline", python, source, offline=True, revision=migration.HERMES_0234_SHA)
    for path in (original, profile_db):
        assert migration.inspect(path)["schema"] == 33
        with migration.connect(path) as database:
            config = database.execute("SELECT model_config FROM sessions WHERE id='delegate-session'").fetchone()[0]
            assert json.loads(config) == {"model": "fixture", "_delegate_from": "fixture-session"}
            assert database.execute("SELECT count(*) FROM resumable_subagents").fetchone() == (0,)
    return {"status": "verified", "schema": 33, "cases": ["profile-and-launch-rehearsal", "offline-replacement"]}


def verify(root: Path, output: Path) -> dict:
    stamp = json.loads((root / "hermes/install-stamp.json").read_text())
    if stamp.get("commit") == migration.HERMES_0234_SHA:
        return verify_0234(root, output)
    if stamp.get("commit") != migration.HERMES_SHA:
        raise ValueError("Native fixture requires an audited Hermes build")
    python, source = root / "python/bin/python3", root / "hermes"
    migration.private_directory(output)
    results = []
    for name, marker, nonempty in (("empty-fresh", None, False), ("empty-fts1", "1", False), ("empty-stamped", "2", False), ("nonempty", "2", True)):
        home = output / name
        original = seed_database(home, marker=marker, nonempty=nonempty)
        before = digest(original)
        records = migration.migrate_directory(home, output / (name + "-evidence"), python, source)
        assert records[0]["attempts"] == (1 if nonempty else 2), records
        assert digest(original) == before, "Rehearsal modified its original database"
        assert records[0]["preservedRows"]["messages"] == (2 if nonempty else 0)
        results.append({"case": name, "attempts": records[0]["attempts"]})

    # Even when native DDL reached schema 31, an unrecognized failure cannot
    # trigger the second-open workaround.
    home = output / "unknown-error"
    original = seed_database(home)
    before = digest(original)
    real_open = migration.open_database
    calls = []
    def unknown(*args):
        calls.append(1)
        result = real_open(*args)
        return subprocess.CompletedProcess(result.args, 1, result.stdout, result.stderr + b"\nUnknown migration error\n")
    migration.open_database = unknown
    try:
        try:
            migration.migrate_directory(home, output / "unknown-error-evidence", python, source)
        except ValueError as error:
            assert "Unknown migration failure" in str(error), error
        else:
            raise AssertionError("Unknown migration failure was retried")
    finally:
        migration.open_database = real_open
    assert len(calls) == 1 and digest(original) == before
    results.append({"case": "unknown-error", "attempts": 1, "rejected": True})
    return {"status": "verified", "cases": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = args.output or Path(tempfile.mkdtemp(prefix="hermes-migration-native-")) / "results"
    print(json.dumps(verify(args.runtime_root.resolve(strict=True), output.resolve())))


if __name__ == "__main__":
    main()
