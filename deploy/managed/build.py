"""Build the managed runtime on its target platform; end users never compile it."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from deploy.managed.manifest import MAX_BYTES, MAX_FILES, PINS


def invoke(*args, **kwargs):
    return subprocess.run([str(x) for x in args], check=True, **kwargs)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def fetch_python(target: str, cache: Path) -> Path:
    pin = PINS["python"][target]
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"python-{target}-{pin['sha256']}.tar.gz"
    if archive.exists() and digest(archive) == pin["sha256"]:
        return archive
    temporary = archive.with_suffix(".download")
    try:
        with urllib.request.urlopen(pin["url"], timeout=60) as response, temporary.open("wb") as output:
            total = 0
            while chunk := response.read(262144):
                total += len(chunk)
                if total > 250_000_000:
                    raise ValueError("Python download exceeds size limit")
                output.write(chunk)
        if digest(temporary) != pin["sha256"]:
            raise ValueError("Portable Python integrity verification failed")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def extract(archive: Path, target: Path) -> None:
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > MAX_FILES or sum(m.size for m in members) > MAX_BYTES:
            raise ValueError("Build archive exceeds limits")
        if any(Path(m.name).is_absolute() or ".." in Path(m.name).parts or "\\" in m.name for m in members):
            raise ValueError("Unsafe build archive path")
        source.extractall(target, filter="data")


def regular_copy(source: Path, destination: Path, root: Path, seen=frozenset(), total=None) -> None:
    total = [0] if total is None else total
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved in seen:
        raise ValueError("Runtime link escapes the source or forms a cycle")
    if resolved.is_dir():
        destination.mkdir()
        for child in resolved.iterdir():
            regular_copy(child, destination / child.name, root, seen | {resolved}, total)
    elif resolved.is_file():
        total[0] += resolved.stat().st_size
        if total[0] > MAX_BYTES:
            raise ValueError("Materialized runtime exceeds limits")
        shutil.copy2(resolved, destination)
    else:
        raise ValueError("Runtime contains a special file")


def archive_source(source: Path, target: Path, revision: str, stage: Path) -> None:
    actual = invoke("git", "-C", source, "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()
    dirty = invoke("git", "-C", source, "status", "--porcelain", "--untracked-files=no", capture_output=True, text=True).stdout.strip()
    if actual != revision or dirty:
        raise ValueError("Build requires the exact clean source revision")
    archive = stage / (target.name + "-source.tar")
    with archive.open("wb") as output:
        invoke("git", "-C", source, "archive", revision, stdout=output)
    unpack = stage / (target.name + "-source")
    unpack.mkdir()
    extract(archive, unpack)
    regular_copy(unpack, target, unpack.resolve())


LAUNCHER = '''#!/bin/sh
set -eu
release_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
export AGENT_CONTROL_RELEASE_ROOT="$release_root"
export PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
export PYTHONPATH="$release_root/connector:$release_root/hermes"
export SSL_CERT_FILE="$release_root/python/lib/python3.12/site-packages/certifi/cacert.pem"
unset PYTHONHOME PYTHONSTARTUP
exec "$release_root/python/bin/python3" -B -m agent_control_connector.setup_engine "$@"
'''


def smoke(root: Path, work: Path) -> None:
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", HERMES_HOME=str(work / "hermes-home"),
               PYTHONPATH=os.pathsep.join((str(root / "connector"), str(root / "hermes"))),
               AGENT_CONTROL_RELEASE_ROOT=str(root), HOME=str(work / "home"))
    (work / "home").mkdir(parents=True, exist_ok=True)
    invoke(root / "python/bin/python3", "-B", "-c",
           "import sys,ssl,sqlite3,anthropic,httpx,websockets,fastapi,uvicorn,hermes_cli,agent_control_connector; assert sys.version_info[:2] == (3,12)", env=env, cwd=work)
    invoke(root / "bin/agent-control-setup", "--help", env=env, cwd=work)
    invoke(root / "python/bin/python3", "-B", "-m", "hermes_cli.main", "serve", "--help", env=env, cwd=work)
    invoke(root / "python/bin/python3", "-B", Path(__file__).with_name("smoke.py"), "--root", root, "--work", work, env=env, cwd=work)
    if sys.platform == "linux":
        invoke(root / "python/bin/python3", "-B", Path(__file__).with_name("systemd_smoke.py"),
               "--root", root, "--work", work, env=env, cwd=work)


def build(output: Path, revision: str, target: str, cache: Path, hermes_source: Path | None = None) -> Path:
    actual_target = ("macos" if sys.platform == "darwin" else "linux") + "-" + ("arm64" if platform.machine() in {"arm64", "aarch64"} else platform.machine())
    if target != actual_target or target not in PINS["python"]:
        raise ValueError("Build must run natively on a supported target")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="managed-runtime-") as temporary:
        stage = Path(temporary)
        root = stage / "agent-control-runtime"
        root.mkdir()
        unpack = stage / "python-unpack"
        unpack.mkdir()
        extract(fetch_python(target, cache), unpack)
        regular_copy(unpack / "python", root / "python", (unpack / "python").resolve())
        if hermes_source is None:
            hermes_source = stage / "hermes-checkout"
            invoke("git", "init", hermes_source)
            invoke("git", "-C", hermes_source, "fetch", "--depth=1", PINS["hermesRepository"], PINS["hermesSourceSha"])
            invoke("git", "-C", hermes_source, "checkout", "--detach", "FETCH_HEAD")
        archive_source(hermes_source, root / "hermes", PINS["hermesSourceSha"], stage)
        archive_source(REPO, stage / "control-source", revision, stage)
        (root / "connector").mkdir()
        (root / "connector/distribution-metadata").mkdir()
        for package, folder in (("connector", "agent_control_connector"), ("hermes-client", "hermes_client")):
            shutil.copytree(stage / "control-source/packages" / package / folder, root / "connector" / folder)
            shutil.copyfile(stage / "control-source/packages" / package / "pyproject.toml",
                            root / "connector/distribution-metadata" / (package + ".toml"))
        requirements = root / "requirements.lock"
        uv_version = invoke(sys.executable, "-m", "uv", "--version", capture_output=True, text=True).stdout.split()[1]
        if uv_version != PINS["uvVersion"]:
            raise ValueError("Install the pinned uv build tool version")
        invoke(sys.executable, "-m", "uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--extra", "anthropic",
               "--output-file", requirements, "--no-python-downloads", cwd=root / "hermes", stdout=subprocess.DEVNULL)
        env = os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull)
        for key in ("PYTHONHOME", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST"):
            env.pop(key, None)
        # All third-party packages must have locked hashes and native wheels.
        # No fallback can compile code or silently choose different versions.
        invoke(root / "python/bin/python3", "-B", "-m", "pip", "install", "--index-url", "https://pypi.org/simple", "--require-hashes", "--only-binary=:all:",
               "--no-compile", "--no-deps", "-r", requirements, env=env)
        # Hermes rejects wheels because they omit source-relative assets. Keep
        # its entire source tree and generate only standard distribution metadata.
        invoke(sys.executable, "-c", "import setuptools.build_meta as b,sys;b.prepare_metadata_for_build_wheel(sys.argv[1])",
               root / "connector", cwd=root / "hermes", env=env)
        for generated in (root / "hermes").glob("*.egg-info"):
            shutil.rmtree(generated)
        (root / "bin").mkdir()
        (root / "bin/agent-control-setup").write_text(LAUNCHER)
        (root / "bin/agent-control-setup").chmod(0o755)
        # Console entrypoints generated by pip contain the temporary build path;
        # all runtime launches use the relocatable interpreter via -m instead.
        for entry in (root / "python/bin").iterdir():
            if entry.is_file() and entry.read_bytes()[:2] == b"#!":
                entry.unlink()
        inventory_code = '''import importlib.metadata as m,json,sys,tomllib
from pathlib import Path
r=Path(sys.argv[1]); rows=[]
for d in m.distributions(path=[str(r/'python/lib/python3.12/site-packages'),str(r/'connector')]):
 rows.append({'name':d.metadata.get('Name'),'version':d.version,'license':d.metadata.get('License-Expression') or d.metadata.get('License'),'licenseFiles':[str(p) for p in (d.files or []) if any(s in str(p).lower() for s in ('license','copying','notice'))]})
for p in (r/'connector/distribution-metadata').glob('*.toml'):
 d=tomllib.loads(p.read_text())['project']; rows.append({'name':d['name'],'version':d['version'],'license':d.get('license','NOASSERTION'),'licenseFiles':[]})
(r/'licenses.json').write_text(json.dumps({'schemaVersion':1,'packages':sorted(rows,key=lambda v:v['name'].lower()),'hermesLicense':'hermes/LICENSE','pythonLicense':'python/lib/python3.12/LICENSE.txt'},sort_keys=True,indent=2)+'\\n')
'''
        invoke(root / "python/bin/python3", "-B", "-c", inventory_code, root, env=env)
        provenance = {"schemaVersion": 1, "release": revision, "platform": target, "hermesSourceSha": PINS["hermesSourceSha"],
                      "hermesVersion": PINS["hermesVersion"], "pythonVersion": PINS["pythonVersion"],
                      "pythonArchiveSha256": PINS["python"][target]["sha256"], "uvVersion": PINS["uvVersion"],
                      "requirementsSha256": digest(requirements), "hermesLockSha256": digest(root / "hermes/uv.lock"), "baseExtras": PINS["baseExtras"]}
        (root / "build-provenance.json").write_text(json.dumps(provenance, sort_keys=True, indent=2) + "\n")
        relocated = stage / "relocated path with spaces" / "agent-control-runtime"
        relocated.parent.mkdir()
        root.rename(relocated)
        smoke(relocated, stage / "smoke")
        archive = output / f"agent-control-runtime-{target}.tar.gz"
        if archive.exists():
            raise ValueError("Build artifact already exists")
        with tarfile.open(archive, "w:gz", dereference=True) as package:
            package.add(relocated, arcname="agent-control-runtime")
        (output / f"{target}.build.json").write_text(json.dumps({**provenance, "archiveSha256": digest(archive), "relocationSmoke": True}, indent=2) + "\n")
        return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--platform", choices=list(PINS["python"]), required=True)
    parser.add_argument("--cache", type=Path, default=Path(".cache/managed-runtime"))
    parser.add_argument("--hermes-source", type=Path)
    args = parser.parse_args()
    print(build(args.output.resolve(), args.revision, args.platform, args.cache.resolve(), args.hermes_source))
