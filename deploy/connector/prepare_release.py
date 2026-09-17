"""Assemble and sign the four native archives into an immutable download tree."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable

REPO = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPO))
from deploy.connector.macos_signing import AppleSigner, NotarizationPending

PLATFORMS = ("linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64")


def materialize(source: Path, target: Path, root: Path, total: list[int], ancestors: frozenset[Path] = frozenset()) -> None:
    """Copy internal native framework links into a regular-file-only bundle."""
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Build artifact link escapes its bundle")
    if resolved.is_dir():
        if resolved in ancestors:
            raise ValueError("Build artifact contains a directory link cycle")
        target.mkdir()
        for child in resolved.iterdir():
            materialize(child, target / child.name, root, total, ancestors | {resolved})
    elif resolved.is_file():
        total[0] += resolved.stat().st_size
        if total[0] > 800_000_000:
            raise ValueError("Unpacked release is too large")
        shutil.copy2(resolved, target)
    else:
        raise ValueError("Build artifact contains a special file")


def prepare(artifacts: Path, output: Path, revision: str, private_key: Path, *, apple_signer: Callable[[Path, str], None]) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}", revision):
        raise ValueError("Invalid release identifier")
    if private_key.is_symlink() or not private_key.is_file() or private_key.stat().st_mode & 0o077:
        raise ValueError("Signing key must be a private regular file (mode 0600)")
    target = output / "connector/releases" / revision
    if target.exists() or target.is_symlink():
        raise ValueError("Immutable release already exists")
    archives = []
    for platform in PLATFORMS:
        name = f"agent-control-connector-{platform}.tar.gz"
        matches = list(artifacts.rglob(name))
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one artifact: {name}")
        archives.append((platform, matches[0]))
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".connector-sign-", dir=target.parent) as temporary:
        stage = Path(temporary)
        publication = stage / "release"
        publication.mkdir()
        public = stage / "release-public-key.pem"
        subprocess.run(["openssl", "pkey", "-in", str(private_key), "-pubout", "-out", str(public)], check=True)
        sums = []
        pending = []
        for platform, source in archives:
            unpack = stage / source.stem
            unpack.mkdir()
            with tarfile.open(source) as archive:
                members = archive.getmembers()
                if len(members) > 50_000 or sum(member.size for member in members) > 800_000_000:
                    raise ValueError("Build artifact is too large")
                for member in members:
                    path = Path(member.name)
                    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "agent-control-connector":
                        raise ValueError("Unsafe build artifact")
                archive.extractall(unpack, filter="data")
            original = unpack / "agent-control-connector"
            bundle = stage / ("materialized-" + platform)
            materialize(original, bundle, original.resolve(), [0])
            metadata_path = bundle / "release.json"
            if not metadata_path.is_file() or metadata_path.stat().st_size > 16384:
                raise ValueError("Build artifact has no release metadata")
            metadata = json.loads(metadata_path.read_text())
            system, architecture = platform.split("-", 1)
            actual_architecture = metadata.get("architecture")
            actual_architecture = "arm64" if actual_architecture in {"aarch64", "arm64"} else actual_architecture
            if (metadata.get("revision") != revision or metadata.get("protocol") != 1
                or metadata.get("system") != {"linux": "Linux", "macos": "Darwin"}[system]
                or actual_architecture != architecture):
                raise ValueError("Build artifact identity does not match the release/platform")
            binary = bundle / "agent-control-connector"
            if not binary.is_file() or not binary.stat().st_mode & 0o111:
                raise ValueError("Build artifact has no executable")
            shutil.copyfile(public, bundle / "release-public-key.pem")
            if system == "macos":
                try:
                    apple_signer(bundle, platform)
                except NotarizationPending as error:
                    pending.append(str(error))
                    continue
            signed_archive = publication / source.name
            with tarfile.open(signed_archive, "w:gz", dereference=True) as archive:
                archive.add(bundle, arcname="agent-control-connector")
            with signed_archive.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            sums.append(f"{digest}  {source.name}\n")
        if pending:
            raise NotarizationPending("\n".join(pending))
        checksums = publication / "SHA256SUMS"
        checksums.write_text("".join(sums))
        signature = publication / "SHA256SUMS.sig"
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private_key), "-out", str(signature), str(checksums)], check=True)
        subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(signature), str(checksums)], check=True)
        rendered = (REPO / "deploy/connector/install.sh").read_text().replace(
            "__CONNECTOR_RELEASE_PUBLIC_KEY__", public.read_text().strip(),
        )
        # Same filesystem rename publishes a complete release or no release.
        publication.rename(target)
        installer = stage / "install.sh"
        installer.write_text(rendered)
        installer.replace(output / "connector/install.sh")
        version = stage / "VERSION"
        version.write_text(revision + "\n")
        version.replace(output / "connector/VERSION")
    print(f"Prepared signed connector release {revision}; four platform archives verified.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--apple-identity", required=True, help="Developer ID Application identity SHA-1 from security find-identity")
    parser.add_argument("--apple-team-id", required=True)
    parser.add_argument("--notary-profile", required=True, help="Local notarytool Keychain profile")
    args = parser.parse_args()
    signer = AppleSigner(args.output / ".apple-signing", args.revision, args.apple_identity, args.apple_team_id, args.notary_profile)
    signer.work.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with (signer.work / "publish.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit("Another signing preparation is already running; leave it to finish.")
            prepare(args.artifacts, args.output, args.revision, args.private_key, apple_signer=signer)
    except NotarizationPending as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(75)  # Temporary failure: retain submission IDs and do not publish.
