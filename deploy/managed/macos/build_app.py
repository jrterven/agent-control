"""Build a signed ARM64 companion app; never install or start user services.

The runtime is an already built, target-native managed runtime directory. Its
Mach-O files are signed first, its inventory is then regenerated and signed,
and the outer app is sealed last. Notarization/DMG creation is a separate step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from deploy.connector.macos_signing import MACHO_MAGICS
from deploy.managed.manifest import create_manifest, file_inventory, sign_manifest

APP_NAME = "Agent Control.app"
APP_ID = "com.jemailabs.agent-control.setup"
SERVICE_ID = "com.jemailabs.agent-control.managed"


def invoke(*args: object) -> str:
    result = subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True, timeout=900)
    return result.stdout + result.stderr


def app_plist(revision: str, version: str, build_number: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("A full source commit SHA is required")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version) or not re.fullmatch(r"[1-9]\d{0,8}", build_number):
        raise ValueError("A numeric app version and build number are required")
    return {"CFBundleIdentifier": APP_ID, "CFBundleName": "Agent Control", "CFBundleDisplayName": "Agent Control",
            "CFBundleExecutable": "AgentControl", "CFBundlePackageType": "APPL", "CFBundleInfoDictionaryVersion": "6.0",
            "CFBundleShortVersionString": version, "CFBundleVersion": build_number, "CFBundleIconFile": "AgentControl",
            "LSMinimumSystemVersion": "13.0", "LSArchitecturePriority": ["arm64"],
            "NSHighResolutionCapable": True, "NSPrincipalClass": "NSApplication",
            "LSApplicationCategoryType": "public.app-category.utilities", "AgentControlRevision": revision}


def service_plist() -> dict:
    return {"Label": SERVICE_ID, "BundleProgram": "Contents/MacOS/AgentControlService",
            "ProgramArguments": ["AgentControlService"], "RunAtLoad": True, "KeepAlive": True,
            "ThrottleInterval": 15, "ProcessType": "Background", "AssociatedBundleIdentifiers": [APP_ID]}


def native_files(root: Path) -> list[Path]:
    # file_inventory also rejects links, special files, and unbounded inputs.
    file_inventory(root)
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as source:
                if source.read(4) in MACHO_MAGICS:
                    files.append(path)
    return sorted(files, key=lambda p: (-len(p.parts), str(p)))


def code_identifier(runtime: Path, path: Path) -> str:
    relative = path.relative_to(runtime).as_posix()
    return SERVICE_ID + ".runtime." + hashlib.sha256(relative.encode()).hexdigest()[:20]


def verify_code(path: Path, team: str, identifier: str, *, notarized: bool = False) -> tuple[str, str]:
    requirement = (f'anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists '
                   f'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists '
                   f'and certificate leaf[subject.OU] = "{team}" and identifier "{identifier}"')
    args = ["/usr/bin/codesign", "--verify", "--strict", "--verbose=2"]
    if notarized:
        args.append("--check-notarization")
        requirement += " and notarized"
    invoke(*args, "-R=" + requirement, path)
    details = invoke("/usr/bin/codesign", "--display", "--verbose=4", "--arch", "arm64", path)
    flags = re.search(r"^CodeDirectory .* flags=0x([a-fA-F0-9]+)", details, re.MULTILINE)
    if (f"TeamIdentifier={team}\n" not in details or not flags or not int(flags[1], 16) & 0x10000
            or not re.search(r"^Timestamp=.+$", details, re.MULTILINE)):
        raise ValueError("Code is missing the required identity, hardened runtime, or secure timestamp")
    cdhash = re.search(r"^CDHash=([a-f0-9]+)$", details, re.MULTILINE)
    if not cdhash:
        raise ValueError("Code has no verifiable ARM64 code-directory hash")
    return cdhash[1], "arm64"


def smoke_runtime(runtime: Path, home: Path) -> None:
    """Run only help/import diagnostics, with an isolated disposable home."""
    home.mkdir(mode=0o700)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
           "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "HERMES_HOME": str(home / ".hermes"),
           "SSL_CERT_FILE": str(runtime / "python/lib/python3.12/site-packages/certifi/cacert.pem"),
           "PYTHONPATH": str(runtime / "connector") + ":" + str(runtime / "hermes")}
    for args in (["-m", "agent_control_connector.setup_engine", "--help"],
                 ["-c", "import hermes_cli.main, ssl, sqlite3, anthropic, httpx, websockets, fastapi, uvicorn; print('signed runtime imports passed')"]):
        subprocess.run([str(runtime / "python/bin/python3"), "-s", "-B", *args], env=env, cwd=home,
                       check=True, capture_output=True, text=True, timeout=60)


def make_icon(resources: Path, scratch: Path) -> None:
    iconset = scratch / "AgentControl.iconset"
    iconset.mkdir()
    source = REPO / "apps/web/public/icon-512.png"
    for pixels in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            size = pixels * scale
            name = f"icon_{pixels}x{pixels}{'@2x' if scale == 2 else ''}.png"
            invoke("/usr/bin/sips", "-z", size, size, source, "--out", iconset / name)
    invoke("/usr/bin/iconutil", "-c", "icns", iconset, "-o", resources / "AgentControl.icns")


def build(runtime: Path, output: Path, revision: str, identity: str, team: str, key: Path,
          *, version: str = "0.1.0", build_number: str = "1", extras_catalog: Path | None = None) -> Path:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise ValueError("Build this app on a trusted Apple Silicon macOS signing host")
    if not re.fullmatch(r"[A-Fa-f0-9]{40}", identity) or not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise ValueError("Expected the Developer ID certificate SHA-1 and Apple Team ID")
    metadata = app_plist(revision, version, build_number)
    inventory = file_inventory(runtime)
    if "python/bin/python3" not in inventory or "connector/agent_control_connector/setup_engine.py" not in inventory:
        raise ValueError("The runtime must include portable Python and the managed setup engine")
    output.mkdir(parents=True, exist_ok=True)
    target = output / APP_NAME
    if target.exists() or target.is_symlink():
        raise ValueError("App output exists; reuse it for notarization or choose a new release directory")
    with tempfile.TemporaryDirectory(prefix=".managed-app-", dir=output) as temporary:
        stage = Path(temporary)
        scratch = stage / "swift-build"
        invoke("xcrun", "swift", "build", "--package-path", REPO / "apps/macos-installer",
               "--scratch-path", scratch, "--configuration", "release", "--triple", "arm64-apple-macosx13.0")
        binaries = Path(invoke("xcrun", "swift", "build", "--package-path", REPO / "apps/macos-installer",
                             "--scratch-path", scratch, "--configuration", "release", "--triple",
                             "arm64-apple-macosx13.0", "--show-bin-path").strip())
        app = stage / APP_NAME
        macos, resources = app / "Contents/MacOS", app / "Contents/Resources"
        agents = app / "Contents/Library/LaunchAgents"
        for directory in (macos, resources, agents):
            directory.mkdir(parents=True, exist_ok=True)
        for name in ("AgentControl", "AgentControlService", "AgentControlUpdater"):
            shutil.copy2(binaries / name, macos / name)
        (app / "Contents/Info.plist").write_bytes(plistlib.dumps(metadata))
        (agents / (SERVICE_ID + ".plist")).write_bytes(plistlib.dumps(service_plist()))
        make_icon(resources, stage)
        bundled = resources / "runtime"
        shutil.copytree(runtime, bundled, symlinks=False)
        if extras_catalog is not None:
            shutil.copyfile(extras_catalog, bundled / "extras-catalog.json")
        for path in native_files(bundled):
            identifier = code_identifier(bundled, path)
            invoke("/usr/bin/codesign", "--force", "--sign", identity, "--timestamp", "--options", "runtime",
                   "--identifier", identifier, path)
            verify_code(path, team, identifier)
        smoke_runtime(bundled, stage / "smoke-home")
        # This order matters: codesign modifies Mach-O bytes. The signed
        # inventory must describe those final bytes before sealing the app.
        create_manifest(bundled, revision, "macos-arm64")
        sign_manifest(bundled, key)
        if json.loads((bundled / "runtime-manifest.json").read_text())["files"] != file_inventory(bundled):
            raise ValueError("Runtime changed after signing its manifest")
        for name, identifier in (("AgentControlService", SERVICE_ID), ("AgentControlUpdater", APP_ID + ".updater"), ("AgentControl", APP_ID)):
            invoke("/usr/bin/codesign", "--force", "--sign", identity, "--timestamp", "--options", "runtime",
                   "--identifier", identifier, macos / name)
        invoke("/usr/bin/codesign", "--force", "--sign", identity, "--timestamp", "--options", "runtime", app)
        verify_code(app, team, APP_ID)
        verify_code(macos / "AgentControlService", team, SERVICE_ID)
        verify_code(macos / "AgentControlUpdater", team, APP_ID + ".updater")
        # Neither --deep signing nor any hardened-runtime exception is used.
        invoke("/usr/bin/codesign", "--verify", "--deep", "--strict", app)
        app.replace(target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--team", required=True)
    parser.add_argument("--manifest-private-key", type=Path, required=True)
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--build-number", default="1")
    parser.add_argument("--extras-catalog", type=Path)
    args = parser.parse_args()
    print(build(args.runtime, args.output, args.revision, args.identity, args.team, args.manifest_private_key,
                version=args.version, build_number=args.build_number, extras_catalog=args.extras_catalog))


if __name__ == "__main__":
    main()
