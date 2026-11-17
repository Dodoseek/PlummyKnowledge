# PlummyKnowledge — API Contracts

| | |
|---|---|
| Статус | Draft v0.1 (до реализации; контракт не стабилен до 1.0.0, §19) |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §5, §6, §10, §12 |
| Решения | [ADR-005](adr/0005-assistant-access-model.md), [ADR-006](adr/0006-initial-setup-and-system-configuration.md) (группы `setup`, `system`), [ADR-007](adr/0007-network-access-and-local-llm.md), [ADR-008](adr/0008-unified-document-ingestion.md) |
| Связанные документы | [ARCHITECTURE.md](ARCHITECTURE.md), [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md), [SECURITY_MODEL.md](SECURITY_MODEL.md) |

**Как читать.** Общие правила (§10), группы endpoints (§10), SSE-события (§5) и требования к цитатам (§6)
нормативны. Пути, поля DTO, коды ошибок и лимиты — **рабочий проект**; пункты **[Proposed]** требуют
подтверждения. Любое изменение публичного API требует предварительного ADR (§15).

Источник истины после начала реализации — OpenAPI-схема, генерируемая FastAPI; этот документ описывает намерение и
правила, а при расхождении обновляется вместе с кодом.

---

## 1. Общие правила (§10)

### 1.1 Версионирование

- Все endpoints — под префиксом `/api/v1` (§10).
- Ломающее изменение = новая версия префикса или ADR с планом миграции. Добавление необязательных полей в ответ
  ломающим не считается; клиенты обязаны игнорировать неизвестные поля.
- Стабильность контрактов — цель релиза 1.0.0 (§19).

### 1.2 DTO

- Запросы и ответы — Pydantic v2 DTO (§10). ORM entities **никогда** не отдаются напрямую (§10).
- Request DTO запрещают неизвестные поля (`extra="forbid"`), чтобы клиент не мог передать, например,
  `organization_id` или `role` в обход правил.
- Write-only поля (секреты, пароли) никогда не появляются в ответах (§12).
- Формат: JSON, `snake_case`, UUID — строки, время — ISO 8601 UTC (`2026-11-17T12:00:00Z`).

### 1.3 Tenant scope (§5)

- Организация определяется **только** из проверенной сессии. `organization_id` не передаётся в путях, query или теле
  запросов организационного уровня.
- Исключения: группы `organizations` и `system` для Platform Owner (scope `platform`); `health`, `setup`,
  `auth/login` (scope `public`).
- Ресурс другой организации или ресурс, к которому у пользователя нет даже права чтения, → `404 not_found`
  (не раскрываем существование). Ресурс доступен для чтения, но действие запрещено → `403 permission_denied`.

### 1.4 Аутентификация и CSRF (§5) — детали в [SECURITY_MODEL.md §4](SECURITY_MODEL.md#4-authentication-и-сессии)

- Access и refresh tokens передаются только в cookies `HttpOnly; Secure; SameSite` (§5). Заголовок
  `Authorization` браузерным клиентом не используется.
- Для небезопасных методов (`POST`, `PUT`, `PATCH`, `DELETE`) обязателен заголовок `X-CSRF-Token`, совпадающий с
  CSRF cookie (double-submit), плюс проверка `Origin` **[Proposed — ADR «Auth/session strategy» (планируется)]**.

### 1.5 Формат ошибок **[Proposed — ADR «Формат ошибок, пагинация, idempotency» (планируется)]**

Единый формат (§10) на базе RFC 9457, `Content-Type: application/problem+json`:

```json
{
  "type": "about:blank",
  "title": "Permission denied",
  "status": 403,
  "code": "permission_denied",
  "detail": "You do not have permission to manage this knowledge base.",
  "correlation_id": "01JD0Z7Q6Y3T9V2K8M4N5P6R7S",
  "errors": [
    { "field": "name", "code": "too_long", "message": "Must be at most 200 characters." }
  ]
}
```

`detail` не содержит секретов, stack traces, SQL, содержимого документов или сведений об объектах других организаций.

