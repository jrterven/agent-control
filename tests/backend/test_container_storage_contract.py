from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO / "deploy" / "docker" / "entrypoint.sh"
COMPOSE = REPO / "deploy" / "docker" / "compose.yml"


def test_container_entrypoint_refuses_missing_data_mount(tmp_path):
    missing = tmp_path / "not-mounted"
    result = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env={**os.environ, "HERMES_CONTROL_DATA_DIR": str(missing)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 70
    assert "data directory does not exist" in result.stderr
    assert "alembic" not in result.stderr


def test_compose_identity_is_bound_to_documented_host_owner_variables():
    source = COMPOSE.read_text()

    assert 'user: "${HERMES_CONTROL_UID:-10001}:${HERMES_CONTROL_GID:-10001}"' in source
    assert "HERMES_CONTROL_DATA_DIR: /var/lib/hermes-control" in source
    assert "- /var/lib/hermes-control:/var/lib/hermes-control" in source
