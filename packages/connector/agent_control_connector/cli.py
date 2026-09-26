from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import warnings
from urllib.parse import urlsplit

import httpx
from hermes_client import HermesGatewayProvider, ProviderConnection
from hermes_client.compatibility import AUDITED_REVISIONS
from . import __version__
from .runtime import ConnectorRuntime
from .storage import SecretStore, atomic_json, private_dir, read_json


def data_directory(value: str | None) -> Path:
    return Path(value or os.environ.get("AGENT_CONTROL_CONNECTOR_HOME", "~/.agent-control-connector")).expanduser().absolute()


def local_endpoint(value: str, *, websocket=False) -> str:
    parsed = urlsplit(value)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if not loopback or parsed.scheme not in ({"ws", "wss"} if websocket else {"http", "https"}) or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Hermes endpoint must use a numeric loopback address without credentials or query parameters")
    return value.rstrip("/")


def cloud_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("The Agent Control server must be an HTTPS origin")
    return value.rstrip("/")


def detect_revision(hermes_home: Path, source: str | None) -> tuple[str, Path]:
    candidates = [Path(source).expanduser()] if source else [hermes_home / "hermes-agent", Path.home() / "hermes-agent"]
    binary = shutil.which("hermes")
    if binary and not source:
        resolved = Path(binary).resolve()
        candidates.extend([resolved.parent.parent, resolved.parent])
    for candidate in candidates:
        if candidate.name == "hermes" and (candidate.parent / "runtime-manifest.json").exists():
            from .managed_manifest import verify_runtime
            manifest = verify_runtime(candidate.parent)
            return manifest["hermesSourceSha"], candidate.resolve()
        try:
            result = subprocess.run(["git", "-C", str(candidate), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
            if result.returncode or result.stdout.strip() not in AUDITED_REVISIONS:
                continue
            dirty = subprocess.run(["git", "-C", str(candidate), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=5)
            if dirty.returncode or dirty.stdout.strip():
                raise ValueError("The installed Hermes checkout has tracked changes; restore a supported revision before connecting")
            return result.stdout.strip(), candidate.resolve()
        except OSError:
            continue
    raise ValueError("A supported clean Hermes checkout was not found. Use --hermes-source PATH to select the installed checkout")


def hermes_token(args, hermes_home: Path) -> str:
    token = os.environ.get("HERMES_DASHBOARD_SESSION_TOKEN") or os.environ.get("HERMES_CONTROL_HERMES_DASHBOARD_TOKEN")
    if not token and args.token_file:
        path = Path(args.token_file).expanduser()
        token = read_token_file(path, maximum=1024, permissions=0o077).strip()
    if not token and sys.platform == "darwin":
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
                                 "-s", "com.agent-control.hermes-dashboard", "-w"], capture_output=True, text=True)
        if result.returncode == 0:
            token = result.stdout.strip()
    if not token:
        candidates = [
            (hermes_home / "control-services" / "hermes-serve.env", 0o077),
            (Path.home() / ".config" / "hermes-control-preview" / "hermes-serve.env", 0o077),
            (hermes_home / ".env", 0o007),
            (Path.home() / ".config" / "hermes" / "serve.env", 0o077),
            (Path("/etc/hermes/serve.env"), 0o007),
        ]
        for path, permissions in candidates:
            try:
                for line in read_token_file(path, maximum=64 * 1024, permissions=permissions).splitlines():
                    key, _, value = line.partition("=")
                    if key.strip() == "HERMES_DASHBOARD_SESSION_TOKEN":
                        token = value.strip().strip("\"'")
                        break
                if token:
                    break
            except (OSError, ValueError):
                continue
    if not token and not getattr(args, "no_prompt", False):
        try:
            # A buffered read/write stream seeks when changing direction; a TTY
            # cannot seek. getpass opens its own TTY for input, so this stream is
            # only for the prompt. Never fall back to echoed or piped stdin.
            with open("/dev/tty", "w") as terminal, warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                token = getpass.getpass("Hermes dashboard token (kept only on this computer): ", stream=terminal)
        except (OSError, EOFError, getpass.GetPassWarning):
            pass
    if not token or not re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", token):
        raise ValueError("A private Hermes dashboard token is required. Use the token configured for your running Hermes dashboard; the connector does not start or change Hermes. Retry from an interactive terminal, set HERMES_DASHBOARD_SESSION_TOKEN, or add --token-file PATH to the installer (a private file containing only the token).")
    return token


def read_token_file(path: Path, *, maximum: int, permissions: int) -> str:
    # Read only bounded regular files owned by this user or the administrator.
    # O_NONBLOCK prevents a replaced candidate FIFO from hanging setup.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {os.getuid(), 0}
            or metadata.st_mode & permissions or metadata.st_size > maximum):
            raise ValueError("The token file must be a private, size-limited regular file owned by this user or root")
        try:
            value = stream.read(maximum + 1)
        except UnicodeError:
            raise ValueError("The token file must contain UTF-8 text") from None
        if len(value.encode("utf-8")) > maximum:
            raise ValueError("The token file exceeds its size limit")
        return value