| HTTP | `code` | Когда |
|---|---|---|
| 400 | `bad_request` | Некорректный запрос, не связанный с валидацией полей |
| 401 | `unauthenticated` | Нет сессии / access token истёк |
| 401 | `session_revoked` | Сессия отозвана (§12) |
| 401 | `invalid_credentials` | Логин — одинаково для неизвестного email, неверного пароля и отключённого пользователя |
| 403 | `csrf_failed` | Нет/неверный CSRF token или `Origin` |
| 403 | `permission_denied` | Недостаточно прав |
| 403 | `assistant_unavailable` | Ассистент собственного диалога пользователя стал недоступен ([ADR-005](adr/0005-assistant-access-model.md)) |
| 403 | `setup_token_invalid` | Bootstrap token неверен, истёк или погашен ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| 404 | `not_found` | Нет ресурса или нет доступа к нему |
| 409 | `conflict` | Конфликт состояния (уникальность, версия) |
| 409 | `idempotency_key_mismatch` | Тот же `Idempotency-Key` с другим телом запроса |
| 409 | `sync_already_running` | Для источника уже есть активный sync (§7) |
| 409 | `setup_already_completed` | Инсталляция уже инициализирована ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| 409 | `reindex_required` | Операция требует переиндексации (смена embedding profile, §5) |
| 413 | `payload_too_large` | Превышен лимит размера (§8 п.6) |
| 415 | `unsupported_media_type` | Формат вне списка MVP (§6) |
| 422 | `validation_error` | Ошибка валидации DTO; список в `errors` |
| 422 | `invalid_query_parameter` | Неразрешённый фильтр/сортировка (§10) |
| 422 | `endpoint_not_allowed` | `base_url` провайдера не проходит сетевую политику инсталляции ([ADR-007](adr/0007-network-access-and-local-llm.md)) |
| 422 | `setting_exceeds_instance_policy` | Значение Organization Settings шире, чем разрешает instance policy ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| 429 | `rate_limited` | Превышен лимит; заголовок `Retry-After` (§8 п.5) |
| 502 | `provider_error` | Ошибка AI provider / источника |
| 502 | `egress_denied` | Исходящее соединение заблокировано сетевой политикой во время выполнения (например, DNS указал на запрещённый адрес) ([ADR-007](adr/0007-network-access-and-local-llm.md)) |
| 503 | `service_unavailable` | Зависимость недоступна (БД, Redis) |
| 504 | `provider_timeout` | Timeout провайдера |

### 1.6 Pagination, фильтры, сортировка (§10) **[Proposed]**

- Cursor pagination: `?limit=25&cursor=<opaque>`; `limit` по умолчанию 25, максимум 100.
- Ответ списка:

  ```json
  { "items": [ ... ], "next_cursor": "eyJ...", "has_more": true }
  ```

- Фильтры и сортировка — **только из allowlist** конкретного endpoint (§10): `?status=failed&sort=-created_at`.
  Неизвестный параметр или поле сортировки → `422 invalid_query_parameter`. Значения никогда не подставляются в SQL
  напрямую.

### 1.7 Idempotency (§10)

- Для чувствительных повторяемых операций (помечены в таблицах как **Idem**) клиент передаёт
  `Idempotency-Key: <uuid>`.
- Тот же ключ + то же тело → возвращается сохранённый результат первой обработки, побочные эффекты не повторяются.
- Тот же ключ + другое тело → `409 idempotency_key_mismatch`.
- Ключи привязаны к пользователю, организации и endpoint; срок хранения **[Proposed]** 24 часа.

### 1.8 Сквозные заголовки (§11)

| Заголовок | Направление | Назначение |
|---|---|---|
| `X-Request-ID` | in/out | Correlation ID; генерируется сервером, если не передан; возвращается всегда |
| `X-CSRF-Token` | in | CSRF для небезопасных методов |
| `Idempotency-Key` | in | См. §1.7 |
| `Retry-After` | out | При 429/503 |
| `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` | out | **[Proposed]** |

### 1.9 Шаблон описания endpoint (§10)

Для каждого endpoint при реализации документируются:

