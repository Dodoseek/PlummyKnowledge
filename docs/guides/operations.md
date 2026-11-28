# Operations

Commands below use `dc` as shorthand for the Compose command of your installation:

```bash
alias dc='docker compose -f compose.prod.yaml --env-file deploy/prod/.env'
```

## Health and logs

- `GET /api/v1/health/live` — the process is up; `GET /api/v1/health/ready` — database, Redis, storage, migrations
  and the key ring are usable. Compose health checks use them.
- `dc ps` shows the health of every service; `dc logs -f backend worker worker-ingestion` follows the logs. Logs
  are JSON lines; API requests carry a `request_id`, which error responses return as
  `correlation_id` (and the `X-Request-ID` header). Logs never
  contain secrets, document text or prompts.
- The **Jobs** page (user menu; administrators and knowledge managers) lists ingestion and sync jobs with their
  stage, attempts and error code; a job can be stopped or retried there.
- The **Audit log** page (role `auditor` or `admin`) shows security events: sign-ins, setup, changes of users,
  roles, access, providers, knowledge bases and sources.

## Server CLI

The CLI runs inside a container on the server; there is no remote equivalent. Passwords are read from a hidden
prompt or `--password-stdin`, never from arguments.

| Command | Where | Purpose |
|---|---|---|
| `setup status`, `setup init-token [--rotate]` | `dc run --rm migrate python -m plummy.cli …` | Setup state; issue a new setup token |
| `admin create --email … --display-name …` | `dc run --rm -it migrate python -m plummy.cli …` | Create an administrator (also the first one) |
| `admin reset-password --email …` | `dc run --rm -it migrate python -m plummy.cli …` | New password; all sessions of the user are revoked |
| `secrets status`, `secrets rotate`, `secrets generate-key` | `dc exec backend python -m plummy.cli …` | Key ring ([below](#key-ring-rotation)) |
| `maintenance export-deletions …` | `dc exec backend python -m plummy.cli …` | [Backup and restore](backup-restore.md) |
| `maintenance prune-audit [--dry-run]` | `dc run --rm --no-deps migrate python -m plummy.cli …` | [Audit retention](#audit-retention) |
| `db grant-runtime` | `dc run --rm migrate python -m plummy.cli …` | Reset the runtime role's grants (`migrate` does it on every start) |
| `worker check` | `dc exec worker python -m plummy.cli …` | Send a test task through the queue |

Every command that changes something is recorded in the audit log.

**Lost the administrator password?** `admin reset-password` or `admin create` on the server. The setup wizard does
not open again once the installation is set up.

## Audit retention

Audit records older than `AUDIT_RETENTION_DAYS` (365 by default) are deleted by `maintenance prune-audit`. Only the
schema owner may delete them, so the command runs in the `migrate` container from a host timer, not from the
application's scheduler. Example with systemd:

```ini
# /etc/systemd/system/plummy-audit-retention.service
[Service]
Type=oneshot
WorkingDirectory=/opt/plummy
ExecStart=/usr/bin/docker compose -f compose.prod.yaml --env-file deploy/prod/.env run --rm --no-deps migrate python -m plummy.cli maintenance prune-audit

# /etc/systemd/system/plummy-audit-retention.timer
[Timer]
OnCalendar=weekly
Persistent=true
[Install]
WantedBy=timers.target
```

Each run records `maintenance.audit_pruned` with the number of records and the cut-off.

## Key ring rotation

The key ring encrypts provider keys and connector tokens stored in the database. To replace the key:

1. `dc exec backend python -m plummy.cli secrets generate-key` prints a new entry (the only command that prints a
   key).
2. As root, add the entry to `keys` in `deploy/prod/secrets/keyring.json` and make its id the `primary`. Keep the
   old key in the file for now.
3. `dc restart backend worker`.
4. `dc exec backend python -m plummy.cli secrets rotate` re-encrypts the records still on the old key.
5. When `secrets status` shows no records on the old key, remove it from the file, restart again, and back up the
   new key ring.

If a key is removed too early, readiness reports `secrets: fail` and the affected keys must be entered again.

## Scaling

One host is the supported deployment model; services scale independently on it.

| Service | How | Notes |
|---|---|---|
| `backend` | `dc up -d --scale backend=N`, then `dc restart nginx` | Each replica holds up to 10 database connections |
| `worker` | `WORKER_CONCURRENCY` and/or `--scale worker=N` | Provider quotas limit what more embeddings workers achieve |
| `worker-ingestion` | `--scale worker-ingestion=N` only | `INGESTION_MEMORY_LIMIT` (4 GB) **per replica** |
| `scheduler` | exactly one | A second one would duplicate periodic jobs |
| `postgres`, `redis`, `s3` | vertically: `*_CPUS`, `*_MEMORY_LIMIT` | Keep the sum of all connection pools below PostgreSQL's `max_connections` (100) |

Jobs that stay `queued` on the **Jobs** page mean too few workers (`ingestion` jobs: `worker-ingestion`;
`embeddings`: `worker`). `OOMKilled` in `docker inspect` means a memory limit is too low. `429 rate_limited` in
chat is the per-user limit of two simultaneous answers or the general rate limit, not a lack of API replicas.

## Security updates

Watch the repository's releases and [SECURITY.md](../../SECURITY.md). Upgrades also rebuild images on the latest
patched base images. CI scans the production images with Trivy on every change.
