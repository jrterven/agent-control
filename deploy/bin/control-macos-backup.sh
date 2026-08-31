#!/bin/zsh
set -euo pipefail

umask 077
unset HERMES_CONTROL_HERMES_DASHBOARD_TOKEN || true

release_root="${HERMES_CONTROL_RELEASE_ROOT:?HERMES_CONTROL_RELEASE_ROOT is required}"
env_file="${HERMES_CONTROL_ENV_FILE:?HERMES_CONTROL_ENV_FILE is required}"

if [[ ! -d "${release_root}" ]]; then
  print -u2 "Release root does not exist: ${release_root}"
  exit 1
fi
if [[ ! -f "${env_file}" ]]; then
  print -u2 "Control environment file does not exist: ${env_file}"
  exit 1
fi

set -a
source "${env_file}"
set +a
unset HERMES_CONTROL_HERMES_DASHBOARD_TOKEN || true

exec "${release_root}/deploy/bin/backup-sqlite.sh"
