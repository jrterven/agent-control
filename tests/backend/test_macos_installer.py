from __future__ import annotations

import base64
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "deploy" / "install-macos.sh"
HERMES_WRAPPER = REPO / "deploy" / "bin" / "hermes-macos-serve.sh"
CONTROL_WRAPPER = REPO / "deploy" / "bin" / "control-macos-serve.sh"
BACKUP_WRAPPER = REPO / "deploy" / "bin" / "control-macos-backup.sh"
VALID_VAULT_KEY = base64.urlsafe_b64encode(b"0" * 32).decode("ascii").rstrip("=")


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = shlex.split(line, posix=True)[0].split("=", 1)
        result[key] = raw_value
    return result


def _read_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _fake_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    for relative in (
        "apps/api",
        "apps/web",
        "packages/hermes-client",
        "deploy/bin",
        "deploy/launchd",
    ):
        (source / relative).mkdir(parents=True, exist_ok=True)
    (source / "package.json").write_text('{"name":"fake-control"}\n', encoding="utf-8")
    (source / "package-lock.json").write_text('{"name":"fake-control"}\n', encoding="utf-8")
    (source / "apps/api/alembic.ini").write_text("[alembic]\nscript_location = alembic\n", encoding="utf-8")
    (source / "apps/api/pyproject.toml").write_text("[project]\nname='fake-api'\n", encoding="utf-8")
    (source / "packages/hermes-client/pyproject.toml").write_text(
        "[project]\nname='fake-client'\n", encoding="utf-8"
    )
    for relative in (
        "deploy/bin/hermes-macos-serve.sh",
        "deploy/bin/control-macos-serve.sh",
        "deploy/bin/control-macos-backup.sh",
        "deploy/launchd/com.agent-control.hermes-serve.plist.example",
        "deploy/launchd/com.agent-control.control.plist.example",
        "deploy/launchd/com.agent-control.backup.plist.example",
    ):
        (source / relative).write_bytes((REPO / relative).read_bytes())
    (source / "deploy/bin/backup-sqlite.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "mkdir -p \"$HERMES_CONTROL_BACKUP_DIR\"\n"
        "target=\"$HERMES_CONTROL_BACKUP_DIR/control-${FAKE_BACKUP_COUNTER:-static}.db\"\n"
        ": >\"$target\"\n"
        "chmod 600 \"$target\"\n"
        "printf '%s\\n' \"$target\"\n",
        encoding="utf-8",
    )
    (source / "deploy/bin/backup-sqlite.sh").chmod(0o755)
    return source


