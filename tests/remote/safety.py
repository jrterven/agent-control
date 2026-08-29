from __future__ import annotations

import os


MUTATION_SENTINEL = "I_UNDERSTAND_CONTROL_DEV_ONLY"


def require_control_dev_mutation(profile_name: str) -> None:
    """Fail closed before constructing any mutating remote request."""

    if profile_name != "control-dev":
        raise RuntimeError("Remote mutations require the exact control-dev profile")
    if os.getenv("HERMES_REMOTE_MUTATIONS") != MUTATION_SENTINEL:
        raise RuntimeError("Remote mutation sentinel is missing")
    if os.getenv("HERMES_TEST_PROFILE") != "control-dev":
        raise RuntimeError("HERMES_TEST_PROFILE must explicitly select control-dev")
