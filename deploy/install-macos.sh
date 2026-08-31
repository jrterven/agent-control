#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_DEFAULT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=deploy/lib/install-common.sh
source "${SCRIPT_DIR}/lib/install-common.sh"

DRY_RUN=0
INSTALL_PACKAGES=1
ALLOW_DIRTY_TREE=0
SKIP_HERMES_SERVICE=0
SKIP_TAILSCALE_SERVE=0
ROTATE_HERMES_TOKEN=0
REPO_ROOT="${REPO_DEFAULT}"
INSTALL_ROOT="${HOME}/.agent-control"
CONFIG_DIR="${INSTALL_ROOT}/config"
DATA_DIR="${INSTALL_ROOT}/data"
BACKUP_DIR="${INSTALL_ROOT}/backups"
LOG_DIR="${INSTALL_ROOT}/logs"
RELEASES_DIR="${INSTALL_ROOT}/releases"
CURRENT_LINK="${INSTALL_ROOT}/current"
ENV_FILE="${CONFIG_DIR}/control.env"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
KEYCHAIN_SERVICE="com.agent-control.hermes-dashboard"
KEYCHAIN_ACCOUNT="$(id -un)"
HERMES_BIN=""
HERMES_PROFILE="default"
HERMES_PORT="9119"
CONTROL_PORT="8000"
ADMIN_USERNAME="admin"
ALLOWED_ORIGIN=""
TRUSTED_HERMES_SHA=""
DEFAULT_PROFILES=""
INTERACTIVE_PROFILES=""
MUTABLE_PROFILES=""
DEFAULT_GATEWAY_NAME="Hermes local"
TAILSCALE_BIN=""
TAILSCALE_BACKEND_MODE=""
TAILSCALE_DNS_NAME=""
TAILSCALE_ORIGIN=""
PLIST_MANAGED_MARKER="Managed by deploy/install-macos.sh"
if [[ -n "${HERMES_CONTROL_INSTALL_TAILSCALE:-}" ]]; then
  TAILSCALE_BIN="${HERMES_CONTROL_INSTALL_TAILSCALE}"
  if [[ "${TAILSCALE_BIN}" == */Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
    TAILSCALE_BACKEND_MODE="cli"
  fi
fi

usage() {
  cat <<'EOF'
Usage: deploy/install-macos.sh [options]

Install or update Agent Control on the current macOS user account.
Hermes and Tailscale must already be installed.

Options:
  --dry-run                Print the planned actions without changing the host.
  --install-packages       Install missing dependencies with Homebrew (default).
  --no-install-packages    Fail instead of installing missing dependencies.
  --allow-dirty-tree       Allow building from a repo with uncommitted changes.
  --repo-root PATH         Use a different Agent Control repository checkout.
  --hermes-bin PATH        Explicit Hermes CLI path. Defaults to `hermes` in PATH.
  --trusted-hermes-sha SHA Optional exact 40-hex audited Hermes commit.
  --allowed-origin URL     Assert the connected node's exact HTTPS origin.
  --admin-username NAME    Create this first local admin when the DB is empty.
  --skip-hermes-service    Reuse an already-running local Hermes loopback service.
  --skip-tailscale-serve   Leave Tailscale Serve unchanged.
  --hermes-profile NAME    Hermes profile used for `hermes serve` (default: default).
  --rotate-hermes-token    Prompt again for the Keychain dashboard token.
  --help                   Show this help text.

The installer is idempotent:
  - releases live under ~/.agent-control/releases/<revision>
  - the active release is ~/.agent-control/current
  - runtime secrets stay outside the repo
  - reruns preserve the existing DB, vault key, admin user and Keychain token
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      ;;
    --install-packages)
      INSTALL_PACKAGES=1
      ;;
    --no-install-packages)
      INSTALL_PACKAGES=0
      ;;
    --allow-dirty-tree)
      ALLOW_DIRTY_TREE=1
      ;;
    --repo-root)
      shift
      REPO_ROOT="${1:?Missing value for --repo-root}"
      ;;
    --hermes-bin)
      shift
      HERMES_BIN="${1:?Missing value for --hermes-bin}"
      ;;
    --trusted-hermes-sha)
      shift
      TRUSTED_HERMES_SHA="${1:?Missing value for --trusted-hermes-sha}"
      ;;
    --allowed-origin)
      shift
      ALLOWED_ORIGIN="${1:?Missing value for --allowed-origin}"
      ;;
    --admin-username)
      shift
      ADMIN_USERNAME="${1:?Missing value for --admin-username}"
      ;;
    --skip-hermes-service)
      SKIP_HERMES_SERVICE=1
      ;;
    --skip-tailscale-serve)
      SKIP_TAILSCALE_SERVE=1
      ;;
    --hermes-profile)
      shift
      HERMES_PROFILE="${1:?Missing value for --hermes-profile}"
      ;;
    --rotate-hermes-token)
      ROTATE_HERMES_TOKEN=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
  shift
done

run_or_print_macos() {
  run_or_print "$@"
}

macos_tailscale() {
  if [[ "${TAILSCALE_BACKEND_MODE}" == "cli" ]]; then
    TAILSCALE_BE_CLI=1 "${TAILSCALE_BIN}" "$@"
  else
    "${TAILSCALE_BIN}" "$@"
  fi
}

