"""Run the fixed native exporter locally after an audited HTTP export failure."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

from hermes_client.compatibility import HERMES_0216_SHA, HERMES_CONNECTOR_REVISIONS
from .visual_media import profile_home


def export_snapshot(config: dict, name: str, output: Path) -> None:
    from .cli import detect_revision

    home = Path(config["hermesHome"])
    profile = profile_home(home, name)
    for directory in (home, home / "profiles", profile):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("Invalid local profile root")
    if name == "default" or config.get("sourceSha") not in HERMES_CONNECTOR_REVISIONS or output.exists():
        raise ValueError("Unsupported local profile export")
    revision, source = detect_revision(home, config["hermesSource"])
    if revision != config["sourceSha"]:
        raise ValueError("Hermes changed before profile export")
    environment = {key: value for key, value in os.environ.items() if key in {"HOME", "USER", "LANG", "LC_ALL", "TMPDIR"}}
    environment.update(PATH=os.defpath, HERMES_HOME=str(home), HERMES_DISABLE_LAZY_INSTALLS="1",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    if source.name == "hermes" and (source.parent / "runtime-manifest.json").exists():
        candidates = [source.parent / "python/bin/python3"]
    else:
        candidates = [root / environment / "bin/python" for root in (source, home / "hermes-agent")
                      for environment in ("venv", ".venv")]
        if revision == HERMES_0216_SHA:
            # Read only PM's committed interpreter; never provision dependencies
            # or run a launcher that can complete a pending upstream update.
            probe = """import json, sys
from pathlib import Path
source = Path(json.load(sys.stdin)['source'])
sys.path.insert(0, str(source))
from hermes_cli._launchers import resolve_store_python
python = resolve_store_python(source, publication=True)
print(json.dumps(str(python) if python else None))
"""
            result = subprocess.run([sys.executable, "-I", "-B", "-c", probe],
                input=json.dumps({"source": str(source)}).encode(), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=environment, cwd=source, timeout=15)
            if result.returncode or len(result.stdout) > 16384:
                raise ValueError("The installed Hermes Python could not be located")
            try:
                selected = json.loads(result.stdout)
            except (ValueError, UnicodeError):
                raise ValueError("Invalid Hermes runtime selection") from None
            if selected is not None:
                if not isinstance(selected, str) or not Path(selected).is_absolute():
                    raise ValueError("Invalid Hermes runtime selection")
                candidates = [Path(selected)]
    python = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    if python is None:
        raise ValueError("The installed Hermes Python could not be located")
    worker = Path(__file__).with_name("profile_export_worker.py").read_text()
    request = {"source": str(source), "home": str(home), "name": name, "output": str(output), "activateDependencies": revision == HERMES_0216_SHA}
    result = subprocess.run([str(python), "-I", "-B", "-c", worker], input=json.dumps(request).encode(),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=environment,
                            cwd=source, timeout=90)
    if result.returncode or not output.is_file():
        raise ValueError("Local profile export failed; source preserved")
