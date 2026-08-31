from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "deploy" / "linux" / "install-agent-control.sh"
STABLE_INSTALLER = REPO / "deploy" / "install-linux.sh"
CONTROL_UNITS = (
    "hermes-control.service",
    "hermes-control-backup.service",
    "hermes-control-backup.timer",
)


def _write_executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "apps/api").mkdir(parents=True)
    (source / "packages/hermes-client").mkdir(parents=True)
    (source / "deploy/systemd").mkdir(parents=True)
    (source / "deploy/bin").mkdir(parents=True)
    (source / "apps/api/pyproject.toml").write_text("[project]\nname='api'\n", encoding="utf-8")
    (source / "packages/hermes-client/pyproject.toml").write_text(
        "[project]\nname='client'\n", encoding="utf-8"
    )
    for unit in CONTROL_UNITS:
        (source / "deploy/systemd" / unit).write_bytes(
            (REPO / "deploy/systemd" / unit).read_bytes()
        )
    (source / "deploy/bin/backup-sqlite.sh").write_bytes(
        (REPO / "deploy/bin/backup-sqlite.sh").read_bytes()
    )
    (source / "deploy/bin/backup-sqlite.sh").chmod(0o755)
    static_source = tmp_path / "static"
    static_source.mkdir()
    (static_source / "index.html").write_text("<!doctype html><title>Control</title>", encoding="utf-8")
    return source, static_source


def _fake_host(tmp_path: Path) -> tuple[Path, dict[str, str], Path, Path]:
    root = tmp_path / "root"
    fake_bin = tmp_path / "fake-bin"
    state = tmp_path / "tailscale-serve.json"
    log = tmp_path / "commands.log"
    root.mkdir()
    fake_bin.mkdir()
    state.write_text("{}", encoding="utf-8")
    log.write_text("", encoding="utf-8")

    hermes_python = root / "home/hermes/.hermes/hermes-agent/venv/bin/python"
    _write_executable(hermes_python, "#!/usr/bin/env bash\nexit 0\n")
    (root / "home/hermes/.hermes/profiles/default").mkdir(parents=True)
    (root / "home/hermes/.hermes/profiles/jarvis").mkdir(parents=True)

    fake_python = fake_bin / "python"
    _write_executable(
        fake_python,
        f"""#!/usr/bin/env bash
set -eu
if [[ "${{1:-}}" == "-m" && "${{2:-}}" == "venv" ]]; then
  target=$3
  [[ "$target" != "--help" ]] || exit 0
  mkdir -p "$target/bin"
  for command in python uvicorn hermes-control-admin; do
    printf '#!/usr/bin/env bash\\nexit 0\\n' >"$target/bin/$command"
    chmod +x "$target/bin/$command"
  done
  cat >"$target/bin/alembic" <<'EOF'
#!/usr/bin/env bash
printf 'alembic %s\\n' "$*" >>"$INSTALL_LOG"
EOF
  chmod +x "$target/bin/alembic"
  exit 0
fi
exec {sys.executable!s} "$@"
""",
    )
    _write_executable(
        fake_bin / "tailscale",
        """#!/usr/bin/env bash
set -eu
printf 'tailscale %s\n' "$*" >>"$INSTALL_LOG"
if [[ "${1:-}" == "status" && "${2:-}" == "--json" ]]; then
  printf '{"Self":{"DNSName":"control-node.example.ts.net."}}\n'
elif [[ "${1:-}" == "serve" && "${2:-}" == "status" ]]; then
  cat "$FAKE_TAILSCALE_STATE"
elif [[ "${1:-}" == "serve" ]]; then
  cat >"$FAKE_TAILSCALE_STATE" <<EOF
{"TCP":{"443":{"HTTPS":true}},"Web":{"control-node.example.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8000"}}}}}
EOF
else
  exit 2
fi
""",
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -eu
printf 'systemctl %s\n' "$*" >>"$INSTALL_LOG"
if [[ "${1:-}" == "cat" ]]; then
  unit="$INSTALL_ROOT/etc/systemd/system/${2:-}"
  [[ -f "$unit" ]] || exit 1
  cat "$unit"
elif [[ "$*" == *"enable --now hermes-serve.service"* ]]; then
  mkdir -p "$INSTALL_ROOT/run"
  : >"$INSTALL_ROOT/run/hermes-ready"
elif [[ "$*" == *"enable --now hermes-control.service"* ]]; then
  mkdir -p "$INSTALL_ROOT/run"
  : >"$INSTALL_ROOT/run/control-ready"
elif [[ "$*" == *"restart hermes-control.service"* ]]; then
  mkdir -p "$INSTALL_ROOT/run"
  : >"$INSTALL_ROOT/run/control-ready"
fi
""",
    )
    _write_executable(
        fake_bin / "port-probe",
        """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  9119) [[ -f "$INSTALL_ROOT/run/hermes-ready" || -f "$INSTALL_ROOT/run/port-9119-claimed" ]] ;;
  8000) [[ -f "$INSTALL_ROOT/run/control-ready" || -f "$INSTALL_ROOT/run/port-8000-claimed" ]] ;;
  *) exit 2 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -eu
