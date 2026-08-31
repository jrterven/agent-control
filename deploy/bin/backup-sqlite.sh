#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly database_path="${HERMES_CONTROL_DATABASE_PATH:-/var/lib/hermes-control/control.db}"
readonly backup_dir="${HERMES_CONTROL_BACKUP_DIR:-/var/backups/hermes-control}"
readonly database_url="${HERMES_CONTROL_DATABASE_URL:-}"

if [[ "$database_path" != /* || "$backup_dir" != /* ]]; then
  printf 'Database and backup paths must be absolute.\n' >&2
  exit 64
fi
if [[ "$database_path" == *"'"* || "$backup_dir" == *"'"* ]]; then
  printf 'Paths containing single quotes are not supported.\n' >&2
  exit 64
fi
if [[ -z "$database_url" || "$database_url" == *'?'* || "$database_url" == *'#'* ]]; then
  printf 'HERMES_CONTROL_DATABASE_URL must be an absolute SQLite file URL without query or fragment.\n' >&2
  exit 64
fi
case "$database_url" in
  sqlite:////*) database_url_path="${database_url#sqlite:///}" ;;
  sqlite+pysqlite:////*) database_url_path="${database_url#sqlite+pysqlite:///}" ;;
  *)
    printf 'HERMES_CONTROL_DATABASE_URL must use sqlite://// or sqlite+pysqlite:////.\n' >&2
    exit 64
    ;;
esac
readonly database_url_path
if [[ "$database_url_path" != "$database_path" ]]; then
  printf 'Refusing backup: DATABASE_PATH does not exactly match DATABASE_URL (%s != %s).\n' \
    "$database_path" "$database_url_path" >&2
  exit 64
fi
if [[ ! -f "$database_path" ]]; then
  printf 'Database does not exist: %s\n' "$database_path" >&2
  exit 66
fi
command -v sqlite3 >/dev/null || { printf 'sqlite3 is required.\n' >&2; exit 69; }

if [[ -L "$backup_dir" ]]; then
  printf 'Backup directory must not be a symbolic link: %s\n' "$backup_dir" >&2
  exit 64
fi
install -d -m 0700 "$backup_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
temporary="$(mktemp "${backup_dir}/.control-${timestamp}.partial.XXXXXX")"
unique_suffix="${temporary##*.partial.}"
destination="${backup_dir}/control-${timestamp}-${unique_suffix}.db"

cleanup() {
  if [[ -f "$temporary" ]]; then
    rm -f -- "$temporary"
  fi
}
trap cleanup EXIT INT TERM

sqlite3 "$database_path" ".timeout 10000" ".backup '${temporary}'"
sqlite3 "$temporary" "PRAGMA quick_check;" | grep -qx 'ok'
chmod 0600 "$temporary"
ln "$temporary" "$destination"
rm -f -- "$temporary"
trap - EXIT INT TERM
printf '%s\n' "$destination"
