# Update and rollback

Hermes Control and Hermes are independent release streams. Updating Control must
not update, reset, patch or check out the Hermes repository.

## Control release

1. Build and test an immutable revision on Mac/CI, including the mock fault
   suite and frontend secret scan.
2. Back up SQLite and verify the backup before running migrations.
3. Install into a new `/opt/hermes-control/releases/<revision>` directory and
   create its virtualenv/assets without changing the current symlink.
4. Run an offline configuration/preflight command against production settings.
5. Stop Control, switch `/opt/hermes-control/current`, start it and inspect
   migrations, liveness, readiness and realtime reconnection.
6. Smoke-test Newton/Jarvis with read-only probes and use `control-dev` for the
   automated mutation path; runtime permissions are validated from the public
   capability projection without mutating the user's normal profiles.

For rollback, stop Control and switch back only if the earlier binary supports
the migrated schema. Otherwise restore the pre-update database first. Never run
an Alembic downgrade against production without a migration-specific reviewed
procedure.

## Hermes compatibility change

An operator may update Hermes separately after its own backup/maintenance plan.
Before accepting a new revision for Control:

- record version and source SHA;
- compare relevant protocol sources against the compatibility matrix;
- run mock/adaptor contract tests;
- probe the real gateway read-only;
- run all mutations only under `control-dev`;
- keep unsupported capabilities disabled until verified.

No Control startup, deploy or health check may execute `hermes update`.
