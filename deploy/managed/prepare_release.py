"""Publish a complete signed managed-runtime release without touching connector-only downloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "packages/connector")]
from agent_control_connector.managed_manifest import PUBLIC_KEY
from deploy.managed.build import digest, extract
from deploy.managed.manifest import create_manifest, file_inventory, sign_manifest, validate_extras
from deploy.update_policy import policy


def prepared_extras(directory: Path | None, release: Path, revision: str, public_key: Path) -> dict:
    """Copy finalized artifacts byte-for-byte; never invalidate an already sealed Mac catalog."""
    catalog = {}
    if directory is None:
        return catalog
    from deploy.managed.extras.prepare import verify_prepared_extra
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Prepared extra directory must be a regular directory")
    for archive in sorted(directory.glob("agent-control-browser-*.tar.gz")):
        descriptor_file = archive.with_name(archive.name + ".descriptor.json")
        if (archive.is_symlink() or not archive.is_file() or descriptor_file.is_symlink()
            or not descriptor_file.is_file() or descriptor_file.stat().st_size > 16_384):
            raise ValueError("Prepared extra requires a bounded descriptor and regular archive")
        descriptor = json.loads(descriptor_file.read_text())
        platform = descriptor.get("platform")
        if platform not in {"linux-arm64", "linux-x86_64", "macos-arm64"} or platform in catalog:
            raise ValueError("Prepared extra platform is invalid or duplicated")
        validate_extras({"browser": descriptor}, revision, platform)
        if archive.name != f"agent-control-browser-{platform}.tar.gz":
            raise ValueError("Prepared extra archive name does not match its platform")
        verify_prepared_extra(archive, descriptor, revision, public_key)
        shutil.copyfile(archive, release / archive.name)
        catalog[platform] = {"browser": descriptor}
    return catalog


def verify_dmg(dmg: Path, receipt: Path, revision: str) -> dict:
    value = json.loads(receipt.read_text())
    artifact = value.get("artifact", {})
    checks = value.get("verified", {})
    if (value.get("schemaVersion") != 1 or value.get("revision") != revision or value.get("platform") != "macos-arm64"
        or value.get("appIdentifier") != "com.jemailabs.agent-control.setup" or not value.get("teamId")
        or value.get("notarization", {}).get("status") != "Accepted"
        or not value.get("notarization", {}).get("submissionId")
        or not all(checks.get(key) is True for key in ("appSignature", "gatekeeper", "stapledApp", "stapledDmg", "runtimeManifest"))
        or artifact.get("name") != dmg.name or artifact.get("size") != dmg.stat().st_size
        or artifact.get("sha256") != digest(dmg)):
        raise ValueError("DMG verification receipt does not match a fully verified release")
    subprocess.run(["xcrun", "stapler", "validate", str(dmg)], check=True, capture_output=True)
    subprocess.run(["spctl", "--assess", "--type", "open", "--context", "context:primary-signature", str(dmg)], check=True, capture_output=True)
    return value


def prepare(artifacts: Path, output: Path, revision: str, private_key: Path, dmg: Path, receipt: Path,
            extras: Path | None = None) -> Path:
    if not re.fullmatch("[a-f0-9]{40}", revision):
        raise ValueError("Use the exact committed source revision")
    public_root = output / "agent-control"
    target = public_root / "releases" / revision
    if target.exists() or target.is_symlink():
        raise ValueError("Immutable managed release already exists")
    dmg_receipt = verify_dmg(dmg, receipt, revision)
    sources = []
    for platform in ("linux-x86_64", "linux-arm64"):
        matches = list(artifacts.rglob(f"agent-control-runtime-{platform}.tar.gz"))
        if len(matches) != 1:
            raise ValueError(f"Expected one native build for {platform}")
        sources.append((platform, matches[0]))
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=target.parent) as temporary:
        stage = Path(temporary)
        release = stage / "release"
        release.mkdir()
        if private_key.is_symlink() or not private_key.is_file() or private_key.stat().st_mode & 0o077:
            raise ValueError("Signing key must be a private regular file")
        public = stage / "public-key.pem"
        subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public)], check=True, capture_output=True)
        if public.read_bytes().strip() != PUBLIC_KEY.strip():
            raise ValueError("Signing key does not match the embedded runtime trust anchor")
        catalog = prepared_extras(extras, release, revision, public)
        if dmg_receipt.get("extras", {}) != catalog.get("macos-arm64", {}):
            raise ValueError("Notarized Mac extra catalog differs from the prepared downloads")
        for platform, archive in sources:
            unpack = stage / platform
            unpack.mkdir()
            extract(archive, unpack)
            root = unpack / "agent-control-runtime"
            file_inventory(root)  # No symlinks/special files may enter publication.
            create_manifest(root, revision, platform, extras=catalog.get(platform, {}))
            sign_manifest(root, private_key)
            with tarfile.open(release / archive.name, "w:gz", dereference=True) as bundle:
                bundle.add(root, arcname="agent-control-runtime")
        dmg_name = f"Agent-Control-{revision}-macos-arm64.dmg"
        shutil.copyfile(dmg, release / dmg_name)
        shutil.copyfile(receipt, release / (dmg_name + ".verification.json"))
        checksums = release / "SHA256SUMS"
        checksums.write_text("".join(f"{digest(path)}  {path.name}\n" for path in sorted(release.iterdir())))
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(release / "SHA256SUMS.sig"), str(checksums)], check=True, capture_output=True)
        subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(release / "SHA256SUMS.sig"), str(checksums)], check=True, capture_output=True)
        latest = {"schemaVersion": 1, "version": revision, "updates": policy(), "downloads": {
            "macosArm64": {"url": f"/downloads/agent-control/releases/{revision}/{dmg_name}", "sha256": digest(release / dmg_name), "minOsVersion": "13"},
            "linux": {"installerUrl": "/downloads/agent-control/install.sh"}}}
        installer = stage / "install.sh"
        installer.write_text((Path(__file__).with_name("install.sh")).read_text().replace("__MANAGED_RELEASE_PUBLIC_KEY__", public.read_text().strip()))
        version = stage / "VERSION"
        version.write_text(revision + "\n")
        latest_path = stage / "latest.json"
        latest_path.write_text(json.dumps(latest, sort_keys=True, indent=2) + "\n")
        latest_signature = stage / "latest.json.sig"
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(latest_signature), str(latest_path)], check=True, capture_output=True)
        release.rename(target)
        installer.replace(public_root / "install.sh")
        version.replace(public_root / "VERSION")
        latest_signature.replace(public_root / "latest.json.sig")
        latest_path.replace(public_root / "latest.json")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("artifacts", "output", "private-key", "dmg", "receipt"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--extras", type=Path, help="Directory containing final signed extra archives and descriptor sidecars")
    args = parser.parse_args()
    print(prepare(args.artifacts, args.output, args.revision, args.private_key, args.dmg, args.receipt, args.extras))
