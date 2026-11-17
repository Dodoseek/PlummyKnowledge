# ADR-002: Modular Monolith и правило зависимостей слоёв

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Blueprint (§3) выбирает Modular Monolith и задаёт принцип зависимостей. Решение фиксируется как ADR, чтобы отступление
от него (микросервисы, логика в routers/tasks) требовало отдельного решения.

## Decision

- Архитектурный стиль — **Modular Monolith**: один backend-артефакт, запускаемый в ролях API (FastAPI) и worker
  (Celery).
- Слои: `domain` → `application` → `infrastructure`; входные адаптеры — `api` и `workers`.
- `domain` не импортирует FastAPI, SDK OpenAI/Anthropic, Celery, Google/Microsoft SDK.
- `application` реализует use cases и зависит только от интерфейсов (ports).
- `infrastructure` реализует интерфейсы через SQLAlchemy, SDK, Redis и storage.
- `api` отвечает за HTTP и DTO; worker вызывает те же use cases. Бизнес-логика не дублируется в routers и tasks.

## Alternatives

- Микросервисы — отклонено до появления доказанной потребности в независимом масштабировании, отдельных security
  boundaries или отдельных командах (§3).
- «Толстые» routers без слоёв — отклонено: смешивает HTTP, бизнес-правила и SDK, ломает тестируемость и
  переиспользование use cases в worker.

## Consequences

- Переход к микросервисам требует нового ADR.
- Каждый внешний сервис подключается через port + adapter.
- Правило слоёв проверяется ревью и, при согласовании зависимости, автоматически в CI.

## Security considerations

Проверки прав живут в use cases, поэтому одинаково применяются к HTTP-запросам и background jobs.

## Implementation notes

Структура пакетов и список ports — [ARCHITECTURE.md §5–§8](../ARCHITECTURE.md).

## Related documents

[PROJECT_BLUEPRINT.md](../PROJECT_BLUEPRINT.md) §3, §4 · [ARCHITECTURE.md](../ARCHITECTURE.md)
