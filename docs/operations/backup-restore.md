# Backup and restore

SQLite and the vault master key must be recoverable, but must never be stored in
the same backup artifact.

## Automated database backup

`hermes-control-backup.timer` invokes an online SQLite backup. It uses SQLite's
backup API instead of copying a live WAL database, validates `PRAGMA quick_check`,
and publishes the destination atomically. Backups are mode 0600 under
`/var/backups/hermes-control`. Each invocation uses an independent random
temporary suffix and retains that suffix in the final filename, so overlapping
timer, installer, or manual runs cannot share or overwrite a backup path.

`HERMES_CONTROL_DATABASE_PATH` is the filesystem path used by the backup job.
The script derives the path from `HERMES_CONTROL_DATABASE_URL` and refuses to
run unless both absolute paths match exactly; it cannot silently back up a
stale database while the API uses another one. The provided production example
pins both to `/var/lib/hermes-control/control.db`. Create the backup directory
ahead of time as `hermes-control:hermes-control` mode 0700; the hardened unit
intentionally cannot write elsewhere.

The timer intentionally performs no automatic deletion. Configure encrypted,
off-host retention in the operator's backup system only after successful copies
are independently verified.

## Vault key custody

Back up `HERMES_CONTROL_VAULT_KEY_B64` in an access-controlled secret manager. Never
put it in the database archive, source repository, container image, systemd unit
or CI logs. Losing it makes stored gateway and owner-scoped integration
credentials unrecoverable; exposing it together with the database defeats
encryption at rest.

## Restore drill

The repository includes `deploy/bin/restore-sqlite.sh`. It refuses to run
without the explicit `--control-stopped` acknowledgement, validates both the
source and staged database, quarantines the current DB/WAL/SHM set, and only
then replaces the database atomically. Its destructive scope is limited to the
single absolute destination path supplied by the operator.

Example (while `hermes-control.service` is stopped):

```bash
sudo -u hermes-control deploy/bin/restore-sqlite.sh --control-stopped \
  /var/backups/hermes-control/control-YYYYMMDDTHHMMSSZ-XXXXXX.db \
  /var/lib/hermes-control/control.db \
  /var/lib/hermes-control/restore-quarantine
```

The script prints the exact quarantine directory. Keep it until all post-start
checks pass.

1. Select a validated backup and record its hash and timestamp.
2. Stop Hermes Control; leave Hermes services running.
3. Copy the current database, `-wal` and `-shm` files (when present) into a
   dated quarantine directory before altering anything. Keep this quarantine
   separate from the vault-key backup.
4. Run the restore script above as `hermes-control`; it stages the selected
   database mode 0600 and checks `PRAGMA integrity_check`.
5. Confirm the printed quarantine contains the previous DB and any WAL/SHM.
6. The script atomically replaces `control.db` and moves stale sidecars only
   after the quarantine copy exists.
7. Start Control, let Alembic migrate forward, then verify login, gateway
   credential decryption, owner-scoped integration presence/decryption, session
   routing and audit continuity. A read check must never reveal the integration
   key; run an external provider test only with the owner's explicit intent.
8. If any check fails, stop Control and atomically restore the quarantined set.

Do not restore Hermes `state.db` from a Control backup: Control does not own or
back up Hermes internal data.

Single-use transcription tokens, microphone audio and provider events are not
backup data because Control never persists them. A restored encrypted
ElevenLabs key remains bound to its original owner ID and can be decrypted only
with the matching external vault key.

The automated regression in `tests/backend/test_backup_restore_scripts.py`
performs this drill against temporary databases and proves the restored data,
permissions, integrity and quarantine contents.
