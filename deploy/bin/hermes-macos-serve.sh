#!/bin/zsh
set -euo pipefail

umask 077

hermes_binary="${HERMES_BIN:?HERMES_BIN is required}"
hermes_profile="${HERMES_PROFILE:-default}"
hermes_port="${HERMES_PORT:-9119}"
keychain_service="${HERMES_TOKEN_KEYCHAIN_SERVICE:-com.agent-control.hermes-dashboard}"
keychain_account="${HERMES_TOKEN_KEYCHAIN_ACCOUNT:-$(/usr/bin/id -un)}"

if [[ ! -x "${hermes_binary}" ]]; then
  print -u2 "Hermes executable was not found at the configured path."
  exit 1
fi
if [[ ! "${hermes_port}" =~ '^[0-9]+$' ]] || (( hermes_port < 1024 || hermes_port > 65535 )); then
  print -u2 "HERMES_PORT must be an unprivileged TCP port."
  exit 1
fi

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

export HERMES_DASHBOARD_SESSION_TOKEN="${session_token}"
unset session_token

exec "${hermes_binary}" -p "${hermes_profile}" serve \
  --host 127.0.0.1 \
  --port "${hermes_port}" \
  --skip-build