if [[ "$*" == *"--config -"* ]]; then
  config=$(cat)
  if [[ "$config" == *"http://127.0.0.1:9119/api/profiles"* && -f "$INSTALL_ROOT/run/hermes-ready" ]]; then
    printf '200'
    exit 0
  fi
  printf '401'
  exit 0
fi
url="${!#}"
case "$url" in
  http://127.0.0.1:9119/*)
    [[ -f "$INSTALL_ROOT/run/hermes-ready" ]] || exit 1
    ;;
  http://127.0.0.1:8000/api/v1/health)
    [[ -f "$INSTALL_ROOT/run/control-ready" ]] || exit 1
    printf '{"status":"ok"}'
    ;;
  http://127.0.0.1:8000/api/v1/ready)
    [[ -f "$INSTALL_ROOT/run/control-ready" ]] || exit 1
    printf '{"status":"ready","database":"ready","upstream":"online"}'
    ;;
  https://control-node.example.ts.net/api/v1/health)
    [[ -f "$INSTALL_ROOT/run/control-ready" ]] || exit 1
    grep -q '127.0.0.1:8000' "$FAKE_TAILSCALE_STATE"
    printf '{"status":"ok"}'
    ;;
  https://control-node.example.ts.net/)
    [[ -f "$INSTALL_ROOT/run/control-ready" ]] || exit 1
    grep -q '127.0.0.1:8000' "$FAKE_TAILSCALE_STATE"
    printf '<!doctype html><html><link rel="manifest" href="/manifest.webmanifest"></html>'
    ;;
  https://control-node.example.ts.net/manifest.webmanifest)
    [[ -f "$INSTALL_ROOT/run/control-ready" ]] || exit 1
    grep -q '127.0.0.1:8000' "$FAKE_TAILSCALE_STATE"
    printf '{"name":"Agent Control","start_url":"/chats","icons":[{"src":"/icon.png"}]}'
    ;;
  *) exit 1 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "sqlite3",
        """#!/usr/bin/env bash
set -eu
printf 'sqlite3 %s\n' "$*" >>"$INSTALL_LOG"
for argument in "$@"; do
  if [[ "$argument" == ".backup '"*"'" ]]; then
    destination=${argument#".backup '"}
    destination=${destination%"'"}
    : >"$destination"
    exit 0
  fi
  if [[ "$argument" == "PRAGMA quick_check;" ]]; then
    printf 'ok\n'
    exit 0
  fi
done
printf '1\n'
""",
    )
    _write_executable(
        fake_bin / "node",
        "#!/usr/bin/env bash\n[[ \"${1:-}\" == -p ]] && printf '20\\n'\n",
    )
    _write_executable(fake_bin / "npm", "#!/usr/bin/env bash\nexit 0\n")

    environment = os.environ.copy()
    environment.update(
        {
            "HERMES_CONTROL_INSTALL_ROOT": str(root),
            "HERMES_CONTROL_INSTALL_TESTING": "1",
            "HERMES_CONTROL_INSTALL_SYSTEMCTL": str(fake_bin / "systemctl"),
            "HERMES_CONTROL_INSTALL_TAILSCALE": str(fake_bin / "tailscale"),
            "HERMES_CONTROL_INSTALL_CURL": str(fake_bin / "curl"),
            "HERMES_CONTROL_INSTALL_SQLITE3": str(fake_bin / "sqlite3"),
            "HERMES_CONTROL_INSTALL_PYTHON": str(fake_python),
            "HERMES_CONTROL_INSTALL_NODE": str(fake_bin / "node"),
            "HERMES_CONTROL_INSTALL_NPM": str(fake_bin / "npm"),
            "HERMES_CONTROL_INSTALL_PORT_PROBE": str(fake_bin / "port-probe"),
            "HERMES_CONTROL_INSTALL_HERMES_HOME": "/home/hermes",
            "INSTALL_ROOT": str(root),
            "INSTALL_LOG": str(log),
            "FAKE_TAILSCALE_STATE": str(state),
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
        }
    )
    return root, environment, state, log