async def authorize_pairing(args, *, existing_config: dict | None = None, saved_token: str | None = None):
    """Approve an identity without changing the currently installed identity."""
    directory = data_directory(args.data_dir)
    private_dir(directory)
    if (directory / "config.json").exists() and existing_config is None:
        raise ValueError("This connector is already paired; run the guided installer again and choose reconnect to preserve your Hermes settings")
    server = cloud_url(args.server)
    hermes_home = Path(args.hermes_home or "~/.hermes").expanduser().resolve()
    revision, source = detect_revision(hermes_home, args.hermes_source)
    token = saved_token if saved_token is not None else hermes_token(args, hermes_home)
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{32,512}", token):
        raise ValueError("The saved Hermes dashboard token is invalid; provide a private --token-file")
    rest = local_endpoint(args.rest_url)
    ws = local_endpoint(args.ws_url or rest.replace("http", "ws", 1) + "/api/ws", websocket=True)
    provider = HermesGatewayProvider(ProviderConnection(gateway_id="pairing", profile_name="default", rest_url=rest,
                                      ws_url=ws, dashboard_token=token, trusted_source_sha=revision))
    try:
        capabilities = await provider.capabilities()
        if capabilities.version != AUDITED_REVISIONS[revision][0]:
            raise ValueError("The running Hermes version does not match its verified local source revision")
        available = [item.name for item in await provider.list_profiles()]
    finally:
        await provider.close()
    profiles = list(dict.fromkeys(args.profiles.split(","))) if args.profiles else available
    if not profiles or not set(profiles) <= set(available):
        raise ValueError("Choose profiles that are available in the local Hermes installation")
    async with httpx.AsyncClient(base_url=server, timeout=20, follow_redirects=False, trust_env=False) as client:
        result = await client.post("/api/v1/connectors/device/authorize", json={"name": args.name or socket.gethostname(),
            "profiles": profiles, "version": __version__, "sourceSha": revision})
        if result.status_code != 200:
            raise RuntimeError(f"Cloud pairing was rejected (HTTP {result.status_code})")
        authorization = result.json()
        print(f"Open {server}/connect?code={authorization['userCode']}\nConfirm code {authorization['userCode']} and select the profiles to share.", flush=True)
        for _ in range(120):
            await asyncio.sleep(5)
            response = await client.post("/api/v1/connectors/device/token", json={"deviceCode": authorization["deviceCode"]})
            if response.status_code == 428:
                continue
            if response.status_code != 200:
                raise RuntimeError(f"Pairing expired or was rejected (HTTP {response.status_code}); run connect again")
            paired = response.json()
            if not set(paired["profiles"]) <= set(profiles):
                raise ValueError("Cloud selected profiles outside the locally offered set")
            config = {**(existing_config or {}), "server": server, "connectorId": paired["connectorId"],
                "gatewayId": paired["gatewayId"], "profiles": paired["profiles"], "restUrl": rest, "wsUrl": ws,
                "hermesHome": str(hermes_home), "hermesSource": str(source), "sourceSha": revision}
            return config, {"accessToken": paired["accessToken"], "hermesToken": token}
    raise RuntimeError("Pairing code expired; run connect again")


async def pair(args):
    config, secrets = await authorize_pairing(args)
    directory = data_directory(args.data_dir)
    SecretStore(directory).save(secrets)
    atomic_json(directory / "config.json", config)
    print("Computer paired. Start and verify its connection with: agent-control-connector install-service", flush=True)


def status(directory):
    try:
        result = read_json(directory / "status.json")
        observed = datetime.fromisoformat(result["observedAt"])
        result["fresh"] = bool(result.get("fresh") and 0 <= (datetime.now(timezone.utc) - observed).total_seconds() < 45)
        if not result["fresh"]:
            result["activeWork"] = None
            result["connected"] = False
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {"activeWork": None, "fresh": False, "connected": False, "version": __version__}


def run(directory):
    private_dir(directory)
    # A second process must never reset the ledger while an existing process is dispatching.
    lock_path = directory / "runtime.lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("The connector is already running") from None
        config = read_json(directory / "config.json")
        cloud_url(config["server"])
        local_endpoint(config["restUrl"])
        local_endpoint(config["wsUrl"], websocket=True)
        revision, _ = detect_revision(Path(config["hermesHome"]), config["hermesSource"])
        if revision != config["sourceSha"]:
            raise ValueError("Hermes source changed since pairing. Verify compatibility and pair again")
        asyncio.run(ConnectorRuntime(directory, config, SecretStore(directory).load()).run())


