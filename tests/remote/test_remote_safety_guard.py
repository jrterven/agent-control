from __future__ import annotations

import pytest

from .safety import MUTATION_SENTINEL, require_control_dev_mutation


@pytest.mark.parametrize("profile_name", ["default", "jarvis", "control-Dev", ""])
def test_mutation_gate_rejects_every_profile_except_exact_control_dev(
    monkeypatch: pytest.MonkeyPatch, profile_name: str
) -> None:
    monkeypatch.setenv("HERMES_REMOTE_MUTATIONS", MUTATION_SENTINEL)
    monkeypatch.setenv("HERMES_TEST_PROFILE", profile_name)
    with pytest.raises(RuntimeError):
        require_control_dev_mutation(profile_name)


def test_mutation_gate_requires_all_three_explicit_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_TEST_PROFILE", "control-dev")
    monkeypatch.delenv("HERMES_REMOTE_MUTATIONS", raising=False)
    with pytest.raises(RuntimeError, match="sentinel"):
        require_control_dev_mutation("control-dev")

    monkeypatch.setenv("HERMES_REMOTE_MUTATIONS", MUTATION_SENTINEL)
    require_control_dev_mutation("control-dev")
