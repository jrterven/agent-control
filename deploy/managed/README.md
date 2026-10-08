# Managed Hermes distribution

This is separate from `/downloads/connector/`. The existing connector-only
installer, paired identity and user-owned Hermes remain supported.

## Build contract

`pins.json` fixes the audited Hermes Git revision, the portable CPython version,
three interpreter archive hashes, and the dependency-export tool. CI builds on
each native target. It never runs an upstream bootstrap script on users' hosts.

```sh
python -m pip --isolated install --index-url https://pypi.org/simple uv==0.12.15 setuptools==83.0.0 wheel==0.45.1
python deploy/managed/build.py --revision COMMITTED_SHA --platform linux-arm64 --output dist/managed
```

The checkout must be clean at that exact revision. `--hermes-source` can select
an already checked-out clean audited source; otherwise the builder fetches the
pinned commit from the explicit upstream repository. Portable Python downloads
are checked against committed SHA-256 values. Hermes dependencies, including
the native Anthropic provider, are exported from the upstream committed
`uv.lock` without resolving a replacement lock. Installation requires hashes
and precompiled wheels. Missing wheels fail the build; no source-build fallback
or dependency version substitution is allowed.

Hermes 0.21.6 runs on the pinned portable CPython 3.14.8. Its base dependencies
no longer include PyYAML, which both Control packages need; the supplemental
`connector-requirements.lock` takes PyYAML 6.0.3 and its wheel hashes from the
same upstream lock. The build installs both locks, runs `pip check`, and the
native smoke verifies every dependency declared by the bundled Control packages.

Hermes deliberately rejects ordinary wheel distribution because it would lose
source-relative assets. The runtime therefore retains the full audited source,
generates only its standard package metadata, and sets a fixed local
`PYTHONPATH`. It also contains both connector packages. The launcher uses only
its own Python, disables bytecode writes/user site packages, and supplies the
bundled certificate store. Build smoke tests move the complete tree to a path
containing spaces before importing native dependencies and running both CLIs.
It also starts an isolated loopback Hermes with disposable configuration and no
provider credentials, checks HTTP authentication, discovers the default profile,
and verifies the audited capabilities through the connector's real adapter.
`requirements.lock`, `licenses.json` and `build-provenance.json` travel inside
the signed runtime. Existing source licenses remain with their packages.

The upstream stamp writer records the exact Hermes commit and release with
`updateMechanism: external` and `payload: runtime`. Hermes PM's separate
`manifest.json` points to the sibling `python` dependency environment using
relative paths. This keeps bootstrap offline and relocatable without a legacy
in-tree venv or a live Git checkout; Control owns updates. Both identity files
are checked before signing and covered by the signed inventory. Certificate
and license paths come from the bundled interpreter instead of a fixed minor
Python directory. Lazy installs remain disabled in the launcher and services.

The Linux server smoke matrix uses clean Ubuntu 22.04/24.04 and Debian 12/13 images on
x86_64 and ARM64, without network or host development tools. This does not
replace full installation/service tests on clean systemd virtual machines.
Mac application and notarization tooling live under `macos/`.
Native Linux builds also render the production user unit in a temporary HOME and
check it with `systemd-analyze --user verify`, including paths containing spaces
and percent signs. Systemctl/loginctl actions are intercepted during rendering:
this never registers a runner service or changes lingering. It validates unit
syntax and the bundled executable, not login/logout/reboot behavior. A full
clean-systemd-VM installation and provider authorization remain acceptance work.

## Signing and publication

Native CI artifacts are unsigned inputs, not user downloads. On the trusted
signing Mac, finalize/sign every runtime Mach-O, then call
`create_manifest(root, revision, "macos-arm64")` and
`sign_manifest(root, private_key)` before sealing the outer application.
The Mac packager must finish app/DMG notarization, stapling and Gatekeeper
verification and produce its `.verification.json` receipt.