| Поле | Содержание |
|---|---|
| Назначение | Что делает |
| Permission | Ключ permission и/или уровень доступа к KB ([SECURITY_MODEL.md §3](SECURITY_MODEL.md#3-roles-и-permissions)) |
| Tenant scope | `organization` (из сессии) \| `platform` \| `public` |
| Schemas | Request / response DTO |
| Ошибки | Коды из §1.5 |
| Side effects | Создаваемые jobs, отзыв сессий, удаление данных |
| Idempotency | Да/нет, ключ |
| Audit event | Имя события ([SECURITY_MODEL.md](SECURITY_MODEL.md), раздел 11 «Audit log») или «—» |
| Rate limits | Лимит или «общий» |

В таблицах ниже: **Scope** — `org` (из сессии), `plat` (Platform Owner), `pub` (без аутентификации);
**Idem** — требуется `Idempotency-Key`; **RL** — специальный rate limit (см. §5); «—» — нет.

---

## 2. Endpoints по группам (§10)

### 2.0 `setup` ([ADR-006](adr/0006-initial-setup-and-system-configuration.md))

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/setup/status` | Состояние первоначальной настройки | — | pub | — | — | общий |
| POST | `/api/v1/setup/initialize` | Создать первую организацию и первого администратора | bootstrap token | pub | — | `setup.completed` / `setup.initialize.rejected` | setup |

**`GET /setup/status` → `200`:** `{ "status": "uninitialized" | "initialized", "web_setup_enabled": true }`. Других
сведений (версии, конфигурация) не раскрывает.

**`POST /setup/initialize`**

```json
{
  "bootstrap_token": "…",
  "organization_name": "Acme",
  "admin": { "email": "admin@acme.test", "display_name": "Admin", "password": "…" }
}
```

- `201`: `{ "organization": {…}, "user": {…} }` + cookies сессии (как `POST /auth/login`). Пользователь получает роли
  Platform Owner и Organization Admin.
- Ошибки: `409 setup_already_completed` (инсталляция инициализирована — в том числе для второго из двух
  одновременных запросов), `403 setup_token_invalid`, `403 csrf_failed` (неверный `Origin`), `404 not_found` (режим
  `cli_only`), `422`, `429`.
- Backend сам проверяет состояние в транзакции с блокировкой `instance_state`; статус, показанный frontend, роли не
  играет. Тело запроса не логируется.
- Повторное создание или восстановление администратора — только CLI на сервере (`plummy admin create`,
  `plummy admin reset-password`), не через API.

### 2.1 `health`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/health/live` | Процесс жив | — | pub | — | — | — |
| GET | `/api/v1/health/ready` | БД, Redis, storage, миграции применены | — | pub | — | — | — |

Ответ `ready` не раскрывает версии, хосты и тексты ошибок зависимостей: `{"status":"ready"}` или
`503 {"status":"not_ready","checks":{"database":"fail"}}`.

### 2.2 `auth`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| POST | `/api/v1/auth/login` | Логин, установка cookies | — | pub | — | `auth.login.succeeded` / `auth.login.failed` | login |
| POST | `/api/v1/auth/refresh` | Ротация refresh token (§5) | refresh cookie | pub | — | `auth.refresh.reuse_detected` (при reuse) | refresh |
| POST | `/api/v1/auth/logout` | Отзыв текущей сессии | сессия | org | — | `auth.logout` | — |
| GET | `/api/v1/auth/me` | Текущий пользователь, организация, эффективные permissions | сессия | org | — | — | — |
| GET | `/api/v1/auth/sessions` | Мои активные сессии | сессия | org | — | — | — |
| DELETE | `/api/v1/auth/sessions/{session_id}` | Отозвать свою сессию | сессия | org | — | `auth.session.revoked` | — |
| POST | `/api/v1/auth/password/change` | Смена пароля; отзыв остальных сессий | сессия | org | — | `auth.password.changed` | login |
| POST | `/api/v1/auth/password/reset-request` | Только при наличии email infrastructure (§5) | — | pub | — | `auth.password.reset_requested` | login |
| POST | `/api/v1/auth/password/reset` | Только при наличии email infrastructure (§5) | reset token | pub | — | `auth.password.reset` | login |

Первоначальная настройка вынесена в группу `setup` (§2.0).

**`POST /auth/login`**

- Request: `{ "email": "user@acme.test", "password": "..." }`
- `200`:

  ```json
  {
    "user": { "id": "…", "email": "user@acme.test", "display_name": "User" },
    "organization": { "id": "…", "name": "Acme" },
    "permissions": ["chat.use"],
    "access_expires_at": "2026-11-17T12:15:00Z"
  }
  ```

  `Set-Cookie`: access, refresh, CSRF (атрибуты — SECURITY_MODEL.md §4).
- Ошибки: `401 invalid_credentials`, `422`, `429 rate_limited`.

**`POST /auth/refresh`** — выдаёт новую пару access/refresh и инвалидирует предъявленный refresh token. Повторное
предъявление использованного токена → отзыв всей сессии, `401 session_revoked`.

### 2.3 `organizations`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/organizations` | Список организаций инсталляции | `platform.organizations.manage` | plat | — | — | — |
| POST | `/api/v1/organizations` | Создать организацию и её первого администратора | `platform.organizations.manage` | plat | Idem | `organization.created` | — |
| GET | `/api/v1/organizations/{id}` | Карточка организации | `platform.organizations.manage` | plat | — | — | — |
| PATCH | `/api/v1/organizations/{id}` | Изменить / приостановить (`status`) | `platform.organizations.manage` | plat | — | `organization.updated`, `organization.suspended` | — |
| GET | `/api/v1/organizations/current` | Текущая организация и публичная часть её настроек | сессия | org | — | — | — |
| GET | `/api/v1/organizations/current/settings` | Organization Settings: заданные и effective значения, instance-границы | `org.settings.manage` | org | — | — | — |
| PATCH | `/api/v1/organizations/current/settings` | Изменить Organization Settings в пределах instance policy | `org.settings.manage` | org | — | `organization.settings.updated` | — |

Platform Owner через эти endpoints **не** получает доступа к документам, диалогам и KB организации.

**`PATCH /organizations/current/settings`** ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)):
тело — частичный `OrganizationSettingsV1`; неизвестные поля → `422`; значение шире instance policy →
`422 setting_exceeds_instance_policy`. Ответ:

```json
{
  "settings_version": 1,
  "values":    { "chat_retention_days": 90, "max_output_tokens_cap": 2048 },
  "effective": { "chat_retention_days": 90, "max_output_tokens_cap": 2048 },
  "instance_limits": { "chat_retention_days": { "max": 365 }, "max_output_tokens_cap": { "max": 4096 } }
}
```

### 2.4 `users`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/users` | Список (фильтры: `status`, `group_id`, `q`) | `users.read` | org | — | — | — |
| POST | `/api/v1/users` | Создать пользователя с временным паролем (смена при первом входе) **[Proposed]** | `users.manage` | org | Idem | `user.created` | — |
| GET | `/api/v1/users/{id}` | Карточка | `users.read` | org | — | — | — |
| PATCH | `/api/v1/users/{id}` | Имя, статус (`disabled` → отзыв всех сессий) | `users.manage` | org | — | `user.updated`, `user.disabled` | — |
| DELETE | `/api/v1/users/{id}` | Soft delete; отзыв сессий | `users.manage` | org | — | `user.deleted` | — |
| PUT | `/api/v1/users/{id}/roles` | Заменить набор ролей | `roles.assign` | org | — | `role.assigned`, `role.revoked` | — |
| POST | `/api/v1/users/{id}/sessions/revoke` | Отозвать все сессии пользователя | `users.manage` | org | — | `auth.session.revoked_by_admin` | — |

