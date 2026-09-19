# Managed Hermes installation

Agent Control now has two onboarding paths: install an owned Hermes runtime, or
connect an existing audited Hermes. The connector-only installer remains under
`/downloads/connector/`. Managed downloads are published independently under
`/downloads/agent-control/`; the web does not advertise missing artifacts.

## Supported delivery targets

The app targets macOS 13+ on Apple Silicon. Install it in
`~/Applications/Agent Control.app`; the native launcher refuses another location.
Its SMAppService LaunchAgent runs only while the user is logged in and awake.
The wizard explains any approval needed in Login Items. Existing Intel Mac
Hermes installations continue using the standalone connector.

The Linux command targets Ubuntu 22.04/24.04 and Debian 12/13, x86_64 and ARM64,
with a functional systemd user session. It requires curl/OpenSSL/tar and normal
shell utilities, but no host Python, Git, Node or compiler. It checks lingering
and reports an administrator action if the session cannot persist after SSH
closes. Never treat a failed lingering check as successful installation.

The bootstrap preserves the archive's signed file permissions during extraction,
including when it runs with `umask 077`. Installation and temporary download
directories remain private (`0700`). Do not relax the manifest checks or change
runtime permissions to work around a verification failure.

If an earlier installer failed with `Runtime file metadata changed` before setup,
rerun the published command. It verifies a fresh download before continuing;
there is no need to revoke the computer or remove existing Hermes data.

Build/import/container checks and actual localhost Hermes startup are automated.
Fresh-machine login, OS background-service permissions, real provider accounts,
and first-message usability still require the acceptance matrix below; an import
check does not certify those scenarios.

## Ownership and secrets

Managed data lives at `~/Library/Application Support/Agent Control/managed` on
Mac and `${XDG_DATA_HOME:-~/.local/share}/agent-control` on Linux. The owned
`hermes-home` contains configuration, histories and workspaces. Code lives in an
immutable signed app/runtime and is never modified by lazy dependency installs.
Hermes is bound to a numeric loopback address with a generated private token.

The existing `~/.agent-control-connector` identity is authoritative. A paired
computer is not enrolled again or silently switched to a different Hermes.
Existing mode preserves its Hermes home/configuration and runs only the owned
connector supervisor. It never starts or stops the external Hermes server.

Connector/dashboard credentials use Keychain on Mac and mode-0600 files on
Linux. Provider secrets use Hermes's protected local store. They are configured
over authenticated loopback before cloud pairing. Neither API keys nor provider
OAuth tokens enter the cloud pairing request or CLI arguments. The separate PWA
voice configuration remains unchanged; ChatGPT login is not a voice API key.

OpenRouter uses PKCE with a one-use random callback path on loopback in the Mac
app. Headless Linux uses a hidden API-key prompt. ChatGPT uses Hermes's existing
device-code implementation; cancellation cancels the corresponding local flow.
OpenAI/Anthropic/Gemini keys are checked with a read-only provider request before
saving. No test completion is generated or billed automatically.

## Lifecycle

Mac offers status, diagnose, updates, rollback, extras and uninstall in the app.
Linux maintenance uses the installed launcher, which selects its bundled Python:

```sh
~/.local/share/agent-control/current/bin/agent-control-setup --diagnose
~/.local/share/agent-control/current/bin/agent-control-setup --update
~/.local/share/agent-control/current/bin/agent-control-setup --rollback
~/.local/share/agent-control/current/bin/agent-control-setup --extras
~/.local/share/agent-control/current/bin/agent-control-setup --install-extra browser
~/.local/share/agent-control/current/bin/agent-control-setup --restart
~/.local/share/agent-control/current/bin/agent-control-setup --uninstall
~/.local/share/agent-control/current/bin/agent-control-setup --resume
```

Use the corresponding XDG location when customized. Updates verify signatures
and inventories, request connector maintenance, inspect every local profile and
the durable operation ledger, and refuse active, unknown or unreachable work.
They snapshot the owned Hermes data after the service stops. Automatic rollback
is restricted to the same data schema; history and operation receipts are never
reset or replayed. A durable pending update requires explicit recovery.

Uninstall unregisters the owned service and preserves user data, pairing and
operation receipts. Revoke the computer in the web to invalidate its credential.
Data erasure is a separate explicit operation, not a side effect of uninstall.

Browser is an optional, versioned module. Its availability comes from signed
release metadata after native verification. Missing Linux system libraries are
reported; the installer does not silently run sudo. Computer control, local
speech models and further integrations are outside this initial module.

## Release and acceptance

Use [managed distribution](../../deploy/managed/README.md) for exact build,
manifest, signing and notarization commands. The runtime includes a dependency
lock, license inventory and source provenance. The publisher requires both Linux
architectures and an accepted, stapled Mac DMG before publishing download
pointers. Never publish diagnostic snapshots or an unsigned fallback.

Follow [the cloud runbook](cloud.md) for scoped commit/push, immutable cloud image,
backup/restore and migration rehearsal, idle cutover and public PWA verification.
Migration `0024_managed_installation` adds nullable diagnostic columns to pairing
requests and connectors; they confer no additional authorization. Existing
connectors continue to work without sending these fields.

Acceptance evidence must identify the exact artifact hashes and cover:

- Clean Mac with quarantine and clean supported Linux machines; first install,
  API/device authorization, pairing and an explicitly sent first message.
- Existing Hermes, an already paired device, occupied ports, interrupted
  downloads, cancelled/expired authorizations and missing provider permissions.
- Logout/reboot, sleep/reconnect, denied background execution, SSH disconnect,
  active/uncertain-work refusal, update rollback and interrupted-update recovery.
- No credential/transcript output, preserved other services, optional-browser
  prerequisites, and uninstall with data retained.

Before widening the beta, run the first-chat task with 3–5 nontechnical users.
Measure time after download with their provider account ready; target under ten
minutes. Record stage/result/version/OS/duration only, never their conversation,
account credentials or personal file paths.