Download all three successful `managed-*` artifacts from the **same**
`managed-runtime.yml` run at `COMMITTED_SHA`. Extract the Mac runtime, then run
the following on the signing Mac (replace the uppercase placeholders with that
release's paths, certificate SHA-1 and Team ID). Notary credentials stay in the
existing `agent-control-notary` Keychain profile.

```sh
mkdir -p /ABSOLUTE_RELEASE_DIR/macos-runtime
tar -xzf /ABSOLUTE_RELEASE_DIR/native/agent-control-runtime-macos-arm64.tar.gz -C /ABSOLUTE_RELEASE_DIR/macos-runtime
python deploy/managed/macos/build_app.py \
  --runtime /ABSOLUTE_RELEASE_DIR/macos-runtime/agent-control-runtime \
  --output /ABSOLUTE_RELEASE_DIR/macos-app --revision COMMITTED_SHA \
  --identity DEVELOPER_ID_CERTIFICATE_SHA1 --team APPLE_TEAM_ID \
  --manifest-private-key ~/.config/agent-control-release/connector-signing.pem
python deploy/managed/macos/notarize_dmg.py \
  --app '/ABSOLUTE_RELEASE_DIR/macos-app/Agent Control.app' \
  --output /ABSOLUTE_RELEASE_DIR --work /ABSOLUTE_RELEASE_DIR/notary \
  --revision COMMITTED_SHA --identity DEVELOPER_ID_CERTIFICATE_SHA1 \
  --team APPLE_TEAM_ID --notary-profile agent-control-notary \
  --manifest-public-key '/ABSOLUTE_RELEASE_DIR/macos-app/Agent Control.app/Contents/Resources/runtime/runtime-public-key.pem'
```

Notarization exit status 3 means still pending: resume the identical notarization
command and work directory, without rebuilding or publishing a replacement.

```sh
python deploy/managed/prepare_release.py \
  --artifacts /ABSOLUTE_RELEASE_DIR/native \
  --output /ABSOLUTE_RELEASE_DIR/downloads \
  --revision COMMITTED_SHA \
  --private-key ~/.config/agent-control-release/connector-signing.pem \
  --dmg /ABSOLUTE_RELEASE_DIR/Agent-Control-COMMITTED_SHA-macos-arm64.dmg \
  --receipt /ABSOLUTE_RELEASE_DIR/Agent-Control-COMMITTED_SHA-macos-arm64.verification.json
```

The publisher requires both Linux builds and the verified DMG. It signs a
complete regular-file inventory for each Linux runtime and signs archive
checksums using the existing RSA release key. Runtime verification embeds the
public trust anchor in the executable package; the neighboring public-key file
is informational. An untrusted replacement key cannot authorize a runtime.
App runtime inventories must be generated after signing changes binary bytes.

Upload the immutable `agent-control/releases/<sha>` directory first, then
atomically replace `install.sh`, `VERSION`, `latest.json.sig` and `latest.json`.
Keep prior pointers for rollback. Never advertise a pending or failed build.
The existing cloud runbook still requires backup/migration rehearsal, idle
checks, immutable deployment, health/readiness and exact public asset checks.

## Linux installer

The guided command installs per-user under `$XDG_DATA_HOME/agent-control` or
`~/.local/share/agent-control`. It requires a regular user with a functioning
systemd user session plus curl, OpenSSL, tar and standard shell utilities. It
does not use sudo, install operating-system packages or alter another Hermes
service. Systemd lingering and lifecycle behavior belong to the shared engine.

The installer verifies RSA checksums before extraction, rejects archive links,
then checks the complete runtime manifest before starting setup. A repeated
command verifies and reuses an identical staged release. The engine handles
resumption, provider authorization, pairing and service activation. Interactive
input is attached to `/dev/tty`, including when the script arrives via a pipe.
Headless SSH works with device-code authorization or hidden API-key input.

Browser/computer-use/voice extensions are independent, explicit installations;
the base runtime never silently invokes upstream lazy installers. Only advertise
modules whose signed artifacts and platform-specific verification have passed.
Prepared browser archives may be supplied to the publisher with `--extras DIR`.
Each must have its `.descriptor.json` sidecar and pass signature, complete file
inventory and native certification checks. The publisher preserves those archive
bytes and embeds matching descriptors in each supported platform's runtime.
The Mac receipt's `extras` map must match the already sealed runtime catalog;
Mac browser downloads remain unavailable until their separate Apple signing and
notarization are implemented and verified. Optional modules never gate the base
installation and never cause an automatic operating-system package installation.

## Hermes 0.21.6 data migration and recovery

The 0.21.6 payload declares `dataSchemaVersion: 2`. This is Agent Control's
rollback compatibility boundary, not the SQLite version number. Hermes changes
`state.db` from schema 30 to 31 and its separate FTS layout from 2 to 3, adds
message identities and changes the FTS source projection. The older executable
is not certified to write those stores. Update, rollback and interrupted-update
recovery therefore refuse to cross the boundary before stopping the service.
Changing the manifest number or bypassing that check is not a migration.

Existing 0.21.2 updaters do not audit the new source revision. The operator must
stage the immutable release through the signed installer/archive verification
and execute the **new release's included Python and connector code** for this
one-time migration. The old updater's rejection must not be bypassed by adding
an unverified SHA to a local allowlist. Future schema-2 to schema-2 updates use
the ordinary lifecycle after the operator has completed this transition.

The operator cutover must satisfy all of these steps on each computer:

1. Verify archive signatures, the exact release and every runtime file before
   execution; use the existing host's account, service ownership and data paths.
   Check all profiles, operation receipts, pending human decisions and background
   tasks are idle. An unavailable inventory or uncertain operation blocks restart.
   Keep the connector drained throughout migration and readiness testing.
2. Stop only the owned supervisor and Hermes processes, confirm their locks have
   been released, then make a cold snapshot of the complete managed Hermes home
   (including named profiles, SQLite sidecars, configuration and credentials),
   setup state, connector configuration and identity/operation receipts. Preserve
   symlinks without following them, restrict the backup to the owner, and record
   a file/hash inventory without printing credentials. Keep the old signed
   release available. A live file copy is not a valid SQLite backup.
3. Rehearse opening **copies** of every profile's state databases under the new
   runtime. Check SQLite integrity, session/message counts, pending cron entries,
   profile identity and the new schema. Exercise native chat, cron, profiles and
   all three Control plugins against disposable homes. Keep the cold snapshot
   unchanged. Ensure sufficient disk space for copies and FTS work.
4. With maintenance still held, switch the setup/runtime and connector source
   identity together, start the new owned service, and verify fresh local
   readiness, profile inventory, chat capability and connector identity. Release
   maintenance only after the canary checks pass. Record the backup and both
   immutable releases for this host. Do not advertise the old release as an
   executable-only rollback for schema-2 data.
5. If activation or readiness fails **before maintenance is released**, stop the
   new owned processes and preserve the failed migrated home separately. Restore
   the cold home and matching setup/connector identity as a unit, then start the
   old signed runtime and verify readiness before releasing maintenance. Do not
   overwrite the backup or silently rerun uncertain operations. Once new user
   work has been accepted, restoring the pre-upgrade snapshot would discard it;
   stop and plan explicit reconciliation instead of automatic rollback.

The migration is an operator procedure, not a generic `--force` option. Its
private evidence and backups stay on the host; release artifacts never include
user homes, tokens, pairing identities or operation receipts.

Use the reviewed utility for database rehearsal; it verifies the exact clean
Hermes commit before importing its code and never writes to the input databases:

```sh
python deploy/managed/migrate_hermes_state.py rehearse \
  --input /PRIVATE_COLD_DATABASE_SNAPSHOT \
  --output /NEW_PRIVATE_REHEARSAL_DIRECTORY \
  --source /CLEAN_HERMES_818C13BE_CHECKOUT \
  --python /VERIFIED_RUNTIME/python/bin/python3
```

For the stopped operator cutover, `offline --input /OWNED_HERMES_HOME --output
/NEW_PRIVATE_MIGRATION_EVIDENCE --runtime-root /VERIFIED_RUNTIME
--offline-confirmed` verifies the runtime's full signed inventory before opening
the databases. It snapshots **all** state databases before any migration, runs
without real profile configuration or credentials, preserves the journal mode,
and verifies every original column and row afterward. It excludes derived FTS
tables and the schema/state metadata that the migration intentionally updates.
This database utility supplements the required complete cold home/identity
backup; it does not replace it or manage services, cron or maintenance admission.
For an external Git installation, the operator may instead supply `--source`
with a separate strictly clean 818c13be checkout and `--python` with the verified
staged interpreter. Do not use the working installation's source directory if it
contains a venv, install stamp or any other untracked/ignored file: source
verification deliberately refuses those. No migration mode fetches source or
installs dependencies at runtime.

An upstream 0.21.6 defect can commit the empty external-FTS migration and then
raise `no such savepoint: fts_align_empty`. The utility permits exactly one
second open only for audited source 818c13be, an empty schema-30 database with
the old external `messages` source and FTS marker absent/1/2, the exact exception
and migration stack, and a resulting schema-31/FTS-3 aligned source. Integrity,
foreign keys and all original rows must already pass before that second open.
Any other failure stops migration and leaves private before-images and logs for
operator recovery. Do not erase those logs or restart the old executable against
the failed migrated data. `verify_state_migration.py` certifies all three empty
states, nonempty history and rejection of unknown errors on every native CI
platform without modifying the audited Hermes source.