Пароли и хэши в ответах отсутствуют. Пользователь не может снять с себя последнюю роль Organization Admin в
организации (защита от потери управления).

### 2.5 `groups`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/groups` | Список | `groups.read` | org | — | — | — |
| POST | `/api/v1/groups` | Создать | `groups.manage` | org | — | `group.created` | — |
| GET | `/api/v1/groups/{id}` | Карточка с участниками | `groups.read` | org | — | — | — |
| PATCH | `/api/v1/groups/{id}` | Изменить | `groups.manage` | org | — | `group.updated` | — |
| DELETE | `/api/v1/groups/{id}` | Удалить (доступы группы к KB снимаются) | `groups.manage` | org | — | `group.deleted` | — |
| PUT | `/api/v1/groups/{id}/members` | Заменить состав | `groups.manage` | org | — | `group.members.changed` | — |
| PUT | `/api/v1/groups/{id}/roles` | Заменить роли группы | `roles.assign` | org | — | `role.assigned`, `role.revoked` | — |

### 2.6 `roles`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/roles` | Роли с их permissions | `roles.read` | org | — | — | — |
| GET | `/api/v1/roles/permissions` | Каталог permissions | `roles.read` | org | — | — | — |
| POST / PATCH / DELETE | `/api/v1/roles[/{id}]` | Пользовательские роли — **не в 0.1.0** **[Proposed]** | `roles.manage` | org | — | `role.created/updated/deleted` | — |

Системные роли (§2) неизменяемы.

### 2.7 `providers` (§5)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/providers` | Список конфигураций | `providers.read` | org | — | — | — |
| POST | `/api/v1/providers` | Создать (секрет шифруется до записи, §5) | `providers.manage` | org | — | `provider.created` | — |
| GET | `/api/v1/providers/{id}` | Карточка без секрета | `providers.read` | org | — | — | — |
| PATCH | `/api/v1/providers/{id}` | Изменить; замена секрета | `providers.manage` | org | — | `provider.updated`, `provider.secret.rotated` | — |
| DELETE | `/api/v1/providers/{id}` | Удалить (запрещено, если используется ассистентом/profile → `409`) | `providers.manage` | org | — | `provider.deleted` | — |
| POST | `/api/v1/providers/{id}/test` | Connection test (§5) | `providers.manage` | org | — | `provider.tested` | provider_test |
| GET | `/api/v1/providers/{id}/models` | Модели (`model_configs`) | `providers.read` | org | — | — | — |
| POST | `/api/v1/providers/{id}/models` | Добавить model ID | `providers.manage` | org | — | `model.created` | — |
| PATCH / DELETE | `/api/v1/providers/{id}/models/{model_id}` | Изменить / удалить | `providers.manage` | org | — | `model.updated` / `model.deleted` | — |

**Секреты — write-only.**

```json
// POST /api/v1/providers
{
  "name": "OpenAI prod",
  "provider_type": "openai",            // openai | anthropic | ollama | openai_compatible
  "base_url": "https://api.openai.com/v1",
  "api_key": "sk-…",                    // write-only
  "settings": { "timeout_seconds": 60 }
}
// 201
{
  "id": "…",
  "name": "OpenAI prod",
  "provider_type": "openai",
  "base_url": "https://api.openai.com/v1",
  "has_secret": true,
  "secret_hint": "••••a1b2",
  "enabled": true,
  "last_test": null,
  "created_at": "2026-11-17T12:00:00Z"
}
```

`PATCH`: поле `api_key` отсутствует → секрет не меняется; строка → замена; `null` → удаление.
`base_url` проверяется сетевой политикой инсталляции ([ADR-007](adr/0007-network-access-and-local-llm.md)) при
создании/изменении (`422 endpoint_not_allowed`) и при каждом подключении (`egress_denied`). Облачные и локальные
провайдеры (Ollama, локальный OpenAI-compatible) создаются одинаково; локальный адрес работает, только если
владелец инсталляции добавил его в allowlist. Ошибки connection test не возвращают сырой ответ провайдера, только
типизированный код:

```json
{ "status": "failed", "error": { "code": "egress_denied", "message": "Endpoint is not allowed by instance network policy." } }
```

### 2.8 `embedding-profiles` (§5, §9)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/embedding-profiles` | Список | `providers.read` | org | — | — | — |
| POST | `/api/v1/embedding-profiles` | Создать (provider, model ID, dimensions, metric) | `providers.manage` | org | — | `embedding_profile.created` | — |
| GET | `/api/v1/embedding-profiles/{id}` | Карточка | `providers.read` | org | — | — | — |
| PATCH | `/api/v1/embedding-profiles/{id}` | Только `name`, `status`; изменение модели/размерности → `409 reindex_required` | `providers.manage` | org | — | `embedding_profile.updated` | — |
| POST | `/api/v1/embedding-profiles/{id}/test` | Тестовый embedding, проверка размерности | `providers.manage` | org | — | — | provider_test |

