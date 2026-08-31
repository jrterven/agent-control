#!/usr/bin/env bash
set -Eeuo pipefail

# Idempotent first-install path for a Linux host that already runs Hermes and
# Tailscale. Secrets are generated only in host-owned configuration paths; this
# script never enables Funnel, clears Serve state, or updates Hermes itself.

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly REPOSITORY_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd -P)"
readonly CONTROL_TARGET="http://127.0.0.1:8000"
readonly HERMES_TARGET="http://127.0.0.1:9119"
readonly PINNED_NODE_VERSION="22.23.2"
readonly PINNED_NODE_LINUX_X64_SHA256="b294a556e639d64338823920e5866c21c02741742d2e1529ee1a225c1ec9252a"
readonly PINNED_NODE_LINUX_ARM64_SHA256="013b59cfd2819703a6f4a14ab891fc46fc2a4e3f5bcd92de3fb4929b43e35b30"

SOURCE_ROOT="$REPOSITORY_ROOT"
STATIC_SOURCE=""
RELEASE_ID=""
TAILSCALE_HOSTNAME=""
HERMES_USER=""
HERMES_BIN=""
HERMES_PROFILE=""
HERMES_SOURCE_SHA=""
REUSE_HERMES_SERVE=0
SKIP_ADMIN=0
DRY_RUN=0
BUILD_WEB=0
SOURCE_IS_GIT=0
STATIC_SOURCE_EXPLICIT=0
CONTROL_INSTALLATION_EXISTS=0
CONTROL_SERVICE_PREEXISTED=0

ROOT_PREFIX="${HERMES_CONTROL_INSTALL_ROOT:-/}"
TESTING="${HERMES_CONTROL_INSTALL_TESTING:-0}"
SYSTEMCTL_BIN="${HERMES_CONTROL_INSTALL_SYSTEMCTL:-systemctl}"
TAILSCALE_BIN="${HERMES_CONTROL_INSTALL_TAILSCALE:-tailscale}"
CURL_BIN="${HERMES_CONTROL_INSTALL_CURL:-curl}"
SQLITE_BIN="${HERMES_CONTROL_INSTALL_SQLITE3:-sqlite3}"
RUNUSER_BIN="${HERMES_CONTROL_INSTALL_RUNUSER:-runuser}"
PORT_PROBE_BIN="${HERMES_CONTROL_INSTALL_PORT_PROBE:-}"

CONTROL_USER="hermes-control"
CONTROL_GROUP="hermes-control"
CONTROL_OWNER="hermes-control"
CONTROL_GROUP_OWNER="hermes-control"
ROOT_OWNER="root"
ROOT_GROUP_OWNER="root"
HERMES_HOME=""
HERMES_FS_HOME=""
HERMES_GROUP=""
HERMES_COMMAND_KIND=""
PYTHON_BIN=""
NODE_BIN=""
NPM_BIN=""
TAILSCALE_ORIGIN=""
TAILSCALE_STATE=""
HERMES_TOKEN=""
PROFILE_LIST=""
HERMES_MEDIA_ROOT=""
INSTALL_HERMES_TOKEN=""

case "$-" in
  *x*) set +x ;;
  *) ;;
esac
INSTALL_HERMES_TOKEN="${HERMES_CONTROL_INSTALL_HERMES_TOKEN:-}"
unset HERMES_CONTROL_INSTALL_HERMES_TOKEN || true

usage() {
  cat <<'EOF'
Usage: sudo deploy/linux/install-agent-control.sh [options]

Options:
  --source DIR                  Agent Control source/release artifact root
  --static-dir DIR              Prebuilt PWA directory (must contain index.html)
  --release-id ID               Immutable release name; defaults to clean Git SHA
  --tailscale-hostname NAME     Assert the connected device MagicDNS hostname
  --hermes-user USER            Existing OS user that owns Hermes
  --hermes-bin PATH             Hermes executable or its virtualenv Python
  --hermes-profile PROFILE      Profile used to launch `hermes serve`
  --hermes-source-sha SHA       Explicit audited 40-hex Hermes trust anchor
  --reuse-hermes-serve          Explicitly trust an existing/unmanaged port 9119
  --skip-admin                  Do not prompt to create the first administrator
  --dry-run                     Validate and print the plan without changing state
  -h, --help                    Show this help

If an existing Hermes service uses a nonstandard token file, provide its token
through HERMES_CONTROL_INSTALL_HERMES_TOKEN. It is intentionally not accepted
as a command-line argument, where it would be visible in the process list.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '%s\n' "$*" >&2
}

