# Configuration

PlummyKnowledge is configured by the server administrator, not through the web app. There are three places:

| Where | What |
|---|---|
| `deploy/prod/.env` | Plain settings: public address, storage, limits, connector client IDs |
| `deploy/prod/secrets/` | Secrets as files, created by `deploy/prod/init-secrets.sh` |
| Instance configuration file (`CONFIG_FILE`, TOML) | The outbound network allowlist |

Invalid configuration stops the service at start with a message that names the setting but never its value.
After a change, apply it with:

```bash
docker compose -f compose.prod.yaml --env-file deploy/prod/.env up -d --wait
```

`GET /api/v1/system/settings` (for administrators) shows the effective, non-secret configuration: limits,
storage, the allowlist, whether connectors and the key ring are configured.

## Main settings

| Variable | Default | Meaning |
|---|---|---|
| `PUBLIC_URL` | — (required) | The `https://` origin of the web app. Requests that change data are accepted only from this origin |
| `PLUMMY_SERVER_NAME` | — | Host name Nginx serves |
| `PLUMMY_BIND_ADDRESS`, `PLUMMY_HTTP_PORT`, `PLUMMY_HTTPS_PORT` | `0.0.0.0`, `80`, `443` | Where Nginx listens. Port 80 only redirects to HTTPS |
| `PLUMMY_TLS_CERT`, `PLUMMY_TLS_KEY` | `deploy/prod/certs/…` | Certificate chain and key |
| `SETUP_MODE` | `web` | `cli_only` disables the setup wizard; the first administrator is created with the CLI |
| `LOG_LEVEL` | `INFO` | Logs are JSON lines on stdout. Secrets, tokens, passwords, document text and prompts are never logged |

## Storage

| Variable | Default | Meaning |
|---|---|---|
| `STORAGE_BACKEND` | `s3` | `s3`, or `local` for a volume on the single host |
| `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION`, `S3_PREFIX` | built-in server | External storage: `https` endpoint without a path; the prefix lets several installations share a bucket |

S3 keys are the files `s3_access_key_id` and `s3_secret_access_key` in `deploy/prod/secrets/`. Storage on a plain
`http` or internal address must be allowed in the instance configuration file (the built-in server already is).

## Limits

| Variable | Default | Meaning |
|---|---|---|
| `UPLOAD_MAX_BYTES` | 50 MB | Largest upload. Keep `PLUMMY_CLIENT_MAX_BODY_SIZE` (Nginx) a little higher |
| `CHAT_GENERATION_TIMEOUT_SECONDS` | `120` | Hard limit for one answer, retrieval included. Keep `PLUMMY_STREAM_READ_TIMEOUT` (Nginx) longer |
| `RATE_LIMIT_GENERAL_PER_MINUTE` | `300` | API requests per minute per user (per IP without a session), 10–100 000 |
| `SESSION_IDLE_TTL`, `SESSION_ABSOLUTE_TTL` | `86400`, `604800` | Session lifetimes in seconds (idle: 5 min – 30 days, absolute: 1 h – 90 days) |
| `WORKER_CONCURRENCY` | `2` | Processes in each `worker` container |
| `AUDIT_RETENTION_DAYS` | `365` | Age after which `maintenance prune-audit` deletes audit records (minimum 30) |
| `*_CPUS`, `*_MEMORY_LIMIT` | per service | Resource limits, for example `INGESTION_MEMORY_LIMIT=4g` |

Sign-in attempts, the number of simultaneous answers per user (2) and parser limits are fixed in the code.

## Connectors

`GOOGLE_OAUTH_CLIENT_ID`, `MICROSOFT_OAUTH_CLIENT_ID` and `MICROSOFT_OAUTH_TENANT_ID` go into `.env`; client
secrets are files. See [Connectors](connectors.md).

## Network allowlist

Every outbound connection of the application (AI providers, connectors, storage) is checked: addresses in private,
loopback, link-local and metadata ranges are refused unless an entry of the instance configuration file allows
them. The production file is `deploy/prod/plummy.toml` (another file: `PLUMMY_CONFIG_FILE` in `.env`):

```toml
[[network.allowlist]]
name           = "local-ollama"          # a label for logs and errors
url            = "http://ollama:11434"   # scheme, host and port that may be used
purposes       = ["llm", "embedding"]    # llm, embedding, connector, storage
provider_types = ["ollama"]              # optional: which provider types may use it
allowed_cidrs  = ["172.30.210.0/24"]     # addresses the host name may resolve to
```

Keep `allowed_cidrs` as narrow as possible. The service refuses to start if a range includes the addresses of
PostgreSQL or Redis, or link-local, metadata, multicast or unspecified addresses. The file is empty by default apart from the built-in S3 server. A typical use is
a [local model](local-models.md).

## Secrets

Never put secret values into `.env` or into environment variables: use files and the `*_FILE` variables. The key
ring (`keyring.json`) encrypts provider keys and connector tokens stored in the database; how to rotate it is in
[Operations](operations.md#key-ring-rotation). API keys entered in the web app are write-only: the API never
returns them.

## Reference

Every variable, including development-only ones, is listed in [`.env.example`](../../.env.example) and
[DEPLOYMENT.md §0, §3](../DEPLOYMENT.md).
