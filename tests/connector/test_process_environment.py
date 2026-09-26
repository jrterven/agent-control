import os
import subprocess

import pytest

from agent_control_connector import manage, managed_manifest, process_environment


@pytest.mark.parametrize("original", [None, "", "/usr/local/lib"])
def test_frozen_linux_restores_system_library_path_only_for_child(monkeypatch, original):
    monkeypatch.setattr(process_environment.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_environment.sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/connector/_internal")
    if original is None:
        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    else:
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", original)
    before = dict(os.environ)
    child = process_environment.system_environment()
    assert child.get("LD_LIBRARY_PATH") == original
    assert {key: value for key, value in child.items() if key != "LD_LIBRARY_PATH"} == {
        key: value for key, value in before.items() if key != "LD_LIBRARY_PATH"}
    assert dict(os.environ) == before


@pytest.mark.parametrize("frozen,platform", [(False, "linux"), (True, "darwin")])
def test_other_process_environments_are_preserved(monkeypatch, frozen, platform):
    monkeypatch.setattr(process_environment.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(process_environment.sys, "platform", platform)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/user/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/another/lib")
    assert process_environment.system_environment() == dict(os.environ)


def test_both_signature_paths_use_system_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(process_environment.sys, "frozen", True, raising=False)
    monkeypatch.setattr(process_environment.sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/connector/_internal")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    calls = []
    def invoke(command, **kwargs):
        calls.append((command, kwargs))
        assert "LD_LIBRARY_PATH" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(subprocess, "run", invoke)
    document = tmp_path / "latest.json"
    signature = tmp_path / "latest.json.sig"
    document.write_text("{}")
    signature.write_bytes(b"invalid")
    with pytest.raises(ValueError, match="signature"):
        managed_manifest.verify_signature(document, signature)
    assert manage.run("openssl", "version", check=False).returncode == 1
    assert calls[0][0][:2] == ["openssl", "dgst"]
    assert calls[0][1]["timeout"] == 20
    assert calls[1][1]["check"] is False
    assert os.environ["LD_LIBRARY_PATH"] == "/connector/_internal"
