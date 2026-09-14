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
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
compose=(docker compose --env-file "$config" -f "$script_dir/compose.yml")
staged=$(mktemp "$backup_dir/.control-XXXXXXXX.dump")
trap 'rm -f -- "$staged"' EXIT
"${compose[@]}" exec -T postgres pg_dump -U agent_control -d agent_control --format=custom --no-owner --no-acl > "$staged"
# Both a readable archive TOC and a successful restore drill are required.
"${compose[@]}" exec -T postgres pg_restore --list < "$staged" >/dev/null
drill="control_restore_$(date -u +%Y%m%d%H%M%S)_${RANDOM}"
"${compose[@]}" exec -T postgres createdb -U agent_control "$drill"
trap '"${compose[@]}" exec -T postgres dropdb -U agent_control --if-exists "$drill" >/dev/null; rm -f -- "$staged"' EXIT
"${compose[@]}" exec -T postgres pg_restore -U agent_control --dbname="$drill" --exit-on-error --no-owner --no-acl < "$staged"
"${compose[@]}" exec -T postgres psql -U agent_control -d "$drill" -v ON_ERROR_STOP=1 -Atc 'SELECT version_num FROM alembic_version' >/dev/null
"${compose[@]}" exec -T postgres dropdb -U agent_control "$drill"
destination="$backup_dir/control-$(date -u +%Y%m%dT%H%M%SZ)-${staged##*-}"
mv -- "$staged" "$destination"
trap - EXIT
printf '%s\n' "$destination"
