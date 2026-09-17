"""Local Developer ID signing and resumable notarization of the final CLI bundle.

Apple private keys stay in the publisher's Keychain. Pending submissions are
retained outside the public download tree; retrying never uploads them again.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import platform as host_platform
import re
import shutil
import subprocess
import sys

IDENTIFIER = "com.jemailabs.agent-control.connector"
MACHO_MAGICS = {bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
)}


class NotarizationPending(RuntimeError):
    pass


def invoke(args: list[str]) -> str:
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=180)
    return result.stdout + result.stderr


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def tree_digest(bundle: Path) -> str:
    entries = []
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Apple bundle must contain only regular files and directories")
        if path.is_file():
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            entries.append((path.relative_to(bundle).as_posix(), path.stat().st_mode & 0o777, digest))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()


def macho_files(bundle: Path) -> list[Path]:
    tree_digest(bundle)  # Reject links and special files before inspecting code.
    result = []
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            with path.open("rb") as handle:
                if handle.read(4) in MACHO_MAGICS:
                    result.append(path)
    main = bundle / "agent-control-connector"
    if main not in result:
        raise ValueError("macOS artifact has no Mach-O connector executable")
    return sorted((path for path in result if path != main), key=lambda path: (-len(path.parts), str(path))) + [main]


def identifier_for(bundle: Path, path: Path) -> str:
    relative = path.relative_to(bundle).as_posix()
    return IDENTIFIER if relative == "agent-control-connector" else IDENTIFIER + ".library." + hashlib.sha256(relative.encode()).hexdigest()[:20]


def flatten_python_framework(bundle: Path) -> None:
    """PyInstaller loads _internal/Python; materialized framework copies are invalid bundles."""
    framework = bundle / "_internal/Python.framework"
    if not framework.exists():
        return
    library = bundle / "_internal/Python"
    versioned = list((framework / "Versions").glob("*/Python"))
    if not library.is_file() or not versioned or any(path.read_bytes() != library.read_bytes() for path in versioned):
        raise ValueError("Unexpected Python framework layout; refuse to remove it")
    # A future build must not silently introduce a loader dependency on this tree.
    for path in macho_files(bundle):
        if any("Python.framework" in line for line in invoke(["/usr/bin/otool", "-L", str(path)]).splitlines()[1:]):
            raise ValueError("Native code depends on Python.framework; packaging must be reviewed")
    shutil.rmtree(framework)


class AppleSigner:
    def __init__(self, work: Path, revision: str, identity: str, team: str, profile: str):
        if sys.platform != "darwin":
            raise ValueError("Publish native releases on the trusted macOS signing host")
        if not re.fullmatch(r"[A-Fa-f0-9]{40}", identity) or not re.fullmatch(r"[A-Z0-9]{10}", team):
            raise ValueError("Expected a Developer ID identity SHA-1 and a ten-character Apple team ID")
        if not profile or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}", revision):
            raise ValueError("A notarization Keychain profile and valid revision are required")
        self.work, self.revision, self.identity, self.team, self.profile = work, revision, identity, team, profile

    def notary(self, *args: str) -> dict:
        return json.loads(invoke(["xcrun", "notarytool", *args, "--keychain-profile", self.profile, "--output-format", "json"]))

    def verify(self, bundle: Path, architecture: str, *, notarized: bool = False) -> set[tuple[str, str]]:
        hashes = set()
        for path in macho_files(bundle):
            identifier = identifier_for(bundle, path)
            requirement = (f'anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists '
                           f'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists '
                           f'and certificate leaf[subject.OU] = "{self.team}" and identifier "{identifier}"')
            args = ["/usr/bin/codesign", "--verify", "--strict", "--verbose=2"]
            if notarized:
                args.append("--check-notarization")
                requirement += " and notarized"
            invoke([*args, "-R=" + requirement, str(path)])
            details = invoke(["/usr/bin/codesign", "-d", "--verbose=4", "--arch", architecture, str(path)])
            flags = re.search(r"^CodeDirectory .* flags=0x([a-fA-F0-9]+)", details, re.MULTILINE)
            if (f"TeamIdentifier={self.team}\n" not in details or f"Identifier={identifier}\n" not in details
                    or not flags or not int(flags[1], 16) & 0x10000 or not re.search(r"^Timestamp=.+$", details, re.MULTILINE)):
                raise ValueError("Missing stable Developer ID, hardened runtime or signing timestamp")
            match = re.search(r"^CDHash=([a-f0-9]+)$", details, re.MULTILINE)
            if not match:
                raise ValueError("Cannot verify native code hash")
            hashes.add((match[1], architecture))
        return hashes

    def __call__(self, bundle: Path, platform: str) -> None:
        if platform not in {"macos-arm64", "macos-x86_64"}:
            raise ValueError("Unexpected Apple platform")
        architecture = platform.removeprefix("macos-")
        work = self.work / self.revision / platform
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        identity = {"input": tree_digest(bundle), "identity": self.identity, "team": self.team, "identifier": IDENTIFIER}
        request = work / "request.json"
        if request.exists() and json.loads(request.read_text()) != identity:
            raise ValueError("Signing input changed for this immutable revision; use a new revision")
        write_json(request, identity)
        signed = work / "agent-control-connector"
        seal = work / "signed.json"
        if not seal.exists():
            if (work / "submission-intent.json").exists():
                raise ValueError("Submission exists without signed files; manual recovery required")
            if signed.exists():
                shutil.rmtree(signed)
            shutil.copytree(bundle, signed)
            flatten_python_framework(signed)
            for path in macho_files(signed):
                invoke(["/usr/bin/codesign", "--force", "--sign", self.identity, "--timestamp", "--options", "runtime",
                        "--identifier", identifier_for(signed, path), str(path)])
            self.verify(signed, architecture)
            if host_platform.machine() == architecture:
                invoke([str(signed / "agent-control-connector"), "--help"])
            write_json(seal, {"sha256": tree_digest(signed)})
        sealed = json.loads(seal.read_text())["sha256"]
        if tree_digest(signed) != sealed:
            raise ValueError("Signed staging files changed; refuse to reuse notarization")
        hashes = self.verify(signed, architecture)
        archive = work / f"agent-control-connector-{self.revision}-{platform}-{sealed[:16]}.zip"
        if not archive.exists():
            if (work / "submission-intent.json").exists():
                raise ValueError("Submitted archive is missing; manual recovery required")
            temporary_archive = archive.with_suffix(".partial.zip")
            temporary_archive.unlink(missing_ok=True)
            invoke(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(signed), str(temporary_archive)])
            temporary_archive.replace(archive)
        with archive.open("rb") as handle:
            archive_hash = hashlib.file_digest(handle, "sha256").hexdigest()
        intent = {"name": archive.name, "sha256": archive_hash}
        intent_path, receipt_path = work / "submission-intent.json", work / "submission.json"
        if receipt_path.exists() and not intent_path.exists():
            raise ValueError("Submission receipt has no archive identity; manual recovery required")
        if intent_path.exists() and json.loads(intent_path.read_text()) != intent:
            raise ValueError("Submitted archive changed")
        if not receipt_path.exists():
            if intent_path.exists():
                # An interrupted upload can have succeeded. Recover its ID, never blindly resubmit.
                matches = [entry for entry in self.notary("history").get("history", []) if entry.get("name") == archive.name]
                if len(matches) != 1:
                    raise ValueError("Upload outcome is uncertain; inspect Apple submission history before retrying")
                receipt = matches[0]
            else:
                write_json(intent_path, intent)
                receipt = self.notary("submit", str(archive))
            if not receipt.get("id"):
                raise ValueError("Apple did not return a submission ID")
            write_json(receipt_path, receipt)
        receipt = json.loads(receipt_path.read_text())
        status = self.notary("info", receipt["id"])
        write_json(work / "status.json", status)
        if status.get("status") == "In Progress":
            raise NotarizationPending(f"{platform}: Apple submission {receipt['id']} is pending; rerun the same preparation command to resume.")
        log_path = work / "notarization-log.json"
        invoke(["xcrun", "notarytool", "log", receipt["id"], str(log_path), "--keychain-profile", self.profile])
        if status.get("status") != "Accepted":
            raise ValueError(f"Apple rejected {platform}; inspect {log_path}")
        log = json.loads(log_path.read_text())
        if log.get("jobId") != receipt["id"] or log.get("status") != "Accepted" or log.get("sha256") != archive_hash:
            raise ValueError("Apple notarization log does not match the submitted archive")
        tickets = {(entry.get("cdhash"), entry.get("arch")) for entry in log.get("ticketContents", [])}
        if not hashes.issubset(tickets):
            raise ValueError("Apple ticket does not cover every delivered native code file")
        self.verify(signed, architecture, notarized=True)
        shutil.rmtree(bundle)
        shutil.copytree(signed, bundle)
        if tree_digest(bundle) != sealed:
            raise ValueError("Signed bundle changed while preparing publication")
