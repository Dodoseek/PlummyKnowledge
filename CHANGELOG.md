# Changelog

All notable changes for administrators, users and integrators of PlummyKnowledge are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/). From 1.0.0 the project
follows [Semantic Versioning](https://semver.org/) with respect to the public contract defined in
[ADR-025](docs/adr/0025-semver-and-contract-stability.md): REST API and SSE, audit events, CLI, configuration,
`compose.prod.yaml` with `deploy/scripts/`, and the backup format. Versions 0.2.0 and 0.3.0 were not released
separately.

## [Unreleased]

### Added

- Versioning and stability policy for 1.x: what the public contract is, what counts as a breaking change,
  deprecation, upgrades and patch releases ([ADR-025](docs/adr/0025-semver-and-contract-stability.md)).
- OpenAPI snapshot of the REST API (`apps/backend/tests/contract/openapi.snapshot.json`) checked in CI, and
  `scripts/openapi_compat.py`, which reports breaking changes against the latest release.
- This changelog. `scripts/version.py release` moves `Unreleased` into the release section and refuses to release
  without entries; `scripts/version.py notes X.Y.Z` prints a release section; pushing a `vX.Y.Z` tag creates the
  GitHub Release from it.
- README in English with diagrams of the services, document indexing and answering a question, and a short
  Russian `README.ru.md`.
- Operator guides in `docs/guides/`: installation, configuration, local models, connectors, upgrade, backup and
  restore, operations, troubleshooting.
- Developer guides for AI providers and connectors (`docs/guides/developing-*.md`).

### Changed

- `compose.prod.yaml` passes `SESSION_IDLE_TTL`, `SESSION_ABSOLUTE_TTL` and `RATE_LIMIT_GENERAL_PER_MINUTE` from
  `deploy/prod/.env` to the API (previously only the defaults applied).
- `docs/API_CONTRACTS.md` is the 1.0 contract candidate. Parts that are not implemented are marked as outside
  1.0: the `Idempotency-Key` and `RateLimit-*` headers, Application Settings (`/settings`), password reset by
  email, custom roles, the knowledge-base `sources` routes, `acl_acknowledged` and the `audit.viewed` event.

## [0.5.0] - 2026-11-28

### Added

- Production stack `compose.prod.yaml`: Nginx with TLS and CSP, secrets as files (`deploy/prod/init-secrets.sh`),
  network segmentation, `PROCESS_ROLE` to check the settings each service requires, and a CI smoke test
  ([ADR-023](docs/adr/0023-production-deployment.md)).
- S3-compatible object storage (`STORAGE_BACKEND=s3`) as an alternative to the local file system
  ([ADR-022](docs/adr/0022-s3-compatible-storage.md)).
- Backup and restore: `deploy/scripts/backup.sh` and `restore.sh`, `plummy.cli maintenance` commands, re-applying
  deletions made after the backup; restore drill and upgrade path from 0.4.0 in CI.
- Job cancellation and the `/admin/jobs` page; retention of document versions (the last 3 `outdated` versions
  are kept).
- Audit log API `GET /api/v1/audit/events` and `GET /api/v1/audit/events/{id}` (permission `audit.read`) and the
  `/admin/audit` page; `plummy.cli maintenance prune-audit` for audit retention.
- General per-user and per-address rate limit for the API (`RATE_LIMIT_GENERAL_PER_MINUTE`).
- Trivy scan of the production images in CI (`deploy/scripts/scan-images.sh`).

### Changed

- The database schema is owned by a separate role used only by `migrate`; all other services connect as the
  runtime role `plummy_app` with data access only ([ADR-024](docs/adr/0024-database-owner-and-runtime-roles.md)).
- Office documents with a DTD are rejected as `unsafe_content`.

### Security

- `audit_logs` is append-only for the runtime role.
- The Phase 12 security gate passed: threat model review, route and role matrix, scenarios SC-01…SC-09
  (`docs/SECURITY_MODEL.md` §17–§19).

### Upgrade notes

- An installation created before 0.5.0 uses a single database role. Take a backup, then run
  `deploy/scripts/split-db-roles.sh` once before starting 0.5.0 (`docs/DEPLOYMENT.md` §6.1).

## [0.4.0] - 2026-11-26

### Added

- Google Drive connector: OAuth with PKCE, folder selection, initial and incremental sync through the Changes API,
  reconciliation and scheduled sync ([ADR-020](docs/adr/0020-google-drive-connector.md)).
- Microsoft 365 connector (SharePoint and OneDrive through Microsoft Graph) for a single Entra ID tenant, with
  Delta Query sync ([ADR-021](docs/adr/0021-microsoft-365-connector.md)).
- `data-sources` API (browse, validate, sync, disconnect), sync jobs in `/jobs?type=sync` and the "External
  sources" card on the knowledge-base page.

## [0.1.0] - 2026-11-25

### Added

- Docker Compose deployment, first-run setup with a bootstrap token or the server CLI.
- Local authentication with server-side sessions, users created by activation link, groups, roles and
  knowledge-base level access control.
- LLM and embedding providers (OpenAI, Anthropic, Ollama, OpenAI-compatible) with secrets encrypted outside the
  database and an outbound network allowlist.
- Document upload and asynchronous ingestion (Docling parsing in an isolated worker, chunking, embeddings).
- Hybrid retrieval (vector and full-text search), assistants over knowledge bases, streamed chat with verified
  citations and an in-app document viewer.
- Retrieval evaluation (`plummy.cli eval run`), audit of security events, README and quick start.

[Unreleased]: https://github.com/Dodoseek/PlummyKnowledge/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Dodoseek/PlummyKnowledge/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Dodoseek/PlummyKnowledge/compare/v0.1.0...v0.4.0
[0.1.0]: https://github.com/Dodoseek/PlummyKnowledge/releases/tag/v0.1.0
