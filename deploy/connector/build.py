"""Build a standalone connector on its target OS/architecture (no cross compile)."""
from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[2]


def build(output: Path, revision: str) -> Path:
    from agent_control_connector.manage import RELEASE_ID, archive_platform
    if not RELEASE_ID.fullmatch(revision):
        raise ValueError("Invalid release identifier")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="connector-build-") as temporary:
        stage = Path(temporary)
        entry = stage / "entry.py"
        entry.write_text("from agent_control_connector.cli import main\nif __name__ == '__main__':\n    raise SystemExit(main())\n")
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
            "--name", "agent-control-connector", "--distpath", str(stage / "dist"),
            "--workpath", str(stage / "work"), "--specpath", str(stage),
            "--collect-submodules", "agent_control_connector",
            "--collect-submodules", "hermes_client",
            "--copy-metadata", "agent-control-connector",
            str(entry),
        ], check=True)
        bundle = stage / "dist/agent-control-connector"
        (bundle / "release.json").write_text(json.dumps({
            "revision": revision, "system": platform.system(), "architecture": platform.machine(),
            "protocol": 1,
        }) + "\n")
        subprocess.run([str(bundle / "agent-control-connector"), "--help"], check=True)
        target = output / archive_platform()
        with tarfile.open(target, "w:gz", dereference=True) as archive:
            archive.add(bundle, arcname="agent-control-connector")
        return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    print(build(args.output, args.revision))
