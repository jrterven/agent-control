"""Build a locked browser extra on its native Linux platform; never on user install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO / "packages/connector"), str(REPO / "packages/hermes-client")]
from deploy.managed.build import digest, extract, regular_copy
from deploy.managed.manifest import file_inventory

PINS = json.loads(Path(__file__).with_name("pins.json").read_text())
COMPONENTS = PINS["components"]
EXTRA_VERSION = f"agent-browser-{COMPONENTS['agentBrowser']}-chrome-{COMPONENTS['chromium']}-node-{COMPONENTS['node']}"


def current_platform():
    machine = platform.machine()
    return ("macos" if sys.platform == "darwin" else "linux") + "-" + ("arm64" if machine in {"arm64", "aarch64"} else machine)


def fetch(pin, cache):
    path = cache / pin["url"].rsplit("/", 1)[1]
    if path.exists() and path.stat().st_size == pin["size"] and digest(path) == pin["sha256"]:
        return path
    temporary = path.with_suffix(".partial")
    try:
        with urllib.request.urlopen(pin["url"], timeout=60) as source, temporary.open("wb") as output:
            total = 0
            while block := source.read(262144):
                total += len(block)
                if total > pin["size"]:
                    raise ValueError("Upstream artifact exceeds locked size")
                output.write(block)
        if total != pin["size"] or digest(temporary) != pin["sha256"]:
            raise ValueError("Upstream artifact differs from reviewed lock")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def unzip(archive, target):
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > 100_000 or sum(m.file_size for m in members) > 3_000_000_000:
            raise ValueError("Chrome archive exceeds limits")
        for member in members:
            path = Path(member.filename)
            mode = member.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or "\\" in member.filename or stat.S_ISLNK(mode):
                raise ValueError("Chrome archive contains unsupported links/paths")
            destination = target / path
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as contents, destination.open("wb") as output:
                    shutil.copyfileobj(contents, output)
                destination.chmod(0o755 if mode & 0o111 else 0o644)


def native_smoke(root):
    # An isolated blank local page proves native executables work without a
    # model, website, existing profile, npm install, or paid API request.
    with tempfile.TemporaryDirectory(prefix="ac-browser-cert-") as temporary:
        env = {"HOME": temporary, "PATH": str(root / "node/bin") + ":/usr/bin:/bin", "LANG": "C.UTF-8",
               "XDG_CACHE_HOME": temporary + "/cache", "XDG_CONFIG_HOME": temporary + "/config",
               "AGENT_BROWSER_IDLE_TIMEOUT_MS": "5000", "AGENT_BROWSER_PROXY": "http://127.0.0.1:9",
               "AGENT_BROWSER_PROXY_BYPASS": "<-loopback>",
               "AGENT_BROWSER_ARGS": "--disable-background-networking,--disable-component-update,--disable-sync"}
        def run(*args):
            result = subprocess.run(list(map(str, args)), capture_output=True, text=True,
                                    env=env, cwd=temporary, timeout=90)
            if result.returncode:
                raise ValueError("Isolated native browser smoke failed: " + (result.stderr or result.stdout)[-4000:])
            return result.stdout.strip()
        if run(root / "node/bin/node", "--version") != "v" + COMPONENTS["node"]:
            raise ValueError("Wrong bundled Node version")
        if COMPONENTS["agentBrowser"] not in run(root / "bin/agent-browser", "--version"):
            raise ValueError("Wrong native agent-browser version")
        if COMPONENTS["chromium"] not in run(root / "chromium/chrome", "--version"):
            raise ValueError("Wrong bundled Chromium version")
        session = "ac-cert-" + uuid.uuid4().hex[:12]
        try:
            run(root / "bin/agent-browser", "--session", session, "open", "about:blank")
            if "about:blank" not in run(root / "bin/agent-browser", "--session", session, "get", "url"):
                raise ValueError("Offline Chromium smoke did not open its blank test page")
        finally:
            subprocess.run([str(root / "bin/agent-browser"), "--session", session, "close"],
                           capture_output=True, env=env, cwd=temporary, timeout=30)


def build(target, revision, output, cache):
    if target != current_platform() or target not in {"linux-x86_64", "linux-arm64"}:
        raise ValueError("Native Linux build required. Mac browser extra remains unavailable pending bundle notarization.")
    if not re.fullmatch("[a-f0-9]{40}", revision) or PINS["components"] != COMPONENTS:
        raise ValueError("Invalid release or component lock")
    output, cache = output.resolve(), cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="browser-build-", dir=output) as temporary:
        stage = Path(temporary)
        root = stage / "browser-extra"
        root.mkdir()
        pin = PINS["platforms"][target]
        extract(fetch(pin["node"], cache), stage / "node-source")
        node = next((stage / "node-source").iterdir())
        (root / "node/bin").mkdir(parents=True)
        shutil.copyfile(node / "bin/node", root / "node/bin/node")
        shutil.copyfile(node / "LICENSE", root / "node/LICENSE")
        (root / "node/bin/node").chmod(0o755)
        extract(fetch(PINS["agentBrowser"], cache), stage / "agent-source")
        agent = stage / "agent-source/package"
        if json.loads((agent / "package.json").read_text()).get("version") != COMPONENTS["agentBrowser"]:
            raise ValueError("Incorrect npm artifact version")
        (root / "agent-browser/bin").mkdir(parents=True)
        shutil.copyfile(agent / "bin" / pin["nativeName"], root / "agent-browser/bin/native")
        (root / "agent-browser/bin/native").chmod(0o755)
        shutil.copyfile(agent / "package.json", root / "agent-browser/package.json")
        for name in ("LICENSE", "LICENSE.md"):
            if (agent / name).exists():
                shutil.copyfile(agent / name, root / "agent-browser" / name)
        unzip(fetch(pin["chromium"], cache), stage / "chrome-source")
        chrome = next((stage / "chrome-source").iterdir())
        regular_copy(chrome, root / "chromium", chrome.resolve())
        (root / "bin").mkdir()
        (root / "bin/agent-browser").write_text('''#!/bin/sh
set -eu
extra_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
export AGENT_BROWSER_EXECUTABLE_PATH="$extra_root/chromium/chrome"
exec "$extra_root/agent-browser/bin/native" --executable-path "$AGENT_BROWSER_EXECUTABLE_PATH" "$@"
''')
        (root / "bin/agent-browser").chmod(0o755)
        # Canonical modes are stable across umasks and archive tools.
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        native_smoke(root)
        files = file_inventory(root)
        manifest = {"schemaVersion": 1, "id": "browser", "release": revision, "platform": target,
                    "version": EXTRA_VERSION, "components": COMPONENTS, "pythonAbi": None,
                    "certification": {"native": True, "offlineBrowser": True}, "files": files,
                    "entrypoints": {"node": "node/bin/node", "agentBrowser": "bin/agent-browser", "chromium": "chromium/chrome"}}
        (root / "extra-manifest.json").write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
        result = output / f"agent-control-browser-{target}.tar.gz"
        if result.exists():
            raise ValueError("Browser build output already exists")
        with tarfile.open(result, "w:gz", dereference=True) as archive:
            archive.add(root, arcname="browser-extra")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.platform, args.revision, args.output, args.cache))
