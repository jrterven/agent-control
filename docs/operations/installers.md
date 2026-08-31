# Local installers

Agent Control includes full local installers for a host that already has Hermes
and Tailscale installed and connected:

- `deploy/install-linux.sh` is the stable Linux entrypoint. It passes every
  argument to `deploy/linux/install-agent-control.sh`, the canonical systemd
  implementation.
- `deploy/install-macos.sh` installs Agent Control for the current signed-in
  macOS user with LaunchAgents.

These are Agent Control installers, not Hermes installers and not remote-gateway
installers. They build and install the Control API and PWA, start or reuse a
local Hermes dashboard endpoint, and publish Control privately with Tailscale
Serve. They do not install or update Hermes, join or reconfigure a tailnet, or
create a reverse SSH tunnel.

The resulting local topology is fixed:

| Component | Listener | Exposure |
|---|---|---|
| Hermes dashboard protocol | `127.0.0.1:9119` | Agent Control on the same host only |
| Agent Control | `127.0.0.1:8000` | Loopback only |
| Tailscale Serve | `https://<device>.<tailnet>.ts.net/` | Private tailnet HTTPS proxy to `127.0.0.1:8000` |

Neither installer calls `tailscale funnel`, `tailscale serve reset`,
`tailscale up`, or `tailscale down`. In the default installation path, an empty
Serve configuration is populated and an existing configuration is accepted
only when it is already the exact Agent Control root proxy. Funnel, a root owned
by another service, or any other Serve route causes an abort. The installers
never auto-merge or replace a conflicting configuration. The macOS
`--skip-tailscale-serve` escape hatch only inspects and leaves Serve unchanged;
the operator then owns that configuration and public-origin verification.

Leave the Hermes trust SHA blank unless an operator has independently audited
an exact Hermes commit. Blank is the default on both operating systems and
keeps revision-gated mutations disabled. Do not copy a value from diagnostics
or assume that the Agent Control release SHA is also the Hermes SHA.

## Linux quick start

Run these commands from a reviewed Agent Control checkout on a systemd host:

```bash
sudo ./deploy/install-linux.sh --dry-run
sudo ./deploy/install-linux.sh
```

The dry run performs source, Hermes, dependency, Tailscale, existing-service,
and existing-configuration preflights, then prints the release and host plan. It
does not create users or directories, install packages, write secrets, start
services, or change Serve. A dry run fails when required dependencies are
missing rather than installing OS packages; when only the PWA build needs Node,
it reports the pinned toolchain it would install.

The real run must be root and prompts for the first administrator password when
the migrated database has no administrator. The username is `admin`; the
password is entered twice, must contain at least 12 characters, and is never a
command-line argument or environment variable. For a deliberately
noninteractive install, use `--skip-admin` and create the administrator later
with the installed `hermes-control-admin` command and production environment.

### Linux requirements and package behavior

The installer requires Linux with systemd active, a connected Tailscale CLI,
and an existing Hermes OS user, profile, and CLI for which `hermes serve --help`
succeeds. It discovers the conventional `hermes` user and Hermes virtualenv
when possible; otherwise pass the corresponding flags.

Agent Control requires Python 3.12, 3.13, or 3.14 with `venv`, plus `curl`,
`sqlite3`, `tar`, an OpenSSH client, and CA certificates. On a real install the
script can fill missing dependencies with `apt`, `dnf`, or `yum`. It selects an
available versioned Python 3.12-3.14 package (and the matching versioned
`pythonX.Y-venv` package on apt); it never silently installs a generic
`python3` package that might be too old. Git must already be available when
`--source` is a Git checkout; a copied or downloaded non-Git artifact does not
need Git but does require an explicit `--release-id`.

A Git source checkout must have no tracked changes. Its clean `HEAD` is the
default release ID, only tracked files enter the release, and the installer
always performs a fresh production PWA build from that committed source. A
pre-existing ignored `apps/web/dist/` is deliberately not trusted. An explicit
`--static-dir` is the only way to use a prebuilt PWA with a Git source. For a
non-Git release artifact, the installer may use an included
`apps/api/static/index.html` or `apps/web/dist/index.html` automatically.

When a PWA build is needed, Node.js 20 or newer and npm are required. The
installer uses a compatible preinstalled pair when available. If none is
available on Linux x86_64 or arm64, it downloads the pinned Node.js v22.23.2
archive from `nodejs.org`, verifies its hard-coded architecture-specific
SHA-256, and installs it under `/opt/hermes-control/toolchains/`. Other
architectures must preinstall Node 20+ or supply `--static-dir`. The installer
never installs a distribution's generic Node/npm package.

