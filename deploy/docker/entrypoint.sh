#!/bin/sh
set -eu

readonly data_dir="${HERMES_CONTROL_DATA_DIR:-/var/lib/hermes-control}"

if [ ! -d "$data_dir" ]; then
  echo "Hermes Control data directory does not exist: $data_dir" >&2
  exit 70
fi

if [ ! -r "$data_dir" ] || [ ! -w "$data_dir" ] || [ ! -x "$data_dir" ]; then
  echo "Hermes Control data directory is not accessible to uid=$(id -u) gid=$(id -g): $data_dir" >&2
  echo "Set HERMES_CONTROL_UID/HERMES_CONTROL_GID to the host directory owner before starting Compose." >&2
  exit 70
fi

readonly database_path="$data_dir/control.db"
if [ -e "$database_path" ] && [ ! -w "$database_path" ]; then
  echo "Hermes Control database is not writable by uid=$(id -u) gid=$(id -g): $database_path" >&2
  exit 70
fi

/opt/venv/bin/alembic -c /opt/hermes-control/apps/api/alembic.ini upgrade head
exec /opt/venv/bin/uvicorn "$@"
