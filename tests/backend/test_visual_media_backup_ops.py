"""Off-host snapshots fail closed before publishing a completion receipt."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("r2_backup", ROOT / "deploy/cloud/r2_backup.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def archive(path, members):
    with tarfile.open(path, "w") as output:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            output.addfile(info, io.BytesIO(data) if data else None)


def test_image_archive_restore_accepts_flat_manifest_and_rejects_traversal_links_duplicates(tmp_path):
    manifest = ("./manifest.json", b'{"version":1,"assets":[]}', tarfile.REGTYPE)
    valid = tmp_path / "valid.tar"
    archive(valid, [manifest])
    backup.unpack_images(valid, tmp_path / "restored")
    assert json.loads((tmp_path / "restored/manifest.json").read_text())["assets"] == []
    for index, member in enumerate([
        ("../secret", b"x", tarfile.REGTYPE),
        ("manifest.json", b"", tarfile.SYMTYPE),
        manifest,
    ]):
        invalid = tmp_path / f"invalid-{index}.tar"
        archive(invalid, [manifest, member])
        with pytest.raises(backup.BackupError, match="unsafe_image_archive"):
            backup.unpack_images(invalid, tmp_path / f"invalid-{index}")
    assert not (tmp_path / "secret").exists()


def test_private_credentials_and_archives_require_owner_only_regular_files(tmp_path):
    path = tmp_path / "private.json"
    path.write_text("{}")
    path.chmod(0o600)
    with backup.private_file(path) as source:
        assert backup.digest(source)[1] == 2
    path.chmod(0o644)
    with pytest.raises(backup.BackupError, match="unsafe_file"):
        backup.private_file(path)
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        backup.private_file(alias)


def test_backup_object_must_have_verified_thirty_day_retention():
    class Client:
        def __init__(self, days):
            self.days = days
        def get_bucket_lifecycle_configuration(self, **_):
            return {"Rules": [{"Status": "Enabled", "Filter": {"Prefix": "prod/"}, "Expiration": {"Days": self.days}}]}
    from datetime import datetime, timezone
    head = {"LastModified": datetime.now(timezone.utc)}
    backup.retention(Client(30), "backups", "prod/snapshot", head)
    with pytest.raises(backup.BackupError, match="unverified_retention"):
        backup.retention(Client(1), "backups", "prod/snapshot", head)
    with pytest.raises(backup.BackupError, match="unverified_retention"):
        backup.retention(Client(30), "backups", "other/snapshot", head)


def test_retention_reuses_original_expiry_and_rejects_already_expired_objects():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    now = datetime.now(timezone.utc).replace(microsecond=0)
    uploaded = now - timedelta(days=7)
    expiry = uploaded + timedelta(days=30)
    head = {"LastModified": uploaded, "Expiration": f'expiry-date="{format_datetime(expiry, usegmt=True)}", rule-id="backup"'}
    assert backup.retention(None, "backups", "prod/object", head) == expiry
    later = now + timedelta(days=30)
    assert backup.snapshot_expiry([{"expiresAt": later.isoformat()}, {"expiresAt": expiry.isoformat()}]) == expiry.isoformat()
    expired = now - timedelta(days=1)
    with pytest.raises(backup.BackupError, match="expired_backup"):
        backup.retention(None, "backups", "prod/object", {
            "LastModified": expired - timedelta(days=30),
            "Expiration": f'expiry-date="{format_datetime(expired, usegmt=True)}", rule-id="backup"',
        })
    with pytest.raises(backup.BackupError, match="expired_backup"):
        backup.snapshot_expiry([{"expiresAt": expired.isoformat()}])


def test_private_directory_rejects_shared_and_symlinked_locations(tmp_path):
    directory = tmp_path / "backups"
    directory.mkdir(mode=0o700)
    backup.private_directory(directory)
    directory.chmod(0o750)
    with pytest.raises(backup.BackupError, match="unsafe_directory"):
        backup.private_directory(directory)
    directory.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with pytest.raises(backup.BackupError, match="unsafe_directory"):
        backup.private_directory(alias)


def test_release_lock_never_truncates_links_or_blocks_on_fifo(tmp_path):
    import os
    config = tmp_path / "compose.env"
    config.write_text("CONTROL_IMAGE=private")
    config.chmod(0o600)
    lock_path = Path(str(config) + ".release.lock")
    target = tmp_path / "valuable.dump"
    target.write_bytes(b"preserve these bytes")
    target.chmod(0o600)
    os.link(target, lock_path)
    with pytest.raises(backup.BackupError, match="invalid_lock"):
        backup.release_lock(config)
    assert target.read_bytes() == b"preserve these bytes"
    lock_path.unlink()
    os.mkfifo(lock_path, mode=0o600)
    with pytest.raises(backup.BackupError, match="invalid_lock"):
        backup.release_lock(config)
    lock_path.unlink()
    lock_path.symlink_to(target)
    with pytest.raises(OSError):
        backup.release_lock(config)
    assert target.read_bytes() == b"preserve these bytes"


def test_release_lock_can_be_inherited_but_blocks_an_independent_opener(tmp_path):
    import os
    config = tmp_path / "compose.env"
    config.write_text("CONTROL_IMAGE=private")
    config.chmod(0o600)
    fd = backup.release_lock(config)
    try:
        assert backup.release_lock(config, inherited=fd) == fd
        with pytest.raises(backup.BackupError, match="release_lock_unavailable"):
            backup.release_lock(config)
    finally:
        os.close(fd)
    next_fd = backup.release_lock(config)
    os.close(next_fd)


def test_local_receipt_replacement_is_atomic_and_private(tmp_path, monkeypatch):
    import os
    receipt = tmp_path / "backup.verified.json"
    backup.write_receipt(receipt, {"status": "verified", "generation": 1})
    original = receipt.read_bytes()
    inode = receipt.stat().st_ino
    assert receipt.stat().st_mode & 0o077 == 0
    original_replace = backup.os.replace
    def failed_replace(*args):
        raise OSError("simulated interrupted commit")
    monkeypatch.setattr(backup.os, "replace", failed_replace)
    with pytest.raises(OSError):
        backup.write_receipt(receipt, {"status": "verified", "generation": 2})
    assert receipt.read_bytes() == original
    assert not list(tmp_path.glob(".receipt-*.tmp"))
    monkeypatch.setattr(backup.os, "replace", original_replace)
    backup.write_receipt(receipt, {"status": "verified", "generation": 2})
    assert json.loads(receipt.read_bytes())["generation"] == 2
    assert receipt.stat().st_ino != inode
    linked = tmp_path / "linked.json"
    os.link(receipt, linked)
    with pytest.raises(backup.BackupError, match="unsafe_file"):
        backup.write_receipt(linked, {"overwrite": True})
    assert json.loads(receipt.read_bytes())["generation"] == 2


def test_completion_receipt_uses_earliest_expiry_and_rejects_expired_retries(tmp_path, monkeypatch, capsys):
    import boto3
    from botocore.exceptions import ClientError
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    import sys
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires = now + timedelta(days=23)
    dump = tmp_path / "control-20260918T120000Z-abcdefgh.dump"
    dump.write_bytes(b"database")
    dump.chmod(0o600)
    Path(str(dump) + ".media.tar").write_bytes(b"images")
    config = tmp_path / "r2.json"
    config.write_text(json.dumps({"endpoint": "https://" + "a"*32 + ".r2.cloudflarestorage.com", "bucket": "backups", "prefix": "prod/", "access_key_id": "private-key-id", "secret_access_key": "private-key-secret"}))
    config.chmod(0o600)
    compose_env = tmp_path / "compose.env"
    compose_env.write_text("CONTROL_IMAGE=private")
    compose_env.chmod(0o600)
    class Client:
        receipt = None
        def put_object(self, **kwargs):
            if self.receipt is not None:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            self.receipt = json.loads(kwargs["Body"])
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(json.dumps(self.receipt).encode())}
        def head_object(self, **kwargs):
            return {"LastModified": now, "Expiration": f'expiry-date="{format_datetime(now+timedelta(days=30), usegmt=True)}", rule-id="backup"'}
    client = Client()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    monkeypatch.setattr(backup, "upload_verified", lambda _client, _bucket, _prefix, path, destination: {
        "key": path.name, "sha256": "a"*64, "size": 7,
        "expiresAt": (expires if path.name.endswith(".dump") else now+timedelta(days=30)).isoformat(),
    })
    monkeypatch.setattr(backup, "restore_check", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["r2_backup.py", str(dump), "--r2-config", str(config), "--compose-env", str(compose_env), "--compose-file", str(tmp_path / "compose.yml")])
    assert backup.main() == 0
    assert client.receipt["expiresAt"] == expires.isoformat()
    assert backup.main() == 0  # Repeat is idempotent despite its new verifiedAt.
    local = Path(str(dump) + ".verified.json")
    previous = local.read_bytes()
    client.receipt["expiresAt"] = (now-timedelta(days=1)).isoformat()
    assert backup.main() == 1
    assert local.read_bytes() == previous
    output = capsys.readouterr().out
    assert '"code": "expired_backup"' in output
    assert "private-key-id" not in output and "private-key-secret" not in output
