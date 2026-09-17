"""Verify portable runtimes against the publisher's pinned public key.

The key shipped beside an archive is informational: it never establishes trust.
All executable code, including Hermes source, is covered by the signed inventory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess
import sys
import tempfile

PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIIBojANBgkqhkiG9w0BAQEFAAOCAY8AMIIBigKCAYEAosAhTp2WRUDxiKwfun/+
AEcs6tJEL2bv+8CwjMqUXFusy/7A1RW2DYK/C+tP9CIuwJJH+gi9fNmqXVi0YiQF
zNL5wH1llDdZLaolxsdURAb8pUirr6IyTNcD/s4EgajbcvlNFAmfNLPmhKXczT4w
GK//Q7606zr3JZEXNFsj/ZFpGUgXNnY0mPUMYbMO5mRsS/FpNddYh/7bn8EQwe6y
M/1MO2KBmj5F7FP7yGzLvMO2j1dhVass8CCu58WctP/7oYxDVstnTS/lqK98sQ1d
4f7zqLMWbqKeYZvr0aWgk0vM97ULCiXCvuyY5ubM74d+FVLV+NyTNMjwGyx7AE6K
9aNBfnVvuBFaH8G3t6K0Ac/YDjFc5M8oVQFbpZFMrnZQgJZ0ovZ9R4pXld0TCKoX
wgrKq0Bb1CF3ALhchphIScfFM16JmJpV/v1/JHelRXHZQs9CG4soOzSECcehegUb
/2lgBBeDkFuvs4Uzd4SiWX4z0BQfk8aoYNm0uuq4Y387AgMBAAE=
-----END PUBLIC KEY-----
"""
METADATA = {"runtime-manifest.json", "runtime-manifest.json.sig", "runtime-public-key.pem"}


def current_platform() -> str:
    machine = platform.machine()
    arch = "arm64" if machine in {"aarch64", "arm64"} else machine
    return ("macos" if sys.platform == "darwin" else "linux") + "-" + arch


def verify_signature(document: Path, signature: Path) -> None:
    if document.is_symlink() or signature.is_symlink() or signature.stat().st_size > 8192:
        raise ValueError("Invalid signed release metadata")
    with tempfile.TemporaryDirectory(prefix="agent-control-verify-") as temporary:
        key = Path(temporary) / "public.pem"
        key.write_bytes(PUBLIC_KEY)
        result = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(key),
                                 "-signature", str(signature), str(document)],
                                capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError("Release signature is not from Agent Control")


def verify_runtime(root: Path, *, expected_release: str | None = None) -> dict:
    from hermes_client.compatibility import AUDITED_REVISIONS
    root = root.resolve(strict=True)
    document = root / "runtime-manifest.json"
    if document.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Runtime manifest is too large")
    verify_signature(document, root / "runtime-manifest.json.sig")
    manifest = json.loads(document.read_bytes())
    if manifest.get("schemaVersion") != 1 or manifest.get("platform") != current_platform():
        raise ValueError("Runtime is not compatible with this computer")
    revision = manifest.get("hermesSourceSha")
    if revision not in AUDITED_REVISIONS or manifest.get("hermesVersion") != AUDITED_REVISIONS[revision][0]:
        raise ValueError("Runtime contains an unaudited Hermes revision")
    if expected_release and manifest.get("release") != expected_release:
        raise ValueError("Runtime does not match the selected release")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or len(files) > 200_000:
        raise ValueError("Invalid runtime inventory")
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("Runtime contains links or special files")
        if path.is_file() and relative not in METADATA:
            actual.add(relative)
    if actual != set(files):
        raise ValueError("Runtime inventory changed; download it again")
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or str(relative) != name:
            raise ValueError("Invalid runtime inventory path")
        path = root / name
        info = path.stat()
        if info.st_size != expected["size"] or stat.S_IMODE(info.st_mode) != expected["mode"]:
            raise ValueError("Runtime file metadata changed")
        with path.open("rb") as content:
            digest = hashlib.file_digest(content, "sha256").hexdigest()
        if digest != expected["sha256"]:
            raise ValueError("Runtime integrity check failed")
    if not os.access(root / "python/bin/python3", os.X_OK):
        raise ValueError("Runtime Python is not executable")
    return manifest
