# Troubleshooting

Start with `dc ps` (service health) and `dc logs <service>`, where `dc` is your Compose command
([Operations](operations.md)). Error responses of the API carry a `code` and a `correlation_id`; search the logs
for that ID.

## Start and setup

| Symptom | What to check |
|---|---|
| A service exits at start with `Invalid configuration: …` | The named setting in `.env`, a secret file or the configuration file ([Configuration](configuration.md)). Values are never printed |
| Readiness does not pass | `dc logs backend`: database, Redis, storage, pending migrations, key ring |
| `migrate`: `Required PostgreSQL extensions are not installed` | External PostgreSQL without `vector`/`citext` ([Installation](installation.md#external-postgresql)) |
| `migrate`: `db grant-runtime` fails | The runtime role does not exist or is a member of the owner role; for installations before 0.5.0 see [Upgrade](upgrade.md#from-versions-before-050) |
| `setup_token_invalid` | The token expired or was used: `dc run --rm migrate python -m plummy.cli setup init-token --rotate` |
| Browser shows a certificate error or no page | Certificate files and their owner (uid `101`), `PLUMMY_SERVER_NAME`, ports 80/443 open |
| Sign-in or changes fail with `csrf_failed` | `PUBLIC_URL` must be exactly the address in the browser, with `https` |

## Providers and chat

| Symptom | What to check |
|---|---|
| Connection test: `endpoint_not_allowed` or `egress_denied` | An internal address without a matching allowlist entry: URL, `purposes`, `allowed_cidrs` ([Local models](local-models.md)) |
| Connection test: `provider_auth_failed`, `provider_model_not_found` | API key or model name |
| Saving a key: `secrets_not_configured` | The key ring is not mounted (`SECRETS_KEYRING_FILE`) |
| The answer arrives in one piece | Something between the browser and Nginx buffers the stream (another proxy, a CDN) |
| `429 rate_limited` in chat | Two answers already running for this user, or `RATE_LIMIT_GENERAL_PER_MINUTE` |
| An assistant is missing for a user | The user needs access to **every** knowledge base of the assistant |
| After a restore, provider keys do not work | A different key ring: restore the original `keyring.json` or enter the keys again |

## Documents and jobs

Open the **Jobs** page: every job shows its stage, attempts and an error code.

| Job state and code | Meaning and action |
|---|---|
| `queued` for a long time | Workers are down or busy: `dc ps`, memory limits. The scheduler re-sends jobs the queue never received |
| `failed`: `provider_auth_failed`, `provider_model_not_found` | Fix the embedding provider (**Test connection**), then **Retry** |
| `dead`: `attempts_exhausted` | The provider was unavailable, timed out or rate limited every time: check quotas and the allowlist, then **Retry** |
| `failed`: `unsupported_media_type`, `archive_limits_exceeded`, `unsafe_content`, `no_text`, `extraction_invalid` | The file was rejected and not kept; a retry gives the same result. Convert the file or check `UPLOAD_MAX_BYTES` |
| `failed`: `embedding_dimensions_mismatch` | The profile's dimensions do not match the model: create a profile with the right dimensions and reindex the knowledge base |
| `cancelled`: `cancelled_by_admin` | Stopped on the Jobs page; **Retry** if needed |
| `cancelled`: `document_deleted`, `source_*`, `superseded_by_sync`, `resource_unavailable` | The document or source changed or was deleted; nothing to do |
| Sync `failed`: `connector_auth_revoked` | Access was revoked at Google or Microsoft: connect the source again |

Scanned PDFs without a text layer give `no_text`: OCR is not part of the product.

## Backup and restore

| Symptom | What to check |
|---|---|
| `refusing: the database is not empty` | Restore only into empty volumes (`dc down --volumes`) or a new host |
| Backup refuses because of the key ring | The key ring or setup token ended up in the backup directory; they must be stored apart |

The full list, in Russian, is in [DEPLOYMENT.md §11](../DEPLOYMENT.md#11-troubleshooting).
