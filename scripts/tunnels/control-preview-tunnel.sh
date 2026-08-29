#!/usr/bin/env bash
set -Eeuo pipefail

# Development preview only. The browser reaches Agent Control through local
# loopback; SSH carries the traffic to the remote loopback listener.

readonly SSH_HOST="${HERMES_CONTROL_SSH_HOST:-agent}"
readonly LOCAL_PORT="${HERMES_CONTROL_LOCAL_PORT:-18000}"
readonly REMOTE_PORT="${HERMES_CONTROL_REMOTE_PORT:-8000}"

child_pid=""
stopping=0

usage() {
  printf 'Usage: %s {run|once|check}\n' "$0"
  printf '  run    supervise the SSH forward and reconnect with bounded backoff\n'
  printf '  once   run one foreground SSH forwarding process\n'
  printf '  check  verify Agent Control through the local forward\n'
}

validate() {
  if [[ ! "$SSH_HOST" =~ ^[A-Za-z0-9._-]+$ ]]; then
    printf 'Invalid SSH alias: %s\n' "$SSH_HOST" >&2
    exit 64
  fi
  local value
  for value in "$LOCAL_PORT" "$REMOTE_PORT"; do
    if [[ ! "$value" =~ ^[0-9]+$ ]] || (( value < 1 || value > 65535 )); then
      printf 'Invalid TCP port: %s\n' "$value" >&2
      exit 64
    fi
  done
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
    -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}"
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
    printf 'Opening Agent Control at http://127.0.0.1:%s through %s…\n' \
      "$LOCAL_PORT" "$SSH_HOST" >&2
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

check() {
  if curl --fail --silent --max-time 3 \
    "http://127.0.0.1:${LOCAL_PORT}/api/v1/health" >/dev/null; then
    printf 'Agent Control reachable at http://127.0.0.1:%s\n' "$LOCAL_PORT"
    return 0
  fi
  printf 'Agent Control unavailable at http://127.0.0.1:%s\n' "$LOCAL_PORT" >&2
  return 1
}

validate
case "${1:-}" in
  run) supervise ;;
  once) run_once ;;
  check) check ;;
  *) usage >&2; exit 64 ;;
esac
