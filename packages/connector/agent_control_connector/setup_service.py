"""Owned process supervision and conservative managed-runtime lifecycle."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import secrets
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

import httpx
from hermes_client import HermesGatewayProvider, ProviderConnection

from .managed_manifest import current_platform, verify_runtime, verify_signature
from .manage import drain, management_lock, atomic_link
from .storage import atomic_json, private_dir, read_json

UNIT = "agent-control-managed.service"
LABEL = "com.jemailabs.agent-control.managed"


def runtime_environment(engine):
    # Do not inherit another Hermes/Codex instance's credentials or Python paths.
    allowed = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "WAYLAND_DISPLAY"}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    root = Path(engine.state.get("releaseRoot", str(engine.root)))
    env.update(HERMES_HOME=engine.state["hermesHome"],
               AGENT_CONTROL_MANAGED_DIR=str(engine.directory),
               HERMES_DASHBOARD_SESSION_TOKEN=engine.token(),
               HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
               PIP_REQUIRE_VIRTUALENV="true",
               PYTHONPATH=os.pathsep.join([str(root / "connector"), str(root / "hermes")]))
    env["PATH"] = str(root / "python/bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    tool_environment = engine.directory / "tool-environments/default"
    if (tool_environment / "bin/python").exists():
        env["PATH"] = str(tool_environment / "bin") + os.pathsep + env["PATH"]
        env["VIRTUAL_ENV"] = str(tool_environment)
    if engine.state.get("extras", {}).get("browser"):
        from .setup_extras import browser_environment
        env.update(browser_environment(engine, env["PATH"]))
    return env


def service_file():
    return Path.home() / ".config/systemd/user" / UNIT


def owned_linux_service():
    target = service_file()
    if target.is_symlink() or (target.exists() and not target.read_text().startswith("# Managed by Agent Control setup\n")):
        raise ValueError("Otro servicio ocupa este nombre. No se modificó.")
    return target


def systemctl(*args, check=True):
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, timeout=60)
    if check and result.returncode:
        raise ValueError("No se pudo configurar el servicio de usuario. Comprueba que systemd --user esté disponible.")
    return result


def install_linux_service(engine):
    if not shutil.which("systemctl"):
        raise ValueError("Esta versión requiere Linux con systemd. La instalación pendiente se ha conservado.")
    target = owned_linux_service()
    marker = "# Managed by Agent Control setup\n"
    root = Path(engine.state["releaseRoot"])
    atomic_link(engine.directory, "current", root)
    def word(value):
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    args = [root / "python/bin/python3", "-s", "-B", "-m", "agent_control_connector.setup_engine", "--service",
            "--release-root", root, "--data-dir", engine.directory, "--server", engine.server]
    private_dir(target.parent)
    target.write_text(marker + "[Unit]\nDescription=Agent Control with Hermes\nAfter=network-online.target\n"
        "[Service]\nType=simple\nExecStart=" + " ".join(map(word, args)) + "\n"
        + "Environment=" + word("PYTHONPATH=" + str(root / "connector")) + "\n"
        + "Restart=on-failure\nRestartSec=10\nUMask=0077\n[Install]\nWantedBy=default.target\n")
    target.chmod(0o600)
    systemctl("daemon-reload")
    systemctl("enable", "--now", UNIT)
    user = os.environ.get("USER", str(os.getuid()))
    result = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"], capture_output=True, text=True, timeout=15)
    if result.returncode or result.stdout.strip() != "yes":
        # loginctl may display the OS's authorization prompt. No sudo/password collection.
        subprocess.run(["loginctl", "enable-linger", user], capture_output=True, timeout=30)
        result = subprocess.run(["loginctl", "show-user", user, "-p", "Linger", "--value"], capture_output=True, text=True, timeout=15)
        if result.returncode or result.stdout.strip() != "yes":
            raise ValueError("Hermes está instalado. Para mantenerlo conectado al cerrar SSH, un administrador debe ejecutar: loginctl enable-linger " + user + ". Luego vuelve a ejecutar el instalador.")


def ensure_service(engine):
    stopped = engine.directory / "stop.request"
    if stopped.exists():
        marker = read_json(stopped)
        if marker.get("reason") == "uninstall" and not (engine.directory / "mac-update.json").exists():
            stopped.unlink()
    deadline = time.monotonic() + 90
    paired = (engine.connector_dir / "config.json").exists()
    while time.monotonic() < deadline:
        value = engine.status()
        if value["ready"] if paired else value["localReady"]:
            pending = engine.directory / "restart.pending.json"
            if pending.exists():
                request = read_json(pending)
                observed = read_json(engine.directory / "service-status.json")
                if observed.get("restartId") != request["id"]:
                    time.sleep(1)
                    continue
                marker = engine.connector_dir / "maintenance.request"
                if marker.exists():
                    if marker.is_symlink() or marker.read_text() != request.get("maintenanceId"):
                        raise ValueError("Otra operación mantiene el equipo en mantenimiento.")
                    marker.unlink()
                for extra in engine.state.get("extras", {}).values():
                    extra.pop("restartRequired", None)
                engine.save()
                pending.unlink()
            return
        time.sleep(1)
    hint = "Permite Agent Control en Ítems de inicio." if sys.platform == "darwin" else "Comprueba systemctl --user status agent-control-managed.service."
    raise ValueError("El servicio aún no está listo. " + hint + " Abre Diagnóstico y reintenta.")


def supervise(engine):
    lock_file = engine.directory / "supervisor.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("El servicio de este equipo ya está ejecutándose.") from None
        if not engine.state or engine.state.get("mode") not in {"managed", "existing"}:
            raise ValueError("Completa el asistente antes de activar el servicio.")
        engine.root = Path(engine.state["releaseRoot"])
        verify_runtime(engine.root)
        if (engine.directory / "stop.request").exists():
            return
        if engine.state["mode"] == "managed":
            prepare_owned_tools(engine)
        env = runtime_environment(engine)
        python = str(engine.root / "python/bin/python3")
        port = engine.state["restUrl"].rsplit(":", 1)[1]
        stopping = False
        def stop(*_):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        children = []
        restart = False
        try:
            # Hermes output can contain user content. The supervisor records only
            # process exit codes and status, never captures transcripts or secrets.
            hermes = None
            if engine.state["mode"] == "managed":
                # A root HERMES_HOME alone still follows Hermes' sticky active_profile.
                # Keep this shared server rooted at default when users switch profiles.
                hermes = subprocess.Popen([python, "-s", "-B", "-c", "from hermes_cli.main import main; main()",
                    "-p", "default", "serve", "--host", "127.0.0.1", "--port", port, "--isolated"],
                    env=env, cwd=engine.state["hermesHome"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                children.append(hermes)
            connector = None
            last_exit = 0.0
            pending = engine.directory / "restart.pending.json"
            restart_id = read_json(pending).get("id") if pending.exists() else None
            atomic_json(engine.directory / "service-status.json", {"startedAt": datetime.now(timezone.utc).isoformat(),
                        "restartId": restart_id, "releaseRoot": str(engine.root)})
            while not stopping:
                if hermes is not None and hermes.poll() is not None:
                    atomic_json(engine.directory / "service-status.json", {"component": "hermes", "exitCode": hermes.returncode})
                    raise ValueError("Hermes se detuvo. Abre Diagnóstico.")
                if (engine.directory / "stop.request").exists():
                    break
                if (engine.directory / "restart.request").exists():
                    request = read_json(engine.directory / "restart.request")
                    if pending.exists() and request.get("id") == read_json(pending).get("id"):
                        (engine.directory / "restart.request").unlink()
                        restart = True
                        break
                if (engine.connector_dir / "config.json").exists():
                    if connector is not None and connector.poll() is not None:
                        children.remove(connector)
                        connector, last_exit = None, time.monotonic()
                    if connector is None and time.monotonic() - last_exit > 10:
                        connector = subprocess.Popen([python, "-s", "-B", "-m", "agent_control_connector", "run",
                            "--data-dir", str(engine.connector_dir)], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                        children.append(connector)
                time.sleep(1)
        finally:
            # Only process groups started by this supervisor are eligible.
            for child in reversed(children):
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=25)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)
        if restart:
            # An explicit, idle-checked restart. Both user service managers
            # restart a nonzero exit; the new instance consumes pending state.
            raise SystemExit(75)


def prepare_owned_tools(engine):
    """Prepare writable tool dependencies and adapters before owned Hermes boots."""
    target = engine.directory / "tool-environments/default"
    if not (target / "bin/python").exists():
        private_dir(target.parent)
        subprocess.run([str(engine.root / "python/bin/python3"), "-s", "-B", "-m", "venv",
                        "--system-site-packages", str(target)], check=True, capture_output=True, timeout=120,
                       env={"HOME": str(Path.home()), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    config_path = engine.connector_dir / "config.json"
    config = read_json(config_path) if config_path.exists() else {**getattr(engine, "state", {}), "profiles": ["default"]}
    if config.get("hermesHome") and config.get("sourceSha"):
        from .chat_modes_install import chat_mode_profiles
        from .media_install import media_profiles
        from .background_install import background_profiles
        for install in (chat_mode_profiles, media_profiles, background_profiles):
            states = install(config, install=True)
            if any(value.get("state") == "installationFailed" for value in states.values()):
                raise ValueError("No se pudo preparar una integración. Conservamos la instalación para diagnóstico.")


async def all_profiles_idle(engine):
    names = await engine.profiles()
    for name in names:
        provider = HermesGatewayProvider(ProviderConnection(gateway_id="maintenance", profile_name=name,
            rest_url=engine.state["restUrl"], ws_url=engine.state["restUrl"].replace("http", "ws", 1) + "/api/ws",
            dashboard_token=engine.token(), trusted_source_sha=engine.state["sourceSha"]))
        try:
            from .runtime import ACTIVE
            sessions = await asyncio.wait_for(provider.list_sessions(), 20)
            if not provider.session_inventory_complete or any(s.status in ACTIVE for s in sessions):
                raise ValueError("Hay trabajo activo o incierto en Hermes. Vuelve a intentar cuando termine.")
        finally:
            await provider.close()


def ledger_idle(engine):
    ledger = engine.connector_dir / "operations.sqlite3"
    if not ledger.exists():
        return
    if ledger.is_symlink() or not ledger.is_file():
        raise ValueError("No se puede comprobar el registro de operaciones.")
    try:
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            count = db.execute("SELECT count(*) FROM operations WHERE state IN ('running','unknown')").fetchone()[0]
    except sqlite3.Error:
        raise ValueError("El registro de operaciones no está disponible. No se reiniciará.") from None
    if count:
        raise ValueError("Hay operaciones con resultado incierto. Resuélvelas antes de reiniciar.")


def supervisor_running(engine):
    path = engine.directory / "supervisor.lock"
    if not path.exists():
        return False
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except OSError:
            return True


@contextmanager
def idle_installation(engine):
    paired = (engine.connector_dir / "config.json").exists()
    with management_lock(engine.directory):
        marker = engine.connector_dir / "maintenance.request"
        if marker.exists() or any((engine.directory / name).exists() for name in ("mac-update.json", "linux-update.json", "restart.pending.json")):
            raise ValueError("Hay otra operación de mantenimiento pendiente. Recupera esa operación primero.")
        try:
            if paired:
                drain(engine.connector_dir)
            if engine.status()["localReady"]:
                asyncio.run(all_profiles_idle(engine))
            elif supervisor_running(engine):
                raise ValueError("Hermes sigue ejecutándose pero no responde. No se interrumpirá para recuperar la actualización.")
            elif paired:
                raise ValueError("No se puede comprobar el estado de Hermes. No se reiniciará.")
            ledger_idle(engine)
            yield
        finally:
            if paired and not any((engine.directory / name).exists() for name in ("linux-update.json", "mac-update.json", "restart.pending.json")):
                (engine.connector_dir / "maintenance.request").unlink(missing_ok=True)


def fetch(url, destination, maximum):
    from .tls import cloud_ssl_context
    size = 0
    with httpx.Client(timeout=120, trust_env=False, follow_redirects=False, verify=cloud_ssl_context()) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for block in response.iter_bytes():
                    size += len(block)
                    if size > maximum:
                        raise ValueError("La descarga supera el tamaño permitido.")
                    output.write(block)


def extract_verified_archive(archive: Path, destination: Path):
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        if len(members) > 200_000 or sum(m.size for m in members) > 8 * 1024**3:
            raise ValueError("Paquete demasiado grande.")
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError("Paquete con rutas o enlaces no permitidos.")
        package.extractall(destination, members=members, filter="data")


def stage_update(engine, *, expected_release=None):
    import hashlib
    import re
    with tempfile.TemporaryDirectory(dir=engine.directory, prefix="download-") as temporary:
        temporary = Path(temporary)
        fetch(engine.server + "/downloads/agent-control/latest.json", temporary / "latest.json", 65536)
        fetch(engine.server + "/downloads/agent-control/latest.json.sig", temporary / "latest.json.sig", 8192)
        verify_signature(temporary / "latest.json", temporary / "latest.json.sig")
        latest = json.loads((temporary / "latest.json").read_bytes())
        release = latest.get("version", "")
        if not re.fullmatch(r"[a-f0-9]{40}", release):
            raise ValueError("No hay una actualización publicada válida.")
        if expected_release is not None and release != expected_release:
            raise ValueError("La publicación cambió; vuelve a comprobar la actualización.")
        base = engine.server + "/downloads/agent-control/releases/" + release + "/"
        fetch(base + "SHA256SUMS", temporary / "SHA256SUMS", 65536)
        fetch(base + "SHA256SUMS.sig", temporary / "SHA256SUMS.sig", 8192)
        verify_signature(temporary / "SHA256SUMS", temporary / "SHA256SUMS.sig")
        name = "agent-control-runtime-" + current_platform() + ".tar.gz"
        checksums = dict(line.split(None, 1)[::-1] for line in (temporary / "SHA256SUMS").read_text().splitlines())
        checksums = {key.strip().lstrip("*"): value for key, value in checksums.items()}
        if name not in checksums:
            raise ValueError("No hay paquete verificado para este equipo.")
        fetch(base + name, temporary / name, 4 * 1024**3)
        with (temporary / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksums[name]:
                raise ValueError("La descarga no pasó la verificación de integridad.")
        destination = engine.directory / "releases" / release
        if destination.exists():
            verify_runtime(destination, expected_release=release)
            return destination
        extracted = temporary / "extracted"
        extracted.mkdir()
        extract_verified_archive(temporary / name, extracted)
        candidates = list(extracted.glob("*/runtime-manifest.json")) + list(extracted.glob("runtime-manifest.json"))
        if len(candidates) != 1:
            raise ValueError("Estructura de paquete no válida.")
        root = candidates[0].parent
        verify_runtime(root, expected_release=release)
        private_dir(destination.parent)
        root.rename(destination)
        return destination


def lifecycle(engine, method, params):
    if method == "extras-list":
        # Availability is release metadata, never inferred from a URL or a Python import.
        manifest = verify_runtime(engine.root)
        installed = bool(engine.state.get("extras", {}).get("browser"))
        return {"items": [{"id": "browser", "name": "Navegador automatizado", "available": bool(manifest.get("extras", {}).get("browser")),
                           "installed": installed, "restartRequired": installed and bool(engine.state.get("extras", {}).get("browser", {}).get("restartRequired")),
                           "message": "Descarga opcional; Linux puede requerir bibliotecas del sistema."}]}
    if method == "extras-install":
        from .setup_extras import install_extra
        return install_extra(engine, params)
    if engine.state.get("mode") != "managed" and method not in {"uninstall", "update", "rollback"}:
        raise ValueError("Esta acción solo administra el Hermes instalado por Agent Control.")
    if sys.platform != "darwin":
        # A failed setup may have persisted state before discovering a name
        # collision. Never stop/disable a unit merely because state exists.
        owned_linux_service()
    if method == "restart":
        with idle_installation(engine):
            if not supervisor_running(engine):
                raise ValueError("El servicio está detenido. Usa Reanudar para iniciarlo.")
            marker = engine.connector_dir / "maintenance.request"
            request = {"id": secrets.token_urlsafe(24), "maintenanceId": marker.read_text() if marker.exists() else None}
            atomic_json(engine.directory / "restart.pending.json", request)
            atomic_json(engine.directory / "restart.request", request)
            ensure_service(engine)
        return {**engine.status(), "status": "complete"}
    if method == "uninstall":
        with idle_installation(engine):
            if sys.platform != "darwin":
                systemctl("disable", "--now", UNIT)
                service_file().unlink(missing_ok=True)
                systemctl("daemon-reload")
            else:
                atomic_json(engine.directory / "stop.request", {"reason": "uninstall"})
            # Histories, credentials, pairing and operation receipts stay intact.
            return {"status": "stopped", "unregisterService": sys.platform == "darwin", "dataPreserved": True}
    if sys.platform == "darwin":
        from .setup_mac_lifecycle import lifecycle as mac_lifecycle
        return mac_lifecycle(engine, method, params)
    transaction_path = engine.directory / "linux-update.json"
    if transaction_path.exists():
        if method != "rollback":
            raise ValueError("Hay una actualización interrumpida. Ejecuta --rollback para recuperar la versión anterior.")
        with management_lock(engine.directory):
            transaction = read_json(transaction_path)
            original = transaction["oldState"]
            verify_runtime(Path(original["releaseRoot"]))
            # Both releases were checked for the same data schema before cutover.
            # Refuse a recovery if user work could have resumed in the meantime.
            ledger_idle(engine)
            if engine.status()["localReady"]:
                asyncio.run(all_profiles_idle(engine))
            elif supervisor_running(engine):
                raise ValueError("Hermes sigue ejecutándose pero no responde. No se interrumpirá para recuperar la actualización.")
            systemctl("stop", UNIT)
            engine.state = original
            engine.root = Path(original["releaseRoot"])
            engine.save()
            if transaction["oldConfig"]:
                atomic_json(engine.connector_dir / "config.json", transaction["oldConfig"])
            install_linux_service(engine)
            ensure_service(engine)
            (engine.connector_dir / "maintenance.request").unlink(missing_ok=True)
            transaction_path.unlink()
        return {**engine.status(), "status": "restored"}
    target = Path(params["releaseRoot"]).resolve() if params.get("releaseRoot") else None
    if method == "rollback":
        if not (engine.directory / "previous").exists():
            raise ValueError("No hay una versión anterior disponible.")
        target = (engine.directory / "previous").resolve()
    target = target or stage_update(engine)
    manifest = verify_runtime(target)
    old_root = Path(engine.state["releaseRoot"])
    old_manifest = verify_runtime(old_root)
    if manifest.get("dataSchemaVersion", 1) != old_manifest.get("dataSchemaVersion", 1):
        raise ValueError("Esta versión requiere una migración de datos; conserva la versión actual.")
    if old_root == target:
        return {"status": "current", "message": "Ya tienes esta versión."}
    from .setup_extras import validate_extra_transition
    next_extras = validate_extra_transition(engine, target)
    with idle_installation(engine):
        from .updates import check_intent
        check_intent(engine.connector_dir, params.get("expectedControl"))
        original_state = dict(engine.state)
        original_config = read_json(engine.connector_dir / "config.json") if (engine.connector_dir / "config.json").exists() else None
        atomic_json(transaction_path, {"oldState": original_state, "oldConfig": original_config,
                    "targetRoot": str(target), "dataSchemaVersion": manifest.get("dataSchemaVersion", 1)})
        systemctl("stop", UNIT)
        try:
            backup = engine.directory / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            private_dir(backup.parent)
            if engine.state["mode"] == "managed":
                shutil.copytree(engine.state["hermesHome"], backup, symlinks=True)
            else:
                private_dir(backup)
                atomic_json(backup / "setup.json", original_state)
            engine.state.update(releaseRoot=str(target), extras=next_extras)
            if engine.state["mode"] == "managed":
                engine.state.update(hermesSource=str(target / "hermes"), sourceSha=manifest["hermesSourceSha"], hermesVersion=manifest["hermesVersion"])
            engine.save()
            if original_config and engine.state["mode"] == "managed":
                atomic_json(engine.connector_dir / "config.json", {**original_config, "hermesSource": str(target / "hermes"),
                            "sourceSha": manifest["hermesSourceSha"]})
            install_linux_service(engine)
            ensure_service(engine)
            atomic_link(engine.directory, "previous", old_root)
            engine.root = target
            transaction_path.unlink()
        except Exception:
            systemctl("stop", UNIT, check=False)
            engine.state = original_state
            engine.save()
            if original_config:
                atomic_json(engine.connector_dir / "config.json", original_config)
            install_linux_service(engine)
            ensure_service(engine)
            transaction_path.unlink(missing_ok=True)
            raise
    return {**engine.status(), "status": "complete"}
