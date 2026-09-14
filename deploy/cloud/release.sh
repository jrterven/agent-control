#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# != 3 ]]; then
  echo 'Usage: release.sh /absolute/compose.env IMAGE@sha256:DIGEST /absolute/backups' >&2
  exit 2
fi
config=$1
new_image=$2
backup_dir=$3
[[ "$config" == /* && -f "$config" && ! -L "$config" && "$backup_dir" == /* && -d "$backup_dir" ]] || exit 2
[[ "$new_image" =~ ^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$ ]] || { echo 'A verified immutable image digest is required' >&2; exit 2; }
command -v flock >/dev/null || { echo 'Install util-linux (flock) before running a release' >&2; exit 2; }
[[ ! -L "$config.release.lock" && ! -L "$config.previous" ]] || exit 2
exec 9>"$config.release.lock"
flock -n 9 || { echo 'Another release is already using this Compose configuration' >&2; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
compose=(docker compose --env-file "$config" -f "$script_dir/compose.yml")
[[ -n "$("${compose[@]}" ps -q control)" ]] || { echo 'Use the first-install runbook for an empty cloud deployment' >&2; exit 2; }
docker pull "$new_image" >/dev/null
switched=false
drain_attempted=false
drill=""
staged=""
cleanup() {
  if [[ -n "$staged" ]]; then rm -f -- "$staged"; fi
  if [[ -n "$drill" ]]; then "${compose[@]}" exec -T postgres dropdb -U agent_control --if-exists "$drill" >/dev/null 2>&1 || true; fi
  if [[ "$switched" == false && "$drain_attempted" == true ]]; then
    "${compose[@]}" exec -T control /opt/venv/bin/python -m hermes_control_api.cloud_operations resume >/dev/null || true
  fi
}
trap cleanup EXIT
drain_attempted=true
"${compose[@]}" exec -T control /opt/venv/bin/python -m hermes_control_api.cloud_operations drain
backup=$(bash "$script_dir/backup.sh" "$config" "$backup_dir")
drill="control_release_check_$(date -u +%Y%m%d%H%M%S)_${RANDOM}"
"${compose[@]}" exec -T postgres createdb -U agent_control "$drill"
"${compose[@]}" exec -T postgres pg_restore -U agent_control --dbname="$drill" --exit-on-error --no-owner --no-acl < "$backup"
HERMES_CONTROL_IMAGE="$new_image" "${compose[@]}" run --rm --no-deps --entrypoint /opt/venv/bin/python control -m hermes_control_api.cloud_migrations "$drill"
# Work may start locally while the backup and migration rehearsal run. Require
# a fresh inventory immediately before cutover, still with HTTP writes drained.
"${compose[@]}" exec -T control /opt/venv/bin/python -m hermes_control_api.cloud_operations drain
cp -p -- "$config" "$config.previous"
staged=$(mktemp "${config}.XXXXXXXX")
awk '!/^HERMES_CONTROL_IMAGE=/' "$config" > "$staged"
printf 'HERMES_CONTROL_IMAGE=%s\n' "$new_image" >> "$staged"
mv -- "$staged" "$config"
switched=true
HERMES_CONTROL_IMAGE="$new_image" "${compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 120 control
origin=$("${compose[@]}" exec -T control /opt/venv/bin/python -c 'from hermes_control_api.config import get_settings; print(get_settings().public_base_url)')
python3 "$script_dir/verify.py" "$origin"
printf 'Release verified. Previous Compose configuration: %s.previous; verified backup: %s\n' "$config" "$backup"
