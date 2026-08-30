#!/bin/zsh
set -euo pipefail

local_port="${HERMES_LOCAL_PORT:-9119}"
remote_port="${HERMES_CONTROL_REMOTE_PORT:-29119}"
ssh_target="${HERMES_CONTROL_SSH_TARGET:?HERMES_CONTROL_SSH_TARGET is required}"

for port in "${local_port}" "${remote_port}"; do
  if [[ ! "${port}" =~ '^[0-9]+$' ]] || (( port < 1024 || port > 65535 )); then
    print -u2 "Tunnel ports must be unprivileged TCP ports."
    exit 1
  fi
done

exec /usr/bin/ssh \
  -N \
  -T \
  -o BatchMode=yes \
  -o ConnectTimeout=10 \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=yes \
  -R "127.0.0.1:${remote_port}:127.0.0.1:${local_port}" \
  "${ssh_target}"
