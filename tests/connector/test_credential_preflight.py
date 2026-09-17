import hashlib
import json
import os

import pytest

from agent_control_connector import cli, keychain


def write_private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_credential_check_does_not_modify_state_or_print_secrets(tmp_path, monkeypatch, capsys, platform):
    monkeypatch.setattr(cli.sys, "platform", platform)
    write_private(tmp_path / "config.json", {"connectorId": "existing"})
    secrets = {"accessToken": "access-secret-placeholder", "hermesToken": "hermes-secret-placeholder"}
    if platform == "linux":
        write_private(tmp_path / "secrets.json", secrets)
    else:
        class Keychain:
            def load(self, service, account):
                assert service == "com.agent-control.connector." + hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:24]
                assert account == str(os.getuid())
                return json.dumps(secrets).encode()
        monkeypatch.setattr(keychain, "MacKeychain", Keychain)
    for name in ("runtime.lock", "status.json", "operations.sqlite3", "maintenance.request"):
        (tmp_path / name).write_bytes(b"existing runtime state")
    tmp_path.chmod(0o750)
    before = {path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) for path in tmp_path.iterdir()}
    for name in ("SecretStore", "ConnectorRuntime", "private_dir", "detect_revision"):
        monkeypatch.setattr(cli, name, lambda *args, **kwargs: pytest.fail("preflight must not initialize the runtime or change files"))
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda *args, **kwargs: pytest.fail("preflight must not use the network"))
    assert cli.main(["check-credentials", "--data-dir", str(tmp_path)]) == 0
    output = capsys.readouterr()
    assert output.out == "Existing connector credentials are accessible.\n"
    assert output.err == ""
    assert {path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns) for path in tmp_path.iterdir()} == before
    assert tmp_path.stat().st_mode & 0o777 == 0o750


def test_credential_check_failure_is_sanitized_and_leaves_missing_directory_absent(tmp_path, monkeypatch, capsys):
    home = tmp_path / "missing"
    assert cli.main(["check-credentials", "--data-dir", str(home)]) == 1
    assert not home.exists()
    capsys.readouterr()
    write_private(tmp_path / "config.json", {"connectorId": "existing"})
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    class Keychain:
        def load(self, service, account):
            raise RuntimeError("secret-placeholder")
    monkeypatch.setattr(keychain, "MacKeychain", Keychain)
    assert cli.main(["check-credentials", "--data-dir", str(tmp_path)]) == 1
    output = capsys.readouterr()
    assert "Cannot access existing connector credentials" in output.err
    assert "secret-placeholder" not in output.err + output.out