def check_credentials(directory: Path) -> None:
    """Read the existing identity without starting a runtime or touching its state."""
    try:
        if directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.getuid():
            raise ValueError("Invalid connector directory")
        read_json(directory / "config.json")
        if sys.platform == "darwin":
            from .keychain import MacKeychain
            # SecretStore's constructor creates/chmods its directory. A foreground
            # permission check must not change a running connector's local state.
            service = "com.agent-control.connector." + hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:24]
            secrets = json.loads(MacKeychain().load(service, str(os.getuid())))
        else:
            secrets = read_json(directory / "secrets.json")
        if not isinstance(secrets, dict) or not all(
            isinstance(secrets.get(key), str) and 0 < len(secrets[key]) <= 4096
            for key in ("accessToken", "hermesToken")
        ):
            raise ValueError("Invalid connector credentials")
    except Exception:
        # Neither Keychain errors nor malformed stored values may expose secrets.
        raise RuntimeError("Cannot access existing connector credentials. On macOS, allow this connector to use its Keychain item, then try again.") from None
    print("Existing connector credentials are accessible.", flush=True)


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "update-worker":
        from .updates import main as update_main
        return update_main(values[1:])
    if values and values[0] in {"install-service", "update", "rollback", "uninstall"}:
        from .manage import management_main
        return management_main(values)
    parser = argparse.ArgumentParser(description="Connect a local Hermes installation to Agent Control")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    pairing = commands.add_parser("connect")
    pairing.add_argument("--server", required=True)
    pairing.add_argument("--name")
    pairing.add_argument("--hermes-home")
    pairing.add_argument("--hermes-source")
    pairing.add_argument("--token-file")
    pairing.add_argument("--rest-url", default="http://127.0.0.1:9119")
    pairing.add_argument("--ws-url")
    pairing.add_argument("--profiles")
    credential_check = commands.add_parser("check-credentials", help="Check access to existing credentials without starting or changing the connector")
    media_status = commands.add_parser("media-status", help="Inspect per-profile image publication activation")
    media_install = commands.add_parser("install-media", help="Install the managed image plugin after the connector confirms idle; does not restart Hermes")
    media_check = commands.add_parser("check-media", help="Verify bundled raster codecs and image plugin without accessing user data")
    background_check = commands.add_parser("check-background", help="Verify bundled native delegation guidance without accessing user data")
    background_status = commands.add_parser("background-status", help="Inspect managed native delegation activation")
    background_install = commands.add_parser("install-background", help="Install native delegation guidance after idle; never restarts Hermes")
    for command in (pairing, commands.add_parser("run"), commands.add_parser("status"), commands.add_parser("doctor"), credential_check, media_status, media_install, media_check, background_check, background_status, background_install):
        command.add_argument("--data-dir")
    args = parser.parse_args(values)
    directory = data_directory(args.data_dir)
    try:
        if args.command == "connect":
            asyncio.run(pair(args))
        elif args.command == "run":
            run(directory)
        elif args.command == "check-credentials":
            check_credentials(directory)
        elif args.command == "check-media":
            from .visual_media import media_self_test
            media_self_test()
            print("ok")
        elif args.command == "check-background":
            from .background_install import background_self_test
            background_self_test()
            from .chat_modes_install import plugin_source
            compile(plugin_source(), "hermes_chat_policy.py", "exec")
            print("ok")
        elif args.command in {"background-status", "install-background"}:
            from .background_install import background_profiles
            if args.command == "install-background":
                from .manage import drain, management_lock
                with management_lock(directory):
                    marker = directory / "maintenance.request"
                    if marker.exists() or marker.is_symlink():
                        raise ValueError("An existing maintenance request must finish first")
                    drain(directory)
                    request = marker.read_text()
                    try:
                        from .chat_modes_install import chat_mode_profiles
                        chat_mode_profiles(read_json(directory / "config.json"), install=True)
                        result = background_profiles(read_json(directory / "config.json"), install=True)
                    finally:
                        if marker.exists() and not marker.is_symlink() and marker.read_text() == request:
                            marker.unlink()
            else:
                result = background_profiles(read_json(directory / "config.json"))
            print(json.dumps({"profiles": result}))
        elif args.command in {"media-status", "install-media"}:
            from .media_install import media_profiles
            if args.command == "install-media":
                from .manage import drain
                drain(directory)
                try:
                    result = media_profiles(read_json(directory / "config.json"), install=True)
                finally:
                    (directory / "maintenance.request").unlink(missing_ok=True)
            else:
                result = media_profiles(read_json(directory / "config.json"))
            print(json.dumps({"profiles": result}))
        else:
            result = status(directory)
            if args.command == "doctor":
                result.update({"paired": (directory / "config.json").is_file(), "platform": sys.platform,
                               "supportedPlatform": sys.platform in {"darwin", "linux"}})
            print(json.dumps(result))
        return 0
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        print("Connector operation failed. Check local Hermes availability and connector configuration; credentials were not logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
