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
