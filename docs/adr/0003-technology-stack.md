# ADR-003: Базовый технологический стек

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Blueprint (§3) фиксирует технологии. Изменение framework, DB или queue требует ADR (§15), поэтому базовый выбор
оформляется как точка отсчёта.

## Decision

| Область | Выбор |
|---|---|
| Backend | Python, FastAPI, SQLAlchemy 2.x async, Pydantic v2, Alembic |
| Database | PostgreSQL + pgvector |
| Jobs / cache | Redis + Celery |
| Frontend | React 18, TypeScript, Vite, Chakra UI 2.8+, Redux Toolkit, RTK Query |
| API | REST (`/api/v1`); streaming — Server-Sent Events |
| Parsing | Docling за интерфейсом `ParserProvider` |
| Storage | Local filesystem и S3-compatible abstraction (MinIO) |
| Deployment | Docker, Docker Compose, Nginx |
| Tests | Pytest, Vitest, React Testing Library, Playwright |
| Quality | Ruff, mypy/pyright, ESLint, Prettier, strict TypeScript (§13 Фаза 1) |
| License | Apache License 2.0 |

## Alternatives

Не рассматривались в рамках этого ADR: выбор сделан в blueprint. Замена любого пункта — отдельный ADR.

## Consequences

- Новые зависимости добавляются только с обоснованием (§15); замена библиотеки — через ADR.
- pgvector и full-text search PostgreSQL избавляют MVP от отдельного векторного хранилища и поискового движка.

## Security considerations

Dependency и container scanning входят в CI (§13 Фаза 12). Лицензии сторонних зависимостей проверяются (§16).

## Implementation notes

Конкретные версии фиксируются lockfiles в Фазе 1.

## Related documents

[PROJECT_BLUEPRINT.md](../PROJECT_BLUEPRINT.md) §3, §13, §16 · [ARCHITECTURE.md §3](../ARCHITECTURE.md)
