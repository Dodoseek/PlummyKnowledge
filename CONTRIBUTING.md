# Contributing

Thank you for your interest in PlummyKnowledge. Bug reports, documentation fixes, translations, tests and code
are all welcome. By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

**Security vulnerabilities are never reported in public**: see [SECURITY.md](SECURITY.md).

## Before you start

- **Bugs and ideas:** open an issue with the version, what happens, what you expected and how to reproduce it.
- **Larger changes** (a feature, a change of API behavior) are discussed in an issue first, so your work fits the
  architecture and the roadmap ([DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md)).
- **Architecture changes** (framework, database, queue, access model, authentication or sessions, the public
  contract) need an Architecture Decision Record with status `Proposed` **before** code: see
  [docs/adr/](docs/adr/README.md).
- **Scope:** the product scope and what is deliberately out of it are in [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md).

Specifications and ADRs are written in Russian; public documents and guides are in English. Issues and pull
requests in English or Russian are both fine.

## Understand the project

Read the [README](README.md), the [architecture](docs/ARCHITECTURE.md), the
[security model](docs/SECURITY_MODEL.md) and the accepted [ADRs](docs/adr/README.md). Working rules and every
command are collected in [CLAUDE.md](CLAUDE.md); they apply to people as much as to AI agents. To add an AI
provider or a document source, follow the [developer guides](docs/guides/README.md#developers).

## Set up

You need Docker with Compose v2, and for the frontend Node.js 20.19+, 22.13+ or 24+ with npm 11.

```bash
sh scripts/setup-git-hooks.sh        # once per clone: secret scan, lint, VERSION bump
docker compose up --build            # the whole development stack
```

In `apps/frontend` use `npx npm@11.21.2 ci` if your npm is older: `.npmrc` refuses other npm versions.

## What a change must include

- **One task, one commit**, with a clear message in English.
- **Layers** ([ADR-002](docs/adr/0002-modular-monolith-and-layering.md)): `domain` and `application` do not import
  FastAPI, Celery, SQLAlchemy or vendor SDKs; business logic lives in use cases, not in routers or Celery tasks;
  the API returns Pydantic DTOs, never ORM objects.
- **Security:** permissions are checked in every use case; knowledge-base data is filtered by access in SQL before
  retrieval and before any text reaches a model; all outbound connections go through `SafeHttpClient`; secrets,
  tokens, passwords, document content and prompts are never logged.
- **Tests are required:** positive, negative, edge cases and errors, and, when access is involved, proof that
  other users' data stays out of reach. Integration tests use real PostgreSQL and Redis, not mocks of security
  checks.
- **Dependencies:** add or upgrade one only with a reason in the pull request.
- **Documentation:** update the affected documents in `docs/` in the same change.
- **Changelog:** a change visible to administrators, users or integrators adds an entry under `Unreleased` in
  [CHANGELOG.md](CHANGELOG.md).
- **API contract:** a change of the REST API updates the OpenAPI snapshot and
  [API_CONTRACTS.md](docs/API_CONTRACTS.md) in the same commit; removing or renaming anything needs a deprecation
  first ([ADR-025](docs/adr/0025-semver-and-contract-stability.md)). Update the snapshot with:

  ```bash
  docker compose run --rm --no-deps -v "$PWD/apps/backend:/work" -w /work -e UPDATE_OPENAPI_SNAPSHOT=1 \
    backend pytest tests/contract/test_openapi_snapshot.py
  ```

- **Version:** the pre-commit hook bumps `VERSION`; do not edit it by hand or skip hooks with `--no-verify`.

## Checks

```bash
docker compose exec backend pytest
docker compose exec backend ruff check src tests alembic
docker compose exec backend ruff format --check src tests alembic
docker compose exec backend mypy
cd apps/frontend && npm test && npm run lint && npm run typecheck && npm run format:check && npm run build
python3 -m unittest discover -s scripts/tests
python3 scripts/check_secrets.py
```

End-to-end tests (Playwright) run in a separate Compose project; the commands are in
[CLAUDE.md](CLAUDE.md#команды). CI runs all of this, plus dependency audits, on every pull request.

## Pull requests

Fill in the pull request template: what and why, decisions and risks, and the checklist. Keep pull requests
focused; unrelated refactoring belongs in its own change. A maintainer reviews every pull request for scope,
security, tests and documentation.

## License

By submitting a contribution you agree that it is licensed under the [Apache License 2.0](LICENSE) (section 5 of
the license).
