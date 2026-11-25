"""Operator-only migration onto an audited Hermes runtime, with before-images.

Rehearsal always copies databases. Offline mode requires an explicitly stopped
installation and verified source; it never stops services or releases drain.
No network, provider configuration, model invocation or upstream source patching.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

HERMES_SHA = "818c13be1dc4fd28987e1e881a9408224afd4535"
HERMES_0234_SHA = "8fc0374447313e276f6b9a2764bfc750190c966a"
TARGETS = {HERMES_SHA: (2, 31), HERMES_0234_SHA: (3, 33)}
OPEN_DATABASE = "from pathlib import Path; import sys; from hermes_state import SessionDB; db=SessionDB(db_path=Path(sys.argv[1])); db.close()"
OPEN_PERSISTENCE = "from pathlib import Path; import sys; from persistence_assembly import open_persistence, profile_scope; open_persistence(profile_scope(home=Path(sys.argv[1]).parent), eager=True).close()"


@contextmanager
def connect(path: Path):
    with closing(sqlite3.connect(path.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=10)) as database:
        yield database


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)


def write_private(path: Path, value: str | bytes) -> None:
    with path.open("xb") as file:
        os.chmod(path, 0o600)
        file.write(value.encode() if isinstance(value, str) else value)


def verify_source(source: Path) -> str:
    """A rehearsal checkout must be the exact clean commit, with no extra code."""
    def git(*arguments):
        return subprocess.run(["git", "-C", str(source), *arguments], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    revision = git("rev-parse", "HEAD")
    if revision not in TARGETS or git("status", "--porcelain", "--untracked-files=all") or git("clean", "-ndx"):
        raise ValueError("Rehearsal requires an exact, clean audited Hermes checkout")
    return revision


def verify_runtime(root: Path) -> tuple[Path, Path, str]:
    # This operator utility is executed from the reviewed Control checkout or
    # its verified distribution, not imported from unverified target code.
    repo = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repo / "packages/connector"), str(repo / "packages/hermes-client")]
    from agent_control_connector.managed_manifest import verify_runtime as verify
    manifest = verify(root)
    revision = manifest.get("hermesSourceSha")
    if revision not in TARGETS or manifest.get("dataSchemaVersion") != TARGETS[revision][0]:
        raise ValueError("Offline migration requires an audited signed Hermes runtime")
    return root / "python/bin/python3", root / "hermes", revision


def databases(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Database directory must be a real directory")
    homes = [directory]
    profiles = directory / "profiles"
    if profiles.is_symlink():
        raise ValueError("Profile directory cannot be a symlink")
    if profiles.exists():
        for profile in sorted(profiles.iterdir()):
            if profile.is_symlink():
                raise ValueError("Profile homes cannot be symlinks during migration")
            if profile.is_dir() and not profile.name.startswith("."):
                homes.append(profile)
    result = []
    for home in homes:
        for name in ("state.db", "shared-state.db"):
            path = home / name
            if path.is_symlink():
                raise ValueError("State databases cannot be symlinks")
            if any(path.with_name(path.name + suffix).is_symlink() for suffix in ("-wal", "-shm", "-journal")):
                raise ValueError("State database sidecars cannot be symlinks")
            if path.exists():
                if not path.is_file():
                    raise ValueError("State database must be a regular file")
                result.append(path)
    if not result:
        raise ValueError("No Hermes state databases were found")
    return result


def backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("A migration before-image must never be overwritten")
    with connect(source) as before, closing(sqlite3.connect(destination)) as after:
        os.chmod(destination, 0o600)
        before.backup(after)
    inspect(destination)


def inspect(path: Path) -> dict:
    with connect(path) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("SQLite foreign-key check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = db.execute("SELECT version FROM schema_version").fetchall() if "schema_version" in tables else []
        fts = db.execute("SELECT value FROM state_meta WHERE key='fts_storage_version'").fetchone() if "state_meta" in tables else None
        source = db.execute("SELECT sql FROM sqlite_master WHERE name='messages_fts' AND type='table'").fetchone()
        return {"schema": version[0][0] if len(version) == 1 else None,
                "fts": fts[0] if fts else None,
                "ftsAligned": bool(source and "content='messages_fts_src'" in source[0].replace('"', "'")),
                "ftsRawMessages": bool(source and "content='messages'" in source[0].replace('"', "'")),
                "messages": db.execute("SELECT count(*) FROM messages").fetchone()[0] if "messages" in tables else None}


def _row_hash(row: tuple) -> bytes:
    values = [[type(value).__name__, value.hex() if isinstance(value, bytes) else value] for value in row]
    return hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode()).digest()


def _session_projection(projection: list[str], columns: list[str]) -> str:
    # Hermes removes a stray marker on a /new row before it retags true
    # delegate children. Both operations change only _delegate_from.
    projection[columns.index("model_config")] = """
        CASE WHEN source='subagent' AND parent_session_id IS NOT NULL
            AND json_valid(COALESCE(healed_config,'{}'))
            AND json_extract(COALESCE(healed_config,'{}'),'$._delegate_from') IS NULL
            AND NOT EXISTS (SELECT 1 FROM sessions p WHERE p.id=base.parent_session_id
                            AND p.source='subagent' AND p.end_reason='compression')
        THEN json_set(COALESCE(healed_config,'{}'),'$._delegate_from',parent_session_id)
        ELSE healed_config END"""
    return """
        WITH base AS (SELECT s.*,
            CASE WHEN session_key IS NOT NULL AND session_key != '' AND json_valid(model_config)
                AND json_extract(model_config,'$._delegate_from') IS NOT NULL
                AND json_extract(model_config,'$._reset_from') IS NOT NULL
            THEN json_remove(model_config,'$._delegate_from') ELSE model_config END AS healed_config
        FROM sessions s)
        SELECT """ + ",".join(projection) + " FROM base"


def verify_preserved(original: Path, migrated: Path, *, target_schema: int | None = None) -> dict[str, int]:
    """Compare every original column/row, including duplicates, without logging data."""
    inspect(migrated)
    counts = {}
    with connect(original) as before, connect(migrated) as after:
        for (table,) in before.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table.startswith(("sqlite_", "messages_fts", "messages_trigram")) or table in {"schema_version", "state_meta"}:
                continue
            columns = [row[1] for row in before.execute("PRAGMA table_info(" + quote(table) + ")")]
            projection = list(map(quote, columns))
            query = "SELECT " + ",".join(projection) + " FROM " + quote(table)
            expected_query = _session_projection(projection, columns) if target_schema == 33 and table == "sessions" else query
            expected = Counter(_row_hash(tuple(row)) for row in before.execute(expected_query))
            if expected != Counter(_row_hash(tuple(row)) for row in after.execute(query)):
                raise ValueError("Migration changed original rows in table " + table)
            counts[table] = sum(expected.values())
    return counts


def open_database(python: Path, source: Path, database: Path, home: Path, *, revision=HERMES_SHA) -> subprocess.CompletedProcess:
    with connect(database) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    if journal_mode not in {"wal", "delete"}:
        raise ValueError("Unreviewed SQLite journal mode; preserve the original database")
    # Preserve an intentional rollback journal while isolating the opener from
    # real profile credentials, plugins, cron and other configuration side effects.
    native = revision == HERMES_0234_SHA
    runtime_home = database.parent if native else home
    config = runtime_home / "config.yaml"
    isolated_config = json.dumps({"database": {"journal_mode": journal_mode}})
    if native:
        write_private(config, isolated_config)
    else:
        config.write_text(isolated_config)
        config.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
    env.update(HOME=str(home), HERMES_HOME=str(runtime_home), PYTHONPATH=str(source),
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", HERMES_DISABLE_LAZY_INSTALLS="1")
    try:
        return subprocess.run([str(python), "-s", "-B", "-c", OPEN_PERSISTENCE if native else OPEN_DATABASE, str(database)],
                              cwd=runtime_home, env=env, capture_output=True, timeout=600)
    finally:
        if native:
            config.unlink()


def known_empty_failure(result: subprocess.CompletedProcess, before: dict, after: dict, source: Path) -> bool:
    error = result.stderr.decode("utf-8", errors="replace")
    expected = "sqlite3.OperationalError: no such savepoint: fts_align_empty"
    exceptions = re.findall(r"^[\w.]+(?:Error|Exception):.*$", error, flags=re.MULTILINE)
    # Fresh 0.21.2 stores omit the marker; older optimized stores retain 1.
    # All observed cases take the same empty external-content DDL branch.
    return (result.returncode == 1 and before["schema"] == 30 and before["fts"] in {None, "1", "2"}
            and before["messages"] == 0 and before["ftsRawMessages"]
            and after["schema"] == 31 and after["fts"] == "3"
            and after["messages"] == 0 and after["ftsAligned"]
            and str(source / "hermes_state_schema.py") in error
            and "in _migrate_misaligned_fts_source" in error
            and exceptions == [expected, expected]
            and error.rstrip().endswith(expected))


def migrate_database(original: Path, target: Path, python: Path, source: Path, home: Path, logs: Path, *, revision=HERMES_SHA) -> dict:
    before = inspect(original)
    if revision == HERMES_0234_SHA:
        if before["schema"] not in {31, 33}:
            raise ValueError("Hermes 0.23.4 migration requires an audited schema 31 or 33 store")
        first = open_database(python, source, target, home, revision=revision)
        write_private(logs / "attempt-1.log", first.stdout + first.stderr)
        if first.returncode:
            raise ValueError("Native migration failed; see the isolated migration log")
        after = inspect(target)
        if after["schema"] != 33 or (before["ftsAligned"] and (after["fts"] != "3" or not after["ftsAligned"])):
            raise ValueError("Migration did not settle at schema 33 with preserved FTS alignment")
        counts = verify_preserved(original, target, target_schema=33 if before["schema"] == 31 else None)
        return {"schema": 33, "fts": after["fts"], "attempts": 1,
                "integrity": "ok", "foreignKeys": "ok", "preservedRows": counts}
    if before["schema"] not in {30, 31}:
        raise ValueError("State migration only supports audited schema 30 or 31")
    first = open_database(python, source, target, home)
    write_private(logs / "attempt-1.log", first.stdout + first.stderr)
    after = inspect(target)
    counts = verify_preserved(original, target)
    attempts = 1
    if first.returncode:
        if not known_empty_failure(first, before, after, source):
            raise ValueError("Unknown migration failure; preserve the failed copy and restore the cold snapshot")
        # The exact audited empty-FTS DDL committed before its lost savepoint
        # error. One further open settles it; no retry applies to any other error.
        attempts = 2
        second = open_database(python, source, target, home)
        write_private(logs / "attempt-2.log", second.stdout + second.stderr)
        if second.returncode:
            raise ValueError("Guarded second migration open failed; restore the cold snapshot")
        after = inspect(target)
        counts = verify_preserved(original, target)
    if after["schema"] != 31:
        raise ValueError("Migration did not finish at schema 31")
    if before["ftsRawMessages"] and (after["fts"] != "3" or not after["ftsAligned"]):
        raise ValueError("Migration did not finish the FTS source transition")
    return {"schema": after["schema"], "fts": after["fts"], "attempts": attempts,
            "integrity": "ok", "foreignKeys": "ok", "preservedRows": counts}


def migrate_directory(input_directory: Path, output: Path, python: Path, source: Path, *, offline=False, revision=HERMES_SHA) -> list[dict]:
    if input_directory.is_symlink():
        raise ValueError("Database directory cannot be a symlink")
    input_directory, output = input_directory.resolve(strict=True), output.resolve()
    if output == input_directory or output.is_relative_to(input_directory):
        raise ValueError("Evidence directory must be separate from the source home")
    paths = databases(input_directory)
    private_directory(output)
    home = output / "isolated-home"
    private_directory(home)
    records = []
    write_private(output / "migration.json", json.dumps({"sourceSha": revision, "offline": offline,
        "databases": [str(path.relative_to(input_directory)) for path in paths]}, indent=2))
    # Finish every before-image before migrating anything, including offline mode.
    for path in paths:
        backup(path, output / "original" / path.relative_to(input_directory))
    for path in paths:
        relative = path.relative_to(input_directory)
        original = output / "original" / relative
        target = path if offline and revision == HERMES_SHA else output / "migrated" / relative
        if target != path:
            backup(original, target)
        logs = output / "logs" / relative
        private_directory(logs)
        if path.name == "state.db":
            record = migrate_database(original, target, python, source, home, logs, revision=revision)
        else:
            record = {"integrity": "ok", "foreignKeys": "ok", "preservedRows": verify_preserved(original, target), "attempts": 0}
        record["database"] = str(relative)
        records.append(record)
    if offline and revision == HERMES_0234_SHA:
        # Settle and verify every clone before replacing any live pages. Use
        # SQLite backup rather than renaming a file beside stale WAL sidecars.
        for path in paths:
            if path.name == "state.db":
                migrated = output / "migrated" / path.relative_to(input_directory)
                install_database(migrated, path)
    write_private(output / "verification.json", json.dumps(records, indent=2))
    return records


def install_database(migrated: Path, target: Path) -> None:
    import time

    deadline = time.monotonic() + 600
    def progress(status, *_):
        if status != sqlite3.SQLITE_DONE and time.monotonic() >= deadline:
            raise TimeoutError("Could not install the verified database during the offline window")

    target_uri = target.resolve(strict=True).as_uri() + "?mode=rw"
    with connect(migrated) as source, closing(sqlite3.connect(target_uri, uri=True, timeout=10)) as destination:
        source.backup(destination, pages=4096, progress=progress)
    if inspect(target)["schema"] != 33:
        raise ValueError("Installed database did not pass the schema integrity check; retain the cold backup")
    verify_preserved(migrated, target)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("rehearse", "offline"))
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--offline-confirmed", action="store_true", help="Operator has drained/stopped all writers and retained the full cold home/identity backup")
    args = parser.parse_args()
    if args.mode == "offline" and not args.offline_confirmed:
        parser.error("Offline writes require --offline-confirmed after stopping writers and taking a complete cold backup")
    if args.runtime_root is not None:
        python, source, revision = verify_runtime(args.runtime_root.resolve(strict=True))
    elif args.python is not None and args.source is not None:
        # Keep a venv's interpreter path: resolving its symlink discards pyvenv.cfg.
        python, source = args.python.absolute(), args.source.resolve(strict=True)
        if not python.is_file() or not os.access(python, os.X_OK):
            parser.error("Rehearsal interpreter must be executable")
        revision = verify_source(source)
    else:
        parser.error("Supply a signed runtime, or --source and --python for the exact audited Git checkout")
    records = migrate_directory(args.input, args.output, python, source, offline=args.mode == "offline", revision=revision)
    print(json.dumps({"status": "verified", "databases": len(records), "guardedSecondOpens": sum(record["attempts"] == 2 for record in records)}))


if __name__ == "__main__":
    main()
