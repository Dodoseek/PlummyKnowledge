# Architecture Decision Records

Архитектурные решения PlummyKnowledge. Blueprint требует фиксировать архитектурные изменения в ADR
([PROJECT_BLUEPRINT.md](../PROJECT_BLUEPRINT.md) §1, §15, §22 п.9).

## Правила

- ADR обязателен **до** изменения framework, DB, queue, tenant model, auth/session strategy или публичного API (§15).
- Номера присваиваются последовательно при создании файла и **никогда не меняются**. Отменённое решение получает
  статус `Superseded` со ссылкой на новое ADR; файл не удаляется.
- Файл: `NNNN-kebab-case-title.md`; заголовок: `ADR-NNN: Название`.
- Статусы: `Proposed` → `Accepted` → (`Superseded` | `Deprecated`).
- Утверждённые ADR имеют приоритет над `docs/*.md`. Если спецификация противоречит Accepted ADR, правится
  спецификация (см. порядок приоритетов в [../../CLAUDE.md](../../CLAUDE.md)).

## Индекс

| ADR | Название | Статус | Дата |
|---|---|---|---|
| [ADR-001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted | 2026-11-17 |
| [ADR-002](0002-modular-monolith-and-layering.md) | Modular Monolith и правило зависимостей слоёв | Accepted | 2026-11-17 |
| [ADR-003](0003-technology-stack.md) | Базовый технологический стек | Accepted | 2026-11-17 |
| [ADR-004](0004-tenant-isolation-and-kb-level-access.md) | Tenant isolation и доступ на уровне Knowledge Base | Accepted | 2026-11-17 |
| [ADR-005](0005-assistant-access-model.md) | Assistant Access Model | Accepted | 2026-11-17 |
| [ADR-006](0006-initial-setup-and-system-configuration.md) | Initial Setup and System Configuration | Accepted | 2026-11-17 |
| [ADR-007](0007-network-access-and-local-llm.md) | Network Access and Local LLM Providers | Accepted | 2026-11-17 |
| [ADR-008](0008-unified-document-ingestion.md) | Unified Document Ingestion | Accepted | 2026-11-17 |
| [ADR-009](0009-automatic-versioning.md) | Automatic Versioning | Accepted | 2026-11-17 |

ADR-001…004 оформляют решения, уже зафиксированные в blueprint v1.0; новых решений они не вводят.

## Планируемые ADR (номер присваивается при создании)

Эти темы помечены в спецификациях как **[Proposed]** и требуют решения до реализации соответствующей фазы.

| Тема | Нужен к | Где описан черновик |
|---|---|---|
| Tenant membership: пользователь принадлежит одной организации, глобально уникальный email | Фаза 2 | [DATABASE_SCHEMA.md §1.1](../DATABASE_SCHEMA.md) |
| Auth/session strategy: TTL, формат access token, cookies, CSRF | Фаза 3 | [SECURITY_MODEL.md §4](../SECURITY_MODEL.md) |
| Шифрование секретов: алгоритм, key ring, ротация | Фаза 4 | [SECURITY_MODEL.md §7](../SECURITY_MODEL.md) |
| Хранение векторов разной размерности в pgvector | Фаза 5 | [DATABASE_SCHEMA.md §5.10](../DATABASE_SCHEMA.md) |
| Формат ошибок API (RFC 9457), пагинация, хранилище idempotency keys | Фаза 2–3 | [API_CONTRACTS.md §1](../API_CONTRACTS.md) |
| Генерация ответа в процессе API или в worker | Фаза 8 | [ARCHITECTURE.md §9.4](../ARCHITECTURE.md) |
| PostgreSQL Row Level Security как второй рубеж tenant isolation | Фаза 2 / 12 | [SECURITY_MODEL.md §5](../SECURITY_MODEL.md) |
| Конфигурация full-text search для русского и английского | Фаза 6 | [DATABASE_SCHEMA.md §5.9](../DATABASE_SCHEMA.md) |

## Шаблон

```markdown
# ADR-NNN: Название решения

- **Status:** Proposed | Accepted | Superseded | Deprecated
- **Date:** YYYY-MM-DD

## Context
## Decision
## Alternatives
## Consequences
## Security considerations
## Implementation notes
## Related documents
```
