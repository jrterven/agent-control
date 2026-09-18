#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 2 ]]; then
  echo "Usage: backup.sh /absolute/compose.env /absolute/backup-directory" >&2
  exit 2
fi
config=$1
backup_dir=$2
[[ "$config" == /* && "$backup_dir" == /* && -f "$config" && ! -L "$config" && -d "$backup_dir" && ! -L "$backup_dir" ]] || exit 2
# The output and lock directories must be private before creating any files.
python3 - "$config" "$backup_dir" <<'PY_PRIVATE'
import os
from pathlib import Path
import stat
import sys
config, directory = map(Path, sys.argv[1:])
for path in (config.parent, directory):
    info = path.lstat()
    if (not path.is_absolute() or path != path.resolve(strict=True) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise SystemExit("Unsafe backup or configuration directory")
info = config.lstat()
if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
    raise SystemExit("Unsafe backup configuration")
PY_PRIVATE
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
compose=(docker compose --env-file "$config" -f "$script_dir/compose.yml")
staged=$(mktemp "$backup_dir/.control-XXXXXXXX.dump")
media_stage=""
media_archive=""
drill=""
cleanup() {
  if [[ -n "$drill" ]]; then "${compose[@]}" exec -T postgres dropdb -U agent_control --if-exists "$drill" >/dev/null 2>&1 || true; fi
  rm -f -- "$staged"
  if [[ -n "$media_archive" ]]; then rm -f -- "$media_archive"; fi
  if [[ -n "$media_stage" ]]; then rm -rf -- "$media_stage"; fi
}
trap cleanup EXIT
"${compose[@]}" exec -T postgres pg_dump -U agent_control -d agent_control --format=custom --no-owner --no-acl > "$staged"
# Both a readable archive TOC and a successful restore drill are required.
"${compose[@]}" exec -T postgres pg_restore --list < "$staged" >/dev/null
drill="control_restore_$(date -u +%Y%m%d%H%M%S)_${RANDOM}"
"${compose[@]}" exec -T postgres createdb -U agent_control "$drill"
"${compose[@]}" exec -T postgres pg_restore -U agent_control --dbname="$drill" --exit-on-error --no-owner --no-acl < "$staged"
"${compose[@]}" exec -T postgres psql -U agent_control -d "$drill" -v ON_ERROR_STOP=1 -Atc 'SELECT version_num FROM alembic_version' >/dev/null
# Read image references from the restored dump, not a changing live database.
# Older pre-image releases have no table and retain their original backup path.
has_media=$("${compose[@]}" exec -T postgres psql -U agent_control -d "$drill" -v ON_ERROR_STOP=1 -Atc "SELECT CASE WHEN to_regclass('public.visual_media') IS NULL THEN 'no' ELSE 'yes' END")
if [[ "$has_media" == yes ]]; then
  [[ "$backup_dir" != *:* && "$backup_dir" != *,* ]] || exit 2
  media_stage=$(mktemp -d "$backup_dir/.media-XXXXXXXX")
  media_name=${media_stage##*/}
  media_command=("${compose[@]}" run --rm --no-deps --user "$(id -u):$(id -g)" --volume "$backup_dir:/backup" --entrypoint /opt/venv/bin/python control -m hermes_control_api.visual_media)
  "${media_command[@]}" backup "/backup/$media_name" --database "$drill" >&2
  # A complete restore into an isolated local blob store checks every reference,
  # image and thumbnail. This never writes to the production image bucket.
  "${media_command[@]}" verify-backup "/backup/$media_name" --database "$drill" >&2
  media_archive=$(mktemp "$backup_dir/.media-XXXXXXXX.tar")
  tar -cf "$media_archive" -C "$media_stage" .
  rm -rf -- "$media_stage"
  media_stage=""
fi
"${compose[@]}" exec -T postgres dropdb -U agent_control "$drill"
drill=""
destination="$backup_dir/control-$(date -u +%Y%m%dT%H%M%SZ)-${staged##*-}"
if [[ -n "$media_archive" ]]; then
  mv -- "$media_archive" "$destination.media.tar"
  media_archive=""
fi
mv -- "$staged" "$destination"
trap - EXIT
printf '%s\n' "$destination"
