# Installation

This guide installs the production stack (`compose.prod.yaml`) on one Linux host. For a local development stack,
see the [README](../../README.md#quick-start-development).

## Requirements

- Linux with Docker Engine and Docker Compose v2, run as `root` or with `sudo` for the secret files.
- 4 CPU cores and 12–16 GB of RAM: the default memory limits of the services add up to about 9.5 GB, of which
  the parser worker takes 4 GB (`INGESTION_MEMORY_LIMIT`). Disk for documents, the database and at least one
  backup.
- A DNS name and a TLS certificate for it. Obtaining and renewing the certificate (for example with ACME) is done
  outside the stack.
- Outbound HTTPS from the host to your AI providers and, if used, Google or Microsoft.

## What runs

| Service | Role |
|---|---|
| `nginx` | The only published service: TLS, the web app, reverse proxy to the API |
| `backend` | REST API and answer streaming |
| `worker` | Chunking, embeddings, connector sync, maintenance |
| `worker-ingestion` | Document parsing with Docling; no internet access and no secrets |
| `scheduler` | Periodic jobs (exactly one instance) |
| `migrate` | One-off: database migrations, database grants and the setup token |
| `postgres`, `redis` | Database with pgvector, queues and rate limits |
| `s3`, `s3-init` | Built-in S3-compatible storage (profile `s3`), optional |

Containers run with read-only file systems, no capabilities and resource limits. PostgreSQL, Redis, S3 and the
workers sit in internal networks without internet access.

## Steps

All commands run from the repository root. Check out a release tag, not a branch:

```bash
git clone https://github.com/Dodoseek/PlummyKnowledge.git /opt/plummy && cd /opt/plummy
git checkout v0.5.0   # the latest release from the Releases page
```

### 1. Secrets

```bash
sudo sh deploy/prod/init-secrets.sh
```

The script creates random passwords for PostgreSQL, Redis and the built-in S3 server, the session `SECRET_KEY`,
the key ring that encrypts provider keys and connector tokens, and connection URLs. They are files in
`deploy/prod/secrets/`, readable only by the service that needs them; values are never printed. Existing files
are kept, so the script is safe to run again.

### 2. Environment

```bash
cp deploy/prod/prod.env.example deploy/prod/.env
```

Set at least `PUBLIC_URL` (the `https://` address users open) and `PLUMMY_SERVER_NAME`. Secrets never go into
`.env`. All settings are described in [Configuration](configuration.md).

### 3. TLS certificate

Put the certificate chain and key into `deploy/prod/certs/fullchain.pem` and `deploy/prod/certs/privkey.pem`, or
point `PLUMMY_TLS_CERT` and `PLUMMY_TLS_KEY` in `.env` at them. Nginx reads them as uid `101`: give the key mode
`0400` with owner `101` (or `0444` inside a `0700` directory).

### 4. Storage

- **Built-in S3 server** (default): keep the storage settings from the example and start with `--profile s3`.
- **External S3-compatible storage**: set `S3_ENDPOINT_URL` (`https`), `S3_BUCKET` and `S3_REGION` in `.env`, put
  the access key into `deploy/prod/secrets/s3_access_key_id` and `s3_secret_access_key` before the first start, and
  start without `--profile s3`. Create the bucket yourself and give the key access to that bucket only.

### 5. Start

```bash
docker compose -f compose.prod.yaml --env-file deploy/prod/.env --profile s3 up -d --build --wait
```

`migrate` applies the migrations before the other services start. `--wait` returns when every service is healthy.

### 6. Back up the key ring

Copy `deploy/prod/secrets/keyring.json` to a safe place **now**, separately from data backups. Without it, the
stored provider keys and connector tokens cannot be decrypted.

### 7. First administrator

Read the one-time setup token and open `PUBLIC_URL`; the setup wizard asks for it:

```bash
docker compose -f compose.prod.yaml --env-file deploy/prod/.env run --rm migrate \
  cat /var/lib/plummy/bootstrap/setup-token
```

The token is valid for 24 hours (`BOOTSTRAP_TOKEN_TTL`). If you prefer not to expose the wizard at all, set
`SETUP_MODE=cli_only` and create the administrator on the server:

```bash
docker compose -f compose.prod.yaml --env-file deploy/prod/.env run --rm -it migrate \
  python -m plummy.cli admin create --email admin@example.com --display-name "Admin"
```

### 8. Check

- `https://<your host>/api/v1/health/ready` answers `200`.
- Sign in, add an AI provider and an embedding profile, upload a document to a knowledge base, and ask an
  assistant a question: the answer should cite the document.

Then plan [backups](backup-restore.md) and [audit retention](operations.md#audit-retention).

## External PostgreSQL

The stack's own PostgreSQL is configured automatically. To use a managed or existing PostgreSQL, create two roles
(a schema owner used only by `migrate` and a runtime role for the services), the database and the `vector` and
`citext` extensions as an administrator, then connect the services with an override file. The SQL and the
details are in [DEPLOYMENT.md §6.1](../DEPLOYMENT.md#61-postgresql-роли-и-расширения-adr-011-adr-024-реализовано).
The application never needs superuser rights.
