#!/bin/sh
# Rendered by prepare_release.py; independent of the existing connector installer.
set -eu
umask 077
server=https://agentcontrol.jemailabs.com
version=
managed_home=${XDG_DATA_HOME:-"$HOME/.local/share"}/agent-control
while [ "$#" -gt 0 ]; do
    case "$1" in
        --server) [ "$#" -ge 2 ] || exit 2; server=$2; shift 2 ;;
        --version) [ "$#" -ge 2 ] || exit 2; version=$2; shift 2 ;;
        --data-dir) [ "$#" -ge 2 ] || exit 2; managed_home=$2; shift 2 ;;
        -h|--help) echo 'Usage: install.sh [--server HTTPS_ORIGIN] [--version RELEASE] [--data-dir PATH]'; exit 0 ;;
        *) echo 'Unknown installer argument' >&2; exit 2 ;;
    esac
done
case "$server" in https://*) ;; *) echo 'An HTTPS server is required' >&2; exit 2 ;; esac
server=${server%/}
authority=${server#https://}
case "$authority" in ''|*'/'*|*'@'*|*'?'*|*'#'*|*' '*|*'\'*|*'	'*|*'
'*) echo 'Invalid server origin' >&2; exit 2 ;; esac
case "$managed_home" in /*) ;; *) echo 'Installation directory must be absolute' >&2; exit 2 ;; esac
case "$managed_home" in *'
'*|*'	'*) echo 'Invalid installation directory' >&2; exit 2 ;; esac
[ "$(uname -s)" = Linux ] || { echo 'Use the signed Agent Control application on macOS.' >&2; exit 2; }
[ "$(id -u)" != 0 ] || { echo 'Run this installer as your normal user, without sudo.' >&2; exit 2; }
for utility in curl openssl tar mktemp awk stat systemctl cmp flock; do
    command -v "$utility" >/dev/null 2>&1 || { echo "Missing prerequisite: $utility" >&2; exit 2; }
done
case "$(uname -m)" in x86_64) architecture=x86_64 ;; aarch64|arm64) architecture=arm64 ;; *) echo 'Unsupported architecture' >&2; exit 2 ;; esac
os_id=$(awk -F= '$1 == "ID" {gsub(/"/, "", $2); print $2}' /etc/os-release)
os_version=$(awk -F= '$1 == "VERSION_ID" {gsub(/"/, "", $2); print $2}' /etc/os-release)
case "$os_id:$os_version" in ubuntu:22.04|ubuntu:24.04|debian:12|debian:13) ;; *) echo 'This release supports Ubuntu 22.04/24.04 and Debian 12/13 with systemd.' >&2; exit 2 ;; esac
systemctl --user show-environment >/dev/null 2>&1 || { echo 'A working systemd user session is required. Sign in normally and retry.' >&2; exit 2; }
if [ -L "$managed_home" ] || [ -L "$managed_home/releases" ]; then echo 'Installation directories cannot be symlinks.' >&2; exit 2; fi
mkdir -p "$managed_home/releases"
[ "$(stat -c %u "$managed_home")" = "$(id -u)" ] && [ "$(stat -c %u "$managed_home/releases")" = "$(id -u)" ] || { echo 'Installation directories must belong to this user.' >&2; exit 2; }
chmod 700 "$managed_home" "$managed_home/releases"
# A kernel-held lock is released after a killed installer and its children exit.
# Keep the inode in place: deleting a live lock file permits a second installer.
lock_file="$managed_home/.install.lock"
if [ -L "$lock_file" ] || { [ -e "$lock_file" ] && { [ ! -f "$lock_file" ] || [ "$(stat -c %u "$lock_file")" != "$(id -u)" ]; }; }; then
    echo 'Invalid installer lock ownership or type.' >&2; exit 2
fi
exec 9>"$lock_file"
chmod 600 "$lock_file"
if ! flock -n 9; then echo 'Another installer is running. Finish or close it before retrying.' >&2; exit 2; fi
stage=
cleanup() { [ -z "$stage" ] || rm -rf -- "$stage"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
stage=$(mktemp -d "$managed_home/.download.XXXXXXXX")
cat > "$stage/release-public-key.pem" <<'PUBLIC_KEY'
__MANAGED_RELEASE_PUBLIC_KEY__
PUBLIC_KEY
openssl pkey -pubin -in "$stage/release-public-key.pem" -noout >/dev/null 2>&1 || { echo 'Installer is not prepared for publication.' >&2; exit 2; }
if [ -z "$version" ]; then version=$(curl --proto '=https' --tlsv1.2 -fsS --max-time 30 --max-filesize 128 "$server/downloads/agent-control/VERSION"); fi
case "$version" in *[!a-f0-9]*|'') echo 'Invalid release identifier' >&2; exit 2 ;; esac
[ "${#version}" = 40 ] || { echo 'Invalid release identifier' >&2; exit 2; }
base="$server/downloads/agent-control/releases/$version"
archive="agent-control-runtime-linux-$architecture.tar.gz"
for item in SHA256SUMS SHA256SUMS.sig "$archive"; do
    case "$item" in SHA256SUMS) maximum=16384 ;; SHA256SUMS.sig) maximum=8192 ;; *) maximum=1500000000 ;; esac
    curl --proto '=https' --tlsv1.2 -fsS --max-time 900 --max-filesize "$maximum" --output "$stage/$item" "$base/$item"
done
openssl dgst -sha256 -verify "$stage/release-public-key.pem" -signature "$stage/SHA256SUMS.sig" "$stage/SHA256SUMS" >/dev/null
expected=$(awk -v file="$archive" '$2 == file {print $1}' "$stage/SHA256SUMS")
case "$expected" in *[!a-f0-9]*|'') echo 'Missing or duplicate release checksum' >&2; exit 2 ;; esac
[ "${#expected}" = 64 ] || exit 2
actual=$(openssl dgst -sha256 "$stage/$archive" | awk '{print $NF}')
[ "$expected" = "$actual" ] || { echo 'Release checksum failed' >&2; exit 2; }
tar -tzf "$stage/$archive" > "$stage/members"
if awk '$0 !~ /^agent-control-runtime\// || $0 ~ /(^|\/)\.\.(\/|$)/ || $0 ~ /\\/ {bad=1} END {exit !bad}' "$stage/members"; then echo 'Unsafe release archive path' >&2; exit 2; fi
tar -tvzf "$stage/$archive" > "$stage/types"
if awk 'substr($0,1,1) != "-" && substr($0,1,1) != "d" {bad=1} END {exit !bad}' "$stage/types"; then echo 'Release contains links or special files' >&2; exit 2; fi
tar -xzf "$stage/$archive" -C "$stage"
candidate="$stage/agent-control-runtime"
verify_runtime() {
    unset PYTHONHOME PYTHONSTARTUP
    PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH="$1/connector" "$1/python/bin/python3" -B -c \
        'import sys; from pathlib import Path; from agent_control_connector.managed_manifest import verify_runtime; m=verify_runtime(Path(sys.argv[1])); assert m["release"] == sys.argv[2] and m["platform"] == sys.argv[3]' "$1" "$version" "linux-$architecture"
}
verify_runtime "$candidate"
destination="$managed_home/releases/$version"
if [ -e "$destination" ] || [ -L "$destination" ]; then
    [ ! -L "$destination" ] && [ -d "$destination" ] || { echo 'Invalid staged release directory' >&2; exit 2; }
    verify_runtime "$destination"
    cmp "$candidate/runtime-manifest.json" "$destination/runtime-manifest.json" >/dev/null || { echo 'Existing staged release differs from verified download' >&2; exit 2; }
else
    mv "$candidate" "$destination"
fi
echo 'Runtime verified. Continuing local setup; existing Hermes installations remain separate.'
# The shell script may arrive on stdin via curl. Keep interactive credentials
# on the controlling terminal and never pass their values through arguments.
if ( : </dev/tty >/dev/tty ) 2>/dev/null; then
    "$destination/bin/agent-control-setup" --wizard --release-root "$destination" --data-dir "$managed_home" --server "$server" </dev/tty
else
    echo "Open an interactive terminal and run: '$destination/bin/agent-control-setup' --wizard --release-root '$destination' --data-dir '$managed_home' --server '$server'" >&2
    exit 2
fi