rooted() {
  local path=$1
  [[ "$path" == /* ]] || die "internal path is not absolute: $path"
  if [[ "$ROOT_PREFIX" == "/" ]]; then
    printf '%s\n' "$path"
  else
    printf '%s%s\n' "${ROOT_PREFIX%/}" "$path"
  fi
}

valid_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$ ]]
}

valid_user() {
  [[ "$1" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]]
}

valid_absolute_command() {
  [[ "$1" =~ ^/[A-Za-z0-9_./@+-]+$ ]] && [[ "$1" != *".."* ]]
}

parse_args() {
  while (($#)); do
    case "$1" in
      --source) [[ $# -ge 2 ]] || die "--source requires a value"; SOURCE_ROOT=$2; shift 2 ;;
      --static-dir) [[ $# -ge 2 ]] || die "--static-dir requires a value"; STATIC_SOURCE=$2; STATIC_SOURCE_EXPLICIT=1; shift 2 ;;
      --release-id) [[ $# -ge 2 ]] || die "--release-id requires a value"; RELEASE_ID=$2; shift 2 ;;
      --tailscale-hostname) [[ $# -ge 2 ]] || die "--tailscale-hostname requires a value"; TAILSCALE_HOSTNAME=$2; shift 2 ;;
      --hermes-user) [[ $# -ge 2 ]] || die "--hermes-user requires a value"; HERMES_USER=$2; shift 2 ;;
      --hermes-bin) [[ $# -ge 2 ]] || die "--hermes-bin requires a value"; HERMES_BIN=$2; shift 2 ;;
      --hermes-profile) [[ $# -ge 2 ]] || die "--hermes-profile requires a value"; HERMES_PROFILE=$2; shift 2 ;;
      --hermes-source-sha) [[ $# -ge 2 ]] || die "--hermes-source-sha requires a value"; HERMES_SOURCE_SHA=$2; shift 2 ;;
      --reuse-hermes-serve) REUSE_HERMES_SERVE=1; shift ;;
      --skip-admin) SKIP_ADMIN=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done
}

require_root_boundary() {
  [[ "$ROOT_PREFIX" == /* ]] || die "HERMES_CONTROL_INSTALL_ROOT must be absolute"
  ROOT_PREFIX="${ROOT_PREFIX%/}"
  [[ -n "$ROOT_PREFIX" ]] || ROOT_PREFIX="/"
  if [[ "$TESTING" == "1" ]]; then
    [[ "$ROOT_PREFIX" != "/" ]] || die "test mode refuses the real root filesystem"
  elif ((DRY_RUN == 0)); then
    [[ "$ROOT_PREFIX" == "/" ]] || die "an alternate root is reserved for installer tests"
    [[ ${EUID:-$(id -u)} -eq 0 ]] || die "run this installer as root"
  fi
}

find_compatible_python() {
  local require_venv=${1:-0}
  local candidate version_ok
  for candidate in python3.14 python3.13 python3.12 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    version_ok="$($candidate -c 'import sys; print(int((3, 12) <= sys.version_info[:2] < (3, 15)))' 2>/dev/null || true)"
    if [[ "$version_ok" == "1" ]] && \
      { [[ "$require_venv" == "0" ]] || python_has_venv "$candidate"; }; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

python_has_venv() {
  "$1" -m venv --help >/dev/null 2>&1
}

python_minor() {
  "$1" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")'
}

apt_package_available() {
  apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ { found=($2 != "(none)") } END { exit !found }'
}

node_tools_supported() {
  NODE_BIN="${HERMES_CONTROL_INSTALL_NODE:-${NODE_BIN:-$(command -v node 2>/dev/null || true)}}"
  NPM_BIN="${HERMES_CONTROL_INSTALL_NPM:-${NPM_BIN:-$(command -v npm 2>/dev/null || true)}}"
  [[ -n "$NODE_BIN" && -n "$NPM_BIN" ]] || return 1
  command -v "$NODE_BIN" >/dev/null 2>&1 || return 1
  command -v "$NPM_BIN" >/dev/null 2>&1 || return 1
  local node_major
  node_major="$($NODE_BIN -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || printf 0)"
  [[ "$node_major" =~ ^[0-9]+$ ]] && ((node_major >= 20)) || return 1
  env PATH="$(dirname -- "$NODE_BIN"):$PATH" "$NPM_BIN" --version \
    >/dev/null 2>&1
}

run_npm() {
  env PATH="$(dirname -- "$NODE_BIN"):$PATH" "$NPM_BIN" "$@"
}

install_pinned_node_toolchain() {
  local architecture archive checksum
  case "$(uname -m)" in
    x86_64|amd64)
      architecture="x64"
      checksum="$PINNED_NODE_LINUX_X64_SHA256"
      ;;
    aarch64|arm64)
      architecture="arm64"
      checksum="$PINNED_NODE_LINUX_ARM64_SHA256"
      ;;
    *)
      die "automatic Node.js installation supports Linux x86_64 and arm64 only; preinstall Node >=20 or pass --static-dir"
      ;;
  esac
  archive="node-v${PINNED_NODE_VERSION}-linux-${architecture}.tar.gz"
  local toolchain_parent toolchain_dir
  toolchain_parent="$(rooted /opt/hermes-control/toolchains)"
  toolchain_dir="$toolchain_parent/node-v${PINNED_NODE_VERSION}-linux-${architecture}"
  NODE_BIN="$toolchain_dir/bin/node"
  NPM_BIN="$toolchain_dir/bin/npm"
  if [[ -d "$toolchain_dir" && ! -L "$toolchain_dir" && \
    -x "$NODE_BIN" && -x "$NPM_BIN" ]] && node_tools_supported; then
    return
  fi
  if ((DRY_RUN)); then
    note "Would install or safely repair pinned Node.js v${PINNED_NODE_VERSION} from nodejs.org for the PWA build."
    return
  fi
  local temporary_root temporary_archive extracted calculated lock_dir old_toolchain
  temporary_root="$(mktemp -d)"
  temporary_archive="$temporary_root/$archive"
  extracted="$toolchain_parent/.node-v${PINNED_NODE_VERSION}-${architecture}.partial.$$"
  lock_dir="$toolchain_parent/.node-install.lock"
  old_toolchain="$toolchain_parent/.node-v${PINNED_NODE_VERSION}-${architecture}.invalid.$$"
  cleanup_node_stage() {
    rm -rf -- "$temporary_root" "$extracted"
    rmdir -- "$lock_dir" 2>/dev/null || true
  }
  trap cleanup_node_stage EXIT INT TERM
  install -d -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0755 "$toolchain_parent"
  mkdir -- "$lock_dir" 2>/dev/null || \
    die "another pinned Node.js installation is in progress at $lock_dir"
  if [[ -d "$toolchain_dir" && ! -L "$toolchain_dir" && \
    -x "$NODE_BIN" && -x "$NPM_BIN" ]] && node_tools_supported; then
    cleanup_node_stage
    trap - EXIT INT TERM
    return
  fi
  "$CURL_BIN" --fail --silent --show-error --location \
    --proto '=https' --tlsv1.2 \
    "https://nodejs.org/download/release/v${PINNED_NODE_VERSION}/${archive}" \
    --output "$temporary_archive"
  calculated="$($PYTHON_BIN - "$temporary_archive" <<'PY'
import hashlib
import pathlib
import sys

digest = hashlib.sha256()
with pathlib.Path(sys.argv[1]).open("rb") as source:
    for chunk in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(chunk)
print(digest.hexdigest())
PY
)"
  [[ "$calculated" == "$checksum" ]] || die "pinned Node.js archive checksum mismatch"
  install -d -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0755 "$extracted"
  tar -C "$extracted" --strip-components=1 -xzf "$temporary_archive"
  chown -R "$ROOT_OWNER:$ROOT_GROUP_OWNER" "$extracted"
  [[ -x "$extracted/bin/node" && -x "$extracted/bin/npm" ]] || \
    die "downloaded pinned Node.js toolchain is incomplete"
  if [[ -e "$toolchain_dir" || -L "$toolchain_dir" ]]; then
    [[ ! -e "$old_toolchain" && ! -L "$old_toolchain" ]] || \
      die "safe Node.js repair staging path already exists: $old_toolchain"
    mv -- "$toolchain_dir" "$old_toolchain"
  fi
  if ! mv -- "$extracted" "$toolchain_dir"; then
    [[ ! -e "$old_toolchain" && ! -L "$old_toolchain" ]] || \
      mv -- "$old_toolchain" "$toolchain_dir"
    die "could not activate the pinned Node.js toolchain"
  fi
  if ! node_tools_supported; then
    rm -rf -- "$toolchain_dir"
    [[ ! -e "$old_toolchain" && ! -L "$old_toolchain" ]] || \
      mv -- "$old_toolchain" "$toolchain_dir"
    die "the pinned Node.js toolchain failed its version/npm check"
  fi
  [[ ! -e "$old_toolchain" && ! -L "$old_toolchain" ]] || rm -rf -- "$old_toolchain"
  rm -rf -- "$temporary_root"
  rmdir -- "$lock_dir"
  trap - EXIT INT TERM
}

select_node_tools() {
  if node_tools_supported; then
    return
  fi
  if [[ -n "${HERMES_CONTROL_INSTALL_NODE:-}" || -n "${HERMES_CONTROL_INSTALL_NPM:-}" ]]; then
    die "the explicitly configured Node.js/npm tools are missing or older than Node 20"
  fi
  install_pinned_node_toolchain
}

install_remaining_packages() {
  if [[ "$TESTING" == "1" ]]; then
    PYTHON_BIN="${HERMES_CONTROL_INSTALL_PYTHON:-$(command -v python3)}"
  else
    PYTHON_BIN="$(find_compatible_python 1 || true)"
    local need_python=0 need_venv=0
    if [[ -z "$PYTHON_BIN" ]]; then
      PYTHON_BIN="$(find_compatible_python 0 || true)"
      if [[ -n "$PYTHON_BIN" ]]; then
        need_venv=1
      else
        need_python=1
      fi
    fi
    local missing_utilities=()
    command -v "$CURL_BIN" >/dev/null 2>&1 || missing_utilities+=(curl)
    command -v "$SQLITE_BIN" >/dev/null 2>&1 || missing_utilities+=(sqlite3)
    command -v ssh >/dev/null 2>&1 || missing_utilities+=(ssh)
    command -v tar >/dev/null 2>&1 || missing_utilities+=(tar)
    if ((need_python || need_venv || ${#missing_utilities[@]})); then
      if ((DRY_RUN)); then
        die "dry-run found missing dependencies: Python 3.12-3.14 with venv, curl, sqlite3, tar and OpenSSH client"
      fi
      local manager="" selected_python_package="" selected_venv_package=""
      local version
      if command -v apt-get >/dev/null 2>&1; then
        manager=apt
        apt-get update
        if ((need_python)); then
          for version in 3.14 3.13 3.12; do
            if apt_package_available "python${version}" && \
              apt_package_available "python${version}-venv"; then
              selected_python_package="python${version}"
              selected_venv_package="python${version}-venv"
              break
            fi
          done
        elif ((need_venv)); then
          version="$(python_minor "$PYTHON_BIN")"
          apt_package_available "python${version}-venv" || \
            die "the configured apt repositories do not offer python${version}-venv; install it explicitly"
          selected_venv_package="python${version}-venv"
        fi
        if ((need_python)) && [[ -z "$selected_python_package" ]]; then
          die "the configured apt repositories offer no versioned Python 3.12-3.14 with venv; no generic python3 package was installed"
        fi
        local apt_packages=(ca-certificates)
        [[ " ${missing_utilities[*]} " == *" curl " ]] && apt_packages+=(curl)
        [[ " ${missing_utilities[*]} " == *" sqlite3 " ]] && apt_packages+=(sqlite3)
        [[ " ${missing_utilities[*]} " == *" ssh " ]] && apt_packages+=(openssh-client)
        [[ " ${missing_utilities[*]} " == *" tar " ]] && apt_packages+=(tar)
        [[ -z "$selected_python_package" ]] || apt_packages+=("$selected_python_package")
        [[ -z "$selected_venv_package" ]] || apt_packages+=("$selected_venv_package")
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${apt_packages[@]}"
      elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
        if command -v dnf >/dev/null 2>&1; then manager=dnf; else manager=yum; fi
        if ((need_python)); then
          for version in 3.14 3.13 3.12; do
            if "$manager" -q list --available "python${version}" >/dev/null 2>&1; then
              selected_python_package="python${version}"
              break
            fi
          done
          [[ -n "$selected_python_package" ]] || \
            die "the configured $manager repositories offer no versioned Python 3.12-3.14; no generic python3 package was installed"
        elif ((need_venv)); then
          die "$(python_minor "$PYTHON_BIN") lacks venv; install its versioned venv support explicitly"
        fi
        local rpm_packages=(ca-certificates)
        [[ " ${missing_utilities[*]} " == *" curl " ]] && rpm_packages+=(curl)
        [[ " ${missing_utilities[*]} " == *" sqlite3 " ]] && rpm_packages+=(sqlite)
        [[ " ${missing_utilities[*]} " == *" ssh " ]] && rpm_packages+=(openssh-clients)
        [[ " ${missing_utilities[*]} " == *" tar " ]] && rpm_packages+=(tar)
        [[ -z "$selected_python_package" ]] || rpm_packages+=("$selected_python_package")
        "$manager" install -y "${rpm_packages[@]}"
      else
        die "install Python 3.12-3.14 with venv, curl, sqlite3, tar, OpenSSH client and CA certificates first"
      fi
      PYTHON_BIN="$(find_compatible_python 1 || true)"
      [[ -n "$PYTHON_BIN" ]] || die "the selected versioned package did not provide Python 3.12-3.14"
    fi
  fi
  python_has_venv "$PYTHON_BIN" || die "selected Python does not provide venv support"
  command -v "$CURL_BIN" >/dev/null || die "curl is required"
  command -v "$SQLITE_BIN" >/dev/null || die "sqlite3 is required"
  command -v tar >/dev/null || die "tar is required"
  command -v ssh >/dev/null || die "OpenSSH client is required"

  if ((BUILD_WEB)); then
    select_node_tools
  fi
}

discover_tailscale_hostname() {
  command -v "$TAILSCALE_BIN" >/dev/null || die "Tailscale must already be installed"
  local status_json discovered_hostname
  status_json="$($TAILSCALE_BIN status --json)" || die "Tailscale is not connected"
  discovered_hostname="$(printf '%s' "$status_json" | "$PYTHON_BIN" -c '
import json, sys
data = json.load(sys.stdin)
state = data.get("BackendState")
if state not in (None, "Running"):
    raise SystemExit(1)
self_node = data.get("Self") or data.get("SelfNode") or {}
if self_node.get("Online") is False:
    raise SystemExit(1)
name = str(self_node.get("DNSName") or "").rstrip(".").lower()
print(name)
')" || die "Tailscale is not connected"
  if [[ -n "$TAILSCALE_HOSTNAME" ]]; then
    TAILSCALE_HOSTNAME="${TAILSCALE_HOSTNAME%.}"
    TAILSCALE_HOSTNAME="$(printf '%s' "$TAILSCALE_HOSTNAME" | tr '[:upper:]' '[:lower:]')"
    [[ "$TAILSCALE_HOSTNAME" == "$discovered_hostname" ]] || \
      die "--tailscale-hostname does not match this connected Tailscale node"
  else
    TAILSCALE_HOSTNAME=$discovered_hostname
  fi
  [[ "$TAILSCALE_HOSTNAME" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*\.ts\.net$ ]] || \
    die "could not determine a valid Tailscale MagicDNS hostname"
  TAILSCALE_ORIGIN="https://${TAILSCALE_HOSTNAME}"
}

classify_serve_config() {
  local hostname=$1 target=$2
  "$PYTHON_BIN" -c '
import json, sys

hostname, target = sys.argv[1:3]
try:
    data = json.load(sys.stdin)
except Exception:
    print("invalid")
    raise SystemExit(0)

allow_funnel = data.get("AllowFunnel") or {}
if (
    isinstance(allow_funnel, dict)
    and any(bool(value) for value in allow_funnel.values())
) or (not isinstance(allow_funnel, dict) and bool(allow_funnel)):
    print("funnel")
    raise SystemExit(0)

web = data.get("Web") or {}
host = web.get(f"{hostname}:443") or {}
handlers = host.get("Handlers") or {}
root = handlers.get("/")
if isinstance(root, dict):
    if root.get("Proxy") != target:
        print("root-conflict")
        raise SystemExit(0)
    exact_root = set(root) == {"Proxy"}
    exact_handlers = set(handlers) == {"/"}
    exact_host = set(host) == {"Handlers"}
    exact_web = set(web) == {f"{hostname}:443"}
    tcp = data.get("TCP") or {}
    tcp_443 = tcp.get("443") or tcp.get(443)
    exact_tcp = not tcp or (
        len(tcp) == 1
        and isinstance(tcp_443, dict)
        and tcp_443.get("HTTPS") is True
        and set(tcp_443) == {"HTTPS"}
    )
    no_other_state = not any(bool(data.get(key)) for key in ("Services", "Foreground"))
    if exact_root and exact_handlers and exact_host and exact_web and exact_tcp and no_other_state:
        print("control")
    else:
        print("other-config")
    raise SystemExit(0)

meaningful = any(bool(data.get(key)) for key in ("TCP", "Web", "Services", "Foreground"))
print("other-config" if meaningful else "empty")
' "$hostname" "$target"
}

inspect_tailscale_serve() {
  local status_json
  status_json="$($TAILSCALE_BIN serve status --json)" || \
    die "could not inspect Tailscale Serve configuration"
  TAILSCALE_STATE="$(printf '%s' "$status_json" | classify_serve_config \
    "$TAILSCALE_HOSTNAME" "$CONTROL_TARGET")"
  case "$TAILSCALE_STATE" in
    empty|control) ;;
    funnel) die "Tailscale Funnel is active; refusing to expose Agent Control" ;;
    root-conflict) die "Tailscale Serve root is already owned; refusing to replace or reuse it" ;;
    other-config) die "Tailscale Serve has existing routes; refusing to merge or replace them" ;;
    *) die "unrecognized Tailscale Serve status JSON" ;;
  esac
}

get_passwd_record() {
  local user=$1
  if [[ "$TESTING" == "1" ]]; then
    printf '%s:x:1000:1000::%s:/bin/sh\n' "$user" \
      "${HERMES_CONTROL_INSTALL_HERMES_HOME:-/home/$user}"
  else
    getent passwd "$user"
  fi
}

validate_control_account_records() {
  local passwd_record=$1 group_record=$2 uid_min=$3 gid_min=$4 primary_users=$5 groups=$6
  if [[ -z "$passwd_record" && -z "$group_record" ]]; then
    return 0
  fi
  [[ "$uid_min" =~ ^[0-9]+$ && "$gid_min" =~ ^[0-9]+$ ]] || \
    die "could not determine safe system account ID boundaries"
  local group_name group_gid group_members
  IFS=: read -r group_name _ group_gid group_members <<<"$group_record"
  [[ "$group_name" == "$CONTROL_GROUP" && "$group_gid" =~ ^[0-9]+$ ]] || \
    die "existing $CONTROL_GROUP group record is malformed"
  ((group_gid > 0 && group_gid < gid_min)) || \
    die "$CONTROL_GROUP must be a non-root system group"
  [[ -z "$group_members" || "$group_members" == "$CONTROL_USER" ]] || \
    die "$CONTROL_GROUP grants secret access to unexpected members"
  [[ -n "$passwd_record" ]] || {
    [[ -z "$primary_users" ]] || die "$CONTROL_GROUP is the primary group of another account"
    return 0
  }
  local user uid primary_gid home shell
  IFS=: read -r user _ uid primary_gid _ home shell <<<"$passwd_record"
  [[ "$user" == "$CONTROL_USER" && "$uid" =~ ^[0-9]+$ && \
    "$primary_gid" =~ ^[0-9]+$ ]] || die "existing $CONTROL_USER account record is malformed"
  ((uid > 0 && uid < uid_min)) || die "$CONTROL_USER must be a non-root system account"
  [[ "$primary_gid" == "$group_gid" ]] || \
    die "$CONTROL_USER must use $CONTROL_GROUP as its primary group"
  [[ "$home" == /var/lib/hermes-control ]] || \
    die "$CONTROL_USER must use /var/lib/hermes-control as its home"
  case "$shell" in
    /usr/sbin/nologin|/sbin/nologin|/bin/false|/usr/bin/false) ;;
    *) die "$CONTROL_USER must use a non-login shell" ;;
  esac
  [[ "$primary_users" == "$CONTROL_USER" ]] || \
    die "$CONTROL_GROUP is shared as a primary group with another account"
  [[ "$groups" == "$CONTROL_GROUP" ]] || \
    die "$CONTROL_USER must not belong to supplementary groups"
}

preflight_control_account() {
  if [[ "$TESTING" == "1" ]]; then
    CONTROL_OWNER="$(id -un)"
    CONTROL_GROUP_OWNER="$(id -gn)"
    ROOT_OWNER=$CONTROL_OWNER
    ROOT_GROUP_OWNER=$CONTROL_GROUP_OWNER
    return
  fi
  local passwd_record group_record uid_min gid_min group_gid primary_users groups
  passwd_record="$(getent passwd "$CONTROL_USER" || true)"
  group_record="$(getent group "$CONTROL_GROUP" || true)"
  [[ -z "$passwd_record" || -n "$group_record" ]] || \
    die "$CONTROL_USER exists without its dedicated $CONTROL_GROUP group"
  uid_min="$(awk '$1 == "UID_MIN" {print $2; exit}' /etc/login.defs 2>/dev/null || true)"
  gid_min="$(awk '$1 == "GID_MIN" {print $2; exit}' /etc/login.defs 2>/dev/null || true)"
  uid_min=${uid_min:-1000}
  gid_min=${gid_min:-1000}
  primary_users=""
  groups=""
  if [[ -n "$group_record" ]]; then
    group_gid="$(printf '%s\n' "$group_record" | awk -F: '{print $3}')"
    primary_users="$(getent passwd | awk -F: -v gid="$group_gid" '$4 == gid {print $1}' | paste -sd, -)"
  fi
  if [[ -n "$passwd_record" ]]; then
    groups="$(id -Gn "$CONTROL_USER" | tr ' ' ',' | sed 's/,$//')"
  fi
  validate_control_account_records "$passwd_record" "$group_record" \
    "$uid_min" "$gid_min" "$primary_users" "$groups"
}

discover_hermes_identity() {
  if [[ -z "$HERMES_USER" ]]; then
    if get_passwd_record hermes >/dev/null 2>&1; then
      HERMES_USER=hermes
    else
      local matches=()
      while IFS= read -r match; do
        matches+=("$match")
      done < <(find /home -maxdepth 7 -type f \
        -path '*/.hermes/hermes-agent/venv/bin/python' -print 2>/dev/null)
      ((${#matches[@]} == 1)) || \
        die "could not discover the Hermes owner; pass --hermes-user"
      local candidate_home="${matches[0]%%/.hermes/*}"
      HERMES_USER="$(getent passwd | awk -F: -v home="$candidate_home" '$6 == home {print $1; exit}')"
    fi
  fi
  valid_user "$HERMES_USER" || die "invalid Hermes user: $HERMES_USER"
  local passwd_record
  passwd_record="$(get_passwd_record "$HERMES_USER" || true)"
  [[ -n "$passwd_record" ]] || die "Hermes user does not exist: $HERMES_USER"
  HERMES_HOME="$(printf '%s\n' "$passwd_record" | awk -F: 'NR == 1 {print $6}')"
  [[ "$HERMES_HOME" == /* ]] || die "Hermes home directory is invalid"
  [[ "$HERMES_HOME" =~ ^/[A-Za-z0-9_./@+-]+$ && "$HERMES_HOME" != *".."* ]] || \
    die "Hermes home directory is unsafe for a systemd unit"
  HERMES_FS_HOME="$(rooted "$HERMES_HOME")"
  if [[ "$TESTING" == "1" ]]; then
    HERMES_GROUP=$HERMES_USER
  else
    HERMES_GROUP="$(id -gn "$HERMES_USER")"
  fi

  if [[ -z "$HERMES_BIN" ]]; then
    local candidate
    for candidate in \
      "$HERMES_HOME/.hermes/hermes-agent/venv/bin/hermes" \
      "$HERMES_HOME/.hermes/hermes-agent/venv/bin/python" \
      /usr/local/bin/hermes /usr/bin/hermes; do
      if [[ -x "$(rooted "$candidate")" ]]; then
        HERMES_BIN=$candidate
        break
      fi
    done
  fi
  [[ -n "$HERMES_BIN" ]] || die "could not discover Hermes; pass --hermes-bin"
  valid_absolute_command "$HERMES_BIN" || die "Hermes binary path is unsafe"
  [[ -x "$(rooted "$HERMES_BIN")" ]] || die "Hermes binary is not executable: $HERMES_BIN"
  case "$(basename "$HERMES_BIN")" in
    python|python3|python3.[0-9]|python3.1[0-9]) HERMES_COMMAND_KIND=python ;;
    *) HERMES_COMMAND_KIND=executable ;;
  esac

  local profile_root="$HERMES_FS_HOME/.hermes/profiles"
  local profile profile_count=0 only_profile=""
  PROFILE_LIST=""
  if [[ -d "$profile_root" ]]; then
    while IFS= read -r profile; do
      valid_name "$profile" || die "unsafe Hermes profile directory name: $profile"
      ((profile_count += 1))
      ((profile_count <= 64)) || die "Hermes has more than the supported 64 profiles"
      only_profile="$profile"
      if [[ -n "$PROFILE_LIST" ]]; then
        PROFILE_LIST+=","
      fi
      PROFILE_LIST+="$profile"
    done < <(
      for profile_path in "$profile_root"/*; do
        [[ -d "$profile_path" ]] || continue
        basename "$profile_path"
      done | LC_ALL=C sort
    )
  fi
  if [[ -z "$HERMES_PROFILE" ]]; then
    if [[ -d "$profile_root/default" ]]; then
      HERMES_PROFILE=default
    elif ((profile_count == 1)); then
      HERMES_PROFILE="$only_profile"
    else
      die "could not choose among Hermes profiles; pass --hermes-profile"
    fi
  fi
  valid_name "$HERMES_PROFILE" || die "invalid Hermes profile: $HERMES_PROFILE"
  if [[ ! -d "$profile_root/$HERMES_PROFILE" && "$TESTING" != "1" ]]; then
    die "Hermes profile does not exist: $HERMES_PROFILE"
  fi
  if ((profile_count == 0)); then
    PROFILE_LIST=$HERMES_PROFILE
  fi
}

ensure_control_account_and_paths() {
  if [[ "$TESTING" == "1" ]]; then
    CONTROL_OWNER="$(id -un)"
    CONTROL_GROUP_OWNER="$(id -gn)"
    ROOT_OWNER=$CONTROL_OWNER
    ROOT_GROUP_OWNER=$CONTROL_GROUP_OWNER
  else
    getent group "$CONTROL_GROUP" >/dev/null || groupadd --system "$CONTROL_GROUP"
    if ! id -u "$CONTROL_USER" >/dev/null 2>&1; then
      useradd --system --gid "$CONTROL_GROUP" --home-dir /var/lib/hermes-control \
        --no-create-home --shell /usr/sbin/nologin "$CONTROL_USER"
    fi
    preflight_control_account
  fi
  install -d -o "$CONTROL_OWNER" -g "$CONTROL_GROUP_OWNER" -m 0700 \
    "$(rooted /var/lib/hermes-control)" "$(rooted /var/backups/hermes-control)"
  install -d -o "$ROOT_OWNER" -g "$CONTROL_GROUP_OWNER" -m 0750 \
    "$(rooted /etc/hermes-control)"
  install -d -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0755 \
    "$(rooted /opt/hermes-control)" "$(rooted /opt/hermes-control/releases)" \
    "$(rooted /etc/systemd/system)"
}

resolve_release_inputs() {
  SOURCE_ROOT="$(cd -- "$SOURCE_ROOT" && pwd -P)"
  [[ -f "$SOURCE_ROOT/apps/api/pyproject.toml" ]] || die "source is missing apps/api"
  [[ -f "$SOURCE_ROOT/packages/hermes-client/pyproject.toml" ]] || die "source is missing Hermes client"
  [[ -f "$SOURCE_ROOT/deploy/systemd/hermes-control.service" ]] || die "source is missing systemd assets"
  if command -v git >/dev/null 2>&1 && \
    git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    SOURCE_IS_GIT=1
    [[ -z "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=no)" ]] || \
      die "source has tracked changes; build from a reviewed commit"
  fi
  if ((SOURCE_IS_GIT && STATIC_SOURCE_EXPLICIT == 0)); then
    [[ -f "$SOURCE_ROOT/package-lock.json" && -f "$SOURCE_ROOT/apps/web/package.json" ]] || \
      die "Git source must contain the committed lockfile and PWA package needed to build HEAD"
    STATIC_SOURCE=""
    BUILD_WEB=1
  elif [[ -z "$STATIC_SOURCE" ]]; then
    if [[ -f "$SOURCE_ROOT/apps/api/static/index.html" ]]; then
      STATIC_SOURCE="$SOURCE_ROOT/apps/api/static"
    elif [[ -f "$SOURCE_ROOT/apps/web/dist/index.html" ]]; then
      STATIC_SOURCE="$SOURCE_ROOT/apps/web/dist"
    else
      [[ -f "$SOURCE_ROOT/package-lock.json" && -f "$SOURCE_ROOT/apps/web/package.json" ]] || \
        die "source has neither a prebuilt PWA nor the files required to build it"
      BUILD_WEB=1
    fi
  fi
  if ((BUILD_WEB == 0)); then
    STATIC_SOURCE="$(cd -- "$STATIC_SOURCE" && pwd -P)"
    [[ -f "$STATIC_SOURCE/index.html" ]] || die "static directory has no index.html"
  fi
  if [[ -z "$RELEASE_ID" ]]; then
    ((SOURCE_IS_GIT)) || die "pass --release-id for a source artifact without Git"
    RELEASE_ID="$(git -C "$SOURCE_ROOT" rev-parse --verify HEAD 2>/dev/null || true)"
    [[ -n "$RELEASE_ID" ]] || die "pass --release-id for a source artifact without Git"
  fi
  [[ "$RELEASE_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]] || die "invalid release id"
}

install_release() {
  local releases current release marker
  releases="$(rooted /opt/hermes-control/releases)"
  current="$(rooted /opt/hermes-control/current)"
  release="$releases/$RELEASE_ID"
  marker="$release/.agent-control-release"

  if [[ -e "$current" || -L "$current" ]]; then
    [[ -L "$current" ]] || die "/opt/hermes-control/current exists and is not a symlink"
    local selected
    selected="$(cd -- "$current" && pwd -P)"
    [[ "$selected" == "$release" ]] || \
      die "another release is active; use the reviewed update runbook instead of this installer"
  fi
  if [[ -d "$release" ]]; then
    [[ -f "$marker" ]] || die "release directory exists without installer marker: $release"
    [[ "$(<"$marker")" == "$RELEASE_ID" ]] || die "release marker mismatch"
    [[ -x "$release/.venv/bin/uvicorn" && -f "$release/apps/api/static/index.html" ]] || \
      die "installed release is incomplete: $release"
  else
    local stage="$releases/.${RELEASE_ID}.partial.$$"
    [[ ! -e "$stage" ]] || die "staging path already exists: $stage"
    install -d -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0755 "$stage"
    cleanup_release_stage() { rm -rf -- "$stage"; }
    trap cleanup_release_stage EXIT INT TERM
    if ((SOURCE_IS_GIT)); then
      git -C "$SOURCE_ROOT" archive --format=tar HEAD | tar -C "$stage" -xf -
    else
      tar -C "$SOURCE_ROOT" \
        --exclude='./.git' --exclude='./.venv' --exclude='./node_modules' \
        --exclude='./artifacts' --exclude='./test-results' \
        --exclude='./.env' --exclude='./.env.*' --exclude='*.db' \
        --exclude='*.db-wal' --exclude='*.db-shm' -cf - . | tar -C "$stage" -xf -
    fi
    rm -rf -- "$stage/apps/api/static"
    install -d -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0755 \
      "$stage/apps/api/static"
    if ((BUILD_WEB)); then
      (
        cd -- "$stage"
        run_npm ci
        run_npm run build
      )
      [[ -f "$stage/apps/web/dist/index.html" ]] || die "PWA build produced no index.html"
      cp -a "$stage/apps/web/dist/." "$stage/apps/api/static/"
    else
      cp -a "$STATIC_SOURCE/." "$stage/apps/api/static/"
    fi
    "$PYTHON_BIN" -m venv "$stage/.venv"
    "$stage/.venv/bin/python" -m pip install --upgrade pip
    "$stage/.venv/bin/python" -m pip install \
      "$stage/packages/hermes-client" "$stage/apps/api"
    printf '%s\n' "$RELEASE_ID" >"$stage/.agent-control-release"
    chmod 0644 "$stage/.agent-control-release"
    chown -R "$ROOT_OWNER:$ROOT_GROUP_OWNER" "$stage"
    mv -- "$stage" "$release"
    trap - EXIT INT TERM
  fi
  if [[ ! -L "$current" ]]; then
    local next_link="$(rooted /opt/hermes-control/.current.$$)"
    ln -s "$release" "$next_link"
    mv -- "$next_link" "$current"
  fi
}

env_value() {
  local path=$1 key=$2
  [[ -f "$path" ]] || return 0
  awk -v wanted="$key" '
    /^[[:space:]]*#/ { next }
    index($0, wanted "=") == 1 { print substr($0, length(wanted) + 2); exit }
  ' "$path"
}

env_key_exists() {
  local path=$1 key=$2
  [[ -f "$path" ]] || return 1
  awk -v wanted="$key" '
    /^[[:space:]]*#/ { next }
    index($0, wanted "=") == 1 { found=1; exit }
    END { exit !found }
  ' "$path"
}

validate_environment_file() {
  local path=$1 expected_uid=$2 expected_gid=$3 expected_mode=$4 label=$5
  [[ ! -e "$path" && ! -L "$path" ]] && return 0
  local failure
  if ! failure="$("$PYTHON_BIN" - "$path" "$expected_uid" "$expected_gid" \
    "$expected_mode" "$label" 2>&1 <<'PY'
import os
import pathlib
import re
import stat
import sys

path = pathlib.Path(sys.argv[1])
expected_uid = int(sys.argv[2])
expected_gid = int(sys.argv[3])
expected_mode = int(sys.argv[4], 8)
label = sys.argv[5]
metadata = path.lstat()
if stat.S_ISLNK(metadata.st_mode):
    raise SystemExit(f"{label} must not be a symlink")
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit(f"{label} must be a regular file")
mode = stat.S_IMODE(metadata.st_mode)
if mode != expected_mode:
    raise SystemExit(f"{label} must have mode {expected_mode:04o}, not {mode:04o}")
if metadata.st_uid != expected_uid or metadata.st_gid != expected_gid:
    raise SystemExit(f"{label} has an unsafe owner or group")
raw = path.read_bytes()
if b"\x00" in raw or b"\r" in raw:
    raise SystemExit(f"{label} contains unsupported control characters")
try:
    text = raw.decode("utf-8", "strict")
except UnicodeDecodeError:
    raise SystemExit(f"{label} is not valid UTF-8") from None
seen = set()
for number, line in enumerate(text.split("\n"), 1):
    if not line or line.startswith("#"):
        continue
    match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
    if not match:
        raise SystemExit(f"{label} has an unsafe line at {number}")
    key = match.group(1)
    if key in seen:
        raise SystemExit(f"{label} contains duplicate key {key}")
    seen.add(key)
PY
)"; then
    die "$failure"
  fi
}

validate_state_path() {
  local path=$1 expected_uid=$2 expected_gid=$3 expected_mode=$4 kind=$5 label=$6
  [[ ! -e "$path" && ! -L "$path" ]] && return 0
  local failure
  if ! failure="$("$PYTHON_BIN" - "$path" "$expected_uid" "$expected_gid" \
    "$expected_mode" "$kind" "$label" 2>&1 <<'PY'
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
uid, gid = int(sys.argv[2]), int(sys.argv[3])
expected_mode = int(sys.argv[4], 8)
kind, label = sys.argv[5:]
metadata = path.lstat()
if stat.S_ISLNK(metadata.st_mode):
    raise SystemExit(f"{label} must not be a symlink")
if kind == "file" and not stat.S_ISREG(metadata.st_mode):
    raise SystemExit(f"{label} must be a regular file")
if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
    raise SystemExit(f"{label} must be a directory")
mode = stat.S_IMODE(metadata.st_mode)
if mode != expected_mode:
    raise SystemExit(f"{label} must have mode {expected_mode:04o}, not {mode:04o}")
if metadata.st_uid != uid or metadata.st_gid != gid:
    raise SystemExit(f"{label} has an unsafe owner or group")
PY
)"; then
    die "$failure"
  fi
}

replace_env_value() {
  local path=$1 key=$2 value=$3
  "$PYTHON_BIN" - "$path" "$key" "$value" <<'PY'
import os
import pathlib
import stat
import sys

path = pathlib.Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
if not key or "\n" in value or "\r" in value:
    raise SystemExit("unsafe environment update")
lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
prefix = f"{key}="
matches = [index for index, line in enumerate(lines) if line.startswith(prefix)]
if len(matches) > 1:
    raise SystemExit(f"duplicate {key} entries in {path}")
replacement = f"{prefix}{value}\n"
if matches:
    lines[matches[0]] = replacement
else:
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += "\n"
    lines.append(replacement)
metadata = path.stat()
temporary = path.with_name(f".{path.name}.media.{os.getpid()}")
temporary.write_text("".join(lines), encoding="utf-8")
os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
os.chown(temporary, metadata.st_uid, metadata.st_gid)
os.replace(temporary, path)
PY
}

generate_token() {
  "$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))'
}

generate_vault_key() {
  "$PYTHON_BIN" -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'
}

hermes_port_ready() {
  # Hermes serve intentionally returns 404 at `/`; a successful transport is
  # enough for ownership detection. Authentication is verified separately.
  "$CURL_BIN" --silent --show-error --output /dev/null --max-time 2 "$HERMES_TARGET/" \
    >/dev/null 2>&1
}

tcp_port_claimed() {
  local port=$1
  if [[ -n "$PORT_PROBE_BIN" ]]; then
    "$PORT_PROBE_BIN" "$port"
  else
    (exec 7<>"/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1
  fi
}

hermes_port_claimed() {
  hermes_port_ready || tcp_port_claimed 9119
}

control_port_ready() {
  "$CURL_BIN" --fail --silent --show-error --output /dev/null --max-time 2 \
    "$CONTROL_TARGET/api/v1/health" >/dev/null 2>&1
}

control_port_claimed() {
  control_port_ready || tcp_port_claimed 8000
}

control_unit_exists() {
  local unit=$1
  [[ -e "$(rooted "/etc/systemd/system/$unit")" || \
    -L "$(rooted "/etc/systemd/system/$unit")" ]] || \
    "$SYSTEMCTL_BIN" cat "$unit" >/dev/null 2>&1
}

preflight_managed_control_installation() {
  local current releases selected marker unit installed_unit dropin
  current="$(rooted /opt/hermes-control/current)"
  releases="$(rooted /opt/hermes-control/releases)"
  if [[ ! -e "$current" && ! -L "$current" ]]; then
    for unit in hermes-control.service hermes-control-backup.service hermes-control-backup.timer; do
      control_unit_exists "$unit" && \
        die "$unit already exists without a managed Agent Control release"
    done
    control_port_claimed && \
      die "port 8000 is already owned without a managed Agent Control installation"
    return 0
  fi
  [[ -L "$current" && -d "$current" ]] || \
    die "/opt/hermes-control/current must be a non-dangling installer-managed symlink"
  selected="$(cd -- "$current" && pwd -P)"
  case "$selected" in
    "$releases"/*) ;;
    *) die "/opt/hermes-control/current points outside the managed releases directory" ;;
  esac
  [[ "$(dirname -- "$selected")" == "$releases" ]] || \
    die "/opt/hermes-control/current points outside the managed releases directory"
  marker="$selected/.agent-control-release"
  [[ -f "$marker" && ! -L "$marker" ]] || \
    die "the active Agent Control release has no safe installer marker"
  [[ "$(<"$marker")" == "$(basename -- "$selected")" ]] || \
    die "the active Agent Control release marker does not match its directory"
  [[ "$(basename -- "$selected")" == "$RELEASE_ID" ]] || \
    die "another release is active; use the reviewed update runbook instead of this installer"
  [[ -x "$selected/.venv/bin/uvicorn" && -f "$selected/apps/api/static/index.html" ]] || \
    die "the active Agent Control release is incomplete"
  CONTROL_INSTALLATION_EXISTS=1
  for unit in hermes-control.service hermes-control-backup.service hermes-control-backup.timer; do
    installed_unit="$(rooted "/etc/systemd/system/$unit")"
    if control_unit_exists "$unit"; then
      [[ -f "$installed_unit" && ! -L "$installed_unit" ]] || \
        die "$unit exists outside the installer-managed systemd path"
      [[ -f "$selected/deploy/systemd/$unit" ]] || \
        die "the active release is missing $unit"
      cmp -s -- "$selected/deploy/systemd/$unit" "$installed_unit" || \
        die "$unit differs from the active immutable release"
      [[ "$unit" != hermes-control.service ]] || CONTROL_SERVICE_PREEXISTED=1
    fi
  done
  if control_port_claimed && ((CONTROL_SERVICE_PREEXISTED == 0)); then
    die "port 8000 is already owned but hermes-control.service is not installer-managed"
  fi
  dropin="$(rooted /etc/systemd/system/hermes-control.service.d/media-root.conf)"
  if [[ -e "$dropin" || -L "$dropin" ]]; then
    [[ -f "$dropin" && ! -L "$dropin" ]] || \
      die "the Agent Control media drop-in must not be a symlink"
    grep -q '^# Managed by Agent Control Linux installer$' "$dropin" || \
      die "the Agent Control media drop-in is not installer-managed"
  fi
}

verify_existing_control_listener() {
  control_port_claimed || return 0
  control_port_ready || \
    die "port 8000 is open but does not answer the Agent Control health contract"
}

validate_hermes_token() {
  (( ${#HERMES_TOKEN} >= 32 && ${#HERMES_TOKEN} <= 512 )) && \
    [[ "$HERMES_TOKEN" =~ ^[A-Za-z0-9._~-]+$ ]] || \
    die "Hermes dashboard token must be 32-512 URL-safe characters"
}

verify_hermes_authenticated() {
  validate_hermes_token
  local http_code
  http_code="$(
    printf 'url = "%s/api/profiles"\nheader = "X-Hermes-Session-Token: %s"\n' \
      "$HERMES_TARGET" "$HERMES_TOKEN" | \
      "$CURL_BIN" --silent --output /dev/null --write-out '%{http_code}' \
        --max-time 5 --config -
  )" || die "Hermes authenticated probe failed"
  [[ "$http_code" =~ ^2[0-9][0-9]$ ]] || \
    die "Hermes rejected the dashboard token (HTTP $http_code)"
}

managed_hermes_unit() {
  local path="$(rooted /etc/systemd/system/hermes-serve.service)"
  [[ -f "$path" && ! -L "$path" ]] && \
    grep -q '^# Managed by Agent Control Linux installer$' "$path" 2>/dev/null
}

hermes_unit_exists() {
  [[ -e "$(rooted /etc/systemd/system/hermes-serve.service)" ]] || \
    "$SYSTEMCTL_BIN" cat hermes-serve.service >/dev/null 2>&1
}

run_as_hermes() {
  if [[ "$TESTING" == "1" ]]; then
    "$@"
  else
    "$RUNUSER_BIN" -u "$HERMES_USER" -- "$@"
  fi
}

run_as_control() {
  if [[ "$TESTING" == "1" ]]; then
    "$@"
  else
    "$RUNUSER_BIN" -u "$CONTROL_USER" -- "$@"
  fi
}

resolve_media_access() {
  local media_fs_root="$HERMES_FS_HOME/.hermes/profiles"
  HERMES_MEDIA_ROOT=""
  if [[ -d "$media_fs_root" ]] && \
    run_as_control test -r "$media_fs_root" && \
    run_as_control test -x "$media_fs_root"; then
    HERMES_MEDIA_ROOT="$HERMES_HOME/.hermes/profiles"
    return
  fi
  note "Hermes media is disabled: $CONTROL_USER cannot read/traverse $HERMES_HOME/.hermes/profiles."
  note "No permissions or ACLs were widened; chat file downloads will remain unavailable."
}

verify_hermes_cli() {
  local fs_bin="$(rooted "$HERMES_BIN")"
  if [[ "$HERMES_COMMAND_KIND" == python ]]; then
    run_as_hermes "$fs_bin" -m hermes_cli.main -p "$HERMES_PROFILE" serve --help \
      >/dev/null
  else
    run_as_hermes "$fs_bin" -p "$HERMES_PROFILE" serve --help >/dev/null
  fi
}

render_hermes_unit() {
  local destination=$1
  local workdir
  if [[ "$HERMES_COMMAND_KIND" == python ]]; then
    workdir="$(dirname "$(dirname "$(dirname "$HERMES_BIN")")")"
  else
    workdir=$HERMES_HOME
  fi
  local command_prefix=$HERMES_BIN
  if [[ "$HERMES_COMMAND_KIND" == python ]]; then
    command_prefix="$command_prefix -m hermes_cli.main"
  fi
  cat >"$destination" <<EOF
# Managed by Agent Control Linux installer
[Unit]
Description=Hermes headless dashboard protocol server
Documentation=file:/opt/hermes-control/current/docs/operations/deployment.md
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$HERMES_USER
Group=$HERMES_GROUP
WorkingDirectory=$workdir
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$HERMES_HOME/.hermes/control-services/hermes-serve.env
ExecStart=$command_prefix -p $HERMES_PROFILE serve --host 127.0.0.1 --port 9119
Restart=on-failure
RestartSec=3s
TimeoutStopSec=30s
UMask=0077
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
LockPersonality=true
RestrictSUIDSGID=true
RestrictRealtime=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF
}

resolve_existing_hermes_token() {
  local control_env=$1 hermes_env=$2
  local candidates=()
  local value
  value="$(env_value "$hermes_env" HERMES_DASHBOARD_SESSION_TOKEN)"
  [[ -z "$value" ]] || candidates+=("$value")
  value="$(env_value "$control_env" HERMES_CONTROL_HERMES_DASHBOARD_TOKEN)"
  [[ -z "$value" ]] || candidates+=("$value")
  value="$INSTALL_HERMES_TOKEN"
  [[ -z "$value" ]] || candidates+=("$value")
  if ((${#candidates[@]})); then
    HERMES_TOKEN="${candidates[0]}"
    local candidate
    for candidate in "${candidates[@]}"; do
      [[ "$candidate" == "$HERMES_TOKEN" ]] || \
        die "existing Hermes/Control dashboard tokens disagree"
    done
    validate_hermes_token
  fi
}

validate_existing_control_environment() {
  local path=$1
  [[ -f "$path" ]] || return 0
  local key
  for key in \
    HERMES_CONTROL_DATABASE_URL \
    HERMES_CONTROL_DATABASE_PATH \
    HERMES_CONTROL_BACKUP_DIR \
    HERMES_CONTROL_ENVIRONMENT \
    HERMES_CONTROL_VAULT_KEY_B64 \
    HERMES_CONTROL_HERMES_DASHBOARD_TOKEN \
    HERMES_CONTROL_HERMES_DASHBOARD_URL \
    HERMES_CONTROL_HERMES_DASHBOARD_WS \
    HERMES_CONTROL_HERMES_API_URL \
    HERMES_CONTROL_HERMES_API_KEY \
    HERMES_CONTROL_HERMES_MEDIA_ROOT \
    HERMES_CONTROL_ALLOWED_ORIGINS \
    HERMES_CONTROL_SECURE_COOKIES \
    HERMES_CONTROL_CREATE_SCHEMA_ON_START \
    HERMES_CONTROL_PROVIDER_MODE \
    HERMES_CONTROL_MOCK_FALLBACK_ENABLED \
    HERMES_CONTROL_TRUST_PRIVATE_ENDPOINTS; do
    env_key_exists "$path" "$key" || die "existing control.env must explicitly define $key"
  done
  [[ "$(env_value "$path" HERMES_CONTROL_ENVIRONMENT)" == production ]] || \
    die "existing control.env must remain in production mode"
  local vault_key
  vault_key="$(env_value "$path" HERMES_CONTROL_VAULT_KEY_B64)"
  [[ -n "$vault_key" ]] || die "existing control.env has no vault key"
  printf '%s' "$vault_key" | "$PYTHON_BIN" -c '
import base64, binascii, sys
try:
    value = base64.b64decode(sys.stdin.buffer.read(), altchars=b"-_", validate=True)
except (binascii.Error, ValueError):
    raise SystemExit(1)
if len(value) != 32:
    raise SystemExit(1)
' || die "existing control.env vault key must decode to exactly 32 bytes"
  [[ -n "$HERMES_TOKEN" && \
    "$(env_value "$path" HERMES_CONTROL_HERMES_DASHBOARD_TOKEN)" == "$HERMES_TOKEN" ]] || \
    die "existing control.env dashboard token does not match the Hermes service token"
  [[ "$(env_value "$path" HERMES_CONTROL_HERMES_DASHBOARD_URL)" == "$HERMES_TARGET" ]] || \
    die "existing control.env dashboard URL must remain on loopback $HERMES_TARGET"
  [[ "$(env_value "$path" HERMES_CONTROL_HERMES_DASHBOARD_WS)" == "ws://127.0.0.1:9119/api/ws" ]] || \
    die "existing control.env dashboard WebSocket must remain on loopback port 9119"
  [[ -z "$(env_value "$path" HERMES_CONTROL_HERMES_API_URL)" ]] || \
    die "existing control.env must keep the legacy Hermes API URL empty"
  [[ -z "$(env_value "$path" HERMES_CONTROL_HERMES_API_KEY)" ]] || \
    die "existing control.env must keep the legacy Hermes API key empty"
  [[ "$(env_value "$path" HERMES_CONTROL_ALLOWED_ORIGINS)" == "$TAILSCALE_ORIGIN" ]] || \
    die "existing control.env allowed origin does not match this Tailscale node"
  [[ "$(env_value "$path" HERMES_CONTROL_SECURE_COOKIES)" == true ]] || \
    die "existing control.env must keep secure cookies enabled"
  [[ "$(env_value "$path" HERMES_CONTROL_CREATE_SCHEMA_ON_START)" == false ]] || \
    die "existing control.env must keep automatic schema creation disabled"
  [[ "$(env_value "$path" HERMES_CONTROL_PROVIDER_MODE)" == real ]] || \
    die "existing control.env must keep the real provider mode"
  [[ "$(env_value "$path" HERMES_CONTROL_MOCK_FALLBACK_ENABLED)" == false ]] || \
    die "existing control.env must keep mock fallback disabled"
  [[ "$(env_value "$path" HERMES_CONTROL_TRUST_PRIVATE_ENDPOINTS)" == true ]] || \
    die "existing control.env must keep private loopback endpoints enabled"
  local database_path backup_path database_url media_root
  database_path="$(rooted /var/lib/hermes-control/control.db)"
  backup_path="$(rooted /var/backups/hermes-control)"
  database_url="sqlite:///$database_path"
  [[ "$(env_value "$path" HERMES_CONTROL_DATABASE_PATH)" == "$database_path" ]] || \
    die "existing control.env database path is outside the managed location"
  [[ "$(env_value "$path" HERMES_CONTROL_DATABASE_URL)" == "$database_url" ]] || \
    die "existing control.env database URL does not match the managed SQLite path"
  [[ "$(env_value "$path" HERMES_CONTROL_BACKUP_DIR)" == "$backup_path" ]] || \
    die "existing control.env backup directory is outside the managed location"
  media_root="$(env_value "$path" HERMES_CONTROL_HERMES_MEDIA_ROOT)"
  [[ -z "$media_root" || "$media_root" == "$HERMES_HOME/.hermes/profiles" ]] || \
    die "existing control.env Hermes media path is outside the selected Hermes home"
  local existing_sha
  existing_sha="$(env_value "$path" HERMES_CONTROL_HERMES_SOURCE_SHA)"
  [[ -z "$existing_sha" || "$existing_sha" =~ ^[0-9a-fA-F]{40}$ ]] || \
    die "existing control.env has a malformed Hermes source SHA"
}

preflight_existing_configuration() {
  local control_env="$(rooted /etc/hermes-control/control.env)"
  local hermes_env="$(rooted "$HERMES_HOME/.hermes/control-services/hermes-serve.env")"
  local root_uid control_uid control_gid hermes_uid hermes_gid
  if [[ "$TESTING" == "1" ]]; then
    root_uid="$(id -u)"
    control_uid="$(id -u)"
    control_gid="$(id -g)"
    hermes_uid=$control_uid
    hermes_gid=$control_gid
  else
    root_uid=0
    control_uid=0
    control_gid=0
    if [[ -f "$control_env" ]]; then
      id -u "$CONTROL_USER" >/dev/null 2>&1 || \
        die "control.env exists without the $CONTROL_USER system account"
      getent group "$CONTROL_GROUP" >/dev/null || \
        die "control.env exists without the $CONTROL_GROUP system group"
      control_uid="$(id -u "$CONTROL_USER")"
      control_gid="$(getent group "$CONTROL_GROUP" | awk -F: '{print $3}')"
    fi
    hermes_uid="$(id -u "$HERMES_USER")"
    hermes_gid="$(id -g "$HERMES_USER")"
  fi
  validate_environment_file "$control_env" "$root_uid" "$control_gid" 0640 control.env
  validate_environment_file "$hermes_env" "$hermes_uid" "$hermes_gid" 0600 hermes-serve.env
  if [[ -f "$hermes_env" ]]; then
    env_key_exists "$hermes_env" HERMES_DASHBOARD_SESSION_TOKEN || \
      die "existing hermes-serve.env has no dashboard token"
  fi
  resolve_existing_hermes_token "$control_env" "$hermes_env"
  validate_existing_control_environment "$control_env"

  if [[ -f "$control_env" ]]; then
    validate_state_path "$(rooted /var/lib/hermes-control)" "$control_uid" \
      "$control_gid" 0700 directory "Agent Control state directory"
    validate_state_path "$(rooted /var/backups/hermes-control)" "$control_uid" \
      "$control_gid" 0700 directory "Agent Control backup directory"
    validate_state_path "$(rooted /var/lib/hermes-control/control.db)" "$control_uid" \
      "$control_gid" 0600 file "Agent Control database"
  fi

  if hermes_port_claimed || hermes_unit_exists; then
    [[ -n "$HERMES_TOKEN" ]] || \
      die "the existing Hermes service needs a dashboard token; set HERMES_CONTROL_INSTALL_HERMES_TOKEN without placing it in argv"
    validate_hermes_token
  fi
  if hermes_port_claimed; then
    hermes_port_ready || die "port 9119 is open but does not answer the Hermes dashboard HTTP contract"
    verify_hermes_authenticated
  fi
}

create_hermes_service_environment() {
  local hermes_env=$1
  local hermes_directory
  hermes_directory="$(dirname -- "$hermes_env")"
  local writer_script
  writer_script="$(mktemp)"
  cat >"$writer_script" <<'SH'
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

directory=$1
target=$2
install -d -m 0700 "$directory"
if [[ -e "$target" || -L "$target" ]]; then
  printf 'Hermes service environment already exists: %s\n' "$target" >&2
  exit 73
fi
temporary="$(mktemp "$directory/.hermes-serve.env.XXXXXX")"
cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT INT TERM
IFS= read -r dashboard_token
printf 'HERMES_DASHBOARD_SESSION_TOKEN=%s\nHERMES_TUI_WS_ORPHAN_REAP_GRACE_S=300\n' \
  "$dashboard_token" >"$temporary"
chmod 0600 "$temporary"
mv -f -- "$temporary" "$target"
trap - EXIT INT TERM
SH
  chmod 0755 "$writer_script"

  local write_status=0
  if [[ "$TESTING" == "1" ]]; then
    printf '%s\n' "$HERMES_TOKEN" | /bin/bash "$writer_script" \
      "$hermes_directory" "$hermes_env" || write_status=$?
  else
    printf '%s\n' "$HERMES_TOKEN" | "$RUNUSER_BIN" -u "$HERMES_USER" -- \
      /bin/bash "$writer_script" "$hermes_directory" "$hermes_env" || write_status=$?
  fi
  rm -f -- "$writer_script"
  ((write_status == 0)) || \
    die "could not create the Hermes service environment as $HERMES_USER"

  local hermes_uid hermes_gid
  if [[ "$TESTING" == "1" ]]; then
    hermes_uid="$(id -u)"
    hermes_gid="$(id -g)"
  else
    hermes_uid="$(id -u "$HERMES_USER")"
    hermes_gid="$(id -g "$HERMES_USER")"
  fi
  validate_environment_file "$hermes_env" "$hermes_uid" "$hermes_gid" 0600 \
    hermes-serve.env
}

ensure_hermes_serve() {
  local control_env="$(rooted /etc/hermes-control/control.env)"
  local hermes_env_target="$HERMES_HOME/.hermes/control-services/hermes-serve.env"
  local hermes_env="$(rooted "$hermes_env_target")"
  resolve_existing_hermes_token "$control_env" "$hermes_env"

  local ready=0 existing_unit=0 managed=0
  hermes_port_ready && ready=1
  hermes_unit_exists && existing_unit=1
  managed_hermes_unit && managed=1
  if ((ready || existing_unit)) && ((managed == 0)) && ((REUSE_HERMES_SERVE == 0)); then
    die "port 9119 or hermes-serve.service is already owned; inspect it and rerun with --reuse-hermes-serve"
  fi

  if ((existing_unit == 0 && ready == 0)); then
    verify_hermes_cli
    [[ -z "$HERMES_TOKEN" ]] && HERMES_TOKEN="$(generate_token)"
    if [[ ! -f "$hermes_env" ]]; then
      create_hermes_service_environment "$hermes_env"
    fi
    local temporary_unit
    temporary_unit="$(mktemp)"
    render_hermes_unit "$temporary_unit"
    install -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0644 "$temporary_unit" \
      "$(rooted /etc/systemd/system/hermes-serve.service)"
    rm -f -- "$temporary_unit"
    "$SYSTEMCTL_BIN" daemon-reload
    "$SYSTEMCTL_BIN" enable --now hermes-serve.service
  elif ((managed && ready == 0)); then
    "$SYSTEMCTL_BIN" enable --now hermes-serve.service
  elif ((ready == 0)); then
    [[ -n "$HERMES_TOKEN" ]] || \
      die "supply the existing dashboard token via HERMES_CONTROL_INSTALL_HERMES_TOKEN"
    "$SYSTEMCTL_BIN" enable --now hermes-serve.service
  fi
  [[ -n "$HERMES_TOKEN" ]] || \
    die "the reused Hermes service token is unknown; set HERMES_CONTROL_INSTALL_HERMES_TOKEN"
  local attempt
  for attempt in {1..20}; do
    if hermes_port_ready; then
      verify_hermes_authenticated
      return 0
    fi
    sleep 1
  done
  die "Hermes did not open loopback port 9119"
}

write_control_environment() {
  local path="$(rooted /etc/hermes-control/control.env)"
  if [[ -f "$path" ]]; then
    local existing_token existing_origin existing_vault existing_media
    validate_existing_control_environment "$path"
    existing_token="$(env_value "$path" HERMES_CONTROL_HERMES_DASHBOARD_TOKEN)"
    existing_origin="$(env_value "$path" HERMES_CONTROL_ALLOWED_ORIGINS)"
    existing_vault="$(env_value "$path" HERMES_CONTROL_VAULT_KEY_B64)"
    existing_media="$(env_value "$path" HERMES_CONTROL_HERMES_MEDIA_ROOT)"
    [[ "$existing_token" == "$HERMES_TOKEN" ]] || die "existing control.env uses another Hermes token"
    [[ "$existing_origin" == "$TAILSCALE_ORIGIN" ]] || die "existing control.env uses another allowed origin"
    [[ -n "$existing_vault" ]] || die "existing control.env has no vault key"
    if [[ "$existing_media" != "$HERMES_MEDIA_ROOT" ]]; then
      replace_env_value "$path" HERMES_CONTROL_HERMES_MEDIA_ROOT "$HERMES_MEDIA_ROOT"
      note "Updated only the Hermes media path after rechecking access as $CONTROL_USER."
    fi
    if [[ -n "$HERMES_SOURCE_SHA" && \
      "$(env_value "$path" HERMES_CONTROL_HERMES_SOURCE_SHA)" != "$HERMES_SOURCE_SHA" ]]; then
      replace_env_value "$path" HERMES_CONTROL_HERMES_SOURCE_SHA "$HERMES_SOURCE_SHA"
      note "Updated only the explicitly supplied Hermes trust SHA."
    fi
    note "Preserving existing /etc/hermes-control/control.env and secrets."
    return
  fi
  local vault_key
  vault_key="$(generate_vault_key)"
  local database_path backup_path
  database_path="$(rooted /var/lib/hermes-control/control.db)"
  backup_path="$(rooted /var/backups/hermes-control)"
  local temporary="${path}.tmp.$$"
  cat >"$temporary" <<EOF
HERMES_CONTROL_DATABASE_URL=sqlite:///$database_path
HERMES_CONTROL_ENVIRONMENT=production
HERMES_CONTROL_VAULT_KEY_B64=$vault_key
HERMES_CONTROL_HERMES_DASHBOARD_URL=$HERMES_TARGET
HERMES_CONTROL_HERMES_DASHBOARD_WS=ws://127.0.0.1:9119/api/ws
HERMES_CONTROL_HERMES_DASHBOARD_TOKEN=$HERMES_TOKEN
HERMES_CONTROL_HERMES_API_URL=
HERMES_CONTROL_HERMES_API_KEY=
HERMES_CONTROL_HERMES_MEDIA_ROOT=$HERMES_MEDIA_ROOT
HERMES_CONTROL_HERMES_MEDIA_MAX_BYTES=52428800
HERMES_CONTROL_HERMES_SOURCE_SHA=$HERMES_SOURCE_SHA
HERMES_CONTROL_DEFAULT_PROFILES=$PROFILE_LIST
HERMES_CONTROL_INTERACTIVE_PROFILES=$PROFILE_LIST
HERMES_CONTROL_MUTABLE_PROFILES=$PROFILE_LIST
HERMES_CONTROL_ALLOWED_ORIGINS=$TAILSCALE_ORIGIN
HERMES_CONTROL_SECURE_COOKIES=true
HERMES_CONTROL_CREATE_SCHEMA_ON_START=false
HERMES_CONTROL_PROVIDER_MODE=real
HERMES_CONTROL_MOCK_FALLBACK_ENABLED=false
HERMES_CONTROL_TRUST_PRIVATE_ENDPOINTS=true
HERMES_CONTROL_WS_MAX_INBOUND_BYTES=4096
HERMES_CONTROL_AUTOMATION_ROUTE_WATCH_SECONDS=30
HERMES_CONTROL_AUTOMATION_ROUTE_STALE_SECONDS=120
HERMES_CONTROL_UPSTREAM_HEALTH_TTL_SECONDS=60
HERMES_CONTROL_CAPABILITY_TTL_SECONDS=60
HERMES_CONTROL_CAPABILITY_REFRESH_SECONDS=30
HERMES_CONTROL_DATABASE_PATH=$database_path
HERMES_CONTROL_BACKUP_DIR=$backup_path
EOF
  chmod 0640 "$temporary"
  chown "$ROOT_OWNER:$CONTROL_GROUP_OWNER" "$temporary"
  mv -- "$temporary" "$path"
}

install_control_units() {
  local current="$(rooted /opt/hermes-control/current)"
  local unit destination
  for unit in hermes-control.service hermes-control-backup.service hermes-control-backup.timer; do
    destination="$(rooted "/etc/systemd/system/$unit")"
    if [[ -e "$destination" || -L "$destination" ]]; then
      [[ -f "$destination" && ! -L "$destination" ]] || \
        die "refusing to replace unsafe systemd path $destination"
      cmp -s -- "$current/deploy/systemd/$unit" "$destination" || \
        die "refusing to overwrite changed systemd unit $unit"
    fi
    install -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0644 \
      "$current/deploy/systemd/$unit" \
      "$destination"
  done
  local dropin_dir="$(rooted /etc/systemd/system/hermes-control.service.d)"
  install -d -o "$ROOT_OWNER" -g "$ROOT_GROUP_OWNER" -m 0755 "$dropin_dir"
  local dropin="${dropin_dir}/media-root.conf"
  if [[ -z "$HERMES_MEDIA_ROOT" ]]; then
    if grep -q '^# Managed by Agent Control Linux installer$' "$dropin" 2>/dev/null; then
      rm -f -- "$dropin"
    elif [[ -e "$dropin" ]]; then
      note "Preserving unmanaged $dropin; Hermes media remains disabled in control.env."
    fi
    "$SYSTEMCTL_BIN" daemon-reload
    return
  fi
  local temporary="${dropin}.tmp.$$"
  cat >"$temporary" <<EOF
# Managed by Agent Control Linux installer
[Service]
ProtectHome=tmpfs
BindReadOnlyPaths=$HERMES_HOME/.hermes/profiles
EOF
  chmod 0644 "$temporary"
  chown "$ROOT_OWNER:$ROOT_GROUP_OWNER" "$temporary"
  mv -- "$temporary" "$dropin"
  "$SYSTEMCTL_BIN" daemon-reload
}

run_with_control_environment() {
  local env_file="$(rooted /etc/hermes-control/control.env)"
  local current="$(rooted /opt/hermes-control/current)"
  (
    umask 077
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ -z "$line" || "$line" == \#* ]] && continue
      [[ "$line" =~ ^[A-Z0-9_]+= ]] || die "unsafe line in control.env"
      export "$line"
    done <"$env_file"
    cd -- "$current"
    if [[ "$TESTING" == "1" ]]; then
      "$@"
    else
      "$RUNUSER_BIN" -u "$CONTROL_USER" -- "$@"
    fi
  )
}

backup_database_before_migration() {
  local env_file="$(rooted /etc/hermes-control/control.env)"
  local current="$(rooted /opt/hermes-control/current)"
  local database_path backup_dir backup_output backup_path control_uid control_gid
  database_path="$(env_value "$env_file" HERMES_CONTROL_DATABASE_PATH)"
  backup_dir="$(env_value "$env_file" HERMES_CONTROL_BACKUP_DIR)"
  [[ ! -e "$database_path" && ! -L "$database_path" ]] && return 0
  [[ -f "$database_path" && ! -L "$database_path" ]] || \
    die "refusing to migrate an unsafe Agent Control database path"
  [[ -x "$current/deploy/bin/backup-sqlite.sh" ]] || \
    die "the active release has no executable SQLite backup helper"
  backup_output="$(run_with_control_environment \
    "$current/deploy/bin/backup-sqlite.sh")" || \
    die "pre-migration SQLite backup failed"
  [[ "$backup_output" != *$'\n'* && -n "$backup_output" ]] || \
    die "pre-migration backup returned an ambiguous path"
  backup_path=$backup_output
  [[ "$(dirname -- "$backup_path")" == "$backup_dir" && \
    "$(basename -- "$backup_path")" == control-*.db ]] || \
    die "pre-migration backup was written outside the configured backup directory"
  if [[ "$TESTING" == "1" ]]; then
    control_uid="$(id -u)"
    control_gid="$(id -g)"
  else
    control_uid="$(id -u "$CONTROL_USER")"
    control_gid="$(id -g "$CONTROL_USER")"
  fi
  validate_state_path "$backup_path" "$control_uid" "$control_gid" 0600 file \
    "pre-migration SQLite backup"
  note "Verified pre-migration SQLite backup: $backup_path"
}

migrate_and_create_admin() {
  local current="$(rooted /opt/hermes-control/current)"
  backup_database_before_migration
  run_with_control_environment "$current/.venv/bin/alembic" \
    -c apps/api/alembic.ini upgrade head
  local database_path="$(env_value "$(rooted /etc/hermes-control/control.env)" \
    HERMES_CONTROL_DATABASE_PATH)"
  local admin_count
  admin_count="$($SQLITE_BIN "$database_path" \
    'SELECT count(*) FROM users WHERE is_admin = 1;' 2>/dev/null || true)"
  [[ "$admin_count" =~ ^[0-9]+$ ]] || die "could not inspect migrated administrator table"
  if ((admin_count == 0)); then
    if ((SKIP_ADMIN)); then
      note "No administrator exists; create one with hermes-control-admin before login."
    elif [[ -t 0 && -t 1 ]]; then
      run_with_control_environment "$current/.venv/bin/hermes-control-admin" \
        create-admin --username admin
    else
      die "administrator creation requires a TTY; rerun interactively or use --skip-admin"
    fi
  else
    note "Preserving the existing administrator account."
  fi
}

wait_for_control() {
  local attempt readiness
  for attempt in {1..60}; do
    if "$CURL_BIN" --fail --silent --show-error --output /dev/null --max-time 3 \
      "$CONTROL_TARGET/api/v1/health" && \
      readiness="$($CURL_BIN --fail --silent --show-error --max-time 3 \
        "$CONTROL_TARGET/api/v1/ready")"; then
      if printf '%s' "$readiness" | "$PYTHON_BIN" -c '
import json, sys
payload = json.load(sys.stdin)
if payload.get("status") != "ready":
    raise SystemExit(1)
if payload.get("database") != "ready":
    raise SystemExit(1)
if payload.get("upstream") != "online":
    raise SystemExit(1)
'; then
        return 0
      fi
    fi
    sleep 2
  done
  die "Agent Control did not reach strict readiness (status/database ready and upstream online)"
}

verify_public_pwa() {
  local root_html manifest_json
  root_html="$($CURL_BIN --fail --silent --show-error --max-time 10 \
    "$TAILSCALE_ORIGIN/")" || return 1
  manifest_json="$($CURL_BIN --fail --silent --show-error --max-time 10 \
    "$TAILSCALE_ORIGIN/manifest.webmanifest")" || return 1
  {
    printf '%s\0' "$root_html"
    printf '%s' "$manifest_json"
  } | "$PYTHON_BIN" -c '
import json, sys
parts = sys.stdin.buffer.read().split(b"\0", 1)
if len(parts) != 2:
    raise SystemExit(1)
html = parts[0].decode("utf-8", "strict").lower()
if "<html" not in html or "manifest.webmanifest" not in html:
    raise SystemExit(1)
manifest = json.loads(parts[1].decode("utf-8", "strict"))
if not (manifest.get("name") or manifest.get("short_name")):
    raise SystemExit(1)
if not isinstance(manifest.get("start_url"), str) or not manifest["start_url"].startswith("/"):
    raise SystemExit(1)
if not isinstance(manifest.get("icons"), list) or not manifest["icons"]:
    raise SystemExit(1)
'
}

start_control_and_backup() {
  if ((CONTROL_SERVICE_PREEXISTED)); then
    "$SYSTEMCTL_BIN" enable hermes-control.service
    "$SYSTEMCTL_BIN" restart hermes-control.service
  else
    "$SYSTEMCTL_BIN" enable --now hermes-control.service
  fi
  "$SYSTEMCTL_BIN" enable --now hermes-control-backup.timer
  wait_for_control
  "$SYSTEMCTL_BIN" start hermes-control-backup.service
}

configure_tailscale_serve() {
  case "$TAILSCALE_STATE" in
    empty)
      "$TAILSCALE_BIN" serve --bg --yes --https=443 --set-path=/ "$CONTROL_TARGET"
      ;;
    control) ;;
    *) die "refusing unsafe Tailscale Serve state: $TAILSCALE_STATE" ;;
  esac
  local status_json verified
  status_json="$($TAILSCALE_BIN serve status --json)"
  verified="$(printf '%s' "$status_json" | classify_serve_config \
    "$TAILSCALE_HOSTNAME" "$CONTROL_TARGET")"
  [[ "$verified" == control ]] || die "Tailscale Serve did not retain the Control root proxy"
  "$CURL_BIN" --fail --silent --show-error --output /dev/null --max-time 10 \
    "$TAILSCALE_ORIGIN/api/v1/health" || \
    die "Tailscale root does not reach Agent Control; existing Serve state was left intact"
  verify_public_pwa || die "Tailscale root did not serve a valid Agent Control page and PWA manifest"
}

validate_options() {
  [[ -z "$HERMES_SOURCE_SHA" || "$HERMES_SOURCE_SHA" =~ ^[0-9a-fA-F]{40}$ ]] || \
    die "--hermes-source-sha must be exactly 40 hexadecimal characters"
  HERMES_SOURCE_SHA="$(printf '%s' "$HERMES_SOURCE_SHA" | tr '[:upper:]' '[:lower:]')"
}

platform_preflight() {
  if [[ "$TESTING" != "1" ]]; then
    [[ "$(uname -s)" == Linux ]] || die "this installer supports Linux only"
    [[ -d /run/systemd/system ]] || die "systemd is not the active init system"
  fi
  command -v "$SYSTEMCTL_BIN" >/dev/null || die "systemctl is required"
  command -v "$TAILSCALE_BIN" >/dev/null || die "Tailscale must already be installed"
  if [[ "$TESTING" != "1" ]]; then
    command -v "$RUNUSER_BIN" >/dev/null || die "runuser is required"
  fi
  "$TAILSCALE_BIN" status --json >/dev/null || die "Tailscale is not connected"
}

select_preflight_python() {
  local bundled_hermes_python="$(rooted "$HERMES_HOME/.hermes/hermes-agent/venv/bin/python")"
  local candidates=() candidate
  [[ -z "${HERMES_CONTROL_INSTALL_PYTHON:-}" ]] || \
    candidates+=("$HERMES_CONTROL_INSTALL_PYTHON")
  if [[ "$HERMES_COMMAND_KIND" == python ]]; then
    candidates+=("$(rooted "$HERMES_BIN")")
  fi
  candidates+=("$bundled_hermes_python")
  command -v python3.14 >/dev/null 2>&1 && candidates+=("$(command -v python3.14)")
  command -v python3.13 >/dev/null 2>&1 && candidates+=("$(command -v python3.13)")
  command -v python3.12 >/dev/null 2>&1 && candidates+=("$(command -v python3.12)")
  command -v python3 >/dev/null 2>&1 && candidates+=("$(command -v python3)")
  for candidate in "${candidates[@]}"; do
    [[ -x "$candidate" ]] || continue
    if "$candidate" -c 'import json; json.loads("{}")' >/dev/null 2>&1; then
      PYTHON_BIN=$candidate
      return
    fi
  done
  die "Python is required to inspect existing Tailscale Serve state safely"
}

print_dry_run_plan() {
  local hermes_action=install
  if hermes_port_ready || hermes_unit_exists; then
    hermes_action=reuse
  fi
  cat <<EOF
Dry-run only; no files, services, users, packages or Tailscale state were changed.
Source: $SOURCE_ROOT
Release: $RELEASE_ID
PWA: $([[ "$BUILD_WEB" == 1 ]] && printf 'build with Node.js >=20' || printf 'prebuilt %s' "$STATIC_SOURCE")
Hermes: user=$HERMES_USER bin=$HERMES_BIN profile=$HERMES_PROFILE action=$hermes_action
Hermes trust SHA: $([[ -n "$HERMES_SOURCE_SHA" ]] && printf '%s' "$HERMES_SOURCE_SHA" || printf 'empty (mutations remain gated)')
Agent Control: loopback 127.0.0.1:8000, immutable /opt/hermes-control/releases/$RELEASE_ID
Systemd: application service plus daily SQLite backup timer
Tailscale: state=$TAILSCALE_STATE root=$TAILSCALE_ORIGIN/ -> $CONTROL_TARGET
EOF
}

main() {
  parse_args "$@"
  validate_options
  require_root_boundary
  platform_preflight
  resolve_release_inputs
  discover_hermes_identity
  verify_hermes_cli
  select_preflight_python
  discover_tailscale_hostname
  inspect_tailscale_serve
  preflight_control_account
  preflight_managed_control_installation

  if (hermes_port_claimed || hermes_unit_exists) && ! managed_hermes_unit && \
    ((REUSE_HERMES_SERVE == 0)); then
    die "port 9119 or hermes-serve.service is already owned; rerun with --reuse-hermes-serve only after review"
  fi

  preflight_existing_configuration
  verify_existing_control_listener
  install_remaining_packages

  if ((DRY_RUN)); then
    print_dry_run_plan
    return 0
  fi
  ensure_control_account_and_paths
  resolve_media_access
  install_release
  ensure_hermes_serve
  write_control_environment
  install_control_units
  migrate_and_create_admin
  start_control_and_backup
  configure_tailscale_serve

  note "Agent Control is installed at $TAILSCALE_ORIGIN"
  note "Loopback listeners: Agent Control 127.0.0.1:8000; Hermes 127.0.0.1:9119"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
