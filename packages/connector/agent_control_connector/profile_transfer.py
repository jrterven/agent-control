"""Profile-bound archive staging for the typed connector transfer protocol.

Only native Hermes export/import can touch profiles. This module owns private
staging files; cloud callers choose opaque identities, never local paths.
"""
from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import tarfile
from typing import Any
import httpx

from hermes_client.compatibility import HERMES_0212_SHA
from hermes_client.types import HermesProfile

from .storage import atomic_json, private_dir, read_json

MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_STAGES = 4
MAX_STAGE_RECEIPTS = 10_000
TRANSFER_OPERATIONS = frozenset({
    "profile_export", "profile_archive_read", "profile_import_begin",
    "profile_archive_write", "profile_import_finish", "profile_archive_cleanup",
})
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
ACTIVE = frozenset({"pending", "queued", "accepted", "starting", "streaming", "running", "working", "waiting", "redirected", "steered"})
# Hermes already excludes .env/auth.json; additionally keep provider-specific
# credential documents out of the cloud and reject them in inbound archives.
CREDENTIAL_FILES = frozenset({".env", "auth.json", ".anthropic_oauth.json", "credentials.json"})


class ProfileImportRefused(ValueError):
    """Native profile creation was definitely not dispatched or was refused."""


def _credential_path(parts) -> bool:
    return any(part.casefold() in CREDENTIAL_FILES or part.casefold().startswith(".env.") for part in parts)


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("Invalid transfer file")
    return info


class _BoundedGzip:
    """Bound decompression, including tar headers parsed before yielding members."""
    def __init__(self, source):
        self.source = source
        self.limit = MAX_EXPANDED_BYTES + MAX_MEMBERS * 4096

    def tell(self):
        return self.source.tell()

    def read(self, size=-1):
        if size < 0 or size > self.limit - self.tell():
            raise ValueError("Expanded profile archive exceeds limit")
        return self.source.read(size)

    def seek(self, offset, whence=0):
        target = offset if whence == 0 else self.tell() + offset if whence == 1 else -1
        if not 0 <= target <= self.limit:
            raise ValueError("Expanded profile archive exceeds limit")
        return self.source.seek(target)


@contextlib.contextmanager
def _open_archive(path):
    with gzip.open(path, "rb") as source, tarfile.open(fileobj=_BoundedGzip(source), mode="r:") as archive:
        yield archive


def _archive_members(archive: tarfile.TarFile, name: str):
    expanded = 0
    count = 0
    seen = set()
    for member in archive:
        count += 1
        path = PurePosixPath(member.name)
        windows = PureWindowsPath(member.name)
        if (count > MAX_MEMBERS or not member.name or "\\" in member.name or path.is_absolute()
                or windows.drive or ".." in path.parts or not path.parts or path.parts[0] != name
                or path.parts in seen or not (member.isdir() or member.isfile()) or member.size < 0
                or (len(path.parts) == 1 and not member.isdir())):
            raise ValueError("Invalid profile archive")
        seen.add(path.parts)
        expanded += member.size
        if expanded > MAX_EXPANDED_BYTES:
            raise ValueError("Expanded profile archive exceeds limit")
        yield member, path
    if not count:
        raise ValueError("Empty profile archive")


def validate_archive(path: Path, name: str) -> None:
    if not 0 < _regular(path).st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid profile archive size")
    try:
        with _open_archive(path) as archive:
            for member, relative in _archive_members(archive, name):
                if _credential_path(relative.parts[1:]):
                    raise ValueError("Profile archive contains credentials")
                # Reading every member detects truncation before native import.
                if member.isfile():
                    with archive.extractfile(member) as source:
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(remaining, MAX_CHUNK_BYTES))
                            if not chunk:
                                raise ValueError("Truncated profile archive")
                            remaining -= len(chunk)
    except (tarfile.TarError, EOFError) as error:
        raise ValueError("Invalid profile archive") from error


def sanitize_export(source: Path, destination: Path, name: str) -> None:
    if not 0 < _regular(source).st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError("Invalid profile archive size")
    try:
        with destination.open("xb") as output:
            destination.chmod(0o600)
            with _open_archive(source) as archive, tarfile.open(fileobj=output, mode="w:gz") as target:
                for member, relative in _archive_members(archive, name):
                    if _credential_path(relative.parts[1:]):
                        continue
                    member.mode &= 0o777
                    if member.isfile():
                        with archive.extractfile(member) as content:
                            target.addfile(member, content)
                    else:
                        target.addfile(member)
            output.flush()
            os.fsync(output.fileno())
        validate_archive(destination, name)
    except (tarfile.TarError, EOFError) as error:
        raise ValueError("Invalid profile archive") from error


