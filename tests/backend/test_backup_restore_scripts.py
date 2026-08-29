from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
RESTORE = REPO / "deploy" / "bin" / "restore-sqlite.sh"
BACKUP = REPO / "deploy" / "bin" / "backup-sqlite.sh"


def _database(path: Path, value: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker VALUES (?)", (value,))


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI unavailable")
def test_online_backup_is_integral_private_and_restorable(tmp_path):
    source = tmp_path / "live-control.db"
    backups = tmp_path / "backups"
    _database(source, "online-backup")
    result = subprocess.run(
        ["bash", str(BACKUP)],
        env={
            **os.environ,
            "HERMES_CONTROL_DATABASE_PATH": str(source),
            "HERMES_CONTROL_DATABASE_URL": f"sqlite:///{source}",
            "HERMES_CONTROL_BACKUP_DIR": str(backups),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    artifact = Path(result.stdout.strip())
    assert artifact.parent == backups
    assert os.stat(artifact).st_mode & 0o777 == 0o600
    with sqlite3.connect(artifact) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT value FROM marker").fetchone() == (
            "online-backup",
        )


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI unavailable")
def test_backup_refuses_a_database_path_that_differs_from_the_runtime_url(tmp_path):
    live = tmp_path / "live.db"
    stale = tmp_path / "stale.db"
    backups = tmp_path / "backups"
    _database(live, "current")
    _database(stale, "obsolete")

    result = subprocess.run(
        ["bash", str(BACKUP)],
        env={
            **os.environ,
            "HERMES_CONTROL_DATABASE_PATH": str(stale),
            "HERMES_CONTROL_DATABASE_URL": f"sqlite:///{live}",
            "HERMES_CONTROL_BACKUP_DIR": str(backups),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "does not exactly match" in result.stderr
    assert not backups.exists()


@pytest.mark.skipif(shutil.which("sqlite3") is None, reason="sqlite3 CLI unavailable")
def test_restore_drill_validates_replaces_and_quarantines_previous_database(tmp_path):
    backup = tmp_path / "validated-backup.db"
    destination = tmp_path / "control.db"
    quarantine = tmp_path / "quarantine"
    _database(backup, "from-backup")
    _database(destination, "before-restore")
    Path(f"{destination}-wal").write_bytes(b"stale-wal")
    Path(f"{destination}-shm").write_bytes(b"stale-shm")

    result = subprocess.run(
        [
            "bash",
            str(RESTORE),
            "--control-stopped",
            str(backup),
            str(destination),
            str(quarantine),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == (
            "from-backup",
        )
    assert os.stat(destination).st_mode & 0o777 == 0o600
    quarantine_path = Path(
        next(line for line in result.stdout.splitlines() if line.startswith("quarantine="))
        .split("=", 1)[1]
    )
    assert (quarantine_path / "control.db-wal").read_bytes() == b"stale-wal"
    assert (quarantine_path / "control.db-shm").read_bytes() == b"stale-shm"
    # Move synthetic sidecars aside before SQLite opens the quarantined DB;
    # SQLite is allowed to rewrite SHM as part of recovery.
    (quarantine_path / "control.db-wal").rename(quarantine_path / "saved-wal")
    (quarantine_path / "control.db-shm").rename(quarantine_path / "saved-shm")
    with sqlite3.connect(quarantine_path / "control.db") as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == (
            "before-restore",
        )


def test_restore_requires_explicit_stopped_acknowledgement_and_absolute_paths(tmp_path):
    rejected = subprocess.run(
        ["bash", str(RESTORE), "backup.db", "control.db", "quarantine"],
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 64
    assert "--control-stopped" in rejected.stderr
