#!/bin/sh
# This template is rendered with the release signing public key before upload.
set -eu
umask 077

server=
version=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --server) [ "$#" -ge 2 ] || { echo "--server requires a value" >&2; exit 2; }; server=$2; shift 2 ;;
        --version) [ "$#" -ge 2 ] || { echo "--version requires a value" >&2; exit 2; }; version=$2; shift 2 ;;
        -h|--help) echo 'Usage: install.sh --server https://control.example.com [--version RELEASE]'; exit 0 ;;
        *) echo 'Unknown installer argument' >&2; exit 2 ;;
    esac
done
case "$server" in https://*) ;; *) echo 'An HTTPS server is required' >&2; exit 2 ;; esac
server=${server%/}
case "$server" in *'?'*|*'#'*|*'@'*|*' '*|*'\'*) echo 'Invalid server origin' >&2; exit 2 ;; esac
authority=${server#https://}
case "$authority" in ''|*'/'*|*' '*|*'	'*|*'
'*) echo 'Server must be an HTTPS origin' >&2; exit 2 ;; esac
for utility in curl openssl tar mktemp awk; do command -v "$utility" >/dev/null 2>&1 || { echo "Missing prerequisite: $utility" >&2; exit 2; }; done
case "$(uname -s)" in Linux) platform=linux ;; Darwin) platform=macos ;; *) echo 'Linux and macOS are supported' >&2; exit 2 ;; esac
case "$(uname -m)" in x86_64) architecture=x86_64 ;; arm64|aarch64) architecture=arm64 ;; *) echo 'Unsupported CPU architecture' >&2; exit 2 ;; esac
connector_home=${AGENT_CONTROL_CONNECTOR_HOME:-"$HOME/.agent-control-connector"}
if [ -e "$connector_home/current" ] || [ -L "$connector_home/current" ]; then
    echo 'Connector is already installed. Use agent-control-connector update.' >&2
    exit 2
fi
stage=$(mktemp -d "${TMPDIR:-/tmp}/agent-control-install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
cat > "$stage/release-key.pem" <<'RELEASE_KEY'
__CONNECTOR_RELEASE_PUBLIC_KEY__
RELEASE_KEY
if ! openssl pkey -pubin -in "$stage/release-key.pem" -noout >/dev/null 2>&1; then
    echo 'Installer has not been prepared with a release signing key.' >&2
    exit 2
fi
if [ -z "$version" ]; then version=$(curl --proto '=https' --tlsv1.2 --fail --silent --show-error --max-time 30 --max-filesize 128 "$server/downloads/connector/VERSION"); fi
case "$version" in ''|*[!a-zA-Z0-9._-]*) echo 'Invalid release identifier' >&2; exit 2 ;; esac
base="$server/downloads/connector/releases/$version"
archive="agent-control-connector-$platform-$architecture.tar.gz"
for item in SHA256SUMS SHA256SUMS.sig "$archive"; do
    case "$item" in SHA256SUMS) maximum=16384 ;; SHA256SUMS.sig) maximum=8192 ;; *) maximum=300000000 ;; esac
    curl --proto '=https' --tlsv1.2 --fail --silent --show-error --max-time 300 --max-filesize "$maximum" --output "$stage/$item" "$base/$item"
done
openssl dgst -sha256 -verify "$stage/release-key.pem" -signature "$stage/SHA256SUMS.sig" "$stage/SHA256SUMS" >/dev/null
expected=$(awk -v file="$archive" '$2 == file {print $1}' "$stage/SHA256SUMS")
case "$expected" in ''|*[!a-f0-9]*) echo 'Release checksum is missing' >&2; exit 2 ;; esac
[ "${#expected}" -eq 64 ] || exit 2
actual=$(openssl dgst -sha256 "$stage/$archive" | awk '{print $NF}')
[ "$actual" = "$expected" ] || { echo 'Release checksum failed' >&2; exit 2; }
# Native framework symlinks are dereferenced by the signing job. Installers
# accept only regular files/directories so system tar cannot follow a link.
if tar -tzf "$stage/$archive" | awk '$0 !~ /^agent-control-connector\// || $0 ~ /(^|\/)\.\.(\/|$)/ {bad=1} END {exit !bad}'; then
    echo 'Invalid release archive layout' >&2; exit 2
fi
if tar -tvzf "$stage/$archive" | awk 'substr($0,1,1) != "-" && substr($0,1,1) != "d" {bad=1} END {exit !bad}'; then
    echo 'Release archive contains unsupported links or special files' >&2; exit 2
fi
tar -xzf "$stage/$archive" -C "$stage"
binary="$stage/agent-control-connector/agent-control-connector"
[ -x "$binary" ] || { echo 'Release executable missing' >&2; exit 2; }
# The signed runtime performs safe filesystem/service setup and pairing.
"$binary" install-service --data-dir "$connector_home" --source "$stage/agent-control-connector" --release "$version" --server "$server"
