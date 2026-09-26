# Coordinated connector updates

The cloud and native runtimes have independent releases. Deploying web-only
changes does not require restarting every computer. A native release becomes
eligible only when its signed `latest.json` publication supports updater protocol
1. Both `/downloads/agent-control/` and `/downloads/connector/` publish this
metadata; immutable archives and their signed checksums remain unchanged.

New supported installations default to automatic updates when idle. Onboarding
discloses this preference; owners can disable it, postpone 24 hours, or request
an update in **My computers**. Preferences and one-use request identities are
durable and delivered after reconnect. The cloud sends no shell commands,
executable paths, arbitrary URLs, credentials or installation parameters.
Old connectors continue working and display one-time local upgrade guidance.

The heartbeat reports the actual running Git release, supported updater
protocol, last check, offered release and bounded status. Package version
`0.1.0` is no longer used as evidence that a native feature is installed.
Features remain gated by their actual native capabilities. Unsupported chat
modes link to **My computers**.

## Local transaction

The connected runtime starts an independent systemd user unit on Linux or
launchd job on macOS. A local kernel lock prevents overlapping workers. The
worker checks signed metadata every six hours with jitter; explicit requests
and preference changes wake it sooner. Busy computers retry their idle check
after one minute. Disconnected computers never initiate a cutover.

The worker validates the pinned release key, protocol, monotonic publication
sequence and stable rollout cohort before selecting a release. It stages only
that verified revision, checks the owner's preference again, and uses the
existing lifecycle transaction. Maintenance blocks new writes before a fresh
inventory scan. Active/unknown tasks, pending delegated completions, operation
receipts, and **every open temporary chat**, even idle, prevent restart. A
temporary chat's normal close/expiry releases the gate; the updater never closes
one itself. No prompt is retried or replayed as part of an update.

Managed updates verify both runtime inventories, require compatible data
schemas, preserve an owned data backup, retain the prior runtime and verify
service readiness. The signed macOS helper performs app replacement and
SMAppService registration. OS permission failures need local attention. An
interrupted transaction requires explicit recovery; unattended checks never
invoke recovery. A failed target is quarantined from automatic retries until
another target or an explicit owner retry. Completion requires observing the
new release connected, not merely finishing a download.

Managed Hermes boots only after preparing its bundled policy/media/background
adapters, avoiding an extra activation restart. Existing-Hermes installations
update the owned connector bundle while retaining the external Hermes home,
source and credentials; they never start or stop the external Hermes process.

Tool dependencies belong in `tool-environments/default`, outside the signed
runtime. Managed tools receive its Python/pip on PATH and pip requires a virtual
environment. Existing custom environments remain intact. If an old runtime
already contains manually installed dependencies, integrity verification stops
the update: preserve and migrate those integrations explicitly before retrying.
Never disable inventory checks or delete unknown user dependencies.

## Publish and widen a release

1. Run the tests/builds and the cloud release, managed runtime and native signing
   procedures at the same committed revision. Publish complete signed downloads
   and compatible cloud API. Both publishers start with **0% automatic rollout**.
2. On operator-owned canaries, use **Update now**. This opts the computer into a
   staged release but still enforces signature, idle, temporary-chat and recovery
   gates. Bootstrap old connectors once with their local updater. Verify the
   running release, connection, native capabilities, preserved identity/history
   and updater diagnostics for both managed and standalone installations.
3. Advance each tested download channel independently, using the existing
   private signing key on the trusted signing host:

   ```sh
   python deploy/update_policy.py \
     --directory /ABSOLUTE_DOWNLOAD_TREE/agent-control \
     --revision COMMITTED_SHA \
     --private-key ~/.config/agent-control-release/connector-signing.pem \
     --rollout-percent 10
   ```

   Use `connector` for the standalone channel. Republish both `latest.json` and
   `latest.json.sig`. After checking fleet error/status counts, advance to 100.
   Signing keys never enter the cloud or a customer's computer. Each policy
   revision increases the sequence and preserves the immutable artifact paths.
4. If verification fails, sign the same publication with `--paused` and stop
   widening. A pause blocks both automatic and explicit new requests after the
   next check; already admitted local transactions finish or recover locally.
   Keep the prior archives and use the established rollback runbook. Never
   downgrade a live data schema automatically.

The initial release needs a local update on legacy computers. The web cannot
give an older executable capabilities it does not implement. macOS can also
require a one-time Keychain or background-service permission approved locally.
