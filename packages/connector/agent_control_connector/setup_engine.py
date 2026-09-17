"""Resumable, local-only setup shared by the native Mac app and Linux wizard."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time
import warnings
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx
from hermes_client import HermesGatewayProvider, ProviderConnection
from hermes_client.compatibility import AUDITED_REVISIONS

from . import __version__
from .cli import cloud_url, data_directory, detect_revision, hermes_token, local_endpoint, status as connector_status
from .managed_manifest import verify_runtime
from .storage import SecretStore, atomic_json, private_dir, read_json
from .setup_providers import OpenRouterFlow, PROVIDERS, validate_key


def managed_directory(value: str | None = None) -> Path:
    if value:
        return Path(value).expanduser().absolute()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Agent Control/managed"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "agent-control"


class SetupEngine:
    def __init__(self, root: Path, directory: Path, server: str, connector_dir: Path | None = None):
        self.root, self.directory = root.resolve(), directory
        self.connector_dir = connector_dir or data_directory(None)
        self.server = cloud_url(server)
        self.flows: dict = {}
        private_dir(directory)
        self.state = read_json(directory / "setup.json") if (directory / "setup.json").exists() else {}
        if self.state.get("server") and self.state["server"] != self.server:
            raise ValueError("Esta instalación pertenece a otro servidor de Agent Control.")

    def save(self):
        atomic_json(self.directory / "setup.json", self.state)

    def token(self):
        return SecretStore(self.directory / "credentials").load()["hermesToken"]

    def local(self, method: str, path: str, **kwargs):
        if not self.state.get("restUrl"):
            raise ValueError("Prepara Hermes antes de configurar el proveedor.")
        with httpx.Client(base_url=local_endpoint(self.state["restUrl"]), trust_env=False,
                          follow_redirects=False, timeout=45,
                          headers={"X-Hermes-Session-Token": self.token()}) as client:
            response = client.request(method, path, **kwargs)
        if response.status_code != 200:
            raise ValueError(f"Hermes no completó la operación (HTTP {response.status_code}). Abre Diagnóstico y vuelve a intentar.")
        return response.json()

    def cloud(self, path: str, body: dict):
        from .tls import cloud_ssl_context
        with httpx.Client(base_url=self.server, trust_env=False, timeout=25,
                          follow_redirects=False, verify=cloud_ssl_context()) as client:
            return client.post(path, json=body)

    def status(self, **_):
        paired = (self.connector_dir / "config.json").exists()
        snapshot = connector_status(self.connector_dir)
        local_ready = False
        existing = read_json(self.connector_dir / "config.json") if paired else {}
        if paired and not self.state:
            local_ready = bool(snapshot.get("fresh") and snapshot.get("activeWork") is not None)
        if self.state.get("restUrl"):
            try:
                self.local("GET", "/api/profiles")
                local_ready = True
            except (ValueError, OSError, httpx.HTTPError):
                pass
        return {"mode": self.state.get("mode", "existing" if paired else None), "installed": bool(self.state) or paired, "paired": paired,
                "alreadyPaired": paired, "localReady": local_ready,
                "ready": bool(paired and local_ready and snapshot.get("fresh") and snapshot.get("connected")),
                "connected": bool(snapshot.get("fresh") and snapshot.get("connected")),
                "activeWork": snapshot.get("activeWork"), "profiles": self.state.get("profiles", existing.get("profiles", [])),
                "needsProvider": self.state.get("mode") == "managed" and not self.state.get("providerReady"),
                "hermesVersion": self.state.get("hermesVersion"), "server": self.server,
                "recoveryRequired": (self.directory / "linux-update.json").exists() or (self.directory / "mac-update.json").exists(),
                "status": "connected" if snapshot.get("connected") else "setup"}

    def detect(self, **_):
        result = self.status()
        candidates = []
        home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
        try:
            revision, source = detect_revision(home, None)
            candidates.append({"hermesHome": str(home), "hermesSource": str(source),
                               "hermesVersion": AUDITED_REVISIONS[revision][0]})
        except (ValueError, OSError):
            pass
        result["existing"] = candidates
        return result

    def install(self, mode="managed", hermesSource=None, hermesHome=None, restUrl="http://127.0.0.1:9119", token=None, **_):
        if (self.directory / "linux-update.json").exists() or (self.directory / "mac-update.json").exists():
            raise ValueError("Hay una actualización interrumpida. Usa Recuperar versión anterior antes de reinstalar.")
        if (self.connector_dir / "config.json").exists():
            return self.status()
        if self.state:
            if mode != self.state["mode"]:
                raise ValueError("Hay una instalación pendiente. Retómala o desinstálala antes de cambiar de modo.")
            if sys.platform != "darwin":
                self.install_service()
            return {**self.status(), "serviceRequired": sys.platform == "darwin"}
        if mode not in {"existing", "managed"}:
            raise ValueError("Selecciona instalar Hermes o conectar el existente.")
        if mode == "managed":
            manifest = verify_runtime(self.root)
            source = self.root / "hermes"
            home = self.directory / "hermes-home"
            private_dir(home)
            # JSON is valid YAML, allowing a dependency-free setup launcher.
            atomic_json(home / "config.yaml", {"security": {"allow_lazy_installs": False},
                "terminal": {"cwd": str(home / "workspace")},
                "tools": {"enabled_toolsets": ["terminal", "file", "cronjob"]},
                "platform_toolsets": {"cli": ["terminal", "file", "cronjob"]}})
            atomic_json(home / "profile.yaml", {"display_name": "Mi asistente"})
            private_dir(home / "workspace")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            restUrl = f"http://127.0.0.1:{port}"
            revision = manifest["hermesSourceSha"]
            token = secrets.token_urlsafe(48)
        else:
            home = Path(hermesHome or "~/.hermes").expanduser().resolve()
            revision, source = detect_revision(home, hermesSource)
            restUrl = local_endpoint(restUrl)
            if not token:
                # GUI cannot read the terminal; pass a token or autodetect known files.
                token = hermes_token(SimpleNamespace(token_file=None, no_prompt=sys.platform == "darwin"), home)
            if not isinstance(token, str) or not 32 <= len(token) <= 512:
                raise ValueError("Introduce el token del Hermes existente.")
        SecretStore(self.directory / "credentials").save({"hermesToken": token})
        self.state = {"schemaVersion": 1, "mode": mode, "server": self.server,
            "hermesHome": str(home), "hermesSource": str(source), "sourceSha": revision,
            "hermesVersion": AUDITED_REVISIONS[revision][0], "restUrl": restUrl,
            "releaseRoot": str(self.root), "providerReady": mode == "existing"}
        self.save()
        if sys.platform != "darwin":
            self.install_service()
        return {**self.status(), "serviceRequired": sys.platform == "darwin"}

    def configure_provider(self, provider, apiKey=None, model=None, **_):
        if self.state.get("mode") != "managed":
            raise ValueError("Configura los proveedores de una instalación existente desde Hermes.")
        provider = "openai-codex" if provider == "chatgpt" else provider
        if provider not in {*PROVIDERS, "openai-codex"}:
            raise ValueError("Proveedor no compatible.")
        if apiKey is not None:
            validate_key(provider, apiKey)
            self.local("PUT", "/api/env", json={"key": PROVIDERS[provider][0], "value": apiKey})
        payload = self.local("GET", "/api/model/options", params={"refresh": "true"})
        rows = [row for row in payload.get("providers", []) if row.get("slug") == provider]
        models = [m for row in rows for m in row.get("models", []) if isinstance(m, str)]
        recommended = self.local("GET", "/api/model/recommended-default", params={"provider": provider}).get("model")
        if not models:
            raise ValueError("Hermes no encontró modelos disponibles. Revisa tu cuenta o clave del proveedor.")
        if model:
            if model not in models:
                raise ValueError("Selecciona un modelo del catálogo disponible.")
            self.local("POST", "/api/model/set", json={"scope": "main", "provider": provider, "model": model})
            self.state.update(providerReady=True, provider=provider, model=model)
            self.save()
        return {"provider": provider, "models": models, "recommendedModel": recommended if recommended in models else None,
                "providerReady": self.state.get("providerReady", False)}

    def oauth_start(self, provider, **_):
        if self.state.get("mode") != "managed":
            raise ValueError("La autorización guiada es para la instalación administrada.")
        flow_id = secrets.token_urlsafe(24)
        if provider == "openrouter":
            flow = OpenRouterFlow()
            self.flows[flow_id] = (provider, flow)
            return {"flowId": flow_id, "authorizationUrl": flow.url, "expiresIn": 600}
        if provider not in {"chatgpt", "openai-codex"}:
            raise ValueError("Este proveedor utiliza una API key.")
        response = self.local("POST", "/api/providers/oauth/openai-codex/start")
        self.flows[flow_id] = ("openai-codex", response["session_id"])
        return {"flowId": flow_id, "authorizationUrl": response.get("verification_url"),
                "userCode": response.get("user_code"), "expiresIn": response.get("expires_in", 900)}

    def oauth_poll(self, flowId, **_):
        if flowId not in self.flows:
            raise ValueError("Inicia una nueva autorización.")
        provider, flow = self.flows[flowId]
        if provider == "openrouter":
            key = flow.poll()
            if key is None:
                return {"status": "pending"}
            self.flows.pop(flowId)
            return {"status": "complete", **self.configure_provider(provider, apiKey=key)}
        response = self.local("GET", f"/api/providers/oauth/openai-codex/poll/{flow}")
        if response.get("status") == "approved":
            self.flows.pop(flowId)
            return {"status": "complete", **self.configure_provider(provider)}
        if response.get("status") not in {"pending", "starting"}:
            self.flows.pop(flowId)
            raise ValueError("La autorización fue cancelada, rechazada o caducó. Iníciala de nuevo.")
        return {"status": "pending"}

    def oauth_cancel(self, flowId, **_):
        value = self.flows.pop(flowId, None)
        if value:
            provider, flow = value
            if provider == "openrouter":
                flow.close()
            else:
                self.local("DELETE", f"/api/providers/oauth/sessions/{flow}")
        return {"status": "cancelled"}

    async def profiles(self):
        provider = HermesGatewayProvider(ProviderConnection(gateway_id="setup", profile_name="default",
            rest_url=self.state["restUrl"], ws_url=self.state["restUrl"].replace("http", "ws", 1) + "/api/ws",
            dashboard_token=self.token(), trusted_source_sha=self.state["sourceSha"]))
        try:
            capabilities = await provider.capabilities()
            if capabilities.version != self.state["hermesVersion"]:
                raise ValueError("La versión de Hermes no coincide con la instalación verificada.")
            return [p.name for p in await provider.list_profiles()]
        finally:
            await provider.close()

    def pair_start(self, **_):
        if (self.connector_dir / "config.json").exists():
            return {**self.status(), "status": "complete"}
        if not self.state.get("providerReady"):
            raise ValueError("Selecciona y confirma un modelo antes de vincular el equipo.")
        profiles = asyncio.run(self.profiles())
        self.state["profiles"] = profiles
        pending = self.directory / "pairing.json"
        if pending.exists():
            flow = read_json(pending)
            if flow.get("expiresAt", 0) > time.time():
                return self.pair_view(flow)
        result = self.cloud("/api/v1/connectors/device/authorize", {"name": socket.gethostname()[:120],
            "profiles": profiles, "version": __version__, "sourceSha": self.state["sourceSha"],
            "installationKind": self.state["mode"], "hermesVersion": self.state["hermesVersion"]})
        if result.status_code != 200:
            raise ValueError(f"No se pudo iniciar la vinculación (HTTP {result.status_code}).")
        flow = result.json()
        flow.update(flowId=secrets.token_urlsafe(24), expiresAt=time.time() + flow["expiresIn"], profiles=profiles)
        atomic_json(pending, flow)
        self.save()
        return self.pair_view(flow)

    def pair_view(self, flow):
        return {"flowId": flow["flowId"], "userCode": flow["userCode"],
                "verificationUrl": self.server + "/connect?" + urlencode({"code": flow["userCode"]}),
                "expiresIn": max(0, int(flow["expiresAt"] - time.time())), "status": "pending"}

    def pair_poll(self, flowId=None, **_):
        if (self.connector_dir / "config.json").exists():
            return {**self.status(), "status": "complete"}
        flow = read_json(self.directory / "pairing.json")
        if flowId != flow["flowId"] or flow["expiresAt"] < time.time():
            raise ValueError("El código caducó. Genera uno nuevo.")
        result = self.cloud("/api/v1/connectors/device/token", {"deviceCode": flow["deviceCode"]})
        if result.status_code == 428:
            return {"status": "pending"}
        if result.status_code != 200:
            (self.directory / "pairing.json").unlink(missing_ok=True)
            raise ValueError("La vinculación caducó o fue rechazada. Genera un nuevo código.")
        value = result.json()
        if not value.get("profiles") or not set(value["profiles"]) <= set(flow["profiles"]):
            raise ValueError("La selección de agentes no coincide con la confirmada localmente.")
        private_dir(self.connector_dir)
        SecretStore(self.connector_dir).save({"accessToken": value["accessToken"], "hermesToken": self.token()})
        atomic_json(self.connector_dir / "config.json", {"server": self.server, "connectorId": value["connectorId"],
            "gatewayId": value["gatewayId"], "profiles": value["profiles"], "restUrl": self.state["restUrl"],
            "wsUrl": self.state["restUrl"].replace("http", "ws", 1) + "/api/ws",
            "hermesHome": self.state["hermesHome"], "hermesSource": self.state["hermesSource"],
            "sourceSha": self.state["sourceSha"], "installationKind": self.state["mode"]})
        (self.directory / "pairing.json").unlink(missing_ok=True)
        self.state["profiles"] = value["profiles"]
        self.save()
        return {**self.status(), "status": "complete"}

    def install_service(self, **_):
        from .setup_service import ensure_service, install_linux_service
        if sys.platform != "darwin":
            install_linux_service(self)
        ensure_service(self)
        return self.status()

    def diagnose(self, **_):
        return {**self.status(), "releaseVerified": bool(verify_runtime(self.root)),
                "message": "El diagnóstico no incluye conversaciones ni credenciales."}

    def dispatch(self, method, params):
        methods = {"status": self.status, "detect": self.detect, "install": self.install,
            "configure-provider": self.configure_provider, "oauth-start": self.oauth_start,
            "oauth-poll": self.oauth_poll, "oauth-cancel": self.oauth_cancel,
            "pair-start": self.pair_start, "pair-poll": self.pair_poll,
            "install-service": self.install_service, "diagnose": self.diagnose}
        if method in methods:
            return methods[method](**params)
        if method in {"update", "rollback", "uninstall", "restart", "extras-list", "extras-install"}:
            from .setup_service import lifecycle
            return lifecycle(self, method, params)
        raise ValueError("Operación de instalación desconocida.")

    def close(self):
        for flow_id in list(self.flows):
            try:
                self.oauth_cancel(flow_id)
            except Exception:
                pass


def rpc(engine, source=sys.stdin, output=sys.stdout):
    for line in source:
        request_id = None
        try:
            if len(line) > 32768:
                raise ValueError("Solicitud demasiado grande.")
            request = json.loads(line)
            request_id = request.get("id")
            if not isinstance(request.get("params", {}), dict):
                raise ValueError("Parámetros no válidos.")
            result = engine.dispatch(request.get("method"), request.get("params", {}))
            response = {"id": request_id, "result": result}
        except ValueError as exc:
            response = {"id": request_id, "error": {"code": "setup_failed", "message": str(exc)}}
        except Exception:
            # Never serialize provider HTTP payloads, subprocess output or secrets.
            response = {"id": request_id, "error": {"code": "setup_unavailable",
                        "message": "No se pudo completar este paso. Comprueba la conexión y abre Diagnóstico."}}
        print(json.dumps(response, ensure_ascii=False), file=output, flush=True)


def wizard(engine):
    # curl | sh consumes stdin; all human interaction uses the controlling TTY.
    with open("/dev/tty", "r") as input_terminal, open("/dev/tty", "w") as terminal:
        def ask(message):
            terminal.write(message + " ")
            terminal.flush()
            return input_terminal.readline().strip()
        def say(message):
            terminal.write(message + "\n")
            terminal.flush()
        found = engine.detect()
        if found["alreadyPaired"]:
            say("Este equipo ya está vinculado. Abre " + engine.server + "/computers")
            return
        say("Agent Control: Google identifica tu cuenta; tu proveedor de IA aplica sus propios cobros y límites.")
        mode, candidate = "managed", {}
        if found["existing"] and ask("Encontré Hermes. ¿Conectar el existente? [S/n]").lower() != "n":
            mode, candidate = "existing", found["existing"][0]
        engine.install(mode=mode, **candidate)
        if mode == "managed" and not engine.state.get("providerReady"):
            choices = ["chatgpt", "openrouter", "openai", "anthropic", "gemini"]
            say("Proveedor: 1 ChatGPT · 2 OpenRouter · 3 OpenAI API · 4 Anthropic API · 5 Gemini API")
            selected = ask("Selecciona [1–5]:")
            if selected not in {"1", "2", "3", "4", "5"}:
                raise ValueError("Selección no válida. Ejecuta de nuevo el instalador para retomar.")
            provider = choices[int(selected) - 1]
            if provider == "chatgpt":
                flow = engine.oauth_start(provider)
                say(str(flow.get("authorizationUrl")) + "\nCódigo: " + str(flow.get("userCode")))
                while True:
                    time.sleep(5)
                    result = engine.oauth_poll(flow["flowId"])
                    if result["status"] == "complete":
                        break
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    key = getpass.getpass("API key (oculta): ", stream=terminal)
                result = engine.configure_provider(provider, apiKey=key)
            recommended = result["recommendedModel"]
            selected = ask(f"Modelo recomendado: {recommended or 'elige uno'}. Pulsa Enter para usarlo o ? para ver modelos:")
            if selected == "?" or (not selected and not recommended):
                for number, model in enumerate(result["models"], 1):
                    say(f"{number}. {model}")
                selected = ask("Selecciona el número del modelo:")
                selected = result["models"][int(selected) - 1] if selected.isdigit() and 1 <= int(selected) <= len(result["models"]) else None
            elif not selected:
                selected = recommended
            if selected not in result["models"]:
                raise ValueError("Selección no válida. Vuelve a ejecutar el instalador para retomar.")
            engine.configure_provider(provider, model=selected)
        flow = engine.pair_start()
        say(flow["verificationUrl"] + "\nCódigo: " + flow["userCode"])
        while engine.pair_poll(flow["flowId"])["status"] != "complete":
            time.sleep(5)
        engine.install_service()
        say("Equipo conectado. Abre " + engine.server + "/chats")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Agent Control: instalación guiada de Hermes")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rpc", action="store_true")
    mode.add_argument("--wizard", action="store_true")
    mode.add_argument("--service", action="store_true")
    for command in ("update", "rollback", "uninstall", "restart", "diagnose", "extras", "resume"):
        mode.add_argument("--" + command, action="store_true")
    mode.add_argument("--install-extra", choices=["browser"])
    parser.add_argument("--release-root", type=Path, default=os.environ.get("AGENT_CONTROL_RELEASE_ROOT"),
                        required=not bool(os.environ.get("AGENT_CONTROL_RELEASE_ROOT")))
    parser.add_argument("--data-dir")
    parser.add_argument("--server", default="https://agentcontrol.jemailabs.com")
    args = parser.parse_args(argv)
    os.umask(0o077)
    engine = SetupEngine(args.release_root, managed_directory(args.data_dir), args.server)
    try:
        if args.service:
            from .setup_service import supervise
            supervise(engine)
        elif args.rpc:
            rpc(engine)
        elif args.wizard:
            wizard(engine)
        elif args.install_extra:
            print(json.dumps(engine.dispatch("extras-install", {"id": args.install_extra}), ensure_ascii=False))
        else:
            command = next(name for name in ("update", "rollback", "uninstall", "restart", "diagnose", "extras", "resume") if getattr(args, name))
            print(json.dumps(engine.dispatch({"extras": "extras-list", "resume": "install-service"}.get(command, command), {}), ensure_ascii=False))
    except (ValueError, OSError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else "No se pudo acceder a la instalación local.", file=sys.stderr)
        return 1
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
