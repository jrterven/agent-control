"""Durable macOS app replacement transactions, coordinated by the signed helper.

No method stops another service or replaces an app. The native helper owns
SMAppService and atomic app renames; these hooks own drain, idle verification,
data snapshots, configuration, and readiness. A crash leaves a transaction for
explicit recovery, never an automatic retry of user work.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import time

from .managed_manifest import verify_runtime, verify_signature
from .manage import drain, management_lock
from .storage import atomic_json, private_dir, read_json

TRANSACTION = "mac-update.json"
MAX_AGE = 30 * 60


def app_path() -> Path:
    return Path.home() / "Applications/Agent Control.app"


def _stopped(engine):
    """A kernel-held lock is authoritative; PID files or old status are not."""
    for path in (engine.directory / "supervisor.lock", engine.connector_dir / "runtime.lock"):
        if not path.exists():
            continue
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "w") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ValueError("El servicio aún está cerrando. Espera antes de continuar; no se han reemplazado datos.") from None


def _ledger_idle(engine):
    path = engine.connector_dir / "operations.sqlite3"
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("El registro de operaciones no es seguro. No se actualizará.")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            pending = db.execute("SELECT count(*) FROM operations WHERE state IN ('running','unknown')").fetchone()[0]
    except sqlite3.Error:
        raise ValueError("No se pudo comprobar el registro de operaciones. No se actualizará.") from None
    if pending:
        raise ValueError("Hay operaciones activas o de resultado incierto. Resuélvelas antes de actualizar.")


def _owned_home(engine) -> Path:
    if engine.state.get("mode") == "existing":
        return None
    expected = engine.directory / "hermes-home"
    actual = Path(engine.state.get("hermesHome", ""))
    if (engine.state.get("mode") != "managed" or actual != expected or actual.is_symlink()
            or not actual.is_dir() or actual.stat().st_uid != os.getuid()):
        raise ValueError("La actualización solo administra la instalación de Hermes creada por Agent Control.")
    return actual


def _read_transaction(engine, nonce: str) -> dict:
    path = engine.directory / TRANSACTION
    if not path.exists():
        raise ValueError("No hay una actualización pendiente.")
    tx = read_json(path)
    if not isinstance(nonce, str) or not secrets.compare_digest(tx.get("transactionId", ""), nonce):
        raise ValueError("La operación no pertenece a esta actualización.")
    return tx


def _clear_owned_marker(engine, tx):
    marker = engine.connector_dir / "maintenance.request"
    if marker.exists():
        if marker.is_symlink() or marker.read_text() != tx.get("maintenanceId"):
            raise ValueError("Otra operación tomó el control de mantenimiento; no se modificará.")
        marker.unlink()
    stop = engine.directory / "stop.request"
    if stop.exists():
        value = read_json(stop)
        if value.get("transactionId") != tx["transactionId"]:
            raise ValueError("Otra operación solicita detener el servicio; se preservó su estado.")
        stop.unlink()


def _offer(engine, method):
    transaction = engine.directory / TRANSACTION
    if transaction.exists():
        tx = read_json(transaction)
        return {"actionRequired": True, "recoveryRequired": True, "transactionId": tx["transactionId"],
                "phase": tx["phase"], "expired": time.time() - tx["createdAt"] > MAX_AGE,
                "message": "Hay una actualización interrumpida. Recupera la versión anterior antes de continuar."}
    _owned_home(engine)
    if method == "rollback":
        previous = engine.directory / "previous-app.json"
        if not previous.exists():
            raise ValueError("No hay una versión anterior disponible.")
        value = read_json(previous)
        path = engine.directory / "app-versions" / value["revision"] / "Agent Control.app"
        manifest = verify_runtime(path / "Contents/Resources/runtime", expected_release=value["revision"])
        return {"actionRequired": True, "appPath": str(path), "revision": value["revision"], "method": method,
                "dataSchemaVersion": manifest.get("dataSchemaVersion", 1)}
    from .setup_service import fetch
    with tempfile.TemporaryDirectory(dir=engine.directory, prefix="mac-offer-") as temporary:
        temporary = Path(temporary)
        fetch(engine.server + "/downloads/agent-control/latest.json", temporary / "latest.json", 65536)
        fetch(engine.server + "/downloads/agent-control/latest.json.sig", temporary / "latest.json.sig", 8192)
        verify_signature(temporary / "latest.json", temporary / "latest.json.sig")
        latest = json.loads((temporary / "latest.json").read_text())
        revision = latest.get("version", "")
        if not re.fullmatch(r"[a-f0-9]{40}", revision):
            raise ValueError("La actualización publicada no tiene una revisión válida.")
        old = verify_runtime(Path(engine.state["releaseRoot"]))
        if revision == old["release"]:
            return {"status": "current", "message": "Ya tienes la versión actual."}
        name = f"Agent-Control-{revision}-macos-arm64.dmg"
        base = engine.server + "/downloads/agent-control/releases/" + revision + "/"
        fetch(base + "SHA256SUMS", temporary / "SHA256SUMS", 65536)
        fetch(base + "SHA256SUMS.sig", temporary / "SHA256SUMS.sig", 8192)
        verify_signature(temporary / "SHA256SUMS", temporary / "SHA256SUMS.sig")
        hashes = [line.split()[0] for line in (temporary / "SHA256SUMS").read_text().splitlines()
                  if len(line.split()) == 2 and line.split()[1] == name]
        if len(hashes) != 1 or not re.fullmatch("[a-f0-9]{64}", hashes[0]):
            raise ValueError("La actualización no incluye un DMG verificado para este Mac.")
        return {"actionRequired": True, "downloadUrl": base + name, "sha256": hashes[0], "revision": revision, "method": method}


def lifecycle(engine, method: str, params: dict) -> dict:
    if method not in {"update", "rollback"}:
        raise ValueError("Operación de aplicación no compatible.")
    phase = params.get("phase", "offer")
    with management_lock(engine.directory):
        if phase == "offer":
            return _offer(engine, method)
        if phase == "prepare":
            if (engine.directory / TRANSACTION).exists():
                raise ValueError("Recupera primero la actualización pendiente.")
            home = _owned_home(engine)
            if (engine.directory / "stop.request").exists() or (engine.connector_dir / "maintenance.request").exists():
                raise ValueError("Hay otra operación de mantenimiento pendiente. No se reiniciará.")
            target = Path(params.get("targetRoot", ""))
            target_manifest = verify_runtime(target, expected_release=params.get("revision"))
            from .setup_extras import validate_extra_transition
            preserved_extras = validate_extra_transition(engine, target)
            if preserved_extras:
                # Browser extras are not published for macOS yet. An extra's
                # trust root cannot stay at the canonical app path across a
                # swap: retain the current app and refuse before draining.
                raise ValueError("Esta instalación tiene una función opcional vinculada a su versión actual. Conserva esta versión; la actualización de funciones opcionales en Mac todavía no está disponible.")
            original_manifest = verify_runtime(Path(engine.state["releaseRoot"]))
            if target_manifest.get("dataSchemaVersion", 1) != original_manifest.get("dataSchemaVersion", 1):
                raise ValueError("Esta actualización cambia el formato de datos. Conserva la versión actual.")
            paired = (engine.connector_dir / "config.json").exists()
            maintenance_id = None
            try:
                if paired:
                    drain(engine.connector_dir)
                    maintenance_id = (engine.connector_dir / "maintenance.request").read_text()
                if not engine.status().get("localReady"):
                    raise ValueError("No se puede comprobar el estado de Hermes. No se reiniciará.")
                from .setup_service import all_profiles_idle
                asyncio.run(all_profiles_idle(engine))
                _ledger_idle(engine)
                from .updates import check_intent
                check_intent(engine.connector_dir, params.get("expectedControl"))
                tx = {"transactionId": secrets.token_urlsafe(32), "method": method, "createdAt": time.time(),
                      "phase": "prepared", "maintenanceId": maintenance_id, "oldState": dict(engine.state),
                      "oldConfig": read_json(engine.connector_dir / "config.json") if paired else None,
                      "oldRelease": original_manifest["release"], "targetRelease": target_manifest["release"],
                      "targetRoot": str(target),
                      "preservedExtras": preserved_extras,
                      "targetManifest": {"hermesSourceSha": target_manifest["hermesSourceSha"], "hermesVersion": target_manifest["hermesVersion"],
                                         "dataSchemaVersion": target_manifest.get("dataSchemaVersion", 1)},
                      "ownedHome": str(home)}
                atomic_json(engine.directory / TRANSACTION, tx)
                return {"transactionId": tx["transactionId"], "phase": "prepared", "oldRelease": tx["oldRelease"]}
            except Exception:
                if maintenance_id is not None:
                    marker = engine.connector_dir / "maintenance.request"
                    if marker.exists() and not marker.is_symlink() and marker.read_text() == maintenance_id:
                        marker.unlink()
                raise
        tx = _read_transaction(engine, params.get("transactionId"))
        if phase == "inspect":
            return {"transactionId": tx["transactionId"], "phase": tx["phase"], "oldRelease": tx["oldRelease"],
                    "targetRelease": tx["targetRelease"], "targetRoot": tx["targetRoot"], "expired": time.time() - tx["createdAt"] > MAX_AGE}
        if phase == "recovery-check":
            _ledger_idle(engine)
            if engine.status().get("localReady"):
                from .setup_service import all_profiles_idle
                asyncio.run(all_profiles_idle(engine))
            else:
                _stopped(engine)
            return {"safeToStop": True}
        if (time.time() - tx["createdAt"] > MAX_AGE and phase not in {"abort", "resume"}
                and not (phase == "complete" and tx["phase"] == "complete")):
            raise ValueError("La actualización interrumpida caducó. Recupera la versión anterior; no se reiniciará automáticamente.")
        if phase == "stopped":
            if tx["phase"] != "prepared":
                raise ValueError("La actualización no está en la fase de copia de seguridad.")
            _stopped(engine)
            _ledger_idle(engine)
            backup = engine.directory / "backups" / ("mac-" + tx["transactionId"])
            private_dir(backup.parent)
            if backup.exists():
                raise ValueError("Ya existe una copia pendiente. Recupera la actualización anterior.")
            # Preserve symlinks themselves; never copy data outside our home.
            if _owned_home(engine) is not None:
                shutil.copytree(_owned_home(engine), backup, symlinks=True)
            else:
                private_dir(backup)
                atomic_json(backup / "setup.json", tx["oldState"])
            tx.update(phase="stopped", backup=str(backup))
            atomic_json(engine.directory / TRANSACTION, tx)
            return {"phase": "stopped", "backupCreated": True}
        if phase == "activate":
            if tx["phase"] != "stopped":
                raise ValueError("Falta la copia de seguridad previa al cambio.")
            _stopped(engine)
            canonical = app_path() / "Contents/Resources/runtime"
            manifest = verify_runtime(canonical, expected_release=tx["targetRelease"])
            if any(manifest.get(k) != v for k, v in tx["targetManifest"].items()):
                raise ValueError("El runtime instalado cambió después de prepararlo.")
            engine.state = {**tx["oldState"], "releaseRoot": str(canonical), "extras": tx.get("preservedExtras", {})}
            if engine.state["mode"] == "managed":
                engine.state.update(hermesSource=str(canonical / "hermes"), sourceSha=manifest["hermesSourceSha"], hermesVersion=manifest["hermesVersion"])
            engine.save()
            if tx["oldConfig"] and engine.state["mode"] == "managed":
                atomic_json(engine.connector_dir / "config.json", {**tx["oldConfig"],
                            "hermesSource": str(canonical / "hermes"), "sourceSha": manifest["hermesSourceSha"]})
            tx["phase"] = "activated"
            atomic_json(engine.directory / TRANSACTION, tx)
            return {"phase": "activated"}
        if phase == "complete":
            if tx["phase"] not in {"activated", "complete"}:
                raise ValueError("La actualización aún no está activada.")
            verify_runtime(app_path() / "Contents/Resources/runtime", expected_release=tx["targetRelease"])
            # Keep drain in force while testing fresh service readiness.
            from .setup_service import ensure_service
            ensure_service(engine)
            _clear_owned_marker(engine, tx)
            atomic_json(engine.directory / "previous-app.json", {"revision": tx["oldRelease"], "dataSchemaVersion": tx["targetManifest"]["dataSchemaVersion"]})
            tx["phase"] = "complete"
            atomic_json(engine.directory / TRANSACTION, tx)
            (engine.directory / TRANSACTION).unlink()
            return {**engine.status(), "status": "complete"}
        if phase == "abort":
            _stopped(engine)
            old_manifest = verify_runtime(app_path() / "Contents/Resources/runtime", expected_release=tx["oldRelease"])
            if old_manifest.get("dataSchemaVersion", 1) != tx["targetManifest"]["dataSchemaVersion"]:
                raise ValueError("No es seguro restaurar esta versión por su formato de datos.")
            engine.state = tx["oldState"]
            engine.save()
            if tx["oldConfig"]:
                atomic_json(engine.connector_dir / "config.json", tx["oldConfig"])
            # Preserve the ledger and current history. Both versions have the
            # same data schema; a backup is for manual recovery, never replay.
            tx["phase"] = "aborted"
            atomic_json(engine.directory / TRANSACTION, tx)
            return {"phase": "aborted"}
        if phase == "resume":
            if tx["phase"] not in {"prepared", "aborted"}:
                raise ValueError("Restaura primero la aplicación anterior antes de reanudar.")
            from .setup_service import ensure_service
            ensure_service(engine)
            _clear_owned_marker(engine, tx)
            (engine.directory / TRANSACTION).unlink()
            return {**engine.status(), "status": "restored"}
        raise ValueError("Fase de actualización desconocida.")
