#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  printf 'Usage: %s --control-stopped BACKUP_DB DESTINATION_DB QUARANTINE_PARENT\n' "$0" >&2
  exit 64
}

[[ "${1:-}" == "--control-stopped" ]] || usage
shift
[[ "$#" -eq 3 ]] || usage

readonly backup_path="$1"
readonly database_path="$2"
readonly quarantine_parent="$3"

for path in "$backup_path" "$database_path" "$quarantine_parent"; do
  if [[ "$path" != /* || "$path" == "/" || "$path" == *"'"* ]]; then
    printf 'Restore paths must be explicit absolute paths without single quotes.\n' >&2
    exit 64
  fi
done
if [[ "$backup_path" == "$database_path" ]]; then
  printf 'Backup and destination must be different files.\n' >&2
  exit 64
fi
if [[ ! -f "$backup_path" || -L "$backup_path" ]]; then
  printf 'Backup must be an existing regular, non-symlink file: %s\n' "$backup_path" >&2
  exit 66
fi
if [[ -e "$database_path" && ( ! -f "$database_path" || -L "$database_path" ) ]]; then
  printf 'Destination must be a regular, non-symlink file: %s\n' "$database_path" >&2
  exit 66
fi
command -v sqlite3 >/dev/null || { printf 'sqlite3 is required.\n' >&2; exit 69; }

readonly database_dir="${database_path%/*}"
[[ -d "$database_dir" ]] || { printf 'Destination directory does not exist: %s\n' "$database_dir" >&2; exit 66; }
install -d -m 0700 "$quarantine_parent"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
quarantine_dir="${quarantine_parent}/restore-${timestamp}-$$"
install -d -m 0700 "$quarantine_dir"
temporary="${database_path}.restore-$$.partial"

cleanup() {
  if [[ -f "$temporary" ]]; then
    rm -f -- "$temporary"
  fi
}
trap cleanup EXIT INT TERM

sqlite3 "$backup_path" "PRAGMA integrity_check;" | grep -qx 'ok'
sqlite3 "$backup_path" ".timeout 10000" ".backup '${temporary}'"
sqlite3 "$temporary" "PRAGMA integrity_check;" | grep -qx 'ok'
chmod 0600 "$temporary"

if [[ -f "$database_path" ]]; then
  cp -p -- "$database_path" "$quarantine_dir/control.db"
fi
for suffix in -wal -shm; do
  sidecar="${database_path}${suffix}"
  if [[ -f "$sidecar" && ! -L "$sidecar" ]]; then
    mv -- "$sidecar" "$quarantine_dir/control.db${suffix}"
  fi
done

mv -f -- "$temporary" "$database_path"
trap - EXIT INT TERM
sqlite3 "$database_path" "PRAGMA integrity_check;" | grep -qx 'ok'
chmod 0600 "$database_path"

printf 'restored=%s\nquarantine=%s\n' "$database_path" "$quarantine_dir"
