#!/usr/bin/env python3
"""Copy a paired PostgreSQL/image snapshot off-host and restore both in isolation.

Run under the same release lock as backup.sh and image GC. Credentials are read
from a private file, never command-line arguments. Only a verified completion
manifest makes a remote snapshot usable before its expiresAt cutoff; partially
uploaded or expired snapshots are ignored.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import time
from uuid import uuid4

MAX_BYTES = 128 * 1024**3
CHUNK = 1024**2
NAME = re.compile(r"control-[0-9]{8}T[0-9]{6}Z-[a-zA-Z0-9]{8}\.dump\Z")


class BackupError(Exception):
    """Only fixed, nonsecret error codes may be returned to an operator."""


def require(ok, code):
    if not ok:
        raise BackupError(code)



def private_directory(path: Path):
    require(path.is_absolute() and path == path.resolve(strict=True), "unsafe_directory")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and not info.st_mode & 0o077, "unsafe_directory")
    finally:
        os.close(fd)


def release_lock(config: Path, inherited=None, wait_seconds=0):
    private_directory(config.parent)
    require(config.is_absolute() and config == config.resolve(strict=True), "unsafe_config_path")
    with private_file(config):
        pass
    path = Path(str(config) + ".release.lock")
    fd = inherited if inherited is not None else os.open(
        path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info, expected = os.fstat(fd), path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                and info.st_uid == os.getuid() and not info.st_mode & 0o077
                and (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino), "invalid_lock")
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                require(time.monotonic() < deadline, "release_lock_unavailable")
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except BaseException:
        if inherited is None:
            os.close(fd)
        raise


def write_receipt(path: Path, payload):
    private_directory(path.parent)
    # Replace, never follow, an existing destination. Reject unsafe pre-existing
    # entries as well so a bad operational directory fails closed.
    if path.exists() or path.is_symlink():
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                and not info.st_mode & 0o077, "unsafe_file")
    fd, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(payload, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def future_expiry(value):
    try:
        expiry = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise BackupError("invalid_expiration") from None
    require(expiry.tzinfo is not None and expiry > datetime.now(timezone.utc), "expired_backup")
    return expiry.astimezone(timezone.utc)


def snapshot_expiry(objects):
    return min(future_expiry(item.get("expiresAt")) for item in objects).isoformat()


def private_file(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.getuid()
                and not st.st_mode & 0o077 and 0 < st.st_size <= MAX_BYTES, "unsafe_file")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def digest(source):
    result, size = hashlib.sha256(), 0
    source.seek(0)
    while data := source.read(CHUNK):
        size += len(data)
        require(size <= MAX_BYTES, "too_large")
        result.update(data)
    source.seek(0)
    return result.hexdigest(), size


def retention(client, bucket, key, head):
    # Prefer R2's object response; backup-only credentials need no admin scope.
    uploaded = head.get("LastModified")
    require(isinstance(uploaded, datetime) and uploaded.tzinfo is not None, "invalid_expiration")
    if head.get("Expiration"):
        match = re.search(r'expiry-date="([^"]+)"', head["Expiration"])
        require(match is not None, "invalid_expiration")
        expiry = parsedate_to_datetime(match[1])
        require(expiry.tzinfo is not None and 29*86400 <= (expiry - uploaded).total_seconds() <= 32*86400,
                "invalid_retention")
        return future_expiry(expiry.isoformat())
    rules = client.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
    days = []
    for rule in rules:
        if rule.get("Status") != "Enabled":
            continue
        filt = rule.get("Filter", {"Prefix": rule.get("Prefix", "")})
        require(isinstance(filt, dict) and set(filt) <= {"Prefix"}, "unsupported_retention")
        if key.startswith(filt.get("Prefix", "")) and "Expiration" in rule:
            days.append(rule["Expiration"].get("Days"))
    require(bool(days) and all(type(day) is int for day in days) and min(days) == 30, "unverified_retention")
    # Lifecycle implementations may round expiry up to midnight. Use the earlier
    # conservative instant rather than extending a reused object's availability.
    return future_expiry((uploaded + timedelta(days=30)).isoformat())


def upload_verified(client, bucket, prefix, path, destination):
    from boto3.s3.transfer import TransferConfig
    from botocore.exceptions import ClientError
    with private_file(path) as source:
        sha, size = digest(source)
        key = f"{prefix}{path.name}-{sha}"
        try:
            head = client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            require(exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}, "storage_unavailable")
            # Random backup suffix plus content hash yields immutable object keys.
            client.upload_fileobj(source, bucket, key,
                ExtraArgs={"ContentType": "application/octet-stream", "CacheControl": "no-store", "Metadata": {"sha256": sha}},
                Config=TransferConfig(multipart_threshold=32*CHUNK, multipart_chunksize=32*CHUNK, max_concurrency=2))
            head = client.head_object(Bucket=bucket, Key=key)
        require(head.get("ContentLength") == size and head.get("Metadata", {}).get("sha256") == sha,
                "remote_integrity_failed")
        expiry = retention(client, bucket, key, head)
        response = client.get_object(Bucket=bucket, Key=key)
        stream = response["Body"]
        try:
            with destination.open("xb") as output:
                received, checksum = 0, hashlib.sha256()
                while data := stream.read(CHUNK):
                    received += len(data)
                    require(received <= size, "download_overflow")
                    checksum.update(data)
                    output.write(data)
            require(received == size and checksum.hexdigest() == sha, "download_integrity_failed")
        finally:
            stream.close()
    return {"key": key, "sha256": sha, "size": size, "expiresAt": expiry.isoformat()}


def unpack_images(archive: Path, target: Path):
    target.mkdir(mode=0o700)
    # backup.sh produces a flat directory. Refuse traversal, links, devices,
    # duplicate names and an oversized expansion before extracting any bytes.
    with tarfile.open(archive) as source:
        names, total = set(), 0
        members = source.getmembers()
        for member in members:
            name = member.name.removeprefix("./")
            if member.isdir() and name in {"", "."}:
                continue
            require(member.isfile() and re.fullmatch(r"manifest\.json|[a-f0-9]{32}-(?:full|thumbnail)-[a-f0-9]{64}", name)
                    and name not in names, "unsafe_image_archive")
            names.add(name)
            total += member.size
            require(total <= MAX_BYTES, "image_archive_too_large")
        require("manifest.json" in names, "missing_image_manifest")
        source.extractall(target, filter="data")


def restore_check(compose, directory, dump, media):
    drill = "control_restore_" + uuid4().hex
    def pg(*args, source=None, capture=False):
        result = subprocess.run([*compose, "exec", "-T", "postgres", *args],
            stdin=source if source else subprocess.DEVNULL, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=900, check=False)
        require(result.returncode == 0, "postgres_restore_failed")
        return result.stdout
    try:
        pg("createdb", "-U", "agent_control", drill)
        with private_file(dump) as source:
            pg("pg_restore", "-U", "agent_control", "--dbname="+drill, "--exit-on-error", "--no-owner", "--no-acl", source=source)
        has_images = pg("psql", "-U", "agent_control", "-d", drill, "-Atc",
            "SELECT CASE WHEN to_regclass('public.visual_media') IS NULL THEN 'no' ELSE 'yes' END", capture=True).strip() == b"yes"
        require(not has_images or media is not None, "missing_image_backup")
        if media is not None:
            target = directory / "images"
            unpack_images(media, target)
            result = subprocess.run([*compose, "run", "--rm", "--no-deps", "--user", f"{os.getuid()}:{os.getgid()}",
                "--volume", f"{directory}:/backup", "--entrypoint", "/opt/venv/bin/python", "control", "-m",
                "hermes_control_api.visual_media", "verify-backup", "/backup/images", "--database", drill],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600, check=False)
            require(result.returncode == 0, "image_restore_failed")
    finally:
        pg("dropdb", "-U", "agent_control", "--if-exists", drill)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--r2-config", type=Path, required=True)
    parser.add_argument("--compose-env", type=Path, required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--lock-fd", type=int)
    args = parser.parse_args()
    os.umask(0o077)
    lock = None
    try:
        require(args.backup.is_absolute() and args.backup == args.backup.resolve(strict=True)
                and NAME.fullmatch(args.backup.name), "invalid_backup_path")
        private_directory(args.backup.parent)
        require(all(char not in str(args.backup.parent) for char in ":,"), "invalid_mount")
        with private_file(args.r2_config) as source:
            require(os.fstat(source.fileno()).st_size <= 16384, "invalid_config")
            cfg = json.load(source)
        require(re.fullmatch(r"https://[a-f0-9]{32}(?:\.(?:eu|us|fedramp))?\.r2\.cloudflarestorage\.com/?", cfg["endpoint"]), "invalid_endpoint")
        require(re.fullmatch(r"[a-z0-9-]{3,63}", cfg["bucket"]) and re.fullmatch(r"[a-zA-Z0-9/_-]+/", cfg["prefix"]), "invalid_destination")
        lock = release_lock(args.compose_env, args.lock_fd)
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", endpoint_url=cfg["endpoint"], region_name="auto",
            aws_access_key_id=cfg["access_key_id"], aws_secret_access_key=cfg["secret_access_key"],
            config=Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 3}))
        compose = ["docker", "compose", "--env-file", str(args.compose_env), "-f", str(args.compose_file)]
        with tempfile.TemporaryDirectory(prefix=".offhost-restore-", dir=args.backup.parent) as temporary:
            work = Path(temporary)
            media_path = Path(str(args.backup) + ".media.tar")
            dump = work / args.backup.name
            manifest = {"version": 1, "database": upload_verified(client, cfg["bucket"], cfg["prefix"], args.backup, dump)}
            media = None
            if media_path.exists():
                media = work / media_path.name
                manifest["images"] = upload_verified(client, cfg["bucket"], cfg["prefix"], media_path, media)
            restore_check(compose, work, dump, media)
            manifest["expiresAt"] = snapshot_expiry([manifest["database"], *([manifest["images"]] if media is not None else [])])
            manifest["verifiedAt"] = datetime.now(timezone.utc).isoformat()
            receipt = json.dumps(manifest, sort_keys=True).encode()
            key = cfg["prefix"] + args.backup.name + ".complete.json"
            from botocore.exceptions import ClientError
            try:
                client.put_object(Bucket=cfg["bucket"], Key=key, Body=receipt, ContentType="application/json", IfNoneMatch="*")
            except ClientError as exc:
                require(exc.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}, "receipt_upload_failed")
                response = client.get_object(Bucket=cfg["bucket"], Key=key)
                try:
                    previous = json.loads(response["Body"].read(16385))
                finally:
                    response["Body"].close()
                future_expiry(previous.get("expiresAt"))
                require({k: v for k, v in previous.items() if k != "verifiedAt"}
                        == {k: v for k, v in manifest.items() if k != "verifiedAt"}, "receipt_conflict")
            retention(client, cfg["bucket"], key, client.head_object(Bucket=cfg["bucket"], Key=key))
            future_expiry(manifest["expiresAt"])
            receipt_path = Path(str(args.backup) + ".verified.json")
            write_receipt(receipt_path, {"status": "verified", "manifestKey": key, **manifest})
        print(json.dumps({"status": "verified", "manifestKey": key, "imagesIncluded": "images" in manifest, "expiresAt": manifest["expiresAt"]}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "code": str(exc) if type(exc) is BackupError else type(exc).__name__}))
        return 1
    finally:
        if lock is not None and args.lock_fd is None:
            os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
