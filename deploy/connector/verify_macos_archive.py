"""Verify a signed/notarized distribution without signing keys or Keychain profiles.

The report binds the checks to the exact archive SHA-256. Run on the matching
native macOS runner before publishing those same bytes to the download server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import re
import subprocess
import sys
import tarfile
import tempfile
import unicodedata

from macos_signing import IDENTIFIER, identifier_for, macho_files, tree_digest


def extract(archive_path: Path, destination: Path) -> Path:
    if not archive_path.is_file() or archive_path.is_symlink() or archive_path.stat().st_size > 800_000_000:
        raise ValueError("Expected a bounded regular-file archive")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 50_000 or sum(member.size for member in members) > 800_000_000:
            raise ValueError("Unpacked archive exceeds release limits")
        seen = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != "agent-control-connector"
                    or not (member.isfile() or member.isdir())):
                raise ValueError("Archive must contain only in-bundle regular files and directories")
            # Default macOS filesystems can alias case and Unicode normalization.
            identity = unicodedata.normalize("NFD", path.as_posix()).casefold()
            if identity in seen:
                raise ValueError("Archive contains duplicate or ambiguous paths")
            seen.add(identity)
        archive.extractall(destination, filter="data")
    return destination / "agent-control-connector"


def command(args: list[str]) -> str:
    result = subprocess.run(args, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"Signature verification failed: {result.stdout}{result.stderr}")
    return result.stdout + result.stderr


def verify_archive(archive_path: Path, revision: str, team: str, architecture: str) -> dict:
    if sys.platform != "darwin" or platform.machine() != architecture:
        raise ValueError("Use the native macOS runner matching the archive architecture")
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise ValueError("Expected a full commit SHA and ten-character Apple team ID")
    with archive_path.open("rb") as handle:
        archive_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    with tempfile.TemporaryDirectory(prefix="connector-verified-") as temporary:
        bundle = extract(archive_path, Path(temporary))
        metadata_path = bundle / "release.json"
        if not metadata_path.is_file() or metadata_path.stat().st_size > 16_384:
            raise ValueError("Archive has no bounded release metadata")
        metadata = json.loads(metadata_path.read_text())
        if (metadata.get("revision") != revision or metadata.get("protocol") != 1
                or metadata.get("system") != "Darwin" or metadata.get("architecture") != architecture):
            raise ValueError("Archive identity differs from the requested revision or architecture")
        main = bundle / "agent-control-connector"
        if not main.is_file() or not main.stat().st_mode & 0o111:
            raise ValueError("Archive has no runnable connector")
        seal = tree_digest(bundle)
        verified = []
        for path in macho_files(bundle):
            identifier = identifier_for(bundle, path)
            requirement = (
                f'anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists '
                f'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists '
                f'and certificate leaf[subject.OU] = "{team}" '
                f'and identifier "{identifier}" and notarized'
            )
            command(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2",
                     "--check-notarization", "-R=" + requirement, str(path)])
            details = command(["/usr/bin/codesign", "--display", "--verbose=4",
                               "--arch", architecture, str(path)])
            flags = re.search(r"^CodeDirectory .* flags=0x([a-fA-F0-9]+)", details, re.MULTILINE)
            cdhash = re.search(r"^CDHash=([a-f0-9]+)$", details, re.MULTILINE)
            if (f"TeamIdentifier={team}\n" not in details or f"Identifier={identifier}\n" not in details
                    or not flags or not int(flags[1], 16) & 0x10000
                    or not re.search(r"^Timestamp=.+$", details, re.MULTILINE) or not cdhash):
                raise ValueError("Code lacks the expected identity, hardened runtime, timestamp, or CDHash")
            verified.append({"path": path.relative_to(bundle).as_posix(), "cdhash": cdhash[1]})
        if tree_digest(bundle) != seal:
            raise ValueError("Extracted release changed during verification")
    with archive_path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != archive_hash:
            raise ValueError("Release archive changed during verification")
    return {"revision": revision, "architecture": architecture, "teamId": team,
            "identifier": IDENTIFIER, "archive": archive_path.name, "sha256": archive_hash,
            "notarizationVerified": True, "machOFiles": verified}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--architecture", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = verify_archive(args.archive, args.revision, args.team, args.architecture)
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Verified {len(report['machOFiles'])} notarized Mach-O files for {args.architecture}; SHA-256 {report['sha256']}")


if __name__ == "__main__":
    main()
