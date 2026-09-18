#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 4 ]]; then
  echo 'Usage: daily-backup.sh /absolute/compose.env /absolute/backups /absolute/r2.json /absolute/python' >&2
  exit 2
fi
config=$1
backup_dir=$2
r2_config=$3
python=$4
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ "$config" == /* && "$backup_dir" == /* && "$r2_config" == /* && "$python" == /* && -x "$python" ]] || exit 2
# Open and validate the lock before any backup work. The supervisor retains it
# throughout the restore, upload, GC and retention steps without a shell reopen.
exec "$python" - "$config" "$backup_dir" "$r2_config" "$script_dir" <<'PY'
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

config, directory, r2_config, script_dir = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location("backup_ops", script_dir / "r2_backup.py")
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)
lock = None
try:
    ops.private_directory(directory)
    lock = ops.release_lock(config, wait_seconds=300)
    result = subprocess.run(["bash", str(script_dir / "backup.sh"), str(config), str(directory)],
                            check=True, stdout=subprocess.PIPE, text=True)
    created = Path(result.stdout.strip())
    ops.require(created.parent == directory and ops.NAME.fullmatch(created.name), "invalid_backup_path")
    subprocess.run([sys.executable, str(script_dir / "r2_backup.py"), str(created),
                    "--lock-fd", str(lock), "--r2-config", str(r2_config),
                    "--compose-env", str(config), "--compose-file", str(script_dir / "compose.yml")],
                   pass_fds=(lock,), check=True)
    # Purge only after a complete off-host database+image restoration succeeds.
    subprocess.run(["docker", "compose", "--env-file", str(config), "-f", str(script_dir / "compose.yml"),
                    "exec", "-T", "control", "/opt/venv/bin/python", "-m", "hermes_control_api.visual_media", "gc"], check=True)
    for dump in directory.iterdir():
        if dump == created or not ops.NAME.fullmatch(dump.name):
            continue
        info = dump.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mtime >= time.time() - 30*86400:
            continue
        for path in (dump, Path(str(dump)+".media.tar"), Path(str(dump)+".verified.json")):
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass
    print("Database and images restored from R2; retention completed: " + created.name)
except Exception as exc:
    code = str(exc) if type(exc) is ops.BackupError else type(exc).__name__
    print(json.dumps({"status": "failed", "code": code}))
    raise SystemExit(75 if code == "release_lock_unavailable" else 1)
finally:
    if lock is not None:
        os.close(lock)
PY
