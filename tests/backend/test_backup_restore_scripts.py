from __future__ import annotations

import base64
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from hermes_control_api.database import Base
from hermes_control_api.integrations import UserIntegrationService
from hermes_control_api.models import User, UserIntegration
from hermes_control_api.security import SecretVault


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
def test_user_integration_ciphertext_restores_with_separate_owner_bound_vault_key(
    tmp_path,
):
    source = tmp_path / "live-control.db"
    destination = tmp_path / "restored-control.db"
    backups = tmp_path / "database-backups"
    quarantine = tmp_path / "restore-quarantine"
    external_key_path = tmp_path / "external-secret-store" / "vault-key.b64"
    external_key_path.parent.mkdir(mode=0o700)
    vault_key = b"v" * 32
    external_key_path.write_text(
        base64.urlsafe_b64encode(vault_key).decode("ascii"),
        encoding="ascii",
    )
    external_key_path.chmod(0o600)
    owner_id = "11111111-1111-4111-8111-111111111111"
    api_key = "sk_backup_restore_1234567890_private"
    aad = f"user-integration:{owner_id}:elevenlabs:api-key"
    vault = SecretVault(vault_key)
    ciphertext = vault.encrypt(api_key, aad=aad)
    assert ciphertext is not None

    engine = create_engine(f"sqlite:///{source}")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(
            User(
                id=owner_id,
                username="backup-owner",
                password_hash="not-used-by-this-restore-drill",
                is_admin=True,
            )
        )
        db.add(
            UserIntegration(
                owner_id=owner_id,
                provider="elevenlabs",
                api_key_ciphertext=ciphertext,
            )
        )
        db.commit()
    engine.dispose()

    backup_result = subprocess.run(
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
    artifact = Path(backup_result.stdout.strip())
    artifact_bytes = artifact.read_bytes()
    assert api_key.encode() not in artifact_bytes
    assert base64.urlsafe_b64encode(vault_key) not in artifact_bytes
    assert external_key_path.parent != artifact.parent

    subprocess.run(
        [
            "bash",
            str(RESTORE),
            "--control-stopped",
            str(artifact),
            str(destination),
            str(quarantine),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    restored_key = base64.urlsafe_b64decode(external_key_path.read_text(encoding="ascii"))
    restored_vault = SecretVault(restored_key)
    restored_engine = create_engine(f"sqlite:///{destination}")
    with Session(restored_engine) as db:
        owner = db.get(User, owner_id)
        row = db.scalar(select(UserIntegration))
        assert owner is not None and row is not None
        assert row.api_key_ciphertext == ciphertext
        assert UserIntegrationService(restored_vault).api_key(db, owner) == api_key
        with pytest.raises(ValueError):
            restored_vault.decrypt(
                row.api_key_ciphertext,
                aad="user-integration:another-owner:elevenlabs:api-key",
            )
    restored_engine.dispose()


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