def _fake_host(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "fake-bin"
    logs = tmp_path / "logs"
    home.mkdir()
    fake_bin.mkdir()
    logs.mkdir()

    fake_hermes = fake_bin / "hermes"
    keychain_state = tmp_path / "keychain-token.txt"
    curl_config_log = logs / "curl-config.log"
    sudo_log = logs / "sudo.log"
    command_log = logs / "commands.log"
    tailscale_state = tmp_path / "tailscale-serve.json"
    tailscale_state.write_text("{}", encoding="utf-8")

    _write_executable(
        fake_bin / "uname",
        "#!/usr/bin/env bash\nprintf 'Darwin\\n'\n",
    )
    _write_executable(
        fake_bin / "git",
        f"""#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == "--version" ]]; then
  printf 'git version 2.50.0\n'
  exit 0
fi
repo=""
if [[ "${{1:-}}" == "-C" ]]; then
  repo="${{2}}"
  shift 2
else
  repo="$PWD"
fi
case "${{1:-}}" in
  rev-parse)
    if [[ "${{2:-}}" == "--show-toplevel" ]]; then
      printf '%s\\n' "$repo"
    elif [[ "${{2:-}}" == "HEAD" ]]; then
      printf '0123456789abcdef0123456789abcdef01234567\\n'
    else
      exit 2
    fi
    ;;
  status)
    if [[ "${{FAKE_GIT_DIRTY:-0}}" == "1" ]]; then
      printf ' M README.md\\n'
    fi
    ;;
  ls-files)
    REPO_ROOT="$repo" {sys.executable!s} - <<'PY'
from __future__ import annotations

import os
import pathlib

root = pathlib.Path(os.environ["REPO_ROOT"])
for path in sorted(item for item in root.rglob("*") if item.is_file()):
    print(path.relative_to(root), end="\\0")
PY
    ;;
  *)
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "python3",
        f"""#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  target="${{3}}"
  mkdir -p "$target/bin"
  cat >"$target/bin/python" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  cat >"$target/bin/alembic" <<'EOF'
#!/usr/bin/env bash
set -eu
mkdir -p "$(dirname "$HERMES_CONTROL_DATABASE_PATH")"
: >"$HERMES_CONTROL_DATABASE_PATH"
exit 0
EOF
  cat >"$target/bin/hermes-control-admin" <<'EOF'
#!/usr/bin/env bash
set -eu
: >"${{HERMES_CONTROL_DATABASE_PATH}}.admin"
exit 0
EOF
  cat >"$target/bin/uvicorn" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$target/bin/python" "$target/bin/alembic" "$target/bin/hermes-control-admin" "$target/bin/uvicorn"
  exit 0
fi
exec {sys.executable!s} "$@"
""",
    )
    _write_executable(
        fake_bin / "node",
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"-p\" ]]; then printf '22\\n'; exit 0; fi\n"
        "if [[ \"${1:-}\" == \"-e\" ]]; then exit 0; fi\n"
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "npm",
        """#!/usr/bin/env bash
set -eu
printf 'npm %s\n' "$*" >>"$COMMAND_LOG"
if [[ "${1:-}" == "ci" ]]; then
  exit 0
fi
if [[ "${1:-}" == "run" && "${2:-}" == "build" ]]; then
  mkdir -p apps/web/dist
  printf '<!doctype html><html><head><link rel="manifest" href="/manifest.webmanifest"></head><body>Control</body></html>\n' >apps/web/dist/index.html
  printf '{"name":"Agent Control","start_url":"/","icons":[{"src":"/icon-192.png","sizes":"192x192","type":"image/png"}]}\n' >apps/web/dist/manifest.webmanifest
  exit 0
fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "sqlite3",
        """#!/usr/bin/env bash
set -eu
database="${1:-}"
query="${2:-}"
if [[ "$query" == *"COUNT(*) FROM users WHERE is_admin = 1"* ]]; then
  if [[ -f "${database}.admin" ]]; then
    printf '1\n'
  else
    printf '0\n'
  fi
  exit 0
fi
printf 'ok\n'
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -eu
if [[ "$*" == *"--config -" ]]; then
  config="$(cat)"
  printf '%s\n' "$config" >>"$CURL_CONFIG_LOG"
  exit 0
fi
url="${!#}"
case "$url" in
  http://127.0.0.1:8000/api/v1/ready)
    printf '{"status":"ready","database":"ready","upstream":"online"}\n'
    ;;
  http://127.0.0.1:8000/*)
    printf '{"status":"ok"}\n'
    ;;
  http://127.0.0.1:9119/*)
    printf '[]\n'
    ;;
  https://control-node.example.ts.net/manifest.webmanifest)
    printf '%s\n' "$FAKE_CURL_PUBLIC_MANIFEST"
    ;;
  https://control-node.example.ts.net/*)
    printf '%s\n' "$FAKE_CURL_PUBLIC_HTML"
    ;;
  *)
    exit 1
    ;;
esac
""",
    )
    _write_executable(fake_bin / "launchctl", "#!/usr/bin/env bash\nprintf 'launchctl %s\n' \"$*\" >>\"$COMMAND_LOG\"\n")
    _write_executable(fake_bin / "plutil", "#!/usr/bin/env bash\nprintf 'plutil %s\n' \"$*\" >>\"$COMMAND_LOG\"\nexit 0\n")
    _write_executable(
        fake_bin / "sudo",
        """#!/usr/bin/env bash
set -eu
printf 'sudo %s\n' "$*" >>"$SUDO_LOG"
if [[ "${1:-}" == "env" ]]; then
  shift
  while [[ "${1:-}" == *=* ]]; do
    export "$1"
    shift
  done
fi
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "security",
        f"""#!/usr/bin/env bash
set -eu
printf 'security %s\n' "$*" >>"$COMMAND_LOG"
state={shlex.quote(str(keychain_state))}
case "${{1:-}}" in
  find-generic-password)
    [[ -f "$state" ]] || exit 44
    if [[ "$*" == *" -w"* || "${{!#}}" == "-w" ]]; then
      cat "$state"
    fi
    ;;
  add-generic-password)
    printf '%s' "${{FAKE_KEYCHAIN_TOKEN:-abcdefghijklmnopqrstuvwxyz0123456789TOKEN}}" >"$state"
    ;;
  delete-generic-password)
    rm -f "$state"
    ;;
  *)
    exit 2
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "tailscale",
        f"""#!/usr/bin/env bash
set -eu
printf 'tailscale[%s] %s\n' "${{TAILSCALE_BE_CLI:-}}" "$*" >>"$COMMAND_LOG"
if [[ "${{1:-}}" == "status" && "${{2:-}}" == "--json" ]]; then
  printf '%s\\n' "$FAKE_TAILSCALE_STATUS_JSON"
  exit 0
fi
if [[ "${{1:-}}" == "serve" && "${{2:-}}" == "status" && "${{3:-}}" == "--json" ]]; then
  if [[ "$FAKE_TAILSCALE_SERVE_JSON" == "{{}}" ]]; then
    cat {shlex.quote(str(tailscale_state))}
  else
    printf '%s\\n' "$FAKE_TAILSCALE_SERVE_JSON"
  fi
  exit 0
fi
if [[ "${{1:-}}" == "serve" ]]; then
  printf '{{"TCP":{{"443":{{"HTTPS":true}}}},"Web":{{"control-node.example.ts.net:443":{{"Handlers":{{"/":{{"Proxy":"http://127.0.0.1:8000"}}}}}}}}}}' >{shlex.quote(str(tailscale_state))}
  exit 0
fi
exit 2
""",
    )
    _write_executable(
        fake_hermes,
        "#!/usr/bin/env bash\nif [[ \"$*\" == *\"serve --help\"* ]]; then exit 0; fi\nexit 0\n",
    )

    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "COMMAND_LOG": str(command_log),
            "CURL_CONFIG_LOG": str(curl_config_log),
            "SUDO_LOG": str(sudo_log),
            "FAKE_GIT_DIRTY": "0",
            "FAKE_KEYCHAIN_TOKEN": "abcdefghijklmnopqrstuvwxyz0123456789TOKEN",
            "FAKE_CURL_PUBLIC_HTML": '<!doctype html><html><head><link rel="manifest" href="/manifest.webmanifest"></head><body>Control</body></html>',
            "FAKE_CURL_PUBLIC_MANIFEST": '{"name":"Agent Control","start_url":"/","icons":[{"src":"/icon-192.png","sizes":"192x192","type":"image/png"}]}',
            "FAKE_TAILSCALE_STATUS_JSON": '{"Self":{"DNSName":"control-node.example.ts.net."}}',
            "FAKE_TAILSCALE_SERVE_JSON": "{}",
        }
    )
    return home, environment, fake_hermes


