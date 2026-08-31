#!/usr/bin/env bash

log() {
  printf '[agent-control] %s\n' "$*"
}

warn() {
  printf '[agent-control] warning: %s\n' "$*" >&2
}

die() {
  printf '[agent-control] error: %s\n' "$*" >&2
  exit 1
}

shell_join() {
  local joined="" arg
  for arg in "$@"; do
    if [[ -n "$joined" ]]; then
      joined+=" "
    fi
    joined+="$(printf '%q' "$arg")"
  done
  printf '%s\n' "$joined"
}

run_or_print() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    log "dry-run $(shell_join "$@")"
    return 0
  fi
  "$@"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

absolute_path_or_die() {
  case "$1" in
    /*) ;;
    *) die "Expected an absolute path, got: $1" ;;
  esac
}

utc_timestamp() {
  date -u +%Y%m%dT%H%M%SZ
}

git_repo_root() {
  git -C "$1" rev-parse --show-toplevel 2>/dev/null || die "Not inside a Git repository: $1"
}

git_head_sha() {
  git -C "$1" rev-parse HEAD
}

git_is_dirty() {
  [[ -n "$(git -C "$1" status --short --untracked-files=normal)" ]]
}

random_token() {
  python3 - <<'PY'
import secrets

print(secrets.token_urlsafe(32))
PY
}

env_get() {
  local env_file="$1"
  local key="$2"
  python3 - "$env_file" "$key" <<'PY'
from __future__ import annotations

import os
import pathlib
import shlex
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
if not path.exists() or not os.access(path, os.R_OK):
    raise SystemExit(0)
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    try:
        parsed = shlex.split(line, posix=True)
    except ValueError:
        continue
    if not parsed:
        continue
    candidate = parsed[0]
    if "=" not in candidate:
        continue
    current_key, value = candidate.split("=", 1)
    if current_key == key:
        print(value)
        break
PY
}

env_key_exists() {
  local env_file="$1"
  local key="$2"
  python3 - "$env_file" "$key" <<'PY'
from __future__ import annotations

import os
import pathlib
import shlex
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
if not path.exists() or not os.access(path, os.R_OK):
    raise SystemExit(1)
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    try:
        parsed = shlex.split(line, posix=True)
    except ValueError:
        continue
    if not parsed:
        continue
    candidate = parsed[0]
    if "=" not in candidate:
        continue
    current_key, _ = candidate.split("=", 1)
    if current_key == key:
        raise SystemExit(0)
raise SystemExit(1)
PY
}

replace_symlink_atomically() {
  local target_path="$1"
  local link_path="$2"
  python3 - "$target_path" "$link_path" <<'PY'
from __future__ import annotations

import os
import pathlib
import uuid
import sys

target = sys.argv[1]
link = pathlib.Path(sys.argv[2])
link.parent.mkdir(parents=True, exist_ok=True)
temporary = link.with_name(f".{link.name}.{uuid.uuid4().hex}.tmp")
if temporary.exists() or temporary.is_symlink():
    temporary.unlink()
os.symlink(target, temporary)
os.replace(temporary, link)
PY
}

write_env_file() {
  local env_file="$1"
  local header="$2"
  shift 2
  python3 - "$env_file" "$header" "$@" <<'PY'
from __future__ import annotations

import pathlib
import shlex
import sys
from collections import OrderedDict

env_path = pathlib.Path(sys.argv[1])
header = sys.argv[2]
pairs = sys.argv[3:]

existing: OrderedDict[str, str] = OrderedDict()
if env_path.exists():
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        try:
            parsed = shlex.split(line, posix=True)
        except ValueError:
            continue
        if not parsed:
            continue
        candidate = parsed[0]
        if "=" not in candidate:
            continue
        key, value = candidate.split("=", 1)
        existing[key] = value

for pair in pairs:
    key, value = pair.split("=", 1)
    existing[key] = value

lines = [header.rstrip(), ""]
for key, value in existing.items():
    lines.append(f"{key}={shlex.quote(value)}")
lines.append("")
payload = "\n".join(lines)
env_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = env_path.with_name(f".{env_path.name}.tmp")
tmp_path.write_text(payload, encoding="utf-8")
tmp_path.replace(env_path)
PY
}

render_template_file() {
  local template_file="$1"
  local output_file="$2"
  shift 2
  python3 - "$template_file" "$output_file" "$@" <<'PY'
from __future__ import annotations

import pathlib
import sys
from xml.sax.saxutils import escape

template_path = pathlib.Path(sys.argv[1])
output_path = pathlib.Path(sys.argv[2])
replacements = sys.argv[3:]
content = template_path.read_text(encoding="utf-8")
for pair in replacements:
    needle, replacement = pair.split("=", 1)
    content = content.replace(
        needle,
        escape(replacement, {'"': "&quot;", "'": "&apos;"}),
    )
output_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = output_path.with_name(f".{output_path.name}.tmp")
tmp_path.write_text(content, encoding="utf-8")
tmp_path.replace(output_path)
PY
}

copy_repo_snapshot() {
  local repo_root="$1"
  local release_dir="$2"
  python3 - "$repo_root" "$release_dir" <<'PY'
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

repo_root = pathlib.Path(sys.argv[1]).resolve()
release_dir = pathlib.Path(sys.argv[2]).resolve()
release_dir.mkdir(parents=True, exist_ok=True)

files = subprocess.check_output(
    ["git", "-C", str(repo_root), "ls-files", "-z"],
)
for raw_path in files.split(b"\0"):
    if not raw_path:
        continue
    relative = pathlib.Path(raw_path.decode("utf-8"))
    source = repo_root / relative
    target = release_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
      if target.exists() or target.is_symlink():
          target.unlink()
      os.symlink(os.readlink(source), target)
      continue
    shutil.copy2(source, target)
PY
}

write_text_file() {
  local output_file="$1"
  local mode="$2"
  local content="$3"
  python3 - "$output_file" "$mode" "$content" <<'PY'
from __future__ import annotations

import pathlib
import sys

path = pathlib.Path(sys.argv[1])
mode = int(sys.argv[2], 8)
content = sys.argv[3]
path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = path.with_name(f".{path.name}.tmp")
tmp_path.write_text(content, encoding="utf-8")
tmp_path.replace(path)
path.chmod(mode)
PY
}
