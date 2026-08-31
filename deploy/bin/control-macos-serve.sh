#!/bin/zsh
set -euo pipefail

umask 077
unset HERMES_CONTROL_HERMES_DASHBOARD_TOKEN || true

release_root="${HERMES_CONTROL_RELEASE_ROOT:?HERMES_CONTROL_RELEASE_ROOT is required}"
env_file="${HERMES_CONTROL_ENV_FILE:?HERMES_CONTROL_ENV_FILE is required}"
control_port="${HERMES_CONTROL_PORT:-8000}"
keychain_service="${HERMES_TOKEN_KEYCHAIN_SERVICE:-com.agent-control.hermes-dashboard}"
keychain_account="${HERMES_TOKEN_KEYCHAIN_ACCOUNT:-$(/usr/bin/id -un)}"

if [[ ! -d "${release_root}" ]]; then
  print -u2 "Release root does not exist: ${release_root}"
  exit 1
fi
if [[ ! -f "${env_file}" ]]; then
  print -u2 "Control environment file does not exist: ${env_file}"
  exit 1
fi
if [[ ! "${control_port}" =~ '^[0-9]+$' ]] || (( control_port < 1024 || control_port > 65535 )); then
  print -u2 "HERMES_CONTROL_PORT must be an unprivileged TCP port."
  exit 1
fi

set -a
source "${env_file}"
set +a
unset HERMES_CONTROL_HERMES_DASHBOARD_TOKEN || true

session_token="$(
  /usr/bin/security find-generic-password \
    -a "${keychain_account}" \
    -s "${keychain_service}" \
    -w
)"
if (( ${#session_token} < 32 || ${#session_token} > 512 )) || [[ ! "${session_token}" =~ ^[A-Za-z0-9._~-]+$ ]]; then
  print -u2 "The Hermes dashboard token stored in Keychain must be 32-512 URL-safe characters."
  exit 1
fi

export HERMES_CONTROL_ENVIRONMENT="${HERMES_CONTROL_ENVIRONMENT:-production}"
export HERMES_CONTROL_CREATE_SCHEMA_ON_START=false
export HERMES_CONTROL_STATIC_DIR="${release_root}/apps/api/static"
env HERMES_CONTROL_HERMES_DASHBOARD_TOKEN="${session_token}" \
  "${release_root}/.venv/bin/alembic" -c "${release_root}/apps/api/alembic.ini" upgrade head

exec env HERMES_CONTROL_HERMES_DASHBOARD_TOKEN="${session_token}" \
  "${release_root}/.venv/bin/uvicorn" hermes_control_api.main:app \
  --host 127.0.0.1 \
  --port "${control_port}" \
  --workers 1 \
  --no-proxy-headers \
  --ws-max-size 4096