Releases are built under
`/opt/hermes-control/releases/<release-id>` and
`/opt/hermes-control/current` points to the installed release.

### Linux files and services

The installer creates a locked `hermes-control` system account and manages:

| Path or unit | Purpose |
|---|---|
| `/opt/hermes-control/releases/<release-id>` | Immutable application, PWA, and virtualenv |
| `/opt/hermes-control/current` | Active release symlink |
| `/opt/hermes-control/toolchains/node-v22.23.2-linux-<arch>` | Checksum-verified PWA build toolchain when no compatible host Node exists |
| `/etc/hermes-control/control.env` | Production configuration and Control-held secrets; `root:hermes-control`, mode `0640` |
| `/var/lib/hermes-control/control.db` | Persistent SQLite database under a mode-`0700` data directory |
| `/var/backups/hermes-control/` | Mode-`0700` SQLite backups |
| `<Hermes home>/.hermes/control-services/hermes-serve.env` | Managed Hermes dashboard token; Hermes-owned, mode `0600` |
| `hermes-serve.service` | Optional managed Hermes listener on `127.0.0.1:9119` |
| `hermes-control.service` | Agent Control on `127.0.0.1:8000` |
| `hermes-control-backup.timer` and `.service` | Daily backup plus an immediate first backup |

When nothing owns port 9119, the installer generates a dashboard token and
creates the reviewed Hermes systemd unit. It will not overwrite or silently
trust an unmanaged listener or unit. After reviewing that existing service,
pass `--reuse-hermes-serve`; the installer then requires its matching token and
authenticates `/api/profiles` before installing a release. Once a Hermes unit
exists, reruns preserve it rather than replacing its command or profile; change
an existing Hermes service through its own reviewed service-management process.

The token may already be available in the managed Hermes or Control env file.
For a reused nonstandard service, provide it for this invocation only through
`HERMES_CONTROL_INSTALL_HERMES_TOKEN`, never a CLI flag. This prompt pattern
keeps the value out of shell history and command arguments:

```bash
read -rsp 'Hermes dashboard token: ' HERMES_CONTROL_INSTALL_HERMES_TOKEN; echo
export HERMES_CONTROL_INSTALL_HERMES_TOKEN
sudo --preserve-env=HERMES_CONTROL_INSTALL_HERMES_TOKEN \
  ./deploy/install-linux.sh --reuse-hermes-serve
unset HERMES_CONTROL_INSTALL_HERMES_TOKEN
```

The installer reads and unsets that one-shot input internally. The supplied
token must be 32-512 URL-safe characters and must match any token already found
in the managed env files. If local sudo policy forbids preserving that single
variable, perform the same hidden prompt and export in an interactive root
shell instead of placing the token after `sudo` or `env` on a command line.

Hermes chat media remains subject to normal POSIX discretionary access control.
The installer tests whether the `hermes-control` user can read and traverse the
Hermes profile tree. If it can, Control receives a read-only systemd bind of
that tree. If it cannot, the media-root setting remains blank and file
downloads remain disabled. The installer does not widen modes or ACLs, change
Hermes ownership, or add users to groups; a rerun rechecks access and updates
only the managed media setting/drop-in.

On rerun, an existing `control.env` must still be production-only, keep its
vault and matching Hermes token, use the exact loopback dashboard HTTP/WS URLs
and Tailscale origin, leave the legacy API URL/key empty, keep secure cookies,
disable automatic schema creation and mock fallback, and use the real provider.
It must also remain a nonsymlink `root:hermes-control` file at mode `0640`, have
no malformed or duplicate keys, decode to a 32-byte vault key, and keep the
managed database and backup paths. The database and backup directories remain
mode `0700`, and an existing database or generated backup remains mode `0600`.
The installer preserves the environment file by default. It changes only the
media path after the DAC recheck and, when explicitly supplied, the
`--hermes-source-sha` trust anchor. Before every migration of an existing
database, it creates and verifies a SQLite backup in the managed backup
directory.

### Linux options

| Option | Effect |
|---|---|
| `--source DIR` | Use another Agent Control checkout or release-artifact root. |
| `--static-dir DIR` | Use a prebuilt PWA directory containing `index.html`. |
| `--release-id ID` | Set the immutable release name; required for non-Git artifacts and otherwise defaults to the clean Git SHA. |
| `--tailscale-hostname NAME` | Assert the expected `.ts.net` MagicDNS hostname; it must equal the connected node's discovered name. |
| `--hermes-user USER` | Select the existing OS user that owns Hermes. |
| `--hermes-bin PATH` | Select an absolute Hermes executable or its virtualenv Python. |
| `--hermes-profile PROFILE` | Select the profile used by `hermes serve`. |
| `--hermes-source-sha SHA` | Set an independently audited, exact 40-hex Hermes trust anchor. |
| `--reuse-hermes-serve` | Explicitly reuse a reviewed listener/unit on port 9119; a matching token is still required. |
| `--skip-admin` | Skip the first-admin prompt when no administrator exists. |
| `--dry-run` | Validate and print the plan without changing host state. |
| `-h`, `--help` | Print installer help. |

