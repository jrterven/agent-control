# Remote integration-test safety

Remote tests are opt-in and are skipped unless all required variables are set.
Two independent gates are required:

```dotenv
HERMES_REMOTE_TESTS=1
HERMES_REMOTE_MUTATION_TESTS=1
HERMES_REMOTE_MUTATIONS=I_UNDERSTAND_CONTROL_DEV_ONLY
HERMES_TEST_PROFILE=control-dev
```

`HERMES_REMOTE_TESTS=1` enables remote access in general. The additional
`HERMES_REMOTE_MUTATION_TESTS=1` selects the separate mutation module; neither
the normal suite nor `test-remote-readonly` imports it as an execution target.
Run it only with the explicit target below after inspecting the profile and
credentials:

```bash
make test-remote-control-dev-mutations
```

Read-only tests may query health, version, profiles, capability metadata and
existing session lists for `default`/Newton and `jarvis`/Jarvis. They may not
create/resume active sessions, send prompts, interrupt, edit configuration,
write secrets, trigger cron or delete/archive anything.

Before every mutating test, the fixture must compare the profile string exactly
to `control-dev`, verify the explicit mutation sentinel and include the resolved
gateway/profile in the test log. Parameterized mutation tests must not accept a
profile from ambient "active profile" state.

Test-created objects use a unique `hc-test-<run-id>` prefix and cleanup only IDs
recorded by the same run. Failed cleanup is reported for manual inspection; it
must never broaden into a profile-wide delete.

The mutation suite uses the stricter `hc-test-run-<uuid>` prefix. It covers a
single isolated session's prompt streaming, interrupt, durable resume/history
and deletion, plus paused cron create/list/update/delete when the exact probed
capabilities advertise those operations. Missing `control-dev`, dashboard
authentication, profile timezone or required capability produces an explicit
skip before that resource type is created. It never mutates Newton or Jarvis,
never triggers a cron job, and cleanup considers only IDs returned to the same
test run.

Profile lifecycle verification is not part of that suite because it deletes an
entire native profile. A manual release check may use only newly created,
random `hc-lifecycle-<run-id>` profiles whose exact IDs are recorded before the
first mutation. Both gateways must report a revision explicitly approved for
durable delete and transfer; currently that means
`4209d371aa1bb8840ce8447555bdd863a1a96c38` on both sides. Never substitute an
existing user profile. Verify destination state before source deletion, clean
only the two recorded temporary names, wait beyond one scheduler heartbeat and
confirm that neither profile reappears. If either gateway is Hermes 0.20.5,
skip the live profile test: its stale cron heartbeat can resurrect deleted
profiles and makes cleanup unsafe.
