from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any


def private_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise ValueError("Connector directory must be owned by the current user")
    path.chmod(0o700)


def atomic_json(path: Path, value: Any):
    private_dir(path.parent)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, separators=(",", ":"))
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def read_json(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 64 * 1024 or path.stat().st_mode & 0o077:
        raise ValueError("Connector file must be private, regular, and bounded")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Invalid connector state")
    return value


class SecretStore:
    def __init__(self, directory: Path):
        private_dir(directory)
        self.directory = directory
        self.service = "com.agent-control.connector." + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]

    def save(self, secrets: dict[str, str]):
        if sys.platform == "darwin":
            from .keychain import MacKeychain
            MacKeychain().save(self.service, str(os.getuid()), json.dumps(secrets).encode())
        else:
            atomic_json(self.directory / "secrets.json", secrets)

    def load(self) -> dict[str, str]:
        if sys.platform == "darwin":
            from .keychain import MacKeychain
            return json.loads(MacKeychain().load(self.service, str(os.getuid())))
        return read_json(self.directory / "secrets.json")

    def delete(self):
        if sys.platform == "darwin":
            from .keychain import MacKeychain
            MacKeychain().delete(self.service, str(os.getuid()))
        else:
            (self.directory / "secrets.json").unlink(missing_ok=True)


class OperationLedger:
    """Reserve before dispatch. An interrupted reservation is never executed again."""
    MAX_OPERATIONS = 100_000
    MAX_RECEIPT_BYTES = 64 * 1024
    MAX_RECEIPTS_BYTES = 64 * 1024 * 1024
    MAX_DATABASE_BYTES = 128 * 1024 * 1024

    def __init__(self, directory: Path):
        private_dir(directory)
        path = directory / "operations.sqlite3"
        if path.is_symlink():
            raise ValueError("Invalid operation ledger")
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        self.db.execute(f"PRAGMA max_page_count={self.MAX_DATABASE_BYTES // page_size}")
        self.db.execute("PRAGMA wal_autocheckpoint=256")
        self.db.execute("PRAGMA journal_size_limit=4194304")
        self.db.execute("CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, result BLOB)")
        # A crashed process may have sent the order. Preserve the uncertainty.
        self.db.execute("UPDATE operations SET state='unknown' WHERE state='running'")
        self.db.commit()
        self.receipt_bytes = self.db.execute("SELECT coalesce(sum(length(result)),0) FROM operations").fetchone()[0]

    def lookup(self, key: str, digest: str) -> tuple[str, bytes | None] | None:
        """Read an existing identity before a fresh-operation safety preflight."""
        row = self.db.execute("SELECT digest,state,result FROM operations WHERE key=?", (key,)).fetchone()
        if row:
            if row[0] != digest:
                return "conflict", None
            return row[1], row[2]
        return None

    def reserve(self, key: str, digest: str) -> tuple[str, bytes | None]:
        previous = self.lookup(key, digest)
        if previous is not None:
            return previous
        if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= self.MAX_OPERATIONS:
            return "full", None
        try:
            self.db.execute("INSERT INTO operations(key,digest,state) VALUES (?,?,'running')", (key, digest))
            self.db.commit()
        except sqlite3.DatabaseError:
            self.db.rollback()
            return "full", None
        return "new", None

    def finish(self, key: str, result: bytes):
        # Keep all operation identities permanently; a full ledger fails closed
        # before dispatch. Receipts have independent per-row and aggregate caps.
        previous = self.db.execute("SELECT coalesce(length(result),0) FROM operations WHERE key=?", (key,)).fetchone()
        previous_bytes = previous[0] if previous else 0
        retained = len(result) <= self.MAX_RECEIPT_BYTES and self.receipt_bytes - previous_bytes + len(result) <= self.MAX_RECEIPTS_BYTES
        try:
            if retained:
                self.db.execute("UPDATE operations SET state='completed',result=? WHERE key=?", (result, key))
            else:
                self.db.execute("UPDATE operations SET state='unknown',result=NULL WHERE key=?", (key,))
            self.db.commit()
            self.receipt_bytes += (len(result) if retained else 0) - previous_bytes
        except sqlite3.DatabaseError:
            # The pre-dispatch reservation remains durable even when the disk
            # fills while recording a receipt. Never release it for a retry.
            self.db.rollback()

    def close(self):
        self.db.close()
