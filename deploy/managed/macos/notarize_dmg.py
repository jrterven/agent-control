"""Notarize and staple an unchanged managed app, then its distribution DMG.

The operation is resumable. Apple may take time to accept a submission or make
its ticket available. Keep the work directory and rerun the same command;
neither pending submissions nor signed code are recreated.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from deploy.connector.macos_signing import NotarizationPending, tree_digest
from deploy.managed.macos.build_app import APP_ID, APP_NAME, SERVICE_ID, code_identifier, invoke, native_files, verify_code
from deploy.managed.manifest import file_inventory


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def verify_runtime(app: Path, revision: str, public_key: Path) -> None:
    runtime = app / "Contents/Resources/runtime"
    manifest = runtime / "runtime-manifest.json"
    value = json.loads(manifest.read_text())
    if value.get("release") != revision or value.get("platform") != "macos-arm64":
        raise ValueError("App runtime revision or platform mismatch")
    if value.get("files") != file_inventory(runtime):
        raise ValueError("App runtime differs from its signed inventory")
    invoke("openssl", "dgst", "-sha256", "-verify", public_key,
           "-signature", runtime / "runtime-manifest.json.sig", manifest)


def verify_app_codes(app: Path, team: str, *, notarized: bool = False) -> set[tuple[str, str]]:
    hashes = {verify_code(app, team, APP_ID, notarized=notarized),
              verify_code(app / "Contents/MacOS/AgentControlService", team, SERVICE_ID, notarized=notarized),
              verify_code(app / "Contents/MacOS/AgentControlUpdater", team, APP_ID + ".updater", notarized=notarized)}
    runtime = app / "Contents/Resources/runtime"
    for path in native_files(runtime):
        hashes.add(verify_code(path, team, code_identifier(runtime, path), notarized=notarized))
    return hashes


def notarize(artifact: Path, work: Path, label: str, profile: str) -> str:
    intent = work / (label + "-intent.json")
    receipt = work / (label + "-submission.json")
    identity = {"name": artifact.name, "sha256": digest(artifact)}
    if intent.exists() and json.loads(intent.read_text()) != identity:
        raise ValueError("Notarization input changed; keep immutable release bytes")

    def notary(*args: str) -> dict:
        return json.loads(invoke("xcrun", "notarytool", *args, "--keychain-profile", profile, "--output-format", "json"))

    if not receipt.exists():
        if intent.exists():
            matches = [item for item in notary("history").get("history", []) if item.get("name") == artifact.name]
            if len(matches) != 1:
                raise ValueError("Previous notarization upload is uncertain; inspect Apple history before retrying")
            result = matches[0]
        else:
            write_json(intent, identity)
            result = notary("submit", str(artifact))
        if not result.get("id"):
            raise ValueError("Apple returned no submission ID")
        write_json(receipt, result)
    submission = json.loads(receipt.read_text())["id"]
    status = notary("info", submission)
    write_json(work / (label + "-status.json"), status)
    if status.get("status") == "In Progress":
        raise NotarizationPending(f"{label}: Apple submission {submission} is pending. Rerun the same command later.")
    log_path = work / (label + "-log.json")
    invoke("xcrun", "notarytool", "log", submission, log_path, "--keychain-profile", profile)
    log = json.loads(log_path.read_text())
    if (status.get("status") != "Accepted" or log.get("status") != "Accepted"
            or log.get("jobId") != submission or log.get("sha256") != identity["sha256"]):
        raise ValueError(f"Apple did not accept these exact bytes; inspect {log_path}")
    if log.get("issues"):
        raise ValueError(f"Apple reported notarization issues; review {log_path} before publication")
    return submission


def prepare(app: Path, output: Path, work: Path, revision: str, identity: str, team: str,
            profile: str, public_key: Path) -> Path:
    if sys.platform != "darwin":
        raise ValueError("Notarization verification requires macOS")
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise ValueError("A source commit SHA and Apple team are required")
    if not re.fullmatch(r"[a-fA-F0-9]{40}", identity) or not profile:
        raise ValueError("A Developer ID certificate SHA-1 and notarization Keychain profile are required")
    if app.is_symlink() or app.name != APP_NAME or not app.is_dir():
        raise ValueError("Expected the built Agent Control.app directory")
    if output.resolve() == work.resolve():
        raise ValueError("Notarization work and distribution output must be separate directories")
    metadata = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if metadata.get("CFBundleIdentifier") != APP_ID or metadata.get("AgentControlRevision") != revision:
        raise ValueError("App identity does not match the release")
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    dmg = output / f"Agent-Control-{revision}-macos-arm64.dmg"
    receipt_path = dmg.with_suffix(".verification.json")
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get("artifact", {}).get("sha256") != digest(dmg):
            raise ValueError("Verified DMG changed after publication preparation")
        return receipt_path
    required_tickets = verify_app_codes(app, team)
    verify_runtime(app, revision, public_key)

    # Notarize the app first so that its own ticket travels with the copied app.
    # Then notarize the final signed DMG containing that stapled app.
    sealed_app = work / "app-stapled.json"
    stapled_app = work / "stapled" / APP_NAME
    if not sealed_app.exists():
        archive = work / f"Agent-Control-{revision}-app.zip"
        seal = work / "app-input.json"
        original = tree_digest(app)
        if seal.exists() and json.loads(seal.read_text()).get("sha256") != original:
            raise ValueError("App changed while notarization was pending")
        if not archive.exists():
            write_json(seal, {"sha256": original})
            temporary = archive.with_suffix(".partial.zip")
            invoke("/usr/bin/ditto", "-c", "-k", "--keepParent", app, temporary)
            temporary.replace(archive)
        app_submission = notarize(archive, work, "app", profile)
        app_log = json.loads((work / "app-log.json").read_text())
        tickets = {(entry.get("cdhash"), entry.get("arch")) for entry in app_log.get("ticketContents", [])}
        if not required_tickets.issubset(tickets):
            raise ValueError("Apple ticket does not cover every native file in the app")
        verify_app_codes(app, team, notarized=True)
        # Staple a copy so an interruption cannot mutate the notarization input
        # or force a second submission on retry.
        if stapled_app.exists():
            shutil.rmtree(stapled_app)
        stapled_app.parent.mkdir(exist_ok=True)
        shutil.copytree(app, stapled_app)
        invoke("xcrun", "stapler", "staple", stapled_app)
        invoke("xcrun", "stapler", "validate", stapled_app)
        write_json(sealed_app, {"sha256": tree_digest(stapled_app), "submissionId": app_submission})
    elif json.loads(sealed_app.read_text())["sha256"] != tree_digest(stapled_app):
        raise ValueError("Stapled app changed before DMG preparation")
    verify_app_codes(stapled_app, team, notarized=True)
    invoke("/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", stapled_app)
    unsigned_ticket_dmg = work / dmg.name
    if not unsigned_ticket_dmg.exists():
        with tempfile.TemporaryDirectory(prefix=".dmg-contents-", dir=output) as temporary:
            contents = Path(temporary)
            shutil.copytree(stapled_app, contents / APP_NAME)
            (contents / "Instalar.txt").write_text(
                "Abre Agent Control y pulsa Instalar y abrir.\n"
                "Se copiará a tu carpeta Aplicaciones; no necesitas Terminal ni privilegios de administrador.\n"
                "Si ya tienes Agent Control, actualiza desde su menú para conservar el trabajo de tus agentes.\n"
            )
            temporary_dmg = work / (dmg.stem + ".partial.dmg")
            invoke("/usr/bin/hdiutil", "create", "-srcfolder", contents, "-volname", "Agent Control", "-format", "UDZO", temporary_dmg)
            invoke("/usr/bin/codesign", "--force", "--sign", identity, "--timestamp", "--identifier", APP_ID + ".dmg", temporary_dmg)
            temporary_dmg.replace(unsigned_ticket_dmg)
    submission = notarize(unsigned_ticket_dmg, work, "dmg", profile)
    ticketed = output / (dmg.stem + ".stapling.dmg")
    shutil.copyfile(unsigned_ticket_dmg, ticketed)
    invoke("xcrun", "stapler", "staple", ticketed)
    invoke("xcrun", "stapler", "validate", ticketed)
    invoke("/usr/bin/hdiutil", "verify", ticketed)
    invoke("/usr/sbin/spctl", "--assess", "--type", "open", "--context", "context:primary-signature", "--verbose=2", ticketed)
    invoke("/usr/bin/codesign", "--verify", "--strict", "--deep", stapled_app)
    verify_runtime(stapled_app, revision, public_key)
    # Assess the payload actually delivered inside the final image, including
    # its stapled ticket, rather than trusting only the staging directory.
    with tempfile.TemporaryDirectory(prefix="mounted-dmg-", dir=work) as temporary:
        mount = Path(temporary)
        invoke("/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount, ticketed)
        try:
            payload = mount / APP_NAME
            verify_app_codes(payload, team, notarized=True)
            invoke("xcrun", "stapler", "validate", payload)
            invoke("/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", payload)
            verify_runtime(payload, revision, public_key)
        finally:
            invoke("/usr/bin/hdiutil", "detach", mount)
    ticketed.replace(dmg)
    manifest = json.loads((stapled_app / "Contents/Resources/runtime/runtime-manifest.json").read_text())
    report = {"schemaVersion": 1, "revision": revision, "platform": "macos-arm64", "appIdentifier": APP_ID,
              "teamId": team, "artifact": {"name": dmg.name, "sha256": digest(dmg), "size": dmg.stat().st_size},
              "notarization": {"submissionId": submission, "status": "Accepted"},
              "verified": {"appSignature": True, "gatekeeper": True, "stapledApp": True, "stapledDmg": True, "runtimeManifest": True},
              "extras": manifest.get("extras", {}), "verifiedAt": datetime.now(timezone.utc).isoformat()}
    write_json(receipt_path, report)
    return receipt_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--notary-profile", required=True)
    parser.add_argument("--manifest-public-key", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(prepare(args.app, args.output, args.work, args.revision, args.identity, args.team,
                      args.notary_profile, args.manifest_public_key))
    except NotarizationPending as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(3)


if __name__ == "__main__":
    main()