port_listening() {
  local port="$1"
  python3 - "$port" <<'PY'
from __future__ import annotations

import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(0.5)
try:
    sock.connect(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

detect_tailscale_dns_name() {
  local json_payload="$1"
  python3 - "${json_payload}" <<'PY'
from __future__ import annotations

import json
import sys

payload = json.loads(sys.argv[1])
backend_state = payload.get("BackendState")
if backend_state not in (None, "Running"):
    raise SystemExit(0)
candidate = ""
for key in ("Self", "SelfNode"):
    node = payload.get(key)
    if isinstance(node, dict):
        if node.get("Online") is False:
            raise SystemExit(0)
        candidate = str(node.get("DNSName") or "").rstrip(".")
        if candidate:
            break
if candidate:
    print(candidate)
PY
}

classify_serve_state() {
  local json_payload="$1"
  local hostname="$2"
  local target="http://127.0.0.1:${CONTROL_PORT}"
  python3 - "$hostname" "$target" "${json_payload}" <<'PY'
from __future__ import annotations

import json
import sys

hostname, target = sys.argv[1:3]
payload = json.loads(sys.argv[3])
allow_funnel = payload.get("AllowFunnel") or {}
if (
    isinstance(allow_funnel, dict)
    and any(bool(value) for value in allow_funnel.values())
) or (not isinstance(allow_funnel, dict) and bool(allow_funnel)):
    print("funnel")
    raise SystemExit(0)

web = payload.get("Web") or {}
host = web.get(f"{hostname}:443") or {}
handlers = host.get("Handlers") or {}
root = handlers.get("/")
if isinstance(root, dict) and root.get("Proxy") == target:
    tcp = payload.get("TCP") or {}
    tcp_443 = tcp.get("443") or tcp.get(443)
    exact_tcp = not tcp or (
        len(tcp) == 1
        and isinstance(tcp_443, dict)
        and tcp_443.get("HTTPS") is True
        and set(tcp_443) == {"HTTPS"}
    )
    exact = (
        set(root) == {"Proxy"}
        and set(handlers) == {"/"}
        and set(host) == {"Handlers"}
        and set(web) == {f"{hostname}:443"}
        and exact_tcp
        and not any(bool(payload.get(key)) for key in ("Services", "Foreground"))
    )
    print("compatible" if exact else "conflict")
    raise SystemExit(0)

meaningful = any(bool(payload.get(key)) for key in ("TCP", "Web", "Services", "Foreground"))
print("conflict" if meaningful else "empty")
PY
}

validate_tailscale_origin() {
  python3 - "$1" "$2" <<'PY'
from __future__ import annotations

import sys
from urllib.parse import urlsplit

value = sys.argv[1].strip()
expected_host = sys.argv[2].strip().lower()
parts = urlsplit(value)
if parts.scheme != "https":
    raise SystemExit(1)
if not parts.hostname or parts.username or parts.password:
    raise SystemExit(1)
if parts.query or parts.fragment:
    raise SystemExit(1)
if parts.path != "":
    raise SystemExit(1)
if parts.port is not None:
    raise SystemExit(1)
hostname = parts.hostname.lower()
if hostname != expected_host or not hostname.endswith(".ts.net"):
    raise SystemExit(1)
print(f"https://{hostname}")
PY
}

validate_dashboard_token() {
  local token="$1"
  (( ${#token} >= 32 && ${#token} <= 512 )) && [[ "${token}" =~ ^[A-Za-z0-9._~-]+$ ]] || \
    die "The Hermes dashboard token must be 32-512 URL-safe characters."
}

validate_vault_key_b64() {
  python3 - "$1" <<'PY'
from __future__ import annotations

import base64
import binascii
import sys

value = sys.argv[1].strip()
padding = "=" * ((4 - len(value) % 4) % 4)
try:
    decoded = base64.b64decode(
        (value + padding).encode("ascii"), altchars=b"-_", validate=True
    )
except (UnicodeEncodeError, binascii.Error, ValueError):
    raise SystemExit(1)
if len(decoded) != 32:
    raise SystemExit(1)
PY
}

validate_database_contract() {
  python3 - "$1" "$2" "$3" "$(id -u)" "${DATA_DIR}" "${BACKUP_DIR}" <<'PY'
from __future__ import annotations

import os
import pathlib
import stat
import sys

database_path = pathlib.Path(sys.argv[1])
database_url = sys.argv[2]
backup_dir = pathlib.Path(sys.argv[3])
expected_uid = int(sys.argv[4])
managed_data_dir = pathlib.Path(sys.argv[5])
managed_backup_dir = pathlib.Path(sys.argv[6])
if not database_path.is_absolute():
    raise SystemExit(1)
if not backup_dir.is_absolute():
    raise SystemExit(1)
expected = f"sqlite:///{database_path}"
if database_url != expected:
    raise SystemExit(1)

def private_directory(path: pathlib.Path, *, managed_path: pathlib.Path) -> bool:
    if path.is_symlink():
        return False
    if not path.exists():
        return path == managed_path
    if not path.is_dir():
        return False
    metadata = path.stat()
    return metadata.st_uid == expected_uid and stat.S_IMODE(metadata.st_mode) == 0o700

if not private_directory(database_path.parent, managed_path=managed_data_dir):
    raise SystemExit(1)
if not private_directory(backup_dir, managed_path=managed_backup_dir):
    raise SystemExit(1)
if os.path.lexists(database_path):
    if database_path.is_symlink() or not database_path.is_file():
        raise SystemExit(1)
    metadata = database_path.stat()
    if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise SystemExit(1)
PY
}

discover_hermes_profiles() {
  python3 - "${HOME}/.hermes/profiles" "${HERMES_PROFILE}" <<'PY'
from __future__ import annotations

import json
import pathlib
import sys

profile_root = pathlib.Path(sys.argv[1])
selected = sys.argv[2]

def valid(name: str) -> bool:
    return bool(name) and len(name) <= 120 and all(ord(char) >= 32 for char in name)

if not valid(selected):
    raise SystemExit("The selected Hermes profile name is invalid.")
profiles = sorted(
    path.name for path in profile_root.iterdir() if path.is_dir()
) if profile_root.is_dir() else []
if profiles and selected not in profiles:
    raise SystemExit(f"Hermes profile does not exist: {selected}")
if not profiles:
    profiles = [selected]
if len(profiles) > 64 or any(not valid(name) for name in profiles):
    raise SystemExit("Hermes profile directories do not satisfy Agent Control's profile contract.")
print(json.dumps(profiles, ensure_ascii=True, separators=(",", ":")))
PY
}

validate_public_pwa_payloads() {
  python3 - <<'PY'
from __future__ import annotations

import json
import os

html = os.environ["HTML_PAYLOAD"].lower()
manifest = json.loads(os.environ["MANIFEST_PAYLOAD"])
if "<html" not in html or "manifest.webmanifest" not in html:
    raise SystemExit(1)
if not (manifest.get("name") or manifest.get("short_name")):
    raise SystemExit(1)
if not isinstance(manifest.get("start_url"), str) or not manifest["start_url"].startswith("/"):
    raise SystemExit(1)
if not isinstance(manifest.get("icons"), list) or not manifest["icons"]:
    raise SystemExit(1)
PY
}

wait_for_http_json() {
  local url="$1"
  local attempts="${2:-30}"
  local sleep_seconds="${3:-2}"
  local index
  for (( index=1; index<=attempts; index+=1 )); do
    if curl --fail --silent --show-error --connect-timeout 5 --max-time 10 "$url" >/dev/null; then
      return 0
    fi
    sleep "${sleep_seconds}"
  done
  return 1
}

python_version_supported() {
  python3 - <<'PY'
from __future__ import annotations

import sys

major, minor = sys.version_info[:2]
if not (major == 3 and 12 <= minor <= 14):
    raise SystemExit(1)
PY
}

node_version_supported() {
  node -e 'const major = Number(process.versions.node.split(".")[0]); if (Number.isNaN(major) || major < 20) process.exit(1);'
}

assert_supported_python_node() {
  python_version_supported
  node_version_supported
}

ensure_ready_payload() {
  local url="$1"
  local attempts="${2:-45}"
  local sleep_seconds="${3:-2}"
  local index payload_file
  payload_file="$(mktemp "${TMPDIR:-/tmp}/agent-control-ready.XXXXXX")"
  trap 'rm -f "${payload_file}"' RETURN
  for (( index=1; index<=attempts; index+=1 )); do
    if curl --silent --show-error --fail --connect-timeout 5 --max-time 10 "$url" >"${payload_file}"; then
      if python3 - "${payload_file}" <<'PY'
from __future__ import annotations

import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "ready":
    raise SystemExit(1)
if payload.get("database") != "ready":
    raise SystemExit(1)
if payload.get("upstream") != "online":
    raise SystemExit(1)
PY
      then
        trap - RETURN
        rm -f "${payload_file}"
        return 0
      fi
    fi
    sleep "${sleep_seconds}"
  done
  trap - RETURN
  rm -f "${payload_file}"
  return 1
}

verify_public_origin() {
  local origin="$1"
  local root_html manifest_json
  root_html="$(curl --silent --show-error --fail --connect-timeout 5 --max-time 10 "${origin}/")" || return 1
  manifest_json="$(curl --silent --show-error --fail --connect-timeout 5 --max-time 10 "${origin}/manifest.webmanifest")" || return 1
  HTML_PAYLOAD="${root_html}" MANIFEST_PAYLOAD="${manifest_json}" validate_public_pwa_payloads
}

ensure_hermes_profiles_api() {
  local token
  token="$(
    security find-generic-password -a "${KEYCHAIN_ACCOUNT}" -s "${KEYCHAIN_SERVICE}" -w
  )"
  validate_dashboard_token "${token}"
  printf 'url = "http://127.0.0.1:%s/api/profiles"\nheader = "X-Hermes-Session-Token: %s"\n' \
    "${HERMES_PORT}" "${token}" \
    | curl --silent --show-error --fail --connect-timeout 5 --max-time 10 --config - >/dev/null
}

wait_for_hermes_profiles_api() {
  local index
  for (( index=1; index<=30; index+=1 )); do
    if ensure_hermes_profiles_api >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

validate_existing_environment_contract() {
  [[ -f "${ENV_FILE}" ]] || return 0
  [[ ! -L "${ENV_FILE}" ]] || die "Existing control.env must not be a symbolic link."
  [[ "$(stat -f '%Lp' "${ENV_FILE}")" == "600" ]] || \
    die "Existing control.env must have mode 0600."
  [[ "$(stat -f '%u' "${ENV_FILE}")" == "$(id -u)" ]] || \
    die "Existing control.env must be owned by the signed-in user."
  local key
  for key in \
    HERMES_CONTROL_ENVIRONMENT \
    HERMES_CONTROL_VAULT_KEY_B64 \
    HERMES_CONTROL_HERMES_DASHBOARD_URL \
    HERMES_CONTROL_HERMES_DASHBOARD_WS \
    HERMES_CONTROL_HERMES_API_URL \
    HERMES_CONTROL_HERMES_API_KEY \
    HERMES_CONTROL_ALLOWED_ORIGINS \
    HERMES_CONTROL_SECURE_COOKIES \
    HERMES_CONTROL_CREATE_SCHEMA_ON_START \
    HERMES_CONTROL_PROVIDER_MODE \
    HERMES_CONTROL_MOCK_FALLBACK_ENABLED \
    HERMES_CONTROL_DATABASE_PATH \
    HERMES_CONTROL_DATABASE_URL \
    HERMES_CONTROL_BACKUP_DIR; do
    env_key_exists "${ENV_FILE}" "${key}" || die "Existing control.env must explicitly define ${key}."
  done
  local environment_value vault_key dashboard_url dashboard_ws api_url api_key secure_cookies create_schema provider_mode mock_fallback existing_origin existing_sha database_path database_url backup_dir
  environment_value="$(env_get "${ENV_FILE}" HERMES_CONTROL_ENVIRONMENT)"
  vault_key="$(env_get "${ENV_FILE}" HERMES_CONTROL_VAULT_KEY_B64)"
  dashboard_url="$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_DASHBOARD_URL)"
  dashboard_ws="$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_DASHBOARD_WS)"
  api_url="$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_API_URL)"
  api_key="$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_API_KEY)"
  secure_cookies="$(env_get "${ENV_FILE}" HERMES_CONTROL_SECURE_COOKIES)"
  create_schema="$(env_get "${ENV_FILE}" HERMES_CONTROL_CREATE_SCHEMA_ON_START)"
  provider_mode="$(env_get "${ENV_FILE}" HERMES_CONTROL_PROVIDER_MODE)"
  mock_fallback="$(env_get "${ENV_FILE}" HERMES_CONTROL_MOCK_FALLBACK_ENABLED)"
  existing_origin="$(env_get "${ENV_FILE}" HERMES_CONTROL_ALLOWED_ORIGINS)"
  existing_sha="$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_SOURCE_SHA)"
  database_path="$(env_get "${ENV_FILE}" HERMES_CONTROL_DATABASE_PATH)"
  database_url="$(env_get "${ENV_FILE}" HERMES_CONTROL_DATABASE_URL)"
  backup_dir="$(env_get "${ENV_FILE}" HERMES_CONTROL_BACKUP_DIR)"

  [[ "${environment_value}" == "production" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_ENVIRONMENT=production."
  validate_vault_key_b64 "${vault_key}" || \
    die "Existing control.env must keep a valid 32-byte HERMES_CONTROL_VAULT_KEY_B64."
  if env_key_exists "${ENV_FILE}" HERMES_CONTROL_HERMES_DASHBOARD_TOKEN; then
    [[ -z "$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_DASHBOARD_TOKEN)" ]] || \
      die "Existing control.env must not persist HERMES_CONTROL_HERMES_DASHBOARD_TOKEN."
  fi
  [[ "${dashboard_url}" == "http://127.0.0.1:${HERMES_PORT}" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_HERMES_DASHBOARD_URL on loopback 127.0.0.1:${HERMES_PORT}."
  [[ "${dashboard_ws}" == "ws://127.0.0.1:${HERMES_PORT}/api/ws" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_HERMES_DASHBOARD_WS on loopback port ${HERMES_PORT}."
  [[ -z "${api_url}" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_HERMES_API_URL empty."
  [[ -z "${api_key}" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_HERMES_API_KEY empty."
  validate_database_contract "${database_path}" "${database_url}" "${backup_dir}" || \
    die "Existing control.env must keep a coherent SQLite URL plus private user-owned database and backup paths (directories 0700; database 0600)."
  [[ "${existing_origin}" == "${TAILSCALE_ORIGIN}" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_ALLOWED_ORIGINS aligned with ${TAILSCALE_ORIGIN}."
  [[ "${secure_cookies}" == "true" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_SECURE_COOKIES=true."
  [[ "${create_schema}" == "false" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_CREATE_SCHEMA_ON_START=false."
  [[ "${provider_mode}" == "real" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_PROVIDER_MODE=real."
  [[ "${mock_fallback}" == "false" ]] || \
    die "Existing control.env must keep HERMES_CONTROL_MOCK_FALLBACK_ENABLED=false."
  [[ -z "${existing_sha}" || "${existing_sha}" =~ ^[0-9a-fA-F]{40}$ ]] || \
    die "Existing control.env has a malformed HERMES_CONTROL_HERMES_SOURCE_SHA value."
}

plist_is_managed() {
  local plist_path="$1"
  [[ -f "${plist_path}" ]] && grep -Fq "${PLIST_MANAGED_MARKER}" "${plist_path}"
}

assert_managed_plist_or_absent() {
  local plist_path="$1"
  [[ ! -f "${plist_path}" ]] && return 0
  plist_is_managed "${plist_path}" || die "Refusing to overwrite unmanaged LaunchAgent plist: ${plist_path}"
}

create_predeploy_backup() {
  if [[ ! -f "${database_path}" ]]; then
    return 0
  fi
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  unset HERMES_CONTROL_HERMES_DASHBOARD_TOKEN || true
  "${release_dir}/deploy/bin/backup-sqlite.sh" >/dev/null
}

ensure_brew_dependencies() {
  local -a missing=()
  local command_name
  for command_name in python3 node npm sqlite3 curl; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
      missing+=("${command_name}")
    fi
  done
  if ! command -v git >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
    missing+=("git")
  fi
  if command -v python3 >/dev/null 2>&1 && ! python_version_supported; then
    missing+=("python3>=3.12")
  fi
  if command -v node >/dev/null 2>&1 && ! node_version_supported; then
    missing+=("node>=20")
  fi
  if [[ "${#missing[@]}" -eq 0 ]]; then
    return 0
  fi
  if [[ "${INSTALL_PACKAGES}" != "1" ]]; then
    die "Missing required build tools: ${missing[*]}. Install them or allow the default Homebrew dependency installation."
  fi
  if ! command -v brew >/dev/null 2>&1; then
    if [[ -x /opt/homebrew/bin/brew ]]; then
      PATH="/opt/homebrew/bin:${PATH}"
    elif [[ -x /usr/local/bin/brew ]]; then
      PATH="/usr/local/bin:${PATH}"
    fi
    export PATH
  fi
  require_command brew
  local -a packages=(python@3.12 node@20 sqlite git)
  run_or_print_macos brew install "${packages[@]}"
  if [[ "${DRY_RUN}" != "1" ]]; then
    PATH="$(brew --prefix python@3.12)/libexec/bin:$(brew --prefix python@3.12)/bin:$(brew --prefix node@20)/bin:$(brew --prefix git)/bin:${PATH}"
    if brew --prefix sqlite >/dev/null 2>&1; then
      PATH="$(brew --prefix sqlite)/bin:${PATH}"
    fi
    export PATH
  fi
}

render_release() {
  local release_dir="$1"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "dry-run would build immutable release at ${release_dir}"
    return 0
  fi
  local staging_parent
  local staging_release
  staging_parent="$(mktemp -d "${TMPDIR:-/tmp}/agent-control-macos-release.XXXXXX")"
  staging_release="${staging_parent}/release"
  trap 'rm -rf "${staging_parent}"' RETURN
  copy_repo_snapshot "${REPO_ROOT}" "${staging_release}"
  (
    cd "${staging_release}"
    npm ci
    npm run build
    python3 - <<'PY'
from __future__ import annotations

import pathlib
import shutil

root = pathlib.Path.cwd()
source = root / "apps" / "web" / "dist"
target = root / "apps" / "api" / "static"
if target.exists():
    shutil.rmtree(target)
shutil.copytree(source, target)
PY
    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install ./packages/hermes-client ./apps/api
    printf '%s\n' "${release_id}" >.agent-control-release
    chmod 0600 .agent-control-release
  )
  install -d -m 0700 "${RELEASES_DIR}"
  mv "${staging_release}" "${release_dir}"
  trap - RETURN
  rm -rf "${staging_parent}"
}

macos_user="$(id -un)"
macos_uid="$(id -u)"
[[ "$(uname -s)" == "Darwin" ]] || die "This installer only supports macOS."
[[ "${macos_uid}" -ne 0 ]] || die "Run this installer as the signed-in macOS user, not root."

REPO_ROOT="$(cd "${REPO_ROOT}" && pwd)"
ensure_brew_dependencies
require_command git
require_command python3
require_command node
require_command npm
require_command sqlite3
require_command curl
require_command launchctl
require_command plutil
require_command security
assert_supported_python_node || die "Agent Control requires Python 3.12-3.14 and Node 20+."
REPO_ROOT="$(git_repo_root "${REPO_ROOT}")"

if [[ -z "${HERMES_BIN}" ]]; then
  HERMES_BIN="$(command -v hermes || true)"
fi
[[ -n "${HERMES_BIN}" ]] || die "Hermes CLI not found. Re-run with --hermes-bin /absolute/path/to/hermes."
absolute_path_or_die "${HERMES_BIN}"
"${HERMES_BIN}" -p "${HERMES_PROFILE}" serve --help >/dev/null 2>&1 || die "Hermes preflight failed for: ${HERMES_BIN} -p ${HERMES_PROFILE} serve --help"
DEFAULT_PROFILES="$(discover_hermes_profiles)" || die "Could not discover a safe Hermes profile list."
INTERACTIVE_PROFILES="${DEFAULT_PROFILES}"
MUTABLE_PROFILES="${DEFAULT_PROFILES}"

if [[ -n "${TRUSTED_HERMES_SHA}" ]] && [[ ! "${TRUSTED_HERMES_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]; then
  die "--trusted-hermes-sha must be exactly 40 hexadecimal characters."
fi

if git_is_dirty "${REPO_ROOT}" && [[ "${ALLOW_DIRTY_TREE}" != "1" ]]; then
  die "The repository has uncommitted changes. Commit or stash them, or re-run with --allow-dirty-tree."
fi

head_sha="$(git_head_sha "${REPO_ROOT}")"
release_id="${head_sha}"
if git_is_dirty "${REPO_ROOT}"; then
  release_id="${head_sha}-dirty-$(utc_timestamp)"
fi
release_dir="${RELEASES_DIR}/${release_id}"
control_plist="${LAUNCH_AGENTS_DIR}/com.agent-control.control.plist"
backup_plist="${LAUNCH_AGENTS_DIR}/com.agent-control.backup.plist"
hermes_plist="${LAUNCH_AGENTS_DIR}/com.agent-control.hermes-serve.plist"

if [[ -z "${TAILSCALE_BIN}" ]]; then
  if command -v tailscale >/dev/null 2>&1; then
    TAILSCALE_BIN="$(command -v tailscale)"
  elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
    TAILSCALE_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    TAILSCALE_BACKEND_MODE="cli"
  fi
fi
[[ -n "${TAILSCALE_BIN}" ]] || die "Tailscale CLI not found."

tailscale_status_json="$(macos_tailscale status --json)"
TAILSCALE_DNS_NAME="$(detect_tailscale_dns_name "${tailscale_status_json}")"
[[ -n "${TAILSCALE_DNS_NAME}" && "${TAILSCALE_DNS_NAME}" == *.ts.net ]] || \
  die "Tailscale must already be connected to a tailnet with a MagicDNS .ts.net name."
TAILSCALE_DNS_NAME="$(printf '%s' "${TAILSCALE_DNS_NAME}" | tr '[:upper:]' '[:lower:]')"
TAILSCALE_ORIGIN="https://${TAILSCALE_DNS_NAME}"
serve_json="$(macos_tailscale serve status --json)"
serve_state="$(classify_serve_state "${serve_json}" "${TAILSCALE_DNS_NAME}")"

existing_keychain_token="$(
  security find-generic-password -a "${KEYCHAIN_ACCOUNT}" -s "${KEYCHAIN_SERVICE}" -w 2>/dev/null || true
)"
if [[ -n "${existing_keychain_token}" && "${ROTATE_HERMES_TOKEN}" != "1" ]]; then
  validate_dashboard_token "${existing_keychain_token}"
fi
validate_existing_environment_contract
existing_database_origin="$(env_get "${ENV_FILE}" HERMES_CONTROL_ALLOWED_ORIGINS)"
if [[ -n "${ALLOWED_ORIGIN}" ]]; then
  ALLOWED_ORIGIN="$(validate_tailscale_origin "${ALLOWED_ORIGIN}" "${TAILSCALE_DNS_NAME}")" || \
    die "--allowed-origin must be exactly ${TAILSCALE_ORIGIN} with no port, slash, query or fragment."
elif [[ -n "${existing_database_origin}" ]]; then
  existing_database_origin="$(validate_tailscale_origin "${existing_database_origin}" "${TAILSCALE_DNS_NAME}")" || \
    die "Existing HERMES_CONTROL_ALLOWED_ORIGINS must be exactly ${TAILSCALE_ORIGIN}."
else
  ALLOWED_ORIGIN="${TAILSCALE_ORIGIN}"
fi
assert_managed_plist_or_absent "${control_plist}"
assert_managed_plist_or_absent "${backup_plist}"
if [[ "${SKIP_HERMES_SERVICE}" != "1" ]]; then
  assert_managed_plist_or_absent "${hermes_plist}"
fi
if port_listening "${HERMES_PORT}" >/dev/null 2>&1 && [[ "${SKIP_HERMES_SERVICE}" != "1" ]]; then
  die "127.0.0.1:${HERMES_PORT} is already in use. Re-run with --skip-hermes-service only after storing the matching dashboard token in the macOS Keychain."
fi
if port_listening "${CONTROL_PORT}" >/dev/null 2>&1 && ! plist_is_managed "${control_plist}"; then
  die "127.0.0.1:${CONTROL_PORT} is already in use and ${control_plist} is not a managed Agent Control LaunchAgent."
fi
if [[ "${SKIP_HERMES_SERVICE}" == "1" && -z "${existing_keychain_token}" ]]; then
  die "--skip-hermes-service requires an existing Keychain token under ${KEYCHAIN_SERVICE}/${KEYCHAIN_ACCOUNT}."
fi
if [[ "${SKIP_HERMES_SERVICE}" == "1" ]]; then
  ensure_hermes_profiles_api || die "The existing Hermes listener on 127.0.0.1:${HERMES_PORT} did not accept the Keychain dashboard token."
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run inspected tailscale status --json and tailscale serve status --json"
fi
if [[ "${SKIP_TAILSCALE_SERVE}" != "1" ]]; then
  case "${serve_state}" in
    empty|compatible) ;;
    funnel)
      die "Tailscale Funnel or another public Serve route is enabled. Disable it manually; this installer will not reset Serve."
      ;;
    *)
      die "Existing Tailscale Serve state is not empty and does not match Agent Control. Merge it manually; this installer will not reset Serve."
      ;;
  esac
fi

run_or_print_macos install -d -m 0700 "${INSTALL_ROOT}" "${CONFIG_DIR}" "${DATA_DIR}" "${BACKUP_DIR}" "${LOG_DIR}"
run_or_print_macos install -d -m 0700 "${LAUNCH_AGENTS_DIR}"

existing_vault_key="$(env_get "${ENV_FILE}" HERMES_CONTROL_VAULT_KEY_B64)"
existing_database_url="$(env_get "${ENV_FILE}" HERMES_CONTROL_DATABASE_URL)"
existing_database_path="$(env_get "${ENV_FILE}" HERMES_CONTROL_DATABASE_PATH)"
existing_backup_dir="$(env_get "${ENV_FILE}" HERMES_CONTROL_BACKUP_DIR)"
existing_allowed_origin="$(env_get "${ENV_FILE}" HERMES_CONTROL_ALLOWED_ORIGINS)"
existing_gateway_name="$(env_get "${ENV_FILE}" HERMES_CONTROL_DEFAULT_GATEWAY_NAME)"
existing_media_root="$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_MEDIA_ROOT)"
existing_default_profiles="$(env_get "${ENV_FILE}" HERMES_CONTROL_DEFAULT_PROFILES)"
existing_interactive_profiles="$(env_get "${ENV_FILE}" HERMES_CONTROL_INTERACTIVE_PROFILES)"
existing_mutable_profiles="$(env_get "${ENV_FILE}" HERMES_CONTROL_MUTABLE_PROFILES)"
existing_trusted_sha="$(env_get "${ENV_FILE}" HERMES_CONTROL_HERMES_SOURCE_SHA)"

vault_key="${existing_vault_key:-$(random_token)}"
database_path="${existing_database_path:-${DATA_DIR}/control.db}"
database_url="${existing_database_url:-sqlite:///${database_path}}"
backup_dir_value="${existing_backup_dir:-${BACKUP_DIR}}"
allowed_origin_value="${existing_allowed_origin:-${ALLOWED_ORIGIN}}"
gateway_name="${existing_gateway_name:-${DEFAULT_GATEWAY_NAME}}"
media_root="${existing_media_root:-${HOME}/.hermes/profiles}"
default_profiles_value="${existing_default_profiles:-${DEFAULT_PROFILES}}"
interactive_profiles_value="${existing_interactive_profiles:-${INTERACTIVE_PROFILES}}"
mutable_profiles_value="${existing_mutable_profiles:-${MUTABLE_PROFILES}}"
trusted_sha_value="${existing_trusted_sha:-}"
if [[ -n "${ALLOWED_ORIGIN}" ]]; then
  allowed_origin_value="${ALLOWED_ORIGIN}"
fi
if [[ -n "${TRUSTED_HERMES_SHA}" ]]; then
  trusted_sha_value="$(printf '%s' "${TRUSTED_HERMES_SHA}" | tr '[:upper:]' '[:lower:]')"
fi

write_env_pairs=(
  "HERMES_CONTROL_DATABASE_URL=${database_url}"
  "HERMES_CONTROL_DATABASE_PATH=${database_path}"
  "HERMES_CONTROL_BACKUP_DIR=${backup_dir_value}"
  "HERMES_CONTROL_ENVIRONMENT=production"
  "HERMES_CONTROL_VAULT_KEY_B64=${vault_key}"
  "HERMES_CONTROL_HERMES_DASHBOARD_URL=http://127.0.0.1:${HERMES_PORT}"
  "HERMES_CONTROL_HERMES_DASHBOARD_WS=ws://127.0.0.1:${HERMES_PORT}/api/ws"
  "HERMES_CONTROL_HERMES_API_URL="
  "HERMES_CONTROL_HERMES_API_KEY="
  "HERMES_CONTROL_HERMES_MEDIA_ROOT=${media_root}"
  "HERMES_CONTROL_HERMES_SOURCE_SHA=${trusted_sha_value}"
  "HERMES_CONTROL_DEFAULT_GATEWAY_NAME=${gateway_name}"
  "HERMES_CONTROL_DEFAULT_PROFILES=${default_profiles_value}"
  "HERMES_CONTROL_ALLOWED_ORIGINS=${allowed_origin_value}"
  "HERMES_CONTROL_INTERACTIVE_PROFILES=${interactive_profiles_value}"
  "HERMES_CONTROL_MUTABLE_PROFILES=${mutable_profiles_value}"
  "HERMES_CONTROL_SECURE_COOKIES=true"
  "HERMES_CONTROL_CREATE_SCHEMA_ON_START=false"
  "HERMES_CONTROL_PROVIDER_MODE=real"
  "HERMES_CONTROL_MOCK_FALLBACK_ENABLED=false"
  "HERMES_CONTROL_TRUST_PRIVATE_ENDPOINTS=true"
  "HERMES_CONTROL_WS_MAX_INBOUND_BYTES=4096"
  "HERMES_CONTROL_AUTOMATION_ROUTE_WATCH_SECONDS=30"
  "HERMES_CONTROL_AUTOMATION_ROUTE_STALE_SECONDS=120"
  "HERMES_CONTROL_UPSTREAM_HEALTH_TTL_SECONDS=60"
  "HERMES_CONTROL_CAPABILITY_TTL_SECONDS=60"
  "HERMES_CONTROL_CAPABILITY_REFRESH_SECONDS=30"
)

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run would write ${ENV_FILE}"
else
  write_env_file "${ENV_FILE}" \
    "# Managed by deploy/install-macos.sh. Secrets stay outside the repository." \
    "${write_env_pairs[@]}"
  chmod 0600 "${ENV_FILE}"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run would ensure Keychain item ${KEYCHAIN_SERVICE}/${KEYCHAIN_ACCOUNT}"
else
  if [[ "${ROTATE_HERMES_TOKEN}" == "1" ]]; then
    existing_keychain_token=""
  fi
  if [[ -z "${existing_keychain_token}" ]]; then
    security add-generic-password \
      -U \
      -a "${KEYCHAIN_ACCOUNT}" \
      -s "${KEYCHAIN_SERVICE}" \
      -w \
      >/dev/null
  fi
  existing_keychain_token="$(
    security find-generic-password -a "${KEYCHAIN_ACCOUNT}" -s "${KEYCHAIN_SERVICE}" -w 2>/dev/null || true
  )"
  validate_dashboard_token "${existing_keychain_token}"
fi

if [[ -d "${release_dir}" ]]; then
  [[ -f "${release_dir}/.agent-control-release" ]] || \
    die "Existing release is missing its installer marker: ${release_dir}"
  [[ "$(<"${release_dir}/.agent-control-release")" == "${release_id}" ]] || \
    die "Existing release marker does not match ${release_id}."
  [[ -x "${release_dir}/.venv/bin/uvicorn" && -f "${release_dir}/apps/api/static/index.html" ]] || \
    die "Existing release is incomplete: ${release_dir}"
  log "Reusing existing immutable release ${release_dir}"
else
  render_release "${release_dir}"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run would repoint ${CURRENT_LINK} -> ${release_dir}"
else
  create_predeploy_backup
  replace_symlink_atomically "${release_dir}" "${CURRENT_LINK}"
fi

hermes_launch_managed=1
if [[ "${SKIP_HERMES_SERVICE}" == "1" ]]; then
  hermes_launch_managed=0
fi

render_or_print_plist() {
  local template_file="$1"
  local output_file="$2"
  shift 2
  assert_managed_plist_or_absent "${output_file}"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "dry-run would render ${output_file} from $(basename "${template_file}")"
    return 0
  fi
  render_template_file "${template_file}" "${output_file}" "$@"
  plutil -lint "${output_file}" >/dev/null
}

reload_launch_agent() {
  local label="$1"
  local plist="$2"
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "dry-run would reload launch agent ${label}"
    return 0
  fi
  launchctl bootout "gui/${macos_uid}" "${plist}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/${macos_uid}" "${plist}"
  launchctl enable "gui/${macos_uid}/${label}"
  launchctl kickstart -k "gui/${macos_uid}/${label}"
}

if [[ "${hermes_launch_managed}" == "1" ]]; then
  render_or_print_plist \
    "${REPO_ROOT}/deploy/launchd/com.agent-control.hermes-serve.plist.example" \
    "${hermes_plist}" \
    "__SERVE_WRAPPER__=${CURRENT_LINK}/deploy/bin/hermes-macos-serve.sh" \
    "__HERMES_BIN__=${HERMES_BIN}" \
    "__HERMES_PROFILE__=${HERMES_PROFILE}" \
    "__MACOS_USER__=${macos_user}" \
    "__LOG_DIR__=${LOG_DIR}"
  reload_launch_agent "com.agent-control.hermes-serve" "${hermes_plist}"
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run would verify the authenticated Hermes /api/profiles endpoint without putting the token in argv"
elif [[ "${hermes_launch_managed}" == "1" || "${SKIP_HERMES_SERVICE}" == "1" ]]; then
  wait_for_hermes_profiles_api || \
    die "Hermes /api/profiles verification failed on 127.0.0.1:${HERMES_PORT}."
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run would run Alembic migration and create the first admin if needed"
else
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  unset HERMES_CONTROL_HERMES_DASHBOARD_TOKEN || true
  export HERMES_CONTROL_STATIC_DIR="${CURRENT_LINK}/apps/api/static"
  "${CURRENT_LINK}/.venv/bin/alembic" -c "${CURRENT_LINK}/apps/api/alembic.ini" upgrade head
  [[ -f "${database_path}" && ! -L "${database_path}" ]] || \
    die "Alembic did not create a regular SQLite database at ${database_path}."
  chmod 0600 "${database_path}"
  admin_count="$(sqlite3 "${database_path}" "SELECT COUNT(*) FROM users WHERE is_admin = 1;")" || \
    die "Could not query the admin count from ${database_path}."
  [[ "${admin_count}" =~ ^[0-9]+$ ]] || \
    die "Unexpected admin-count query result from ${database_path}: ${admin_count}"
  if [[ "${admin_count}" == "0" ]]; then
    "${CURRENT_LINK}/.venv/bin/hermes-control-admin" create-admin --username "${ADMIN_USERNAME}"
  fi
fi

render_or_print_plist \
  "${REPO_ROOT}/deploy/launchd/com.agent-control.control.plist.example" \
  "${control_plist}" \
  "__CONTROL_WRAPPER__=${CURRENT_LINK}/deploy/bin/control-macos-serve.sh" \
  "__RELEASE_ROOT__=${CURRENT_LINK}" \
  "__ENV_FILE__=${ENV_FILE}" \
  "__MACOS_USER__=${macos_user}" \
  "__LOG_DIR__=${LOG_DIR}"
reload_launch_agent "com.agent-control.control" "${control_plist}"

if [[ "${SKIP_TAILSCALE_SERVE}" != "1" ]]; then
  if [[ "${DRY_RUN}" == "1" ]]; then
    log "dry-run would ensure tailscale serve --bg --yes --https=443 --set-path=/ http://127.0.0.1:${CONTROL_PORT}"
  elif [[ "${serve_state}" == "empty" ]]; then
    macos_tailscale serve --bg --yes --https=443 --set-path=/ "http://127.0.0.1:${CONTROL_PORT}"
  else
    log "Tailscale Serve already points / to http://127.0.0.1:${CONTROL_PORT}"
  fi
  if [[ "${DRY_RUN}" != "1" ]]; then
    verified_serve_json="$(macos_tailscale serve status --json)"
    verified_serve_state="$(classify_serve_state "${verified_serve_json}" "${TAILSCALE_DNS_NAME}")"
    [[ "${verified_serve_state}" == "compatible" ]] || \
      die "Tailscale Serve did not retain the exact Agent Control root proxy."
  fi
fi

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run would verify /api/v1/health, strict readiness, and public PWA assets"
else
  wait_for_http_json "http://127.0.0.1:${CONTROL_PORT}/api/v1/health" 45 2 || die "Health check failed on 127.0.0.1:${CONTROL_PORT}."
  ensure_ready_payload "http://127.0.0.1:${CONTROL_PORT}/api/v1/ready" 60 2 || die "Readiness check failed on 127.0.0.1:${CONTROL_PORT}."
fi

render_or_print_plist \
  "${REPO_ROOT}/deploy/launchd/com.agent-control.backup.plist.example" \
  "${backup_plist}" \
  "__BACKUP_WRAPPER__=${CURRENT_LINK}/deploy/bin/control-macos-backup.sh" \
  "__RELEASE_ROOT__=${CURRENT_LINK}" \
  "__ENV_FILE__=${ENV_FILE}" \
  "__LOG_DIR__=${LOG_DIR}"
reload_launch_agent "com.agent-control.backup" "${backup_plist}"

if [[ "${DRY_RUN}" == "1" ]]; then
  log "dry-run would run the first online backup"
elif [[ -x "${CURRENT_LINK}/deploy/bin/control-macos-backup.sh" ]]; then
  "${CURRENT_LINK}/deploy/bin/control-macos-backup.sh"
fi

if [[ "${DRY_RUN}" != "1" && "${SKIP_TAILSCALE_SERVE}" != "1" ]]; then
  verify_public_origin "${allowed_origin_value}" || die "Public Tailscale origin verification failed for ${allowed_origin_value}."
fi

log "macOS deployment ready."
log "Release: ${release_dir}"
log "Loopback: http://127.0.0.1:${CONTROL_PORT}"
log "Origin: ${allowed_origin_value}"
