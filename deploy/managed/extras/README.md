# Browser extra: release and installation gates

The managed base installs terminal, files and cron tools. Browser support is
optional and appears only when the **signed base runtime manifest** contains a
`extras.browser` descriptor for that exact release and platform. An absent
descriptor means unavailable; the installer must not fall back to npm, npx,
pip, a system browser, or an upstream “latest” download.

## Reviewed dependency lock

`pins.json` fixes Node **22.23.2**, agent-browser **0.26.0**, and Chrome for
Testing **153.0.8010.47**, including platform URLs, SHA-256 and exact sizes.
Node hashes were checked against the release's official SHASUMS256; the npm
package also records the registry SHA-512 integrity value. Chrome checksums
were calculated from the exact versioned official artifacts. Every build
revalidates SHA-256 and size before extraction.

Sources: [Node release](https://nodejs.org/dist/v22.23.2/),
[agent-browser package](https://registry.npmjs.org/agent-browser/0.26.0),
[Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/).

This agent-browser version ships a native Rust CLI/daemon with no npm runtime
dependencies. The package includes the locked native binary and Node, but no
npm/npx executables or installation scripts. A wrapper supplies the bundled
Chrome path explicitly, including when Hermes filters inherited environment
variables. There are no Python extension modules; `pythonAbi` is null.

Changing any component requires reviewing the upstream source/artifacts,
updating every affected platform hash and size, rerunning native certification,
and producing a new immutable signed release. Never edit an already published
descriptor or archive.

## Native build and certification

Run on a clean native Linux x86_64 or ARM64 host as an unprivileged user. Use
Python 3.12 and a full committed source SHA. No local Hermes profile is needed.

```sh
python3 deploy/managed/extras/build.py \
  --platform linux-arm64 \
  --revision FULL_COMMITTED_SHA \
  --output /tmp/browser-build \
  --cache /tmp/browser-cache
```

The builder downloads only the pinned artifacts, rejects unsafe extraction,
and checks native Node/agent-browser/Chrome versions. It then opens
`about:blank` through agent-browser in a temporary HOME/profile, reads the URL,
and closes the unique test session. The daemon receives a five-second idle
timeout. There is no AI/model call or existing user profile. Chromium retains
its sandbox; `--no-sandbox` is never supplied. Only a successful smoke writes
`certification: {native: true, offlineBrowser: true}` and the build archive.

The separate `managed-browser-certification.yml` workflow performs this on
Ubuntu 22.04 x86_64 and ARM64. Failures remain visible; there is no
`continue-on-error`. A failed platform uploads no archive and must have no
descriptor. The managed base runtime workflow is independent. Passing a base
job does not certify browser support.

The builder canonicalizes file modes to 0644/0755. No SUID sandbox binary,
privileged service, kernel setting, or operating-system package is installed.

## Signing and immutable publication

The operator consumes a successful native CI artifact, never an archive from a
failed or cross-platform smoke. Prepare it with the same RSA signing key as the
managed base runtime; the private key must be a private regular file.

```sh
.venv/bin/python deploy/managed/extras/prepare.py \
  --archive /tmp/browser-build/agent-control-browser-linux-arm64.tar.gz \
  --output /tmp/browser-signed \
  --revision FULL_COMMITTED_SHA \
  --private-key /secure/path/release-signing-key.pem
```

The result is `agent-control-browser-PLATFORM.tar.gz` plus its
`.tar.gz.descriptor.json` sidecar. The signed inner manifest binds release,
platform, component versions, certification, executable paths, and every
regular file's hash, size and mode. The outer descriptor binds the final
compressed archive's hash and size, and an exact same-origin immutable URL:

```text
/downloads/agent-control/releases/FULL_SHA/agent-control-browser-PLATFORM.tar.gz
```

Before signing each corresponding base runtime, put
`{"browser": <descriptor>}` into its `extras-catalog.json`, or supply that
catalog to `create_manifest(..., extras=...)`. The catalog is included in the
base inventory and signed manifest. Then pass the same already-prepared
archive directory to the managed release publisher's `--extras` option. Its
`verify_prepared_extra` check validates the compressed digest, pinned public
key, inner RSA signature and inventory, even when publishing Linux artifacts
from a Mac. Do not repackage/resign extras after base manifests are signed.

## Explicit installation and host checks

`extras-list` reads signed availability. `install-extra {id: "browser"}` is an
explicit user action, supported only in managed mode. It verifies the base
manifest, exact archive size/hash, safe extraction and inner signature before
using any executable. Existing Hermes installations keep their original
management flow.

Linux first checks Chrome's shared libraries using `ldd`. Missing libraries
return `dependencies-required` and their names. An administrator can identify
the corresponding distribution packages using apt or the distribution's
package manager. The app never runs sudo, installs packages, or changes OS
configuration on the user's behalf.

While the managed installation is idle, a second diagnostic opens only
`about:blank` with direct Chromium, a temporary HOME/profile, background
networking disabled, and a non-listening loopback proxy. It is limited to 30
seconds and cleans its own process group. Missing sandbox support returns
`sandbox-blocked`, the verified executable path, and a documentation link;
other launch failures return `diagnostic-failed`. Neither activates the browser
tool nor changes the Hermes configuration.

An administrator may need a narrowly scoped AppArmor policy for the verified
Chrome executable when the host restricts user namespaces. Do not disable
AppArmor globally, disable the Chromium sandbox, or grant a directory-wide
exception automatically. Review the host's policy and rerun explicit
installation after the prerequisite is resolved. Chromium documents the
[relationship between AppArmor and user namespaces](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).

Successful installation adds `browser` to both `tools.enabled_toolsets` and
`platform_toolsets.cli`, and records the verified immutable root in setup
state, with `restartRequired: true`. It requests an explicit idle service restart; it does not start a
personal browser session or restart services automatically. The supervisor
uses `browser_environment` only for a matching signed **origin runtime** catalog
and rechecks installed integrity. `validate_extra_transition(engine,
target_root)` returns a canonical complete extras map without mutation, or
raises before services are stopped. The lifecycle stores that map in the new
state. A verified browser survives Agent Control update/rollback even when the
destination release has no browser descriptor: its original signed runtime
must remain available at `extras.browser.releaseRoot` and match its recorded
release. Cleanup must retain every referenced origin runtime. An optional
`origin_root` argument permits an already-existing verified relocated copy.
The target platform and audited Hermes source SHA must match the origin;
changing either blocks the transition until browser compatibility is certified.
No upstream auto-installer or silent tool removal is used. Mac app replacement
currently blocks non-empty extra state explicitly because no Mac browser extra
has been certified and source-app retention needs separate validation.

## Current certification limitations

The isolated ARM64 test on ASUS verified all three native component versions,
but Chromium reported **No usable sandbox**. No host policy was changed and no
certified ARM64 browser archive was produced by that test. A successful native
CI artifact is still required before publishing a Linux descriptor, and host
installation must pass the local diagnostic as well.

Mac source artifacts are pinned for review, but Mac browser build/publication
is deliberately rejected. Chrome.app requires preserved framework layout and
verified nested Apple signing/notarization; the current base application's
flat Mach-O signer does not certify this module. Omit the Mac browser
descriptor until that separate release process is implemented and verified.
This restriction does not block the base macOS managed installer.
