# ADR-012: Single-Organization Deployment

- **Status:** Accepted
- **Date:** 2026-11-17
- **Supersedes:** [ADR-004](0004-tenant-isolation-and-kb-level-access.md)
- **Amends:** [ADR-005](0005-assistant-access-model.md), [ADR-006](0006-initial-setup-and-system-configuration.md),
  [ADR-007](0007-network-access-and-local-llm.md), [ADR-008](0008-unified-document-ingestion.md),
  [ADR-010](0010-api-error-format.md), [ADR-011](0011-database-roles-and-extensions.md)

## Context

Blueprint и спецификации v0.1 описывали multi-tenant модель: сущность `Organization` как tenant, `organization_id` в
каждой таблице, составные внешние ключи, роль Platform Owner, управляющая организациями, и два уровня настроек
(инсталляция и организация). ADR-004 закрепил эту модель, а планируемый ADR «Tenant membership» должен был решить,
может ли пользователь состоять в нескольких организациях.

PlummyKnowledge — **self-hosted** продукт. Владелец проекта зафиксировал: инсталляция принадлежит **одной**
организации и работает на её серверах или в её облаке. Хранить в БД сведения об организации и поддерживать
изоляцию нескольких организаций внутри одной инсталляции не нужно. Multi-tenancy — признак SaaS, а SaaS complexity
явно вне MVP (blueprint §2, §14).

## Decision

1. **Одна инсталляция = одна организация.** Организация — это владелец инсталляции, а не сущность данных. Таблицы
   `organizations` нет; колонки `organization_id` нет ни в одной таблице; Redis-ключи, ключи storage и payload задач
   не содержат идентификатора организации.
2. **Нескольким организациям — несколько инсталляций.** Если группе компаний нужна изоляция, каждая компания
   разворачивает свою инсталляцию (своя БД, storage, ключи). Это сильнее логической изоляции внутри одной БД.
   Возврат к multi-tenancy возможен только новым ADR.
3. **Сведения об организации** (название, логотип) в БД не хранятся. Если они понадобятся интерфейсу, это
   Instance Setting (конфигурация сервера), а не данные.
4. **Пользователи** принадлежат инсталляции. Email уникален в инсталляции (`citext`), логин не требует выбора
   организации. Вопрос планируемого ADR «Tenant membership» закрыт этим решением.
5. **Роли.** Platform Owner и Organization Admin объединяются в одну системную роль **Administrator** (`admin`).
   Остальные роли: Knowledge Manager, Member, Auditor. Scope ролей (`platform` / `organization`) упраздняется.
   Permissions `platform.*` и `org.settings.manage` заменяются: `platform.settings.read` → `system.settings.read`,
   `org.settings.manage` → `settings.manage`, `platform.audit.read` поглощается `audit.read`,
   `platform.organizations.manage` удаляется.
6. **Два уровня настроек сохраняются**, меняется только второй ([ADR-006](0006-initial-setup-and-system-configuration.md)):
   - *Instance Settings* — конфигурация сервера (env, `*_FILE`, файл конфигурации); через API не меняются.
   - *Application Settings* (бывшие Organization Settings) — настройки, которые Administrator меняет через API в
     пределах instance policy (только сужают её). Хранение — типизированная версионированная модель в
     строке-синглтоне; таблица проектируется в фазе, где эти настройки появятся. Универсальная таблица «ключ–значение»
     по-прежнему не создаётся.
7. **Модель доступа к данным из ADR-004 сохраняется** без tenant-уровня:
   - авторизация выполняется в каждом use case; frontend не является security boundary;
   - единица доступа к документам в MVP — **Knowledge Base** (`read`, `manage`; пользователю или группе);
     document-level ACL нет;
   - в одну KB подключаются только источники, все документы которых могут видеть все участники KB;
   - retrieval фильтрует по KB, источникам, документам и версиям **до** передачи контекста LLM;
   - ресурс, к которому у пользователя нет доступа даже на чтение, → `404` (не раскрывать существование).
8. **Личные данные пользователей** (диалоги, сессии) доступны только владельцу; чужие → `404`.

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| Сохранить multi-tenant модель «на будущее» | Сложность (составные FK, tenant-фильтры в каждом запросе, тесты изоляции, Platform Owner, два уровня ролей) без потребителя; противоречит self-hosted модели и запрету SaaS complexity (§14) |
| Таблица `organizations` с одной строкой | Хранит в БД то, что владелец решил не хранить; создаёт иллюзию поддержки нескольких организаций и требует `organization_id` или неявных допущений повсюду |
| Отдельные роли Platform Owner и Administrator | Без организаций у Platform Owner нет отдельной зоны ответственности: управление инсталляцией делается на сервере (ADR-006), а в приложении всё делает Administrator |

## Consequences

- ADR-004 получает статус `Superseded`; его правила доступа на уровне KB перенесены в п.7.
- ADR-005, 006, 007, 008, 010, 011 читаются с учётом этого решения: «организация» = инсталляция; упоминания tenant,
  `organization_id`, Platform Owner, Organization Settings и фильтра allowlist по организациям утрачивают силу
  (в файлах ADR стоят пометки об изменении).
- Схема данных проще: обычные внешние ключи вместо составных `(organization_id, id)`; нет tenant-фильтра в
  repository-методах.
- Обязательные тесты «чужой tenant недоступен» заменяются тестами «чужая KB / чужой диалог / документ без доступа
  недоступны» и тестами RBAC.
- Группа API `organizations` не создаётся; `GET/PATCH /organizations/current/settings` → `/settings`.
- `POST /setup/initialize` создаёт только первого администратора (без организации).
- Планируемые ADR «Tenant membership» и «RLS как второй рубеж tenant isolation» сняты. Разделение роли миграций и
  runtime-роли БД (ADR-011) остаётся актуальным.
- Следующий этап после аутентификации — «Users & RBAC», а не «Organizations & RBAC».

## Security considerations

- Исчезает класс угроз cross-tenant leakage; основными остаются RBAC bypass, доступ к чужой KB, чужому диалогу и
  документам без доступа. Проверка прав в каждом use case и фильтры доступа в SQL остаются обязательными.
- Изоляция разных организаций обеспечивается развёртыванием (отдельные БД, storage, ключи, сети), а не кодом.
- Instance Settings (сеть, storage, ключи, лимиты) по-прежнему недоступны для изменения через API — компрометация
  web-учётной записи Administrator не открывает доступ во внутреннюю сеть (ADR-006, ADR-007).

## Implementation notes

- Миграции Фазы 2–3 создаются сразу без `organizations` и `organization_id`.
- Каталог permissions и матрица ролей — [SECURITY_MODEL.md §3](../SECURITY_MODEL.md).

## Related documents

[PRODUCT_SPEC.md](../PRODUCT_SPEC.md) · [DATABASE_SCHEMA.md §1](../DATABASE_SCHEMA.md) ·
[SECURITY_MODEL.md §3, §5](../SECURITY_MODEL.md) · [API_CONTRACTS.md](../API_CONTRACTS.md) ·
[ARCHITECTURE.md](../ARCHITECTURE.md) · [PROJECT_BLUEPRINT.md](../PROJECT_BLUEPRINT.md)