### 2.9 `data-sources` (§7)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/data-sources` | Список, статусы sync | `data_sources.read` | org | — | — | — |
| POST | `/api/v1/data-sources` | Создать внешний источник (`google_drive` \| `microsoft_365`); `upload` через API не создаётся | `data_sources.manage` | org | — | `data_source.created` | — |
| GET | `/api/v1/data-sources/{id}` | Карточка: статус, `granted_scopes`, последний sync | `data_sources.read` | org | — | — | — |
| PATCH | `/api/v1/data-sources/{id}` | Выбранная папка/site/library, расписание | `data_sources.manage` | org | — | `data_source.updated` | — |
| DELETE | `/api/v1/data-sources/{id}` | Удалить источник и его документы по lifecycle | `data_sources.manage` | org | Idem | `data_source.deleted` | — |
| POST | `/api/v1/data-sources/{id}/oauth/start` | Начать OAuth (state + PKCE), вернуть `authorization_url` | `data_sources.manage` | org | — | `data_source.oauth.started` | — |
| GET | `/api/v1/data-sources/oauth/callback` | Callback провайдера: проверка `state`, обмен кода, шифрование refresh token, redirect в UI | state | pub* | — | `data_source.connected` / `data_source.oauth.failed` | — |
| POST | `/api/v1/data-sources/{id}/validate` | `validate_connection` (§7) | `data_sources.manage` | org | — | — | provider_test |
| GET | `/api/v1/data-sources/{id}/browse` | Folder/site/drive/library picker (`?parent_id=`) | `data_sources.manage` | org | — | — | general |
| POST | `/api/v1/data-sources/{id}/sync` | Ручной запуск sync (`mode`: `incremental` \| `reconciliation`) | `data_sources.sync` | org | Idem | `sync.requested` | sync |
| POST | `/api/v1/data-sources/{id}/disconnect` | Revoke у провайдера + удаление credentials | `data_sources.manage` | org | — | `data_source.disconnected` | — |

\* Callback не имеет сессионного контекста API в обычном смысле: организация и пользователь восстанавливаются из
серверного состояния, связанного с одноразовым `state`, и сверяются с текущей сессией браузера.

Document Source типа `upload` создаётся и удаляется вместе с KB ([ADR-008](adr/0008-unified-document-ingestion.md)):
`POST` с `type = upload` → `422`; `DELETE`/`disconnect`/`sync`/`oauth` для него → `409 conflict`. В списке он
виден с `owner_knowledge_base_id`.

UI обязан показывать **фактические** `granted_scopes` и предупреждение, что выбор папки не ограничивает OAuth token
(§7).

### 2.10 `knowledge-bases` (§5)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/knowledge-bases` | KB, доступные пользователю | KB `read` | org | — | — | — |
| POST | `/api/v1/knowledge-bases` | Создать (embedding profile обязателен) | `knowledge_bases.manage` | org | — | `kb.created` | — |
| GET | `/api/v1/knowledge-bases/{id}` | Карточка + статусы индексации | KB `read` | org | — | — | — |
| PATCH | `/api/v1/knowledge-bases/{id}` | Изменить; смена embedding profile → reindex | KB `manage` | org | — | `kb.updated` | — |
| DELETE | `/api/v1/knowledge-bases/{id}` | Удалить | `knowledge_bases.manage` | org | Idem | `kb.deleted` | — |
| GET | `/api/v1/knowledge-bases/{id}/access` | Список доступа | KB `manage` | org | — | — | — |
| PUT | `/api/v1/knowledge-bases/{id}/access` | Заменить список `{principal_type, principal_id, access_level}` | KB `manage` | org | — | `kb.access.granted`, `kb.access.revoked` | — |
| GET | `/api/v1/knowledge-bases/{id}/sources` | Привязанные источники | KB `read` | org | — | — | — |
| PUT | `/api/v1/knowledge-bases/{id}/sources/{data_source_id}` | Привязать источник | KB `manage` + `data_sources.read` | org | — | `kb.source.attached` | — |
| DELETE | `/api/v1/knowledge-bases/{id}/sources/{data_source_id}` | Отвязать (документы источника исключаются из KB) | KB `manage` | org | — | `kb.source.detached` | — |
| POST | `/api/v1/knowledge-bases/{id}/reindex` | Переиндексация KB | KB `manage` | org | Idem | `kb.reindex.requested` | expensive |

Привязка источника к KB требует подтверждения, что все участники KB имеют право видеть все документы источника
(§8 Document ACL); UI показывает предупреждение, API принимает флаг `acl_acknowledged: true` **[Proposed]**.

