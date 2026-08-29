from __future__ import annotations

import pytest

from hermes_control_api.cli import create_admin


def test_create_admin_rejects_short_password_without_traceback(monkeypatch):
    prompts: list[str] = []

    def read_password(prompt: str) -> str:
        prompts.append(prompt)
        return "too-short"

    monkeypatch.setattr("hermes_control_api.cli.getpass.getpass", read_password)

    with pytest.raises(SystemExit, match="at least 12 characters"):
        create_admin("juan")

    assert prompts == ["New admin password (minimum 12 characters): "]
