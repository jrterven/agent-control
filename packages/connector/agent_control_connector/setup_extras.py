"""Explicit installation of publisher-verified optional browser packages.

No npm/pip resolution, privileged package install, or service restart takes
place here. Explicit installation includes an isolated blank-page diagnostic;
availability comes only from the signed runtime.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit

from .managed_manifest import current_platform, verify_runtime, verify_signature
from .storage import atomic_json, private_dir

EXTRA_VERSION = "agent-browser-0.26.0-chrome-153.0.8010.47-node-22.23.2"
COMPONENTS = {"agentBrowser": "0.26.0", "chromium": "153.0.8010.47", "node": "22.23.2"}
MAX_BYTES = 3_000_000_000
MAX_ARCHIVE = 1_000_000_000
MAX_FILES = 100_000
METADATA = {"extra-manifest.json", "extra-manifest.json.sig"}


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _relative(name):
    if not isinstance(name, str) or not name or "\\" in name or any(ord(c) < 32 for c in name):
        raise ValueError("Ruta no válida en el módulo de navegador.")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts or str(path) != name:
        raise ValueError("Ruta no válida en el módulo de navegador.")
    return path


def descriptor(manifest):
    extras = manifest.get("extras", {})
    if not isinstance(extras, dict):
        raise ValueError("El catálogo firmado del navegador no es compatible.")
    entry = extras.get("browser")
    if not entry:
        raise ValueError("El navegador opcional todavía no está publicado para esta versión y plataforma.")
    release = manifest.get("release")
    platform = current_platform()
    expected_url = f"/downloads/agent-control/releases/{release}/agent-control-browser-{platform}.tar.gz"
    if (not re.fullmatch(r"[a-f0-9]{40}", str(release))
        or not isinstance(entry, dict) or entry.get("schemaVersion") != 1 or entry.get("id") != "browser"
        or entry.get("version") != EXTRA_VERSION or entry.get("platform") != platform
        or entry.get("url") != expected_url
        or not re.fullmatch(r"[a-f0-9]{64}", str(entry.get("sha256", "")))
        or type(entry.get("size")) is not int or not 0 < entry["size"] <= MAX_ARCHIVE):
        raise ValueError("El catálogo firmado del navegador no es compatible.")
    return entry


def verify_extra(root: Path, *, signature=True, expected_release=None, expected_platform=None):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("El navegador debe estar en un directorio propio.")
    document = root / "extra-manifest.json"
    if document.is_symlink() or document.stat().st_size > 32_000_000:
        raise ValueError("Manifiesto de navegador no válido.")
    if signature:
        verify_signature(document, root / "extra-manifest.json.sig")
    manifest = json.loads(document.read_bytes())
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or manifest.get("id") != "browser"
        or manifest.get("version") != EXTRA_VERSION or manifest.get("platform") != (expected_platform or current_platform())
        or manifest.get("components") != COMPONENTS or manifest.get("pythonAbi") is not None
        or not re.fullmatch(r"[a-f0-9]{40}", str(manifest.get("release", "")))
        or (expected_release is not None and manifest["release"] != expected_release)
        or manifest.get("certification") != {"native": True, "offlineBrowser": True}):
        raise ValueError("El navegador no está certificado para este equipo.")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise ValueError("Inventario de navegador no válido.")
    actual, total = set(), 0
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError("El navegador contiene enlaces o archivos especiales.")
        if path.is_file() and path.relative_to(root).as_posix() not in METADATA:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files):
        raise ValueError("El inventario de navegador cambió.")
    for name, expected in files.items():
        _relative(name)
        path = root / name
        info = path.stat()
        total += info.st_size
        if (not isinstance(expected, dict) or total > MAX_BYTES or info.st_size != expected.get("size")
            or stat.S_IMODE(info.st_mode) != expected.get("mode") or stat.S_IMODE(info.st_mode) not in {0o644, 0o755}
            or digest(path) != expected.get("sha256")):
            raise ValueError("Falló la verificación de integridad del navegador.")
    entrypoints = manifest.get("entrypoints", {})
    if not isinstance(entrypoints, dict) or entrypoints.get("node") != "node/bin/node" or entrypoints.get("agentBrowser") != "bin/agent-browser":
        raise ValueError("Los ejecutables del navegador no son compatibles.")
    for name in (entrypoints.get("node"), entrypoints.get("agentBrowser"), entrypoints.get("chromium")):
        _relative(name)
        if name not in files or not os.access(root / name, os.X_OK):
            raise ValueError("Falta un ejecutable verificado del navegador.")
    return manifest


def extract_extra(archive: Path, destination: Path):
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names, total = set(), 0
        if len(members) > MAX_FILES:
            raise ValueError("El navegador supera el límite de archivos.")
        for member in members:
            name = member.name.rstrip("/") if member.isdir() else member.name
            path = _relative(name)
            total += member.size
            if (name in names or path.parts[0] != "browser-extra" or total > MAX_BYTES
                or not (member.isfile() or member.isdir()) or member.mode & 0o7000):
                raise ValueError("Archivo comprimido de navegador no seguro.")
            names.add(name)
        bundle.extractall(destination, members=members, filter="data")


def missing_linux_libraries(root, manifest):
    if not current_platform().startswith("linux-"):
        return []
    ldd = shutil.which("ldd")
    if not ldd:
        raise ValueError("No se pueden comprobar las bibliotecas del sistema: falta ldd. Pide ayuda a un administrador.")
    executable = root / manifest["entrypoints"]["chromium"]
    result = subprocess.run([ldd, str(executable)], capture_output=True, text=True, timeout=30,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    missing = sorted(set(re.findall(r"(lib[A-Za-z0-9_.+-]+\.so(?:\.[0-9]+)*)\s+=>\s+not found", result.stdout)))
    if result.returncode and not missing:
        raise ValueError("No se pudieron comprobar las bibliotecas del navegador. No se activó el módulo.")
    return missing


def probe_browser(root, manifest):
    """Check a blank page in a temporary profile only after explicit install."""
    with tempfile.TemporaryDirectory(prefix="agent-control-browser-check-") as temporary:
        command = [str(root / manifest["entrypoints"]["chromium"]), "--headless", "--dump-dom",
                   "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
                   "--disable-component-update", "--disable-sync", "--disable-extensions", "--password-store=basic",
                   "--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>",
                   "--user-data-dir=" + temporary + "/profile", "about:blank"]
        environment = {"HOME": temporary, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                       "XDG_CACHE_HOME": temporary + "/cache", "XDG_CONFIG_HOME": temporary + "/config"}
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env=environment, cwd=temporary, start_new_session=True)
        try:
            try:
                output, error = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                return {"status": "diagnostic-failed", "id": "browser",
                        "message": "El navegador no terminó la comprobación local. No se activó; revisa los recursos y permisos del equipo."}
            if process.returncode or "<html" not in output.lower():
                if any(word in error.lower() for word in ("no usable sandbox", "apparmor", "operation not permitted", "failed to move to new namespace")):
                    return {"status": "sandbox-blocked", "id": "browser",
                            "executable": str(root / manifest["entrypoints"]["chromium"]),
                            "helpUrl": "https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md",
                            "message": "El sistema bloqueó el sandbox de Chromium. No se activó el navegador. Un administrador debe revisar el permiso de espacios de nombres para este ejecutable y, si aplica, un perfil AppArmor específico. No desactives el sandbox ni la protección global del sistema."}
                return {"status": "diagnostic-failed", "id": "browser",
                        "message": "Chromium no pudo abrir una página local vacía en un perfil temporal. No se activó; pide a un administrador que revise las dependencias y permisos del navegador."}
        finally:
            # This group belongs exclusively to this explicit diagnostic.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
    return None


def install_extra(engine, params):
    if params.get("id") != "browser" or set(params) != {"id"}:
        raise ValueError("Selecciona el módulo de navegador desde el asistente.")
    if engine.state.get("mode") != "managed":
        raise ValueError("Configura los extras de un Hermes existente desde su instalación original.")
    runtime = verify_runtime(Path(engine.state.get("releaseRoot", str(engine.root))))
    entry = descriptor(runtime)
    parsed = urlsplit(engine.server)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("El navegador requiere el servidor HTTPS verificado.")
    from .setup_service import fetch, idle_installation
    root = engine.directory / "extras"
    private_dir(root)
    target = root / entry["sha256"]
    with tempfile.TemporaryDirectory(dir=root, prefix=".browser-") as temporary:
        stage = Path(temporary)
        archive = stage / "browser.tar.gz"
        if target.exists() or target.is_symlink():
            manifest = verify_extra(target, expected_release=runtime["release"])
        else:
            fetch(engine.server.rstrip("/") + entry["url"], archive, entry["size"])
            if archive.stat().st_size != entry["size"] or digest(archive) != entry["sha256"]:
                raise ValueError("La descarga del navegador no coincide con la publicación firmada.")
            extract_extra(archive, stage / "unpacked")
            manifest = verify_extra(stage / "unpacked/browser-extra", expected_release=runtime["release"])
            target.parent.mkdir(parents=True, exist_ok=True)
            (stage / "unpacked/browser-extra").rename(target)
        missing = missing_linux_libraries(target, manifest)
        if missing:
            return {"status": "dependencies-required", "id": "browser", "missingLibraries": missing,
                    "message": "Faltan bibliotecas del sistema. Un administrador debe instalar los paquetes correspondientes con apt u otro gestor. Agent Control no ejecutará sudo ni instalará paquetes sin tu intervención."}
        with idle_installation(engine):
            failure = probe_browser(target, manifest)
            if failure:
                return failure
            # State is the only activation record. The supervisor consumes the
            # environment on the next explicit, idle service lifecycle action.
            import yaml
            config = Path(engine.state["hermesHome"]) / "config.yaml"
            if config.is_symlink() or not config.is_file() or config.stat().st_size > 4_000_000:
                raise ValueError("No se modificó la configuración de Hermes: archivo no válido.")
            original = config.read_bytes()
            configuration = yaml.safe_load(original)
            if not isinstance(configuration, dict):
                raise ValueError("La configuración de Hermes no es válida.")
            tool_settings = configuration.setdefault("tools", {})
            platforms = configuration.setdefault("platform_toolsets", {})
            if not isinstance(tool_settings, dict) or not isinstance(platforms, dict):
                raise ValueError("La configuración de herramientas no es válida.")
            for settings, key in ((tool_settings, "enabled_toolsets"), (platforms, "cli")):
                existing = settings.setdefault(key, [])
                if not isinstance(existing, list) or not all(isinstance(item, str) for item in existing):
                    raise ValueError("La lista de herramientas de Hermes no es válida.")
                if "browser" not in existing:
                    existing.append("browser")
            old_state = copy.deepcopy(engine.state)
            try:
                atomic_json(config, configuration)
                engine.state.setdefault("extras", {})["browser"] = {
                    "root": str(target), "sha256": entry["sha256"], "version": entry["version"],
                    "releaseRoot": str(Path(engine.state.get("releaseRoot", str(engine.root)))),
                    "release": runtime["release"], "restartRequired": True,
                }
                engine.save()
            except Exception:
                engine.state = old_state
                with tempfile.NamedTemporaryFile(dir=config.parent, delete=False) as restore:
                    restore.write(original)
                    restore.flush()
                    os.fsync(restore.fileno())
                    restore_path = Path(restore.name)
                restore_path.chmod(0o600)
                restore_path.replace(config)
                raise
    return {"status": "installed", "id": "browser", "restartRequired": True,
            "message": "Navegador verificado e instalado. Reinicia desde el asistente cuando no haya trabajo activo para habilitarlo."}


def _installed_browser(engine, *, origin_root=None):
    installed = engine.state.get("extras", {}).get("browser")
    if not isinstance(installed, dict) or engine.state.get("mode") != "managed":
        raise ValueError("El navegador instalado no pertenece a una instalación administrada.")
    source = Path(origin_root or installed.get("releaseRoot") or engine.state.get("releaseRoot", str(engine.root)))
    runtime = verify_runtime(source)
    if installed.get("release", runtime["release"]) != runtime["release"]:
        raise ValueError("El runtime de origen no corresponde al navegador instalado.")
    entry = descriptor(runtime)
    root = engine.directory / "extras" / entry["sha256"]
    if (installed.get("sha256") != entry["sha256"] or installed.get("root") != str(root)
        or installed.get("version") != entry["version"]):
        raise ValueError("El navegador instalado no pertenece a esta versión verificada.")
    manifest = verify_extra(root, expected_release=runtime["release"])
    canonical = {"root": str(root), "sha256": entry["sha256"], "version": entry["version"],
                 "releaseRoot": str(source), "release": runtime["release"],
                 "restartRequired": bool(installed.get("restartRequired"))}
    return canonical, root, manifest, runtime


def validate_extra_transition(engine, target_root: Path, *, origin_root: Path | None = None):
    """Return the verified complete extra state for update/rollback, unchanged on error.

    The source runtime must remain immutable on disk. ``origin_root`` supports
    a preverified copy when an app replacement will relocate the source runtime.
    It must already exist; this function never copies, downloads or launches.
    """
    extras = engine.state.get("extras", {})
    if not isinstance(extras, dict) or set(extras) - {"browser"}:
        raise ValueError("La transición contiene módulos opcionales no compatibles.")
    if not extras:
        return {}
    canonical, _, _, source = _installed_browser(engine, origin_root=origin_root)
    target = source if Path(canonical["releaseRoot"]) == Path(target_root) else verify_runtime(Path(target_root))
    _require_compatible_hermes(source, target)
    return {"browser": canonical}


def _require_compatible_hermes(source, target):
    if (source.get("platform") != target.get("platform")
        or source.get("hermesSourceSha") != target.get("hermesSourceSha")):
        raise ValueError("Esta actualización cambia la compatibilidad de Hermes con el navegador instalado. Se requiere certificar ese módulo antes de continuar.")


def browser_environment(engine, base_path: str):
    if not engine.state.get("extras", {}).get("browser") or engine.state.get("mode") != "managed":
        return {}
    target_root = Path(engine.state.get("releaseRoot", str(engine.root)))
    canonical, root, manifest, source = _installed_browser(engine)
    target = source if Path(canonical["releaseRoot"]) == target_root else verify_runtime(target_root)
    _require_compatible_hermes(source, target)
    return {"PATH": os.pathsep.join([str(root / "bin"), str(root / "node/bin"), base_path]),
            "AGENT_BROWSER_EXECUTABLE_PATH": str(root / manifest["entrypoints"]["chromium"])}
