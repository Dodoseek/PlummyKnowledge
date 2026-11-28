## What and why

<!-- Scope of the change and the issue or roadmap phase it belongs to. -->

## Decisions and risks

<!-- Architectural decisions (link the ADR if one was needed), trade-offs, known limitations. -->

## Checklist ([CONTRIBUTING.md](../CONTRIBUTING.md))

- [ ] Scope matches the agreed task; no unrelated changes
- [ ] Permissions checked in use cases (not only in routers or the frontend)
- [ ] Tests: positive, negative, edge cases, errors; access to other users' data and knowledge bases denied
- [ ] Migrations checked on a clean database and on upgrade
- [ ] No secrets, tokens, passwords or document content in code, logs or fixtures
- [ ] REST API changes: OpenAPI snapshot and `docs/API_CONTRACTS.md` updated; nothing removed without deprecation
- [ ] `CHANGELOG.md` (`Unreleased`) updated for changes visible to administrators, users or integrators
- [ ] Documentation and ADRs updated
- [ ] CI passes (tests, lint, typecheck, dependency audit)
