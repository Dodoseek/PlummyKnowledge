# ADR-004: Tenant isolation и доступ на уровне Knowledge Base

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Cross-tenant leakage и RBAC bypass — первые угрозы в threat model (§8). Blueprint задаёт: tenant context из
проверенной сессии (§5), проверку permissions в каждом use case (§5), авторизацию до retrieval и до передачи текста
LLM (§8), доступ на уровне Knowledge Base в MVP и запрет смешивать в одной KB документы с разными внешними ACL (§8).

## Decision

1. Tenant — организация. Все tenant-данные содержат `organization_id`.
2. Tenant context устанавливается сервером только из проверенной сессии; `organization_id` от клиента не
   принимается.
3. Каждый API request и каждый background job выполняется в контексте одной организации и фильтрует данные по ней.
4. Авторизация выполняется в каждом use case; frontend не является security boundary.
5. В MVP единица доступа к документам — **Knowledge Base** (уровни `read`, `manage`; пользователю или группе).
   Document-level ACL отсутствует.
6. В одну KB подключаются только источники, все документы которых могут видеть все участники KB.
7. Retrieval фильтрует по tenant, KB, документам и версиям **до** передачи контекста LLM.

## Alternatives

- Document-level ACL с синхронизацией внешних ACL в MVP — отклонено (вне MVP, §2; позже, §14).
- Отдельная БД или схема на организацию — не требуется для целевой аудитории MVP; усложняет миграции и deployment.

## Consequences

- Все repository-методы принимают `organization_id`; составные FK защищают связи на уровне БД.
- Тесты изоляции двух организаций обязательны (§13 Фаза 2, §12).
- ADR-005 строит доступ к ассистентам поверх доступа к KB.

## Security considerations

Ресурсы другой организации возвращают `404`, чтобы не раскрывать их существование. Ограничение п.6 — операционное
правило: его соблюдение зависит от администратора, поэтому UI и API требуют явного подтверждения при подключении
источника к KB.

## Implementation notes

Модель членства пользователя в организациях (одна или несколько) — планируемый ADR, см. [README.md](README.md).
PostgreSQL RLS как второй рубеж — планируемый ADR.

## Related documents

[SECURITY_MODEL.md §5–§6](../SECURITY_MODEL.md) · [DATABASE_SCHEMA.md §1](../DATABASE_SCHEMA.md) ·
[PROJECT_BLUEPRINT.md](../PROJECT_BLUEPRINT.md) §5, §8