def _install_command(source: Path, static_source: Path) -> list[str]:
    return [
        "bash",
        str(INSTALLER),
        "--source",
        str(source),
        "--static-dir",
        str(static_source),
        "--release-id",
        "test-release",
        "--tailscale-hostname",
        "control-node.example.ts.net",
        "--hermes-user",
        "hermes",
        "--hermes-bin",
        "/home/hermes/.hermes/hermes-agent/venv/bin/python",
        "--hermes-profile",
        "default",
        "--skip-admin",
    ]


def test_linux_installer_has_valid_shell_and_no_destructive_network_commands():
    source = INSTALLER.read_text(encoding="utf-8")
    stable_source = STABLE_INSTALLER.read_text(encoding="utf-8")
    syntax = subprocess.run(
        ["bash", "-n", str(INSTALLER)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    assert INSTALLER.stat().st_mode & stat.S_IXUSR
    assert STABLE_INSTALLER.stat().st_mode & stat.S_IXUSR
    assert 'exec "${SCRIPT_DIR}/linux/install-agent-control.sh" "$@"' in stable_source
    assert "${TAILSCALE_BIN} funnel" not in source
    assert "${TAILSCALE_BIN} serve reset" not in source
    assert "tailscale down" not in source.lower()
    assert "tailscale up" not in source.lower()
    assert "hermes update" not in source.lower()
    assert "--hermes-token" not in source
    assert "--set-path=/" in source
    assert "--reuse-tailscale-root" not in source
    assert "--merge-tailscale-root" not in source
    assert "apt-get install -y --no-install-recommends nodejs npm" not in source
    assert "python3 python3-venv" not in source
    assert "127.0.0.1:8000" in source
    assert "127.0.0.1:9119" in source
    assert "--dry-run" in source
    assert "${#HERMES_TOKEN} >= 32" in source
    assert 'payload.get("upstream") != "online"' in source
    assert "manifest.webmanifest" in source
    assert '.hermes-serve.env.XXXXXX' in source
    assert '"$RUNUSER_BIN" -u "$HERMES_USER"' in source
    assert 'hermes_env}.tmp.$$' not in source


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, "empty"),
        (
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "node.tail.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}
                    }
                },
            },
            "control",
        ),
        (
            {
                "Web": {
                    "node.tail.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}}
                    }
                }
            },
            "root-conflict",
        ),
        (
            {
                "Web": {
                    "node.tail.ts.net:443": {
                        "Handlers": {"/metrics": {"Proxy": "http://127.0.0.1:9000"}}
                    }
                }
            },
            "other-config",
        ),
        (
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "node.tail.ts.net:443": {
                        "Handlers": {
                            "/": {"Proxy": "http://127.0.0.1:8000"},
                            "/metrics": {"Proxy": "http://127.0.0.1:9000"},
                        }
                    }
                },
            },
            "other-config",
        ),
        (
            {"AllowFunnel": {"node.tail.ts.net:443": True}},
            "funnel",
        ),
    ],
)
def test_tailscale_serve_classifier_preserves_root_and_rejects_funnel(
    payload: dict[str, object], expected: str
):
    command = f"""
source {INSTALLER!s}
PYTHON_BIN={sys.executable!s}
classify_serve_config node.tail.ts.net http://127.0.0.1:8000
"""
    result = subprocess.run(
        ["bash", "-c", command],
        input=json.dumps(payload),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_installer_builds_immutable_release_and_rerun_preserves_secrets(
    tmp_path: Path,
):
    source, static_source = _fake_source(tmp_path)
    root, environment, state, log = _fake_host(tmp_path)
    command = _install_command(source, static_source)

    first = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    control_env = root / "etc/hermes-control/control.env"
    hermes_env = root / "home/hermes/.hermes/control-services/hermes-serve.env"
    initial_control_env = control_env.read_bytes()
    initial_hermes_env = hermes_env.read_bytes()
    assert stat.S_IMODE(control_env.stat().st_mode) == 0o640
    assert stat.S_IMODE(hermes_env.stat().st_mode) == 0o600
    assert "HERMES_CONTROL_HERMES_SOURCE_SHA=\n" in initial_control_env.decode()
    assert "HERMES_CONTROL_DEFAULT_PROFILES=default,jarvis\n" in initial_control_env.decode()
    assert "<generate" not in initial_control_env.decode()

    release = root / "opt/hermes-control/releases/test-release"
    current = root / "opt/hermes-control/current"
    assert current.is_symlink()
    assert current.resolve() == release
    assert (release / ".agent-control-release").read_text().strip() == "test-release"
    assert (release / "apps/api/static/index.html").is_file()
    assert (release / ".venv/bin/uvicorn").is_file()
    assert "Managed by Agent Control Linux installer" in (
        root / "etc/systemd/system/hermes-serve.service"
    ).read_text()
    assert "ProtectHome=tmpfs" in (
        root / "etc/systemd/system/hermes-control.service.d/media-root.conf"
    ).read_text()
    assert "BindReadOnlyPaths=/home/hermes/.hermes/profiles" in (
        root / "etc/systemd/system/hermes-control.service.d/media-root.conf"
    ).read_text()
    assert json.loads(state.read_text())["Web"]["control-node.example.ts.net:443"][
        "Handlers"
    ]["/"]["Proxy"] == "http://127.0.0.1:8000"

    second = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    assert control_env.read_bytes() == initial_control_env
    assert hermes_env.read_bytes() == initial_hermes_env
    command_log = log.read_text(encoding="utf-8")
    assert command_log.count("tailscale serve --bg") == 1
    assert "funnel" not in command_log.lower()
    assert "reset" not in command_log.lower()
    assert command_log.count("systemctl start hermes-control-backup.service") == 2
    assert command_log.count("systemctl enable --now hermes-control.service") == 1
    assert command_log.count("systemctl restart hermes-control.service") == 1


def test_installer_aborts_before_writing_secrets_when_9119_is_unmanaged(
    tmp_path: Path,
):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, _ = _fake_host(tmp_path)
    (root / "run").mkdir()
    (root / "run/hermes-ready").touch()

    result = subprocess.run(
        _install_command(source, static_source),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--reuse-hermes-serve" in result.stderr
    assert not (root / "etc/hermes-control/control.env").exists()
    assert not (root / "opt/hermes-control/releases/test-release").exists()


def test_reused_hermes_requires_and_authenticates_token_before_release(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, log = _fake_host(tmp_path)
    (root / "run").mkdir()
    (root / "run/hermes-ready").touch()
    command = [*_install_command(source, static_source), "--reuse-hermes-serve"]

    missing = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing.returncode != 0
    assert "needs a dashboard token" in missing.stderr
    assert not (root / "opt/hermes-control/releases/test-release").exists()
    assert not (root / "etc/hermes-control/control.env").exists()

    strong_token = "reuse-token-that-is-at-least-32-characters"
    environment["HERMES_CONTROL_INSTALL_HERMES_TOKEN"] = strong_token
    reused = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert reused.returncode == 0, reused.stderr
    assert strong_token not in log.read_text(encoding="utf-8")
    assert strong_token in (
        root / "etc/hermes-control/control.env"
    ).read_text(encoding="utf-8")


def test_dry_run_reports_plan_without_writing_or_starting_services(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, state, log = _fake_host(tmp_path)
    command = [*_install_command(source, static_source), "--dry-run"]

    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Dry-run only" in result.stdout
    assert "Hermes trust SHA: empty" in result.stdout
    assert not (root / "etc/hermes-control").exists()
    assert not (root / "opt/hermes-control").exists()
    assert json.loads(state.read_text(encoding="utf-8")) == {}
    command_log = log.read_text(encoding="utf-8")
    assert "systemctl daemon-reload" not in command_log
    assert "systemctl enable" not in command_log
    assert "systemctl start" not in command_log
    assert "tailscale serve --bg" not in command_log


def test_dry_run_detects_source_build_and_requires_node_20(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    del static_source
    (source / "apps/web").mkdir(parents=True)
    (source / "apps/web/package.json").write_text(
        '{"name":"web","scripts":{"build":"true"}}', encoding="utf-8"
    )
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    _, environment, _, _ = _fake_host(tmp_path)
    command = _install_command(source, tmp_path / "unused")
    static_index = command.index("--static-dir")
    del command[static_index : static_index + 2]
    command.append("--dry-run")

    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "PWA: build with Node.js >=20" in result.stdout


def test_incompatible_node_aborts_without_generic_install_or_release(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    del static_source
    (source / "apps/web").mkdir(parents=True)
    (source / "apps/web/package.json").write_text(
        '{"name":"web","scripts":{"build":"true"}}', encoding="utf-8"
    )
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    root, environment, _, _ = _fake_host(tmp_path)
    node = Path(environment["HERMES_CONTROL_INSTALL_NODE"])
    _write_executable(
        node,
        "#!/usr/bin/env bash\n[[ \"${1:-}\" == -p ]] && printf '18\\n'\n",
    )
    assert subprocess.run(
        [str(node), "-p", "ignored"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "18"
    command = _install_command(source, tmp_path / "unused")
    static_index = command.index("--static-dir")
    del command[static_index : static_index + 2]

    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "explicitly configured Node.js/npm tools" in result.stderr
    assert not (root / "opt/hermes-control/releases/test-release").exists()


def test_dry_run_plans_checksum_pinned_node_when_host_node_is_old(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    del static_source
    (source / "apps/web").mkdir(parents=True)
    (source / "apps/web/package.json").write_text(
        '{"name":"web","scripts":{"build":"true"}}', encoding="utf-8"
    )
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    _, environment, _, _ = _fake_host(tmp_path)
    node = Path(environment.pop("HERMES_CONTROL_INSTALL_NODE"))
    environment.pop("HERMES_CONTROL_INSTALL_NPM")
    environment["PATH"] = f"{node.parent}{os.pathsep}{environment['PATH']}"
    _write_executable(
        node,
        "#!/usr/bin/env bash\n[[ \"${1:-}\" == -p ]] && printf '18\\n'\n",
    )
    assert subprocess.run(
        [str(node), "-p", "ignored"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "18"
    command = _install_command(source, tmp_path / "unused")
    static_index = command.index("--static-dir")
    del command[static_index : static_index + 2]
    command.append("--dry-run")

    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Would install or safely repair pinned Node.js v22.23.2 from nodejs.org" in (
        result.stdout + result.stderr
    )


def test_installer_aborts_before_writing_secrets_when_serve_root_is_owned(
    tmp_path: Path,
):
    source, static_source = _fake_source(tmp_path)
    root, environment, state, _ = _fake_host(tmp_path)
    state.write_text(
        json.dumps(
            {
                "Web": {
                    "control-node.example.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        _install_command(source, static_source),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Tailscale Serve root is already owned" in result.stderr
    assert not (root / "etc/hermes-control/control.env").exists()
    assert not (root / "opt/hermes-control/releases/test-release").exists()


def test_installer_aborts_before_writes_when_serve_has_any_other_route(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, state, _ = _fake_host(tmp_path)
    state.write_text(
        json.dumps(
            {
                "Web": {
                    "control-node.example.ts.net:443": {
                        "Handlers": {
                            "/metrics": {"Proxy": "http://127.0.0.1:9999"}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        _install_command(source, static_source),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "existing routes" in result.stderr
    assert not (root / "etc/hermes-control/control.env").exists()
    assert not (root / "opt/hermes-control/releases/test-release").exists()


def test_installer_disables_media_without_widening_profile_permissions(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, _ = _fake_host(tmp_path)
    profile_root = root / "home/hermes/.hermes/profiles"
    profile_root.chmod(0)
    try:
        result = subprocess.run(
            _install_command(source, static_source),
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        control_env = (root / "etc/hermes-control/control.env").read_text(
            encoding="utf-8"
        )
        assert "HERMES_CONTROL_HERMES_MEDIA_ROOT=\n" in control_env
        assert "Hermes media is disabled" in result.stderr
        assert stat.S_IMODE(profile_root.stat().st_mode) == 0
        assert not (
            root / "etc/systemd/system/hermes-control.service.d/media-root.conf"
        ).exists()
    finally:
        profile_root.chmod(0o700)


def test_existing_environment_security_invariants_fail_before_release(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, _ = _fake_host(tmp_path)
    config_dir = root / "etc/hermes-control"
    config_dir.mkdir(parents=True)
    env_path = config_dir / "control.env"
    database_path = root / "var/lib/hermes-control/control.db"
    backup_path = root / "var/backups/hermes-control"
    env_path.write_text(
        "\n".join(
            (
                f"HERMES_CONTROL_DATABASE_URL=sqlite:///{database_path}",
                f"HERMES_CONTROL_DATABASE_PATH={database_path}",
                f"HERMES_CONTROL_BACKUP_DIR={backup_path}",
                "HERMES_CONTROL_ENVIRONMENT=production",
                "HERMES_CONTROL_VAULT_KEY_B64=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                "HERMES_CONTROL_HERMES_DASHBOARD_TOKEN=" + "t" * 40,
                "HERMES_CONTROL_HERMES_DASHBOARD_URL=http://0.0.0.0:9119",
                "HERMES_CONTROL_HERMES_DASHBOARD_WS=ws://127.0.0.1:9119/api/ws",
                "HERMES_CONTROL_HERMES_API_URL=",
                "HERMES_CONTROL_HERMES_API_KEY=",
                "HERMES_CONTROL_HERMES_MEDIA_ROOT=",
                "HERMES_CONTROL_ALLOWED_ORIGINS=https://control-node.example.ts.net",
                "HERMES_CONTROL_SECURE_COOKIES=true",
                "HERMES_CONTROL_CREATE_SCHEMA_ON_START=false",
                "HERMES_CONTROL_PROVIDER_MODE=real",
                "HERMES_CONTROL_MOCK_FALLBACK_ENABLED=false",
                "HERMES_CONTROL_TRUST_PRIVATE_ENDPOINTS=true",
                "HERMES_CONTROL_HERMES_SOURCE_SHA=",
                "",
            )
        ),
        encoding="utf-8",
    )
    env_path.chmod(0o640)

    result = subprocess.run(
        _install_command(source, static_source),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "dashboard URL must remain on loopback" in result.stderr
    assert not (root / "opt/hermes-control/releases/test-release").exists()


def test_explicit_trusted_sha_updates_only_that_non_secret_setting(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, _ = _fake_host(tmp_path)
    command = _install_command(source, static_source)
    first = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    env_path = root / "etc/hermes-control/control.env"

    def values() -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in env_path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )

    before = values()
    trusted_sha = "a" * 40
    second = subprocess.run(
        [*command, "--hermes-source-sha", trusted_sha],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    after = values()
    assert after["HERMES_CONTROL_HERMES_SOURCE_SHA"] == trusted_sha
    assert after["HERMES_CONTROL_VAULT_KEY_B64"] == before[
        "HERMES_CONTROL_VAULT_KEY_B64"
    ]
    assert after["HERMES_CONTROL_HERMES_DASHBOARD_TOKEN"] == before[
        "HERMES_CONTROL_HERMES_DASHBOARD_TOKEN"
    ]
    assert {
        key: value
        for key, value in after.items()
        if key != "HERMES_CONTROL_HERMES_SOURCE_SHA"
    } == {
        key: value
        for key, value in before.items()
        if key != "HERMES_CONTROL_HERMES_SOURCE_SHA"
    }


def test_unmanaged_port_8000_aborts_before_release_or_secrets(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, _ = _fake_host(tmp_path)
    (root / "run").mkdir()
    (root / "run/port-8000-claimed").touch()

    result = subprocess.run(
        _install_command(source, static_source),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "port 8000 is already owned" in result.stderr
    assert not (root / "opt/hermes-control/releases/test-release").exists()
    assert not (root / "etc/hermes-control/control.env").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate", "duplicate key HERMES_CONTROL_ALLOWED_ORIGINS"),
        ("symlink", "control.env must not be a symlink"),
        ("mode", "control.env must have mode 0640"),
    ],
)
def test_existing_environment_rejects_ambiguous_or_unsafe_file_before_migration(
    tmp_path: Path, mutation: str, message: str
):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, log = _fake_host(tmp_path)
    command = _install_command(source, static_source)
    first = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    env_path = root / "etc/hermes-control/control.env"
    if mutation == "duplicate":
        with env_path.open("a", encoding="utf-8") as destination:
            destination.write("HERMES_CONTROL_ALLOWED_ORIGINS=https://attacker.invalid\n")
    elif mutation == "symlink":
        real_path = env_path.with_name("control.env.real")
        env_path.rename(real_path)
        env_path.symlink_to(real_path)
    else:
        env_path.chmod(0o644)
    log.write_text("", encoding="utf-8")

    result = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)

    assert result.returncode != 0
    assert message in result.stderr
    assert "alembic " not in log.read_text(encoding="utf-8")


def test_existing_database_is_backed_up_and_verified_before_alembic(tmp_path: Path):
    source, static_source = _fake_source(tmp_path)
    root, environment, _, log = _fake_host(tmp_path)
    command = _install_command(source, static_source)
    first = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    database = root / "var/lib/hermes-control/control.db"
    database.write_bytes(b"existing sqlite database")
    database.chmod(0o600)
    log.write_text("", encoding="utf-8")

    second = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)

    assert second.returncode == 0, second.stderr
    command_log = log.read_text(encoding="utf-8")
    assert command_log.index("sqlite3 ") < command_log.index("alembic ")
    backups = list((root / "var/backups/hermes-control").glob("control-*.db"))
    assert len(backups) == 1
    assert backups[0].is_file() and not backups[0].is_symlink()
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert "Verified pre-migration SQLite backup" in second.stderr


def test_git_source_builds_committed_head_with_node_directory_first_in_path(
    tmp_path: Path,
):
    source, _ = _fake_source(tmp_path)
    (source / "apps/web/dist").mkdir(parents=True)
    (source / "apps/web/dist/index.html").write_text("STALE DIST", encoding="utf-8")
    (source / "apps/api/static").mkdir(parents=True)
    (source / "apps/api/static/index.html").write_text("STALE API", encoding="utf-8")
    (source / "apps/web/package.json").write_text(
        '{"name":"web","scripts":{"build":"fake"}}', encoding="utf-8"
    )
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Installer Test",
            "-c",
            "user.email=installer@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    root, environment, _, _ = _fake_host(tmp_path)
    tools = tmp_path / "node-tools"
    _write_executable(
        tools / "node",
        "#!/usr/bin/env bash\n[[ \"${1:-}\" == -p ]] && printf '22\\n'\n",
    )
    _write_executable(
        tools / "npm",
        """#!/usr/bin/env bash
set -eu
[[ "${PATH%%:*}" == "$(dirname "$0")" ]] || exit 71
[[ "${1:-}" != "--version" ]] || { printf '10\n'; exit 0; }
if [[ "${1:-}" == "run" && "${2:-}" == "build" ]]; then
  rm -rf apps/web/dist
  mkdir -p apps/web/dist
  printf '<!doctype html><title>BUILT FROM HEAD</title>' >apps/web/dist/index.html
fi
""",
    )
    environment["HERMES_CONTROL_INSTALL_NODE"] = str(tools / "node")
    environment["HERMES_CONTROL_INSTALL_NPM"] = str(tools / "npm")
    command = _install_command(source, tmp_path / "unused")
    static_index = command.index("--static-dir")
    del command[static_index : static_index + 2]

    result = subprocess.run(command, env=environment, check=False, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    installed = (
        root / "opt/hermes-control/releases/test-release/apps/api/static/index.html"
    ).read_text(encoding="utf-8")
    assert "BUILT FROM HEAD" in installed
    assert "STALE" not in installed


@pytest.mark.parametrize(
    ("passwd_record", "group_record", "primary_users", "groups", "expected"),
    [
        (
            "hermes-control:x:950:950::/var/lib/hermes-control:/usr/sbin/nologin",
            "hermes-control:x:950:",
            "hermes-control",
            "hermes-control",
            0,
        ),
        (
            "hermes-control:x:1000:950::/var/lib/hermes-control:/usr/sbin/nologin",
            "hermes-control:x:950:",
            "hermes-control",
            "hermes-control",
            1,
        ),
        (
            "hermes-control:x:950:950::/home/operator:/bin/bash",
            "hermes-control:x:950:operator",
            "hermes-control,operator",
            "hermes-control,sudo",
            1,
        ),
    ],
)
def test_control_account_validator_rejects_interactive_or_shared_accounts(
    passwd_record: str,
    group_record: str,
    primary_users: str,
    groups: str,
    expected: int,
):
    command = f"""
source {INSTALLER!s}
validate_control_account_records {passwd_record!r} {group_record!r} 1000 1000 {primary_users!r} {groups!r}
"""
    result = subprocess.run(["bash", "-c", command], check=False, capture_output=True, text=True)
    assert result.returncode == expected
