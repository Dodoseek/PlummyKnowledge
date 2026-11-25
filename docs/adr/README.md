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
| [ADR-004](0004-tenant-isolation-and-kb-level-access.md) | Tenant isolation и доступ на уровне Knowledge Base | Superseded by ADR-012 | 2026-11-17 |
| [ADR-005](0005-assistant-access-model.md) | Assistant Access Model | Accepted | 2026-11-17 |
| [ADR-006](0006-initial-setup-and-system-configuration.md) | Initial Setup and System Configuration | Accepted | 2026-11-17 |
| [ADR-007](0007-network-access-and-local-llm.md) | Network Access and Local LLM Providers | Accepted | 2026-11-17 |
| [ADR-008](0008-unified-document-ingestion.md) | Unified Document Ingestion | Accepted | 2026-11-17 |
| [ADR-009](0009-automatic-versioning.md) | Automatic Versioning | Accepted | 2026-11-17 |
| [ADR-010](0010-api-error-format.md) | API Error Format (RFC 9457) | Accepted | 2026-11-17 |
| [ADR-011](0011-database-roles-and-extensions.md) | PostgreSQL Roles and Extensions | Accepted | 2026-11-17 |
| [ADR-012](0012-single-organization-deployment.md) | Single-Organization Deployment | Accepted | 2026-11-17 |
| [ADR-013](0013-auth-session-strategy.md) | Auth and Session Strategy | Accepted | 2026-11-17 |
| [ADR-014](0014-list-pagination.md) | List Pagination | Accepted | 2026-11-18 |
| [ADR-015](0015-user-activation-links.md) | User Onboarding by Activation Link | Accepted | 2026-11-18 |
| [ADR-016](0016-secret-encryption.md) | Secret Encryption at Rest | Accepted | 2026-11-18 |
| [ADR-017](0017-vector-storage.md) | Vector Storage in pgvector | Accepted | 2026-11-19 |
| [ADR-018](0018-full-text-search-configuration.md) | Full-Text Search Configuration | Accepted | 2026-11-25 |
| [ADR-019](0019-answer-generation-in-api-process.md) | Answer Generation in the API Process | Accepted | 2026-11-25 |
| [ADR-020](0020-google-drive-connector.md) | Google Drive Connector | Accepted | 2026-11-26 |

ADR-001…004 оформляют решения, уже зафиксированные в blueprint v1.0; новых решений они не вводят. ADR-012 изменяет
блупринт: инсталляция принадлежит одной организации (multi-tenancy упразднена); ADR-005…008, 010, 011 содержат
пометки «Amended by ADR-012».

## Планируемые ADR (номер присваивается при создании)

Эти темы помечены в спецификациях как **[Proposed]** и требуют решения до реализации соответствующей фазы.

| Тема | Нужен к | Где описан черновик |
|---|---|---|
| Хранилище idempotency keys (пагинация решена в ADR-014) | С первой операцией без естественного ключа | [API_CONTRACTS.md §1.7](../API_CONTRACTS.md) |
| Разделение роли миграций и runtime-роли API/worker | Фаза 2 / 12 | [ADR-011](0011-database-roles-and-extensions.md) |
| PostgreSQL Row Level Security как второй рубеж доступа на уровне KB (нужна ли без multi-tenancy) | Фаза 12 | [SECURITY_MODEL.md §5](../SECURITY_MODEL.md) |

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