### 2.11 `documents` (§5, §6)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/documents` | Список; обязательный фильтр `knowledge_base_id`; фильтры `processing_status`, `data_source_id`, `q` | KB `read` | org | — | — | — |
| POST | `/api/v1/documents` | Upload (`multipart/form-data`: `knowledge_base_id`, `file`) → quarantine + ingestion job | KB `manage` | org | Idem | `document.uploaded` | upload |
| GET | `/api/v1/documents/{id}` | Карточка: версия, MIME, checksum, размер, source ID, timestamps, processing status | KB `read` | org | — | — | — |
| GET | `/api/v1/documents/{id}/versions` | Версии | KB `read` | org | — | — | — |
| DELETE | `/api/v1/documents/{id}` | Удаление: немедленное исключение из retrieval + purge job | KB `manage` | org | Idem | `document.deleted` | — |
| POST | `/api/v1/documents/{id}/reindex` | Переиндексация | KB `manage` | org | Idem | `document.reindex.requested` | expensive |
| GET | `/api/v1/documents/{id}/versions/{version_id}/chunks/{chunk_id}/view` | Защищённый in-app source viewer (§5) | KB `read` | org | — | — **[Proposed: `document.viewed` опционально]** | general |

**Upload `202`:**

```json
{
  "document": { "id": "…", "filename": "Policy.pdf", "processing_status": "pending" },
  "job": { "id": "…", "type": "ingestion", "status": "queued" }
}
```

Upload и документы из коннекторов проходят один pipeline ([ADR-008](adr/0008-unified-document-ingestion.md)); HTTP-запрос
только принимает файл в quarantine и ставит job. Повторная загрузка того же контента в существующий документ не
создаёт новую версию; повторный `reindex` уже обработанной версии завершается без изменений (`result:
skipped_already_processed`).

Ошибки: `413 payload_too_large`, `415 unsupported_media_type` (MVP: PDF, DOCX, XLSX, TXT, CSV, Markdown; в 0.1.0 —
PDF, DOCX), `429`. Тип определяется по содержимому, `filename` используется только для отображения.

**Source viewer `200`:**

```json
{
  "document": { "id": "…", "filename": "Policy.pdf", "mime_type": "application/pdf" },
  "version": { "id": "…", "version_number": 3, "is_current": true },
  "chunk": {
    "id": "…", "content": "…", "page_start": 12, "page_end": 12,
    "section_path": ["4. Отпуск", "4.2 Перенос"], "sheet_name": null, "row_start": null, "row_end": null
  },
  "context": { "before": ["…"], "after": ["…"] }
}
```

Доступ проверяется при **каждом** запросе (наличие цитаты в истории не даёт права). Удалённый/purged документ или
отозванный доступ → `404`. Текст отдаётся как данные; клиент отображает его без интерпретации HTML.

### 2.12 `assistants` (§5)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/assistants` | Без `assistants.manage` — только доступные для чата ([ADR-005](adr/0005-assistant-access-model.md)); с `assistants.manage` — все, с полем `availability` | `chat.use` / `assistants.manage` | org | — | — | — |
| POST | `/api/v1/assistants` | Создать: instructions, LLM, embedding/retrieval profile, KB, context limits | `assistants.manage` | org | — | `assistant.created` | — |
| GET | `/api/v1/assistants/{id}` | Карточка (instructions видны только с `assistants.manage`) | доступ к ассистенту | org | — | — | — |
| PATCH | `/api/v1/assistants/{id}` | Изменить | `assistants.manage` | org | — | `assistant.updated` | — |
| DELETE | `/api/v1/assistants/{id}` | Удалить | `assistants.manage` | org | — | `assistant.deleted` | — |

**Доступ ([ADR-005](adr/0005-assistant-access-model.md)).** Ассистент доступен пользователю, только если у него есть
доступ ко всем KB ассистента; ассистент без KB — только пользователям с `assistants.manage`. Недоступный ассистент:
`GET /assistants/{id}` → `404`. Проверка повторяется на каждом запросе.

```json
// элемент списка для пользователя с assistants.manage
{ "id": "…", "name": "HR Assistant", "status": "active",
  "availability": { "available": false, "reason": "missing_kb_access" } }   // missing_kb_access | no_knowledge_bases | disabled
```

Валидация: все KB ассистента используют тот же embedding profile, что и ассистент **[Proposed]**; модель —
включённый `model_config` той же организации.

### 2.13 `conversations` (§5)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/conversations` | Мои диалоги (`assistant_id`, `sort=-last_message_at`) | `chat.use` | org | — | — | — |
| POST | `/api/v1/conversations` | Создать диалог с ассистентом | `chat.use` + доступ к ассистенту | org | — | — | — |
| GET | `/api/v1/conversations/{id}` | Диалог (только владелец) | владелец | org | — | — | — |
| PATCH | `/api/v1/conversations/{id}` | Переименовать | владелец | org | — | — | — |
| DELETE | `/api/v1/conversations/{id}` | Удалить (soft delete → purge по retention) | владелец | org | — | `conversation.deleted` | — |
| GET | `/api/v1/conversations/{id}/messages` | Сообщения с цитатами | владелец | org | — | — | — |

### 2.14 `chat/stream` (§5, §6)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| POST | `/api/v1/chat/stream` | Отправить сообщение и получить SSE-поток ответа | `chat.use` + доступ к ассистенту и его KB | org | `client_message_id` | — (метрики в `generation_runs`) | chat |
| POST | `/api/v1/chat/runs/{generation_run_id}/stop` | Остановить генерацию (§5) | владелец диалога | org | — | — | — |

