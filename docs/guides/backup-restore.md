# Backup and restore

A backup is a consistent copy of the database and of every stored object (original files, extracted text). The
scripts work the same for local storage, the built-in S3 server and external S3 storage. Run them from the
repository root while the stack is running; they use `compose.prod.yaml` and `deploy/prod/.env` (or
`COMPOSE_FILE` and `COMPOSE_ENV_FILES` if set).

## What to keep

| What | How | Where to keep it |
|---|---|---|
| Database and objects | `deploy/scripts/backup.sh`, at least daily | Encrypted, off the host |
| Key ring `deploy/prod/secrets/keyring.json` | Copy once after install and after each key rotation | **Separately** from the data backups |
| Other secret files, `.env`, `plummy.toml`, override files | Copy after each change | With the key ring |

The key ring is deliberately not part of a backup: whoever holds both could decrypt provider keys and connector
tokens. Without it, a restored installation works, but provider keys must be entered again and sources connected
again.

## Backup

```bash
sh deploy/scripts/backup.sh /srv/plummy-backups
```

It creates `plummy-<UTC time>/` (mode `0700`) with:

| File | Contents |
|---|---|
| `database.dump` | `pg_dump` of the application database |
| `objects.tar` | every stored object, read through the application's storage layer |
| `manifest.json` | version, migration revision, `dump_started_at`, SHA-256 of both files |

Encrypt and move it off the host yourself, for example:

```bash
tar -C /srv/plummy-backups -cf - plummy-<time> | age -r <recipient> > plummy-<time>.tar.age
```

Deleted data stays in old backups until they expire; choose a retention period with that in mind.

## Deletions made after the backup

A restore brings back everything that existed when the dump started, including documents, knowledge bases,
sources, conversations and users deleted later. While the original installation is still available, export
those deletions so the restore can apply them again:

```bash
docker compose -f compose.prod.yaml --env-file deploy/prod/.env exec -T backend \
  python -m plummy.cli maintenance export-deletions --since <dump_started_at from manifest.json> --output - \
  > deletions.json
```

The file contains only identifiers, types and times. Changes that cannot be replayed automatically (users
disabled, roles and access revoked, passwords changed, sessions revoked) are counted in it: review them in the
audit log of the original installation and repeat them by hand.

## Restore

Restore into an empty installation (a new host, or after `docker compose … down --volumes`) that has the same
secrets, above all the same key ring:

```bash
sh deploy/scripts/restore.sh /srv/plummy-backups/plummy-<time> --deletions deletions.json
```

Without a deletions file, pass `--no-deletions` explicitly. The script checks the checksums, refuses a non-empty
database, restores the database, migrates it to the current version, imports the objects, applies the deletions,
and only then starts Nginx, so users never see deleted data. If it fails after the database was restored, start
again from empty volumes.

Afterwards check sign-in, search and an answer with a citation.

## Drill

CI runs a full backup, delete, export, restore and verify cycle on every change. Before each upgrade, repeat a
restore on a copy of your own installation.