def _digest(path: Path) -> str:
    _regular(path)
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


class ProfileTransfers:
    def __init__(self, runtime):
        self.runtime = runtime
        self.root = runtime.directory / "profile-transfers"
        self.lock = asyncio.Lock()

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not NAME.fullmatch(name) or name == "default":
            raise ValueError("Invalid transfer profile")

    def _path(self, transfer_id: str) -> Path:
        if not isinstance(transfer_id, str) or not IDENTIFIER.fullmatch(transfer_id):
            raise ValueError("Invalid transfer identity")
        private_dir(self.root)
        path = self.root / transfer_id
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Invalid transfer directory")
        return path

    def _load(self, manager: str, transfer_id: str) -> tuple[Path, dict]:
        path = self._path(transfer_id)
        meta = read_json(path / "meta.json")
        if (meta.get("gateway") != self.runtime.gateway_id or meta.get("manager") != manager
                or meta.get("transferId") != transfer_id):
            raise ValueError("Transfer is not owned by this profile")
        self._name(meta.get("name"))
        return path, meta

    def _create(self, manager: str, transfer_id: str, name: str, direction: str) -> tuple[Path, dict]:
        self._name(name)
        path = self._path(transfer_id)
        entries = list(self.root.iterdir())
        if len(entries) >= MAX_STAGE_RECEIPTS:
            raise ValueError("Transfer receipt storage is full")
        active = sum(read_json(entry / "meta.json").get("state") != "cleaned" for entry in entries)
        if active >= MAX_STAGES:
            raise ValueError("Too many staged transfers")
        path.mkdir(mode=0o700)  # existing identities are never reused
        meta = {"gateway": self.runtime.gateway_id, "manager": manager, "transferId": transfer_id,
                "name": name, "direction": direction, "state": "preparing", "offset": 0}
        atomic_json(path / "meta.json", meta)
        return path, meta

    @staticmethod
    def _receipt(meta: dict) -> dict[str, Any]:
        return {key: meta[key] for key in ("transferId", "size", "sha256", "offset")}

    async def check_export_idle(self, name: str):
        self._name(name)
        provider = self.runtime.providers.get(name)
        if provider is None:
            raise ValueError("Profile is not locally shared")
        sessions = await provider.list_sessions()
        if not provider.session_inventory_complete or any(session.status.casefold() in ACTIVE for session in sessions):
            raise ValueError("Profile has active or uncertain work")
        if self.runtime.config.get("sourceSha") == HERMES_0212_SHA:
            evidence = await asyncio.to_thread(self.runtime._background_snapshot, name)
            if (evidence.get("complete") is not True or type(evidence.get("activeCount")) is not int
                    or type(evidence.get("pendingDeliveryCount")) is not int or evidence["activeCount"] != 0
                    or evidence["pendingDeliveryCount"] != 0):
                raise ValueError("Profile has active or uncertain background work")

    async def execute(self, operation: str, manager: str, args: tuple, kwargs: dict):
        if operation not in TRANSFER_OPERATIONS:
            raise ValueError("Unknown profile transfer operation")
        async with self.lock:
            return await getattr(self, operation)(manager, *args, **kwargs)

    async def profile_export(self, manager: str, name: str, transfer_id: str) -> dict[str, Any]:
        await self.check_export_idle(name)
        provider = self.runtime.providers[manager]
        path, meta = self._create(manager, transfer_id, name, "export")
        await provider.export_profile_archive_to(name, path / "native.tar.gz")
        await asyncio.to_thread(sanitize_export, path / "native.tar.gz", path / "archive.tar.gz", name)
        (path / "native.tar.gz").unlink()
        meta.update(state="ready", size=(path / "archive.tar.gz").stat().st_size,
                    sha256=await asyncio.to_thread(_digest, path / "archive.tar.gz"))
        atomic_json(path / "meta.json", meta)
        return self._receipt(meta)

    async def profile_archive_read(self, manager: str, transfer_id: str, offset: int, length: int) -> bytes:
        path, meta = self._load(manager, transfer_id)
        if (meta["direction"] != "export" or meta["state"] != "ready" or type(offset) is not int
                or type(length) is not int or offset < 0 or not 0 < length <= MAX_CHUNK_BYTES
                or offset + length > meta["size"]):
            raise ValueError("Invalid archive read")
        archive = path / "archive.tar.gz"
        if _regular(archive).st_size != meta["size"]:
            raise ValueError("Transfer archive changed")
        with archive.open("rb") as source:
            source.seek(offset)
            result = source.read(length)
        if len(result) != length:
            raise ValueError("Transfer archive changed")
        return result

    async def profile_import_begin(self, manager: str, name: str, transfer_id: str, size: int, sha256: str) -> dict[str, Any]:
        self._name(name)
        if (type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES or not isinstance(sha256, str)
                or not SHA256.fullmatch(sha256) or len(self.runtime.providers) >= 64):
            raise ValueError("Invalid profile archive metadata")
        # Native list is intentionally unfiltered; an unshared agent is private.
        if name in {item.name for item in await self.runtime.providers[manager].list_profiles()}:
            raise ValueError("Destination profile already exists")
        path, meta = self._create(manager, transfer_id, name, "import")
        with (path / "archive.tar.gz").open("xb"):
            (path / "archive.tar.gz").chmod(0o600)
        meta.update(state="receiving", size=size, sha256=sha256)
        atomic_json(path / "meta.json", meta)
        return self._receipt(meta)

    async def profile_archive_write(self, manager: str, transfer_id: str, offset: int, chunk: bytes) -> dict[str, Any]:
        path, meta = self._load(manager, transfer_id)
        if (meta["direction"] != "import" or meta["state"] != "receiving" or type(offset) is not int
                or offset != meta["offset"] or not isinstance(chunk, bytes) or not 0 < len(chunk) <= MAX_CHUNK_BYTES
                or offset + len(chunk) > meta["size"]):
            raise ValueError("Invalid archive write")
        archive = path / "archive.tar.gz"
        if _regular(archive).st_size != offset:
            raise ValueError("Archive write outcome is uncertain")
        with archive.open("ab") as output:
            output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        meta["offset"] += len(chunk)
        atomic_json(path / "meta.json", meta)
        return self._receipt(meta)

    async def profile_import_finish(self, manager: str, transfer_id: str) -> HermesProfile:
        path, meta = self._load(manager, transfer_id)
        if meta["direction"] != "import":
            raise ProfileImportRefused("Invalid import stage")
        if meta["state"] == "imported":
            # An owned, committed import can be reconciled without native mutation.
            return HermesProfile(name=meta["name"], display_name=meta["name"], status="unknown")
        if meta["state"] == "importing":
            raise ValueError("Import outcome is incomplete or uncertain")
        try:
            if meta["state"] != "receiving" or meta["offset"] != meta["size"]:
                raise ValueError("Import is incomplete")
            archive = path / "archive.tar.gz"
            if _regular(archive).st_size != meta["size"] or await asyncio.to_thread(_digest, archive) != meta["sha256"]:
                raise ValueError("Profile archive checksum mismatch")
            await asyncio.to_thread(validate_archive, archive, meta["name"])
            if meta["name"] in {item.name for item in await self.runtime.providers[manager].list_profiles()}:
                raise ValueError("Destination profile already exists")
        except (ValueError, OSError) as error:
            raise ProfileImportRefused("Profile import was refused before dispatch") from error
        meta["state"] = "importing"
        atomic_json(path / "meta.json", meta)  # crashes never repeat native import
        try:
            result = await self.runtime.providers[manager].import_profile_archive_from(meta["name"], archive)
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {400, 409}:
                meta["state"] = "refused"
                atomic_json(path / "meta.json", meta)
                raise ProfileImportRefused("Hermes refused the import") from error
            raise
        if result.name != meta["name"]:
            raise ValueError("Unexpected imported profile")
        meta["state"] = "imported"
        atomic_json(path / "meta.json", meta)
        return result

    async def profile_archive_cleanup(self, manager: str, transfer_id: str) -> None:
        path = self._path(transfer_id)
        if not path.exists():
            return
        path, meta = self._load(manager, transfer_id)
        # Keep tiny ownership/uncertainty receipts after cleanup; payloads alone
        # are disposable. They let an operator reconcile a lost import outcome.
        for name in ("native.tar.gz", "archive.tar.gz"):
            file = path / name
            if file.exists() or file.is_symlink():
                _regular(file)
                file.unlink()
        if meta["state"] != "importing":
            meta["state"] = "cleaned"
            atomic_json(path / "meta.json", meta)