Подробно — в разделе 3.

Авторизация чата ([ADR-005](adr/0005-assistant-access-model.md)): тело запроса **не содержит** идентификаторов KB или
источников — их вычисляет backend. Недоступный ассистент: новый диалог → `404 not_found`; сообщение в собственный
диалог → `403 assistant_unavailable`.

### 2.15 `jobs` (§11, §13 Фаза 11)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/jobs` | Ingestion и sync jobs (`type`, `status`, `knowledge_base_id`, `data_source_id`) | `jobs.read` | org | — | — | — |
| GET | `/api/v1/jobs/{type}/{id}` | Детали, stage, attempts, redacted error | `jobs.read` | org | — | — | — |
| POST | `/api/v1/jobs/{type}/{id}/retry` | Повтор `failed`/`dead` job | `jobs.manage` | org | Idem | `job.retried` | expensive |
| POST | `/api/v1/jobs/{type}/{id}/cancel` | Отмена `queued`/`running` | `jobs.manage` | org | — | `job.cancelled` | — |

`{type}`: `ingestion` \| `sync`.

### 2.16 `audit` (§8 п.9)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/audit/events` | Журнал (`action`, `actor_user_id`, `target_type`, `target_id`, `outcome`, `from`, `to`) | `audit.read` | org | — | **[Proposed]** `audit.viewed` | general |
| GET | `/api/v1/audit/events/{id}` | Событие | `audit.read` | org | — | — | — |
| GET | `/api/v1/audit/platform-events` | События уровня инсталляции | `platform.audit.read` | plat | — | — | — |

Аудит доступен только на чтение; endpoints изменения/удаления событий отсутствуют.

### 2.17 `system` ([ADR-006](adr/0006-initial-setup-and-system-configuration.md), [ADR-007](adr/0007-network-access-and-local-llm.md))

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/system/settings` | Instance Settings только на чтение (без секретов), включая network allowlist | `platform.settings.read` | plat | — | — | general |

Endpoints для **изменения** Instance Settings и allowlist **не существуют**: они меняются конфигурацией сервера и
перезапуском. Секретные значения представлены как `{ "configured": true }`.

```json
{
  "public_url": "https://kb.example.test",
  "setup_mode": "web",
  "limits": { "upload_max_bytes": 52428800, "max_concurrent_generations_per_user": 2 },
  "smtp": { "configured": false },
  "storage": { "backend": "local" },
  "network": {
    "allowlist": [
      { "name": "local-ollama", "url": "http://ollama:11434", "purposes": ["llm", "embedding"],
        "provider_types": ["ollama"], "allowed_cidrs": ["172.30.10.0/24"], "organizations": [] }
    ]
  }
}
```

---

## 3. Chat streaming — SSE (§5, §6)

### 3.1 Запрос

```http
POST /api/v1/chat/stream
Accept: text/event-stream
Content-Type: application/json
X-CSRF-Token: …

{
  "conversation_id": "…",        // null → создаётся новый диалог
  "assistant_id": "…",           // обязателен, если conversation_id = null
  "content": "Как перенести отпуск?",
  "client_message_id": "…"       // UUID, генерирует клиент; повтор не создаёт дубликат
}
```

Ошибки, обнаруженные **до** начала потока (аутентификация, CSRF, валидация, права, rate limit), возвращаются
обычным HTTP-ответом в формате §1.5. Поскольку метод — `POST`, клиент использует `fetch` со стримингом, а не
`EventSource`.

### 3.2 Ответ

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
X-Request-ID: …
```

Формат кадра:

```text
id: 7
event: token
data: {"delta":"Перенос возможен "}

```

### 3.3 События (§5)

| Событие | Когда | `data` |
|---|---|---|
| `message_start` | Первым, после сохранения сообщения пользователя | `{ "conversation_id", "user_message_id", "assistant_message_id", "generation_run_id", "created_at" }` |
| `token` | Фрагмент ответа | `{ "delta": "…" }` |
| `citation` | Когда в ответе впервые встречается **валидный** citation ID | см. ниже |
| `message_complete` | Последним при успехе или остановке | `{ "assistant_message_id", "finish_reason": "stop" \| "length" \| "user_cancelled", "citation_ids": ["c1","c3"], "usage": { "prompt_tokens", "completion_tokens" } }` |
| `error` | Ошибка после начала потока; поток закрывается | `{ "code", "message", "retryable": false, "correlation_id" }` |
| `heartbeat` | Каждые 15 с **[Proposed]** при отсутствии других событий | `{ "ts": "…" }` |

`citation`:

```json
{
  "citation_id": "c1",
  "document_id": "…",
  "document_version_id": "…",
  "chunk_id": "…",
  "filename": "Policy.pdf",
  "page": 12,
  "section": "4.2 Перенос",
  "sheet": null,
  "row": null,
  "snippet": "…",
  "viewer_route": "/viewer/documents/{document_id}/versions/{version_id}?chunk={chunk_id}"
}
```

### 3.4 Правила цитирования (§6, §12)

1. Перед вызовом модели backend присваивает каждому отобранному chunk временный citation ID (`c1`, `c2`, …),
   действующий только в рамках этой генерации.
