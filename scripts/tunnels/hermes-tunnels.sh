#!/usr/bin/env bash
set -Eeuo pipefail

# Development-only supervisor. It never reads Hermes credentials: SSH carries
# traffic from local loopback to remote loopback over the existing `agent` alias.

readonly SSH_HOST="${HERMES_SSH_HOST:-agent}"
readonly DASHBOARD_LOCAL_PORT="${HERMES_DASHBOARD_LOCAL_PORT:-19119}"
readonly DASHBOARD_REMOTE_PORT="${HERMES_DASHBOARD_REMOTE_PORT:-9119}"
readonly API_LOCAL_PORT="${HERMES_API_LOCAL_PORT:-18642}"
readonly API_REMOTE_PORT="${HERMES_API_REMOTE_PORT:-8642}"

child_pid=""
stopping=0

usage() {
  printf 'Usage: %s {run|once|check}\n' "$0"
  printf '  run    supervise both SSH forwards and reconnect with bounded backoff\n'
  printf '  once   run one foreground SSH forwarding process\n'
  printf '  check  verify both local TCP listeners are reachable\n'
}

validate() {
  if [[ ! "$SSH_HOST" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'Invalid SSH alias: %s\n' "$SSH_HOST" >&2
    exit 64
  fi
  local value
  for value in \
    "$DASHBOARD_LOCAL_PORT" "$DASHBOARD_REMOTE_PORT" \
    "$API_LOCAL_PORT" "$API_REMOTE_PORT"; do
    if [[ ! "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > 65535 )); then
      printf 'Invalid TCP port: %s\n' "$value" >&2
      exit 64
    fi
  done
  if (( DASHBOARD_LOCAL_PORT == API_LOCAL_PORT )); then
    printf 'Local ports must be different.\n' >&2
    exit 64
  fi
  command -v ssh >/dev/null || { printf 'ssh is required.\n' >&2; exit 69; }
}

ssh_args() {
  SSH_ARGS=(
    -N -T
    -o BatchMode=yes
    -o ConnectTimeout=10
    -o ExitOnForwardFailure=yes
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=3
    -o TCPKeepAlive=yes
    -o StrictHostKeyChecking=yes
    -o UpdateHostKeys=yes
    -L "127.0.0.1:${DASHBOARD_LOCAL_PORT}:127.0.0.1:${DASHBOARD_REMOTE_PORT}"
    -L "127.0.0.1:${API_LOCAL_PORT}:127.0.0.1:${API_REMOTE_PORT}"
    "$SSH_HOST"
  )
}

stop_child() {
  stopping=1
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
}

run_once() {
  ssh_args
  exec ssh "${SSH_ARGS[@]}"
}

supervise() {
  trap stop_child INT TERM HUP EXIT
  local backoff=1
  while (( stopping == 0 )); do
    ssh_args
    printf 'Opening loopback tunnels through %s (dashboard %s, API %s)…\n' \
      "$SSH_HOST" "$DASHBOARD_LOCAL_PORT" "$API_LOCAL_PORT" >&2
    ssh "${SSH_ARGS[@]}" &
    child_pid=$!
    local started=$SECONDS
    local exit_code=0
    wait "$child_pid" || exit_code=$?
    child_pid=""
    (( stopping == 1 )) && break
    local lived=$((SECONDS - started))
    if (( lived >= 60 )); then
      backoff=1
    fi
    printf 'SSH tunnel exited (%s); retrying in %ss.\n' "$exit_code" "$backoff" >&2
    sleep "$backoff"
    backoff=$((backoff * 2))
    (( backoff > 30 )) && backoff=30
  done
}

check_port() {
  local label=$1
  local port=$2
  if command -v nc >/dev/null && nc -z -w 2 127.0.0.1 "$port" >/dev/null 2>&1; then
    printf '%-10s reachable at 127.0.0.1:%s\n' "$label" "$port"
    return 0
  fi
  printf '%-10s unavailable at 127.0.0.1:%s\n' "$label" "$port" >&2
  return 1
}

check() {
  local failed=0
  check_port dashboard "$DASHBOARD_LOCAL_PORT" || failed=1
  check_port api-server "$API_LOCAL_PORT" || failed=1
  return "$failed"
}

validate
case "${1:-}" in
  run) supervise ;;
  once) run_once ;;
  check) check ;;
  *) usage >&2; exit 64 ;;
esac
