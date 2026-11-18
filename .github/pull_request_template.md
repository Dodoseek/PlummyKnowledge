## What and why

<!-- Scope of the change and the roadmap phase / issue it belongs to. -->

## Decisions and risks

<!-- Architectural decisions (link the ADR if one was needed), trade-offs, known limitations. -->

## Checklist (CLAUDE.md "Definition of Done")

- [ ] Scope matches the agreed task; no unrelated changes
- [ ] Permissions checked in use cases (not only in routers or the frontend)
- [ ] Tests: positive, negative, edge cases, errors; access to other users' data / KBs denied
- [ ] Migrations checked on a clean database and on upgrade
- [ ] No secrets, tokens, passwords or document content in code, logs or fixtures
- [ ] Documentation and ADRs updated
- [ ] CI passes (tests, lint, typecheck, dependency audit)