2. Модель получает инструкцию ссылаться только на эти ID в формате `[c1]` **[Proposed]**.
3. Backend буферизует потенциальные маркеры в потоке: валидный ID пропускается в `token` и порождает `citation`
   (один раз на ID); **невалидный ID удаляется** из текста и учитывается в
   `generation_runs.rejected_citation_count`.
4. В `message_citations` сохраняются только валидированные цитаты.
5. Если подходящих chunks не найдено, модель получает инструкцию корректно отказать; ответ без цитат допустим только
   как отказ/уточнение (§6).

### 3.5 Остановка генерации (§5)

`POST /api/v1/chat/runs/{generation_run_id}/stop` → `202`. Поток завершается событием `message_complete` с
`finish_reason = "user_cancelled"`; частичный ответ сохраняется со статусом `stopped`. Повторный вызов для уже
завершённой генерации → `202` без эффекта (идемпотентно).

**Открытый вопрос:** поведение при разрыве соединения клиентом (отмена vs. завершение генерации в фоне) и
поддержка `Last-Event-ID` для переподключения.

---

## 4. Ключевые DTO (черновик)

```jsonc
// KnowledgeBase
{
  "id": "…", "name": "HR", "description": "…",
  "embedding_profile_id": "…",
  "indexing": { "documents_total": 120, "indexed": 117, "processing": 2, "failed": 1 },
  "my_access_level": "manage",
  "created_at": "…", "updated_at": "…"
}

// Document
{
  "id": "…", "filename": "Policy.pdf", "mime_type": "application/pdf",
  "data_source": { "id": "…", "type": "upload" },
  "external_id": "…",
  "current_version": { "id": "…", "version_number": 3, "checksum_sha256": "…", "size_bytes": 482133, "status": "ready" },
  "processing_status": "indexed",
  "source_modified_at": "…", "created_at": "…", "updated_at": "…"
}

// Assistant
{
  "id": "…", "name": "HR Assistant", "description": "…",
  "instructions": "…",                           // только для assistants.manage
  "model_config_id": "…", "embedding_profile_id": "…",
  "knowledge_base_ids": ["…"],
  "retrieval": { "top_k": 8, "vector_top_k": 40, "keyword_top_k": 40, "rrf_k": 60 },
  "limits": { "max_context_tokens": 8000, "max_history_messages": 10, "max_output_tokens": 1024 },
  "status": "active",
  "availability": { "available": true, "reason": null }   // ADR-005
}

// Job
{
  "id": "…", "type": "ingestion", "kind": "ingest", "status": "failed", "stage": "parse",
  "attempt": 3, "max_attempts": 3,
  "error": { "code": "parse_timeout", "message": "Parsing exceeded time limit." },
  "queued_at": "…", "started_at": "…", "finished_at": "…"
}
```

## 5. Rate limits (§5, §8 п.5) **[Proposed — значения по умолчанию, настраиваются]**

| Профиль | Лимит | Ключ |
|---|---|---|
| `login` | 5/мин и 20/час | IP + email; дополнительно на аккаунт |
| `refresh` | 30/мин | сессия |
| `setup` | 5/мин | IP |
| `upload` | 30/мин, суммарный объём 500 МБ/час | пользователь |
| `chat` | 20/мин, не более 2 одновременных генераций | пользователь |
| `provider_test` | 10/мин | организация |
| `sync` | 6/час | источник |
| `expensive` (reindex, retry) | 10/час | организация |
| `general` | 300/мин | пользователь |

При превышении — `429 rate_limited` + `Retry-After`.

## 6. Frontend ↔ API (§10)

| Раздел UI (§10) | Группы endpoints |
|---|---|
| Login/Setup | `setup`, `auth` |
| Dashboard | `knowledge-bases`, `jobs`, `data-sources` (агрегаты) |
| Chat | `assistants`, `conversations`, `chat/stream`, `documents` (viewer) |
| Assistants | `assistants`, `providers`, `embedding-profiles`, `knowledge-bases` |
| Knowledge Bases | `knowledge-bases`, `groups`, `users` |
| Documents | `documents` |
| Data Sources | `data-sources` |
| Providers & Models | `providers`, `embedding-profiles` |
| Users & Groups | `users`, `groups` |
| Roles & Permissions | `roles` |
| Jobs/Sync | `jobs`, `data-sources` |
| Audit | `audit` |
| System Settings | `organizations` (`/current/settings`), `system` (только чтение) |

## 7. Открытые вопросы

1. Формат ошибок и пагинации (планируемый ADR).
2. Хранилище idempotency keys (PostgreSQL / Redis).
3. Пользовательские роли в MVP.
4. SSE: разрыв соединения, `Last-Event-ID`.
5. Нужен ли endpoint скачивания оригинала документа (и с какими заголовками безопасности).

Решено: setup и системные настройки ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)), доступ к
ассистентам ([ADR-005](adr/0005-assistant-access-model.md)), сетевые ошибки провайдеров
([ADR-007](adr/0007-network-access-and-local-llm.md)), upload как Document Source
([ADR-008](adr/0008-unified-document-ingestion.md)).
