"""Operator signing gate for already-certified, immutable Linux browser extras."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO / "packages/connector"), str(REPO / "packages/hermes-client")]
from agent_control_connector import setup_extras
from agent_control_connector.managed_manifest import PUBLIC_KEY


def verify_prepared_extra(archive: Path, descriptor: dict, revision: str, public_key: Path) -> dict:
    if (archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= setup_extras.MAX_ARCHIVE
        or not isinstance(descriptor, dict) or not re.fullmatch("[a-f0-9]{40}", revision)):
        raise ValueError("Prepared browser archive or revision is invalid")
    platform = descriptor.get("platform")
    name = f"agent-control-browser-{platform}.tar.gz"
    if (platform not in {"linux-x86_64", "linux-arm64"} or descriptor.get("schemaVersion") != 1
        or descriptor.get("id") != "browser" or descriptor.get("version") != setup_extras.EXTRA_VERSION
        or descriptor.get("url") != f"/downloads/agent-control/releases/{revision}/{name}"
        or descriptor.get("sha256") != setup_extras.digest(archive) or descriptor.get("size") != archive.stat().st_size
        or public_key.read_bytes().strip() != PUBLIC_KEY.strip()):
        raise ValueError("Prepared browser package does not match the signed release catalog")
    with tempfile.TemporaryDirectory(prefix="browser-verify-") as temporary:
        destination = Path(temporary)
        setup_extras.extract_extra(archive, destination)
        return setup_extras.verify_extra(destination / "browser-extra", expected_release=revision, expected_platform=platform)


def prepare_extra(archive: Path, output: Path, revision: str, private_key: Path) -> dict:
    if not re.fullmatch("[a-f0-9]{40}", revision):
        raise ValueError("Use the exact committed source revision")
    if private_key.is_symlink() or not private_key.is_file() or private_key.stat().st_mode & 0o077:
        raise ValueError("Signing key must be a private regular file")
    if archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= setup_extras.MAX_ARCHIVE:
        raise ValueError("Browser build archive is invalid")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".browser-prepare-", dir=output) as temporary:
        temporary = Path(temporary)
        setup_extras.extract_extra(archive, temporary)
        root = temporary / "browser-extra"
        document = root / "extra-manifest.json"
        value = json.loads(document.read_bytes())
        target_platform = value.get("platform")
        if target_platform not in {"linux-x86_64", "linux-arm64"}:
            raise ValueError("Mac browser extra is not published until separate Apple signing/notarization is verified")
        setup_extras.verify_extra(root, signature=False, expected_release=revision, expected_platform=target_platform)
        signature = root / "extra-manifest.json.sig"
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature), str(document)], check=True, capture_output=True)
        key = temporary / "trusted.pem"
        key.write_bytes(PUBLIC_KEY)
        subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(key), "-signature", str(signature), str(document)], check=True, capture_output=True)
        name = f"agent-control-browser-{target_platform}.tar.gz"
        destination = output / name
        metadata = output / (name + ".descriptor.json")
        if destination.exists() or metadata.exists():
            raise ValueError("Immutable browser extra already exists")
        staged = temporary / name
        with tarfile.open(staged, "w:gz", dereference=True) as bundle:
            bundle.add(root, arcname="browser-extra")
        descriptor = {"schemaVersion": 1, "id": "browser", "version": setup_extras.EXTRA_VERSION, "platform": target_platform,
                      "url": f"/downloads/agent-control/releases/{revision}/{name}", "sha256": setup_extras.digest(staged), "size": staged.stat().st_size}
        staged_metadata = temporary / (name + ".descriptor.json")
        staged_metadata.write_text(json.dumps(descriptor, sort_keys=True, indent=2) + "\n")
        staged.rename(destination)
        staged_metadata.rename(metadata)
    return descriptor


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("archive", "output", "private-key"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_extra(args.archive, args.output, args.revision, args.private_key), indent=2))