## macOS quick start

Run the installer as the signed-in macOS user, not with `sudo`:

```bash
./deploy/install-macos.sh --dry-run
./deploy/install-macos.sh
```

The installer does not run as root and does not invoke `sudo`. Its dry run
performs source, tool, Hermes, environment, port, connected-tailnet, and Serve
checks, then prints the planned filesystem, build, Keychain, LaunchAgent, and
verification actions without creating `~/.agent-control` or
`~/Library/LaunchAgents` or changing Serve.

The first real run prompts securely for a Hermes dashboard token through the
macOS `security` command. Store a strong token of at least 32 characters in the
login Keychain item whose service is
`com.agent-control.hermes-dashboard` and whose account is the current username.
The value must be 32-512 URL-safe characters and is not written to `control.env`,
a plist, or a wrapper argument; Hermes and Control retrieve it from Keychain
when their LaunchAgents start. Reruns preserve the item.
`--rotate-hermes-token` prompts again and updates the existing item; coordinate
that rotation with any reused Hermes listener so both sides use the same token.

After migration, an empty database triggers the first administrator prompt.
The default username is `admin`, or the value of `--admin-username`; the
password is entered twice and must contain at least 12 characters. An existing
administrator is left unchanged.

### macOS requirements and package behavior

The installer requires a signed-in, non-root macOS account; a connected
Tailscale installation; an existing Hermes CLI/profile for which
`hermes serve --help` succeeds; Git; Python 3.12-3.14; Node.js 20 or newer; npm;
`sqlite3`; `curl`; `launchctl`; `plutil`; and `security`.

Missing build dependencies are installed by default with Homebrew using
`python@3.12`, `node@20`, `sqlite`, and Git. Homebrew itself and the macOS
system tools must already be available. Use `--no-install-packages` when a
preflight-only failure is preferable to package installation. Unlike Linux,
macOS always runs `npm ci` and a production PWA build for each new release.

The repository must be clean by default. `--allow-dirty-tree` is available for
an intentional local build; its release name is the Git `HEAD` plus a UTC dirty
timestamp, and only Git-tracked paths are copied into the release. This is not a
substitute for the reviewed production release workflow.

Existing directories under `~/.hermes/profiles` become the initial default,
interactive, and mutable profile lists (serialized as JSON so profile names are
not split accidentally). The selected `--hermes-profile` must exist when that
directory is present. A blank trusted Hermes SHA still keeps revision-gated
mutations disabled until the installed Hermes revision is audited.

The CLI is found first as `tailscale` in `PATH`, then at the Tailscale app's
bundled path `/Applications/Tailscale.app/Contents/MacOS/Tailscale`. When that
bundled binary is used, the installer sets `TAILSCALE_BE_CLI=1` for each call so
the command talks to the installed Tailscale backend. An operator can select a
CLI with `HERMES_CONTROL_INSTALL_TAILSCALE`; the same backend handling applies
when its value ends in the bundled app path.

### macOS files and LaunchAgents

The per-user installation is contained in these locations:

| Path | Purpose |
|---|---|
| `~/.agent-control/releases/<revision>` | Immutable application, PWA, and virtualenv |
| `~/.agent-control/current` | Active release symlink |
| `~/.agent-control/config/control.env` | Production configuration excluding the Hermes token; mode `0600` |
| `~/.agent-control/data/control.db` | Persistent SQLite database |
| `~/.agent-control/backups/` | Pre-deploy and scheduled SQLite backups |
| `~/.agent-control/logs/` | LaunchAgent stdout/stderr logs |
| `~/Library/LaunchAgents/com.agent-control.hermes-serve.plist` | Optional managed Hermes listener on port 9119 |
| `~/Library/LaunchAgents/com.agent-control.control.plist` | Agent Control on port 8000 |
| `~/Library/LaunchAgents/com.agent-control.backup.plist` | Backup at load and daily at 03:17 local time |

If port 9119 is already occupied, the installer aborts. Use
`--skip-hermes-service` only to reuse an intentionally managed local listener;
the matching Keychain item must already exist and pass an authenticated
`/api/profiles` probe before installation proceeds. Existing LaunchAgent files
with these labels must contain the installer-managed marker; the installer will
not overwrite an unrelated plist. An occupied port 8000 is accepted only when
the Control plist is already installer-managed.

