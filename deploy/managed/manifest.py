"""Operator-side manifest creation; runtime verification uses the pinned public key."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess

PINS = json.loads(Path(__file__).with_name("pins.json").read_text())
EXCLUDED = frozenset({"runtime-manifest.json", "runtime-manifest.json.sig", "runtime-public-key.pem"})
MAX_BYTES = 3_000_000_000
MAX_FILES = 100_000


def file_inventory(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Runtime must be a real directory")
    result, total = {}, 0
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        name = path.relative_to(root).as_posix()
        if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Runtime contains a link or special file")
        if stat.S_ISDIR(metadata.st_mode) or name in EXCLUDED:
            continue
        if any(part in {"", ".", ".."} for part in path.relative_to(root).parts) or "\\" in name or any(ord(c) < 32 for c in name):
            raise ValueError("Invalid runtime path")
        total += metadata.st_size
        if total > MAX_BYTES or len(result) >= MAX_FILES:
            raise ValueError("Runtime exceeds inventory limits")
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        result[name] = {"sha256": digest, "size": metadata.st_size, "mode": stat.S_IMODE(metadata.st_mode)}
    return result


def validate_extras(value: dict, revision: str, platform: str) -> dict:
    if not isinstance(value, dict) or set(value) - {"browser"}:
        raise ValueError("Unknown managed extra catalog")
    for name, descriptor in value.items():
        if (not isinstance(descriptor, dict) or descriptor.get("schemaVersion") != 1 or descriptor.get("id") != name
            or descriptor.get("platform") != platform or not isinstance(descriptor.get("version"), str)
            or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}", descriptor["version"])
            or descriptor.get("url") != f"/downloads/agent-control/releases/{revision}/agent-control-browser-{platform}.tar.gz"
            or not re.fullmatch("[a-f0-9]{64}", str(descriptor.get("sha256", "")))
            or type(descriptor.get("size")) is not int or not 0 < descriptor["size"] <= 1_000_000_000):
            raise ValueError("Invalid managed extra descriptor")
    return value


def create_manifest(root: Path, revision: str, platform: str, *, extras: dict | None = None) -> Path:
    if not re.fullmatch("[a-f0-9]{40}", revision) or platform not in PINS["python"]:
        raise ValueError("Invalid runtime release/platform")
    files = file_inventory(root)
    required = {"python/bin/python3", "hermes/pyproject.toml", "hermes/uv.lock", "bin/agent-control-setup", "build-provenance.json", "licenses.json"}
    if not required <= files.keys():
        raise ValueError("Runtime is incomplete")
    if (root / "build-provenance.json").stat().st_size > 16_384:
        raise ValueError("Runtime provenance exceeds size limit")
    provenance = json.loads((root / "build-provenance.json").read_text())
    for key, expected in {"release": revision, "platform": platform, "hermesSourceSha": PINS["hermesSourceSha"], "pythonVersion": PINS["pythonVersion"]}.items():
        if provenance.get(key) != expected:
            raise ValueError("Runtime provenance does not match requested release")
    if extras is None and (root / "extras-catalog.json").exists():
        if (root / "extras-catalog.json").stat().st_size > 16_384:
            raise ValueError("Managed extra catalog exceeds size limit")
        extras = json.loads((root / "extras-catalog.json").read_text())
    value = {"schemaVersion": 1, "dataSchemaVersion": 1, "release": revision, "platform": platform,
             "hermesSourceSha": PINS["hermesSourceSha"], "hermesVersion": PINS["hermesVersion"],
             "pythonVersion": PINS["pythonVersion"], "entrypoint": "bin/agent-control-setup", "files": files}
    if extras is not None:
        value["extras"] = validate_extras(extras, revision, platform)
    path = root / "runtime-manifest.json"
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    return path


def sign_manifest(root: Path, private_key: Path) -> Path:
    if private_key.is_symlink() or not private_key.is_file() or private_key.stat().st_mode & 0o077:
        raise ValueError("Signing key must be a private regular file")
    manifest = root / "runtime-manifest.json"
    # Refuse stale inventories after code signing or any other bundle mutation.
    if json.loads(manifest.read_text()).get("files") != file_inventory(root):
        raise ValueError("Runtime changed after manifest creation")
    public = root / "runtime-public-key.pem"
    signature = root / "runtime-manifest.json.sig"
    subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public)], check=True, capture_output=True)
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature), str(manifest)], check=True, capture_output=True)
    subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signature), str(manifest)], check=True, capture_output=True)
    return signature
