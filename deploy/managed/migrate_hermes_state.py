"""Operator-only Hermes 0.21.6 migration, with immutable before-images.

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
OPEN_DATABASE = "from pathlib import Path; import sys; from hermes_state import SessionDB; db=SessionDB(db_path=Path(sys.argv[1])); db.close()"


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


def verify_source(source: Path) -> None:
    """A rehearsal checkout must be the exact clean commit, with no extra code."""
    def git(*arguments):
        return subprocess.run(["git", "-C", str(source), *arguments], check=True,
                              capture_output=True, text=True, timeout=30).stdout.strip()
    if git("rev-parse", "HEAD") != HERMES_SHA or git("status", "--porcelain", "--untracked-files=all") or git("clean", "-ndx"):
        raise ValueError("Rehearsal requires the exact clean Hermes 818c13be source checkout")


def verify_runtime(root: Path) -> tuple[Path, Path]:
    # This operator utility is executed from the reviewed Control checkout or
    # its verified distribution, not imported from unverified target code.
    repo = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(repo / "packages/connector"), str(repo / "packages/hermes-client")]
    from agent_control_connector.managed_manifest import verify_runtime as verify
    manifest = verify(root)
    if manifest.get("hermesSourceSha") != HERMES_SHA or manifest.get("dataSchemaVersion") != 2:
        raise ValueError("Offline migration requires the signed Hermes 0.21.6 runtime")
    return root / "python/bin/python3", root / "hermes"


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


def verify_preserved(original: Path, migrated: Path) -> dict[str, int]:
    """Compare every original column/row, including duplicates, without logging data."""
    inspect(migrated)
    counts = {}
    with connect(original) as before, connect(migrated) as after:
        for (table,) in before.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if table.startswith(("sqlite_", "messages_fts", "messages_trigram")) or table in {"schema_version", "state_meta"}:
                continue
            columns = [row[1] for row in before.execute("PRAGMA table_info(" + quote(table) + ")")]
            query = "SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table)
            expected = Counter(_row_hash(tuple(row)) for row in before.execute(query))
            if expected != Counter(_row_hash(tuple(row)) for row in after.execute(query)):
                raise ValueError("Migration changed original rows in table " + table)
            counts[table] = sum(expected.values())
    return counts


def open_database(python: Path, source: Path, database: Path, home: Path) -> subprocess.CompletedProcess:
    with connect(database) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    if journal_mode not in {"wal", "delete"}:
        raise ValueError("Unreviewed SQLite journal mode; preserve the original database")
    # Preserve an intentional rollback journal while isolating the opener from
    # real profile credentials, plugins, cron and other configuration side effects.
    config = home / "config.yaml"
    config.write_text(json.dumps({"database": {"journal_mode": journal_mode}}))
    config.chmod(0o600)
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}}
    env.update(HOME=str(home), HERMES_HOME=str(home), PYTHONPATH=str(source),
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", HERMES_DISABLE_LAZY_INSTALLS="1")
    return subprocess.run([str(python), "-s", "-B", "-c", OPEN_DATABASE, str(database)],
                          cwd=home, env=env, capture_output=True, timeout=600)


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


def migrate_database(original: Path, target: Path, python: Path, source: Path, home: Path, logs: Path) -> dict:
    before = inspect(original)
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


def migrate_directory(input_directory: Path, output: Path, python: Path, source: Path, *, offline: bool = False) -> list[dict]:
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
    write_private(output / "migration.json", json.dumps({"sourceSha": HERMES_SHA, "offline": offline,
        "databases": [str(path.relative_to(input_directory)) for path in paths]}, indent=2))
    # Finish every before-image before migrating anything, including offline mode.
    for path in paths:
        backup(path, output / "original" / path.relative_to(input_directory))
    for path in paths:
        relative = path.relative_to(input_directory)
        original = output / "original" / relative
        target = path if offline else output / "migrated" / relative
        if not offline:
            backup(original, target)
        logs = output / "logs" / relative
        private_directory(logs)
        if path.name == "state.db":
            record = migrate_database(original, target, python, source, home, logs)
        else:
            record = {"integrity": "ok", "foreignKeys": "ok", "preservedRows": verify_preserved(original, target), "attempts": 0}
        record["database"] = str(relative)
        records.append(record)
    write_private(output / "verification.json", json.dumps(records, indent=2))
    return records


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
        python, source = verify_runtime(args.runtime_root.resolve(strict=True))
    elif args.python is not None and args.source is not None:
        # Keep a venv's interpreter path: resolving its symlink discards pyvenv.cfg.
        python, source = args.python.absolute(), args.source.resolve(strict=True)
        if not python.is_file() or not os.access(python, os.X_OK):
            parser.error("Rehearsal interpreter must be executable")
        verify_source(source)
    else:
        parser.error("Supply a signed runtime, or --source and --python for the exact audited Git checkout")
    records = migrate_directory(args.input, args.output, python, source, offline=args.mode == "offline")
    print(json.dumps({"status": "verified", "databases": len(records), "guardedSecondOpens": sum(record["attempts"] == 2 for record in records)}))


if __name__ == "__main__":
    main()