Before it rewrites the installation, a reused `control.env` must be a regular,
user-owned mode-`0600` file and retain the production security contract: a
valid vault key, coherent absolute SQLite path/URL, absolute backup path, exact
loopback dashboard URLs, empty legacy Hermes API URL/key, the current node's
exact Tailscale origin, secure cookies, automatic schema creation disabled,
real provider mode, and disabled mock fallback. The database parent and backup
directory must be nonsymlink, user-owned mode-`0700` directories; an existing
database must be a nonsymlink, user-owned mode-`0600` regular file. A persisted
dashboard token, unsafe custom storage path, or widened setting causes an abort
rather than a silent rewrite. New SQLite files are created under a private
umask and normalized to mode `0600` after migration.

These are user LaunchAgents, not system daemons. Agent Control is available only
while that user is logged in and the Mac is awake and reachable; sleep, logout,
reboot-before-login, and network transitions can interrupt access. `launchd`
restarts jobs after the user session resumes, but this layout is not an
always-on substitute for the Linux systemd deployment.

### macOS options

| Option | Effect |
|---|---|
| `--dry-run` | Print the plan without changing the host. |
| `--install-packages` | Explicitly retain the default behavior of installing missing Python, Node, SQLite, and Git packages with Homebrew. |
| `--no-install-packages` | Fail instead of installing missing build dependencies. |
| `--allow-dirty-tree` | Build from a checkout with tracked or untracked changes; only tracked paths enter the release. |
| `--repo-root PATH` | Use another Agent Control Git checkout. |
| `--hermes-bin PATH` | Select an absolute Hermes CLI path. |
| `--trusted-hermes-sha SHA` | Set an independently audited, exact 40-hex Hermes trust anchor. |
| `--allowed-origin URL` | Explicitly supply the current node's exact discovered `https://…ts.net` origin, with no port, slash, query, or fragment. |
| `--admin-username NAME` | Set the username used only when creating the first administrator; default `admin`. |
| `--skip-hermes-service` | Reuse an authenticated Hermes listener already running on `127.0.0.1:9119`. |
| `--skip-tailscale-serve` | Leave Serve unchanged; Tailscale must still be connected and the allowed origin remains this node's exact `.ts.net` origin. |
| `--hermes-profile NAME` | Select the profile used by `hermes serve`; default `default`. |
| `--rotate-hermes-token` | Replace the current user's Keychain dashboard token and prompt securely for the new value. |
| `-h`, `--help` | Print installer help. |

## Completion, reruns, and recovery

An install is complete only after all of its checks pass. Both installers:

1. authenticate the Hermes `/api/profiles` endpoint on loopback;
2. migrate SQLite and preserve an existing administrator;
3. start Control and require `/api/v1/health` to succeed;
4. require strict readiness with `status=ready`, `database=ready`, and
   `upstream=online` rather than accepting a stale or merely live process;
5. verify the public Tailscale origin and validate the PWA HTML/manifest; and
6. run the first backup using a collision-resistant filename, so simultaneous
   scheduled/manual backups do not share a temporary or final path.

Both validate that the HTML references the manifest and that the manifest has a
name, root-relative start URL, and icons. On macOS, `--skip-tailscale-serve`
also skips that public fetch because the installer does not own the route. A
readiness or required public-origin failure is an installation failure even
when services are already running.

Reruns reconcile managed host files while preserving the database, vault key,
administrator, backups, and Hermes token. Linux also
refuses to replace an active release with a different release ID through the
first-install path; use the reviewed [update and rollback runbook](update.md)
for upgrades.

The installers are rerunnable, but they are not transactions. A late failure
can leave a staged or active release, generated configuration, loaded unit or
LaunchAgent, migration, backup, or newly created Serve root in place. They do
not automatically undo host state or run an Alembic downgrade. Diagnose the
failed check, preserve the database and secrets, and rerun after correcting the
cause. For manual rollback, retain the previous immutable release and follow
the schema-compatibility and database-restore rules in
[update.md](update.md); never point an older binary at an incompatible migrated
schema.

## Separate remote Mac gateway workflow

To keep Control on Linux while connecting a different Mac's Hermes instance,
use the manual reverse-tunnel workflow in
[Add a macOS Hermes gateway](deployment.md#add-a-macos-hermes-gateway). It uses
`deploy/bin/hermes-macos-reverse-tunnel.sh` and separate launchd templates. No
local installer configures that SSH tunnel, and `deploy/install-macos.sh`
instead installs Agent Control itself on the Mac.