def test_macos_installer_has_valid_shell_and_safe_keychain_prompt():
    bash_syntax = subprocess.run(
        ["bash", "-n", str(INSTALLER)],
        check=False,
        capture_output=True,
        text=True,
    )
    serve_syntax = subprocess.run(
        ["zsh", "-n", str(CONTROL_WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
    )
    backup_syntax = subprocess.run(
        ["zsh", "-n", str(BACKUP_WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
    )
    hermes_syntax = subprocess.run(
        ["zsh", "-n", str(HERMES_WRAPPER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert bash_syntax.returncode == 0, bash_syntax.stderr
    assert hermes_syntax.returncode == 0, hermes_syntax.stderr
    assert serve_syntax.returncode == 0, serve_syntax.stderr
    assert backup_syntax.returncode == 0, backup_syntax.stderr
    installer_source = INSTALLER.read_text(encoding="utf-8")
    wrapper_source = CONTROL_WRAPPER.read_text(encoding="utf-8")
    hermes_wrapper_source = HERMES_WRAPPER.read_text(encoding="utf-8")
    assert INSTALLER.stat().st_mode & stat.S_IXUSR
    assert "--dry-run" in installer_source
    assert "--rotate-hermes-token" in installer_source
    assert "--no-install-packages" in installer_source
    assert "INSTALL_PACKAGES=1" in installer_source
    assert "security add-generic-password" in installer_source
    assert "sudo " not in installer_source
    assert '-w "$' not in installer_source
    assert ' -w \n' in installer_source or ' -w \\\n' in installer_source
    assert "export HERMES_CONTROL_HERMES_DASHBOARD_TOKEN" not in wrapper_source
    assert "${#session_token} > 512" in hermes_wrapper_source
    assert "A-Za-z0-9._~-" in hermes_wrapper_source


def test_macos_dry_run_does_not_create_install_dirs(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--dry-run",
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert not (home / ".agent-control").exists()
    assert not (home / "Library/LaunchAgents").exists()
    command_log = (tmp_path / "logs/commands.log").read_text(encoding="utf-8")
    assert "tailscale[] status --json" in command_log
    assert "tailscale[] serve status --json" in command_log
    assert not _read_if_exists(tmp_path / "logs/sudo.log")


def test_macos_conflicting_serve_state_aborts_before_writes(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    environment["FAKE_TAILSCALE_SERVE_JSON"] = '{"TCP":{"443":{"HTTPS":true}},"Web":{"control-node.example.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:9999"}}}}}'
    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "does not match Agent Control" in result.stderr
    assert not (home / ".agent-control").exists()


def test_macos_configures_empty_tailscale_state_and_renders_selected_profile(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
            "--hermes-profile",
            "jarvis",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert (
        tmp_path / "tailscale-serve.json"
    ).read_text(encoding="utf-8") == (
        '{"TCP":{"443":{"HTTPS":true}},"Web":{"control-node.example.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8000"}}}}}'
    )
    hermes_plist = home / "Library/LaunchAgents/com.agent-control.hermes-serve.plist"
    control_plist = home / "Library/LaunchAgents/com.agent-control.control.plist"
    backup_plist = home / "Library/LaunchAgents/com.agent-control.backup.plist"
    assert "<string>jarvis</string>" in hermes_plist.read_text(encoding="utf-8")
    assert "Managed by deploy/install-macos.sh" in hermes_plist.read_text(encoding="utf-8")
    assert "Managed by deploy/install-macos.sh" in control_plist.read_text(encoding="utf-8")
    assert "Managed by deploy/install-macos.sh" in backup_plist.read_text(encoding="utf-8")
    command_log = (tmp_path / "logs/commands.log").read_text(encoding="utf-8")
    assert "plutil -lint" in command_log
    assert "sudo " not in command_log


def test_macos_rerun_preserves_env_and_writes_plists(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    command = [
        "bash",
        str(INSTALLER),
        "--repo-root",
        str(source),
        "--hermes-bin",
        str(fake_hermes),
        "--skip-tailscale-serve",
        "--allowed-origin",
        "https://control-node.example.ts.net",
    ]
    first = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == 0, first.stderr

    env_file = home / ".agent-control/config/control.env"
    env_values = _parse_env_file(env_file)
    vault_key = env_values["HERMES_CONTROL_VAULT_KEY_B64"]
    assert env_values["HERMES_CONTROL_ALLOWED_ORIGINS"] == "https://control-node.example.ts.net"
    assert env_values["HERMES_CONTROL_HERMES_API_URL"] == ""
    assert env_values["HERMES_CONTROL_DEFAULT_PROFILES"] == '["default"]'
    assert "HERMES_CONTROL_HERMES_DASHBOARD_TOKEN" not in env_values
    assert (home / "Library/LaunchAgents/com.agent-control.control.plist").exists()
    assert (home / "Library/LaunchAgents/com.agent-control.backup.plist").exists()
    assert (home / "Library/LaunchAgents/com.agent-control.hermes-serve.plist").exists()

    second = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert second.returncode == 0, second.stderr
    env_values_after = _parse_env_file(env_file)
    assert env_values_after["HERMES_CONTROL_VAULT_KEY_B64"] == vault_key
    assert (home / ".agent-control/current").is_symlink()
    assert list((home / ".agent-control/backups").glob("control-*.db"))
    assert stat.S_IMODE((home / ".agent-control/data").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / ".agent-control/backups").stat().st_mode) == 0o700
    assert stat.S_IMODE((home / ".agent-control/data/control.db").stat().st_mode) == 0o600
    assert "abcdefghijklmnopqrstuvwxyz0123456789TOKEN" not in (
        tmp_path / "logs/commands.log"
    ).read_text(encoding="utf-8")
    assert "X-Hermes-Session-Token: abcdefghijklmnopqrstuvwxyz0123456789TOKEN" in (
        tmp_path / "logs/curl-config.log"
    ).read_text(encoding="utf-8")
    assert not _read_if_exists(tmp_path / "logs/sudo.log")


def test_macos_rerun_rejects_insecure_database_directory(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    command = [
        "bash",
        str(INSTALLER),
        "--repo-root",
        str(source),
        "--hermes-bin",
        str(fake_hermes),
        "--skip-tailscale-serve",
        "--allowed-origin",
        "https://control-node.example.ts.net",
    ]
    first = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == 0, first.stderr

    data_dir = home / ".agent-control/data"
    data_dir.chmod(0o755)
    second = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert second.returncode != 0
    assert "private user-owned database and backup paths" in second.stderr
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755


def test_macos_rerun_recreates_missing_managed_backup_directory(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    command = [
        "bash",
        str(INSTALLER),
        "--repo-root",
        str(source),
        "--hermes-bin",
        str(fake_hermes),
        "--skip-tailscale-serve",
        "--allowed-origin",
        "https://control-node.example.ts.net",
    ]
    first = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert first.returncode == 0, first.stderr

    backup_dir = home / ".agent-control/backups"
    for backup in backup_dir.iterdir():
        backup.unlink()
    backup_dir.rmdir()
    second = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert second.returncode == 0, second.stderr
    assert backup_dir.is_dir()
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert list(backup_dir.glob("control-*.db"))


def test_macos_rejects_non_origin_allowed_origin_before_writes(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
            "--skip-tailscale-serve",
            "--allowed-origin",
            "https://control-node.example.ts.net/",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "--allowed-origin must be exactly https://control-node.example.ts.net" in result.stderr
    assert not (home / ".agent-control").exists()


def test_macos_invalid_existing_env_aborts_before_rewrite(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    env_file = home / ".agent-control/config/control.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "\n".join(
            (
                "HERMES_CONTROL_ENVIRONMENT=production",
                f"HERMES_CONTROL_VAULT_KEY_B64={VALID_VAULT_KEY}",
                "HERMES_CONTROL_HERMES_DASHBOARD_URL=http://127.0.0.1:9119",
                "HERMES_CONTROL_HERMES_DASHBOARD_WS=ws://127.0.0.1:9119/api/ws",
                "HERMES_CONTROL_HERMES_API_URL=http://127.0.0.1:18642",
                "HERMES_CONTROL_HERMES_API_KEY=",
                "HERMES_CONTROL_ALLOWED_ORIGINS=https://control-node.example.ts.net",
                "HERMES_CONTROL_SECURE_COOKIES=true",
                "HERMES_CONTROL_CREATE_SCHEMA_ON_START=false",
                "HERMES_CONTROL_PROVIDER_MODE=real",
                "HERMES_CONTROL_MOCK_FALLBACK_ENABLED=false",
                f"HERMES_CONTROL_DATABASE_PATH={home}/.agent-control/data/control.db",
                f"HERMES_CONTROL_DATABASE_URL=sqlite:///{home}/.agent-control/data/control.db",
                f"HERMES_CONTROL_BACKUP_DIR={home}/.agent-control/backups",
                "",
            )
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)

    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
            "--skip-tailscale-serve",
            "--allowed-origin",
            "https://control-node.example.ts.net",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "must keep HERMES_CONTROL_HERMES_API_URL empty" in result.stderr
    assert env_file.read_text(encoding="utf-8").startswith(
        "HERMES_CONTROL_ENVIRONMENT=production"
    )
    assert not (home / ".agent-control/releases").exists()


def test_macos_unmanaged_plist_aborts_before_write(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    plist_path = home / "Library/LaunchAgents/com.agent-control.control.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text("<plist><dict/></plist>\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
            "--skip-tailscale-serve",
            "--allowed-origin",
            "https://control-node.example.ts.net",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "Refusing to overwrite unmanaged LaunchAgent plist" in result.stderr
    assert not (home / ".agent-control").exists()


def test_macos_skip_tailscale_still_requires_connected_tailnet(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    environment["FAKE_CURL_PUBLIC_HTML"] = '<!doctype html><html><head><link rel="manifest" href="/manifest.webmanifest"></head><body>Control</body></html>'
    environment["FAKE_TAILSCALE_STATUS_JSON"] = "{}"

    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
            "--skip-tailscale-serve",
            "--allowed-origin",
            "https://control-node.example.ts.net",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "Tailscale must already be connected" in result.stderr
    assert not (home / ".agent-control").exists()


def test_macos_rejects_invalid_keychain_token_before_authenticated_probe(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    environment["FAKE_KEYCHAIN_TOKEN"] = "not valid"
    (tmp_path / "keychain-token.txt").write_text("not valid", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
            "--skip-hermes-service",
            "--skip-tailscale-serve",
            "--allowed-origin",
            "https://control-node.example.ts.net",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "dashboard token must be 32-512 URL-safe characters" in result.stderr
    assert not _read_if_exists(tmp_path / "logs/curl-config.log")


def test_macos_public_pwa_verification_is_strict(tmp_path):
    source = _fake_source(tmp_path)
    _, environment, fake_hermes = _fake_host(tmp_path)
    environment["FAKE_CURL_PUBLIC_HTML"] = "<!doctype html><html><head></head><body>Control</body></html>"
    environment["FAKE_CURL_PUBLIC_MANIFEST"] = '{"name":"Agent Control"}'

    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode != 0
    assert "Public Tailscale origin verification failed" in result.stderr


def test_macos_tailscale_app_fallback_uses_cli_backend_env(tmp_path):
    source = _fake_source(tmp_path)
    home, environment, fake_hermes = _fake_host(tmp_path)
    app_bundle_cli = tmp_path / "Applications/Tailscale.app/Contents/MacOS/Tailscale"
    _write_executable(app_bundle_cli, "#!/usr/bin/env bash\nexec \"$(dirname \"$0\")/../../../../fake-bin/tailscale\" \"$@\"\n")
    environment["HERMES_CONTROL_INSTALL_TAILSCALE"] = str(app_bundle_cli)
    result = subprocess.run(
        [
            "bash",
            str(INSTALLER),
            "--repo-root",
            str(source),
            "--hermes-bin",
            str(fake_hermes),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert "tailscale[1] status --json" in (tmp_path / "logs/commands.log").read_text(encoding="utf-8")
