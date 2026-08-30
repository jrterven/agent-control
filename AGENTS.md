# Repository workflow

- After completing any task that changes the product, run the relevant tests and production build, commit the scoped changes, push them to the canonical branch, deploy the resulting immutable release to production, and verify both health/readiness and the public PWA assets before reporting completion.
- Do not deploy after read-only analysis, diagnosis, review, or status tasks that make no product changes.
- Never deploy a failing build. If tests fail, production has active work that makes the restart unsafe, required access is unavailable, or deployment verification fails, stop and report the blocker instead of claiming completion.
- Preserve unrelated working-tree changes and use the repository deployment runbook, backup, rollback, and migration checks for every production release.
