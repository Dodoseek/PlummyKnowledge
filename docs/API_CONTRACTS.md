# PlummyKnowledge — API Contracts

| | |
|---|---|
| Статус | v0.3 — реализованы `health`, `setup`, `auth`, `users`, `groups`, `roles` (чтение), `providers`, `embedding-profiles`, `system`; контракт не стабилен до 1.0.0, §19 |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §5, §6, §10, §12 |
| Решения | [ADR-005](adr/0005-assistant-access-model.md), [ADR-006](adr/0006-initial-setup-and-system-configuration.md) (группы `setup`, `system`), [ADR-007](adr/0007-network-access-and-local-llm.md), [ADR-008](adr/0008-unified-document-ingestion.md), [ADR-010](adr/0010-api-error-format.md), [ADR-016](adr/0016-secret-encryption.md) |
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
  `user_id`, `role` или `permissions` в обход правил.
- Write-only поля (секреты, пароли) никогда не появляются в ответах (§12).
- Формат: JSON, `snake_case`, UUID — строки, время — ISO 8601 UTC (`2026-11-17T12:00:00Z`).

### 1.3 Контекст запроса (§5, [ADR-012](adr/0012-single-organization-deployment.md))

- Инсталляция принадлежит одной организации: идентификатора организации в API нет.
- Пользователь, его роли и permissions определяются **только** из проверенной сессии; клиент не передаёт их в путях,
  query, заголовках или теле.
- Без аутентификации (scope `public`): `health`, `setup`, `auth/login`, `auth/logout`.
- Нет сессии → `401 unauthenticated` (`session_revoked` для отозванной). Ресурс, к которому у пользователя нет даже
  права чтения (KB без доступа, чужой диалог), → `404 not_found` (не раскрываем существование). Ресурс доступен для
  чтения или endpoint требует permission, которой нет, → `403 permission_denied`.

### 1.4 Аутентификация и CSRF (§5) — детали в [SECURITY_MODEL.md §4](SECURITY_MODEL.md#4-authentication-и-сессии)

- Серверная сессия с непрозрачным токеном в cookie `__Host-pk_session` (`HttpOnly; Secure; SameSite=Lax`)
  ([ADR-013](adr/0013-auth-session-strategy.md)). Заголовок `Authorization` не используется; refresh tokens нет.
- Для небезопасных методов (`POST`, `PUT`, `PATCH`, `DELETE`) с действующей сессией обязателен заголовок
  `X-CSRF-Token` — токен, привязанный к сессии и полученный в теле `Session` (§2.2). Для всех небезопасных методов
  `Origin` (или `Referer`) должен совпадать с origin `PUBLIC_URL` или `CORS_ORIGINS`. Нарушение → `403 csrf_failed`.

### 1.5 Формат ошибок ([ADR-010](adr/0010-api-error-format.md), реализовано)

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
    { "field": "body.name", "code": "string_too_long", "message": "String should have at most 200 characters" }
  ]
}
```

Правила (ADR-010):

- `type` = `about:blank`; `title` — фраза HTTP-статуса; `status` = HTTP-статус.
- `code` — обязательный стабильный идентификатор; клиенты ветвятся по `code`, а не по `title`/`detail`.
- `correlation_id` — всегда, равен заголовку ответа `X-Request-ID`.
- `detail` — необязателен, отсутствует у `5xx`. `detail` и `errors[].message` не содержат секретов, stack traces,
  SQL, переданных значений, содержимого документов или сведений об объектах, недоступных пользователю.
- `errors` — только у `validation_error` и `invalid_query_parameter`. `field` — путь через точку с источником
  (`body.…`, `query.…`, `path.…`, `header.…`); `code` — тип ошибки Pydantic (`missing`, `int_parsing`, …) или
  прикладной код.
- Статус без прикладного кода: прочие `4xx` → `bad_request`, прочие `5xx` → `internal_error`.
- Исключение: `503` от `GET /health/ready` — `application/json` со статусом проверок (§2.1).

| HTTP | `code` | Когда |
|---|---|---|
| 400 | `bad_request` | Некорректный запрос, не связанный с валидацией полей |
| 400 | `activation_token_invalid` | Ссылка активации неверна, истекла, использована или пользователь уже не `invited` — одинаково для всех причин ([ADR-015](adr/0015-user-activation-links.md)) |
| 401 | `unauthenticated` | Нет сессии, сессия истекла или пользователь неактивен |
| 401 | `session_revoked` | Сессия отозвана (§12) |
| 401 | `invalid_credentials` | Логин — одинаково для неизвестного email, неверного пароля и отключённого пользователя |
| 403 | `csrf_failed` | Нет/неверный `X-CSRF-Token` или `Origin` |
| 403 | `permission_denied` | Недостаточно прав |
| 403 | `assistant_unavailable` | Ассистент собственного диалога пользователя стал недоступен ([ADR-005](adr/0005-assistant-access-model.md)) |
| 403 | `setup_token_invalid` | Bootstrap token неверен, истёк или погашен ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| 404 | `not_found` | Нет ресурса или нет доступа к нему |
| 405 | `method_not_allowed` | Метод не поддерживается маршрутом; заголовок `Allow` |
| 409 | `conflict` | Конфликт состояния (уникальность, версия) |
| 409 | `idempotency_key_mismatch` | Тот же `Idempotency-Key` с другим телом запроса |
| 409 | `sync_already_running` | Для источника уже есть активный sync (§7) |
| 409 | `email_taken` | Email уже занят пользователем, который не удалён |
| 409 | `group_name_taken` | Имя группы (без учёта регистра) уже занято |
| 409 | `last_administrator` | Операция оставила бы инсталляцию без активного администратора |
| 409 | `cannot_modify_self` | Администратор пытается отключить или удалить собственную учётную запись |
| 409 | `invalid_user_state` | Действие невозможно в текущем статусе пользователя (например, перевыпуск ссылки для `active`) |
| 409 | `setup_already_completed` | Инсталляция уже инициализирована ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| 409 | `reindex_required` | Операция требует переиндексации (смена модели или параметров векторов embedding profile, §5) |
| 409 | `provider_name_taken` | Имя AI-провайдера (без учёта регистра) уже занято неудалённым провайдером |
| 409 | `provider_in_use` | Провайдер используется embedding profile (позднее — ассистентами) и не может быть удалён |
| 409 | `model_id_taken` | У провайдера уже есть модель с таким model ID |
| 409 | `embedding_profile_name_taken` | Имя embedding profile (без учёта регистра) уже занято |
| 409 | `knowledge_base_name_taken` | Имя базы знаний (без учёта регистра) уже занято |
| 409 | `source_unavailable` | Upload: источник `upload` базы знаний отсутствует или не активен |
| 409 | `no_version` | Reindex: у документа нет версии, которую можно обработать |
| 409 | `job_not_retryable` | Retry: job не в `failed`/`dead`, документ удалён, у документа уже есть активный job или версия удалена |
| 409 | `message_already_sent` | Chat: сообщение с этим `client_message_id` уже отправлено; в теле — `user_message_id` и `assistant_message_id` ([ADR-019](adr/0019-answer-generation-in-api-process.md)) |
| 409 | `model_unavailable` | Chat: чат-модель ассистента или её провайдер отключены или удалены |
| 409 | `invalid_state` | Действие невозможно в текущем состоянии ресурса (например, вернуть `retired` profile в `active`) |
| 413 | `payload_too_large` | Превышен лимит размера (§8 п.6; upload — `UPLOAD_MAX_BYTES`) |
| 415 | `unsupported_media_type` | Формат вне списка MVP (§6); определяется по содержимому, не по имени и `Content-Type` |
| 422 | `validation_error` | Ошибка валидации DTO; список в `errors` |
| 422 | `invalid_query_parameter` | Неизвестный или повторённый query-параметр, неразрешённая сортировка, неверный `limit` или `cursor` (§1.6); причина — в `errors[].code` |
| 422 | `endpoint_not_allowed` | `base_url` провайдера не проходит сетевую политику инсталляции ([ADR-007](adr/0007-network-access-and-local-llm.md)); причина — в `errors[].code` поля `body.base_url` |
| 422 | `setting_exceeds_instance_policy` | Значение Application Settings шире, чем разрешает instance policy ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| 429 | `rate_limited` | Превышен лимит; заголовок `Retry-After` (§8 п.5) |
| 500 | `internal_error` | Необработанная ошибка; без `detail`, подробности — в логе по `correlation_id` |
| 502 | `provider_error` | Ошибка AI provider / источника |
| 502 | `egress_denied` | Исходящее соединение заблокировано сетевой политикой во время выполнения (например, DNS указал на запрещённый адрес) ([ADR-007](adr/0007-network-access-and-local-llm.md)) |
| 503 | `service_unavailable` | Зависимость недоступна (БД, Redis) |
| 503 | `secrets_not_configured` | Операция сохраняет секрет, а key ring на сервере не настроен (`SECRETS_KEYRING_FILE`, [ADR-016](adr/0016-secret-encryption.md)) |
| 503 | `secret_unavailable` | Сохранённый секрет нельзя расшифровать текущим key ring (ключ удалён или запись повреждена); значение и `key_id` в ответ не попадают |
| 504 | `provider_timeout` | Timeout провайдера |

### 1.6 Pagination, фильтры, сортировка ([ADR-014](adr/0014-list-pagination.md), реализовано)

- Cursor (keyset) pagination: `?limit=25&cursor=<opaque>`; `limit` — 1…100, по умолчанию 25.
- Ответ списка:

  ```json
  { "items": [ ... ], "next_cursor": "eyJ...", "has_more": true }
  ```

- `cursor` непрозрачен для клиента (base64url JSON с позицией `(значение сортировки, id)`), привязан к сортировке:
  курсор другой сортировки, повреждённый или длиннее 1024 символов → `422 invalid_query_parameter`
  (`errors[].code = invalid_cursor`). Курсор только задаёт позицию: фильтры доступа всегда применяет сервер.
- Фильтры и сортировка — **только из allowlist** конкретного endpoint: `?status=invited&sort=-created_at`.
  Неизвестный или повторённый параметр, сортировка вне списка, `limit` вне диапазона → `422 invalid_query_parameter`
  (`errors[].field = query.<имя>`, `errors[].code` — `unknown_query_parameter`, `duplicate_query_parameter`,
  `invalid_sort`, `invalid_limit`, `invalid_cursor`). Значения никогда не подставляются в SQL напрямую; поиск `q`
  экранирует `%`, `_` и `\`.

### 1.7 Idempotency (§10)

- Для чувствительных повторяемых операций (помечены в таблицах как **Idem**) клиент передаёт
  `Idempotency-Key: <uuid>`.
- Тот же ключ + то же тело → возвращается сохранённый результат первой обработки, побочные эффекты не повторяются.
- Тот же ключ + другое тело → `409 idempotency_key_mismatch`.
- Ключи привязаны к пользователю и endpoint; срок хранения **[Proposed]** 24 часа.

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
| Scope | `auth` (сессия пользователя) \| `public` |
| Schemas | Request / response DTO |
| Ошибки | Коды из §1.5 |
| Side effects | Создаваемые jobs, отзыв сессий, удаление данных |
| Idempotency | Да/нет, ключ |
| Audit event | Имя события ([SECURITY_MODEL.md](SECURITY_MODEL.md), раздел 11 «Audit log») или «—» |
| Rate limits | Лимит или «общий» |

В таблицах ниже: **Scope** — `auth` (нужна сессия), `pub` (без аутентификации);
**Idem** — требуется `Idempotency-Key`; **RL** — специальный rate limit (см. §5); «—» — нет.

---

## 2. Endpoints по группам (§10)

### 2.0 `setup` ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) — реализовано

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/setup/status` | Состояние первоначальной настройки | — | pub | — | — | — |
| POST | `/api/v1/setup/initialize` | Создать первого администратора | bootstrap token | pub | — | `setup.completed` / `setup.initialize.rejected` | setup |

**`GET /setup/status` → `200`:** `{ "status": "uninitialized" | "initialized", "web_setup_enabled": true }`. Других
сведений (версии, конфигурация) не раскрывает.

**`POST /setup/initialize`**

```json
{
  "bootstrap_token": "…",
  "admin": { "email": "admin@example.com", "display_name": "Admin", "password": "…" }
}
```

- `201`: тело `Session` (как `POST /auth/login`) + cookie сессии. Пользователь получает роль Administrator (`admin`).
  Организация в БД не создаётся ([ADR-012](adr/0012-single-organization-deployment.md)).
- Ошибки: `409 setup_already_completed` (инсталляция инициализирована — в том числе для второго из двух
  одновременных запросов и при любом токене), `403 setup_token_invalid` (неверный, истёкший или погашенный токен),
  `403 csrf_failed` (неверный или отсутствующий `Origin`), `404 not_found` (режим `cli_only`), `422 validation_error`
  (поля, email, парольная политика — `errors[].code`: `invalid_email`, `invalid_display_name`,
  `password_too_short`, `password_too_long`, `password_matches_email`, `password_repetitive`), `429 rate_limited`, `503 service_unavailable` (Redis недоступен).
- Backend сам проверяет состояние в транзакции с блокировкой `instance_state`; статус, показанный frontend, роли не
  играет. Тело запроса не логируется.
- Повторное создание или восстановление администратора — только CLI на сервере
  (`python -m plummy.cli admin create`, `python -m plummy.cli admin reset-password`), не через API.

### 2.1 `health`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/health/live` | Процесс жив | — | pub | — | — | — |
| GET | `/api/v1/health/ready` | БД, Redis, storage, миграции применены, ключи всех сохранённых секретов есть в key ring | — | pub | — | — | — |

Ответ `ready` не раскрывает версии, хосты и тексты ошибок зависимостей: `{"status":"ready"}` или
`503 {"status":"not_ready","checks":{"database":"fail"}}`. Проверка `secrets` падает, если в БД есть секреты на
ключе, которого нет в key ring, или секреты есть, а key ring не настроен ([ADR-016](adr/0016-secret-encryption.md)).

### 2.2 `auth` ([ADR-013](adr/0013-auth-session-strategy.md))

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| POST | `/api/v1/auth/login` | Логин, установка cookie сессии — **реализовано** | — | pub | — | `auth.login.succeeded` / `auth.login.failed` | login |
| POST | `/api/v1/auth/logout` | Отзыв текущей сессии — **реализовано** | — | pub | — | `auth.logout` | — |
| GET | `/api/v1/auth/me` | Текущий пользователь, роли, эффективные permissions, сессия, CSRF-токен — **реализовано** | сессия | auth | — | — | — |
| GET | `/api/v1/auth/sessions` | Мои активные сессии — **реализовано** | сессия | auth | — | — | — |
| DELETE | `/api/v1/auth/sessions/{session_id}` | Отозвать свою сессию — **реализовано** | сессия | auth | — | `auth.session.revoked` | — |
| POST | `/api/v1/auth/password/change` | Смена пароля; отзыв всех сессий и новая сессия — **реализовано** | сессия | auth | — | `auth.password.changed` | login |
| POST | `/api/v1/auth/activate` | Активация учётной записи по одноразовой ссылке ([ADR-015](adr/0015-user-activation-links.md)) — **реализовано** | activation token | pub | — | `user.activated` / `user.activation.rejected` | activation |
| POST | `/api/v1/auth/password/reset-request` | Только при наличии email infrastructure (§5) | — | pub | — | `auth.password.reset_requested` | login |
| POST | `/api/v1/auth/password/reset` | Только при наличии email infrastructure (§5) | reset token | pub | — | `auth.password.reset` | login |

Первоначальная настройка вынесена в группу `setup` (§2.0). Endpoint `/auth/refresh` не существует (ADR-013).
Автоматической регистрации нет.

**DTO `Session`** — ответ `POST /auth/login` (`200`), `POST /setup/initialize` (`201`) и `GET /auth/me` (`200`):

```json
{
  "user": { "id": "…", "email": "user@example.com", "display_name": "User" },
  "roles": ["member"],
  "permissions": ["chat.use"],
  "session": { "expires_at": "2026-11-24T12:00:00Z", "idle_expires_at": "2026-11-18T12:00:00Z" },
  "csrf_token": "…"
}
```

`csrf_token` передаётся в `X-CSRF-Token` с небезопасными методами; хранится только в памяти страницы.
Все ответы группы — `Cache-Control: no-store`.

**`POST /auth/login`**

- Request: `{ "email": "user@example.com", "password": "..." }` (неизвестные поля → `422`).
- `200`: `Session` + `Set-Cookie: __Host-pk_session=…; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=…`
  (атрибуты — SECURITY_MODEL.md §4.3). Сессия из прежней cookie отзывается.
- Ошибки: `401 invalid_credentials` (одинаково для неизвестного email, неверного пароля, отключённого или удалённого
  пользователя), `403 csrf_failed` (`Origin`), `422`, `429 rate_limited` + `Retry-After`, `503 service_unavailable`.

**`POST /auth/logout`** — `204` и удаление cookie. С действующей сессией требует `X-CSRF-Token` (иначе
`403 csrf_failed`) и отзывает её; без сессии (нет cookie, истекла, отозвана) — идемпотентно `204`.

**`GET /auth/me`** — `200 Session`; `401 unauthenticated` (нет cookie, сессия не найдена или истекла, пользователь
неактивен), `401 session_revoked` (сессия отозвана). При `401` cookie удаляется.

**`GET /auth/sessions`** — `200 { "items": [SessionItem] }`, только действующие сессии текущего пользователя,
сначала последние использованные. `SessionItem`: `id`, `created_at`, `last_seen_at`, `expires_at`, `user_agent`,
`ip_address`, `current` (сессия этого запроса). Хэши токенов не возвращаются.

**`DELETE /auth/sessions/{session_id}`** — `204`. Чужая, отозванная или несуществующая сессия → одинаковый
`404 not_found`. Отзыв текущей сессии удаляет cookie (это выход).

**`POST /auth/password/change`** — `{ "current_password": "…", "new_password": "…" }`. Проверка текущего пароля идёт
через лимиты `login` (неудача считается неудачной попыткой входа). `200 Session` + новая cookie: все сессии
пользователя отзываются (`password_change`), этому браузеру выдаётся новая сессия и новый CSRF-токен. Ошибки:
`422` с `errors[].code` — `invalid_current_password` (поле `body.current_password`), `password_unchanged` или
правило политики паролей (поле `body.new_password`); `403 csrf_failed`; `429 rate_limited`.

**`POST /auth/activate`** — публичный: `{ "token": "…", "password": "…" }`; токен — из фрагмента ссылки
`{PUBLIC_URL}/activate#token=…`. `200 Session` + cookie (как login). Неверный, истёкший, использованный токен или
пользователь, который уже не `invited`, → одинаковый `400 activation_token_invalid`. Пароль, нарушающий политику, →
`422` (`body.password`), ссылка остаётся действующей. Лимит `activation`; требуется доверенный `Origin`.

### 2.3 `settings` — Application Settings ([ADR-006](adr/0006-initial-setup-and-system-configuration.md), [ADR-012](adr/0012-single-organization-deployment.md))

Группы `organizations` нет: инсталляция принадлежит одной организации (ADR-012).

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/settings` | Application Settings: заданные и effective значения, instance-границы | `settings.manage` | auth | — | — | — |
| PATCH | `/api/v1/settings` | Изменить Application Settings в пределах instance policy | `settings.manage` | auth | — | `settings.updated` | — |

**`PATCH /settings`** ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)): тело — частичный
`ApplicationSettingsV1`; неизвестные поля → `422`; значение шире instance policy →
`422 setting_exceeds_instance_policy`. Ответ:

```json
{
  "settings_version": 1,
  "values":    { "chat_retention_days": 90, "max_output_tokens_cap": 2048 },
  "effective": { "chat_retention_days": 90, "max_output_tokens_cap": 2048 },
  "instance_limits": { "chat_retention_days": { "max": 365 }, "max_output_tokens_cap": { "max": 4096 } }
}
```

### 2.4 `users` (реализовано)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/users` | Список: фильтры `status`, `group_id`, `q`; сортировка `email` (по умолчанию), `-email`, `created_at`, `-created_at` | `users.read` | auth | — | — | — |
| POST | `/api/v1/users` | Создать пользователя `invited` и получить одноразовую ссылку активации ([ADR-015](adr/0015-user-activation-links.md)) | `users.manage` (+ `roles.assign`, если переданы `roles`) | auth | — | `user.created` | — |
| GET | `/api/v1/users/{id}` | Карточка: прямые роли и группы | `users.read` | auth | — | — | — |
| PATCH | `/api/v1/users/{id}` | Имя, статус `active`/`disabled` (`disabled` → отзыв всех сессий) | `users.manage` | auth | — | `user.updated`, `user.disabled` | — |
| DELETE | `/api/v1/users/{id}` | Soft delete; отзыв сессий, удаление из групп и ролей, email освобождается | `users.manage` | auth | — | `user.deleted` | — |
| PUT | `/api/v1/users/{id}/roles` | Заменить набор прямых ролей | `roles.assign` | auth | — | `role.assigned`, `role.revoked` | — |
| POST | `/api/v1/users/{id}/sessions/revoke` | Отозвать все сессии пользователя → `{ "revoked": N }` | `users.manage` | auth | — | `auth.session.revoked_by_admin` | — |
| POST | `/api/v1/users/{id}/activation` | Новая ссылка активации (только `invited`); прежняя перестаёт действовать | `users.manage` | auth | — | `user.activation.reissued` | — |

**DTO `User`**: `id`, `email`, `display_name`, `status` (`active` / `invited` / `disabled`), `roles` (прямые роли),
`created_at`, `last_login_at`; карточка дополнительно содержит `groups: [{ id, name }]`. Пароли, хэши и токены в
ответах отсутствуют.

**`POST /users`** — `{ "email": "…", "display_name": "…", "roles": ["member"] }` (неизвестные поля, в том числе
`password`, → `422`). `201 { "user": User, "activation": { "url": "…/activate#token=…", "expires_at": "…" } }`.
Ссылка показывается один раз и действует 72 часа. Idempotency-Key не нужен: повтор даёт `409 email_taken`
(email уникален среди неудалённых пользователей, без учёта регистра). Ошибки: `422` (`invalid_email`,
`invalid_display_name`, `unknown_role`), `403 permission_denied`.

**Инварианты:** в инсталляции всегда есть хотя бы один активный администратор — снятие роли `admin`, отключение и
удаление, которые нарушили бы это, → `409 last_administrator` (изменения сериализуются блокировкой строки роли
`admin`). Администратор не может отключить или удалить себя (`409 cannot_modify_self`). Статус `invited` нельзя
задать через `PATCH`; `invited` → `active` только через активацию (`409 invalid_user_state`). Включение
отключённого пользователя, который не активировался, возвращает его в `invited`. Восстановление доступа без
администратора — только CLI на сервере.

### 2.5 `groups` (реализовано)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/groups` | Список с `member_count`: фильтр `q`; сортировка `name` (по умолчанию), `-name`, `created_at`, `-created_at` | `groups.read` | auth | — | — | — |
| POST | `/api/v1/groups` | Создать | `groups.manage` | auth | — | `group.created` | — |
| GET | `/api/v1/groups/{id}` | Карточка с участниками и ролями | `groups.read` | auth | — | — | — |
| PATCH | `/api/v1/groups/{id}` | Имя, описание (`description: null` очищает) | `groups.manage` | auth | — | `group.updated` | — |
| DELETE | `/api/v1/groups/{id}` | Soft delete: участники и роли группы снимаются (позже — и доступы к KB); имя освобождается | `groups.manage` | auth | — | `group.deleted` | — |
| PUT | `/api/v1/groups/{id}/members` | Заменить состав | `groups.manage` | auth | — | `group.members.changed` | — |
| PUT | `/api/v1/groups/{id}/roles` | Заменить роли группы | `roles.assign` | auth | — | `role.assigned`, `role.revoked` | — |

Имя группы — 1–100 символов, уникально среди неудалённых групп без учёта регистра (`409 group_name_taken`);
описание — до 500 символов. `PUT …/members` принимает `{ "user_ids": [...] }` (до 1000); несуществующие или
удалённые пользователи → `422 unknown_user`. Роль `admin` группе не назначается (`422 role_not_assignable_to_group`):
администратор назначается только напрямую. Эффективные permissions пользователя = прямые роли ∪ роли его групп;
вычисляются на каждом запросе, поэтому изменения действуют со следующего запроса участников.

### 2.6 `roles` (чтение реализовано)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/roles` | `{ items: [{ key, name, description, is_system, permissions }] }` | `roles.read` | auth | — | — | — |
| GET | `/api/v1/roles/permissions` | `{ items: [{ key, description }] }` | `roles.read` | auth | — | — | — |
| POST / PATCH / DELETE | `/api/v1/roles[/{id}]` | Пользовательские роли — **не в 0.1.0** **[Proposed]** | `roles.manage` | auth | — | `role.created/updated/deleted` | — |

Системные роли (§2) неизменяемы.

### 2.7 `providers` (§5) — реализовано

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/providers` | Список (cursor, `sort`: `name`, `-name`, `created_at`, `-created_at`) | `providers.read` | auth | — | — | — |
| POST | `/api/v1/providers` | Создать (секрет шифруется до записи, [ADR-016](adr/0016-secret-encryption.md)) | `providers.manage` | auth | — | `provider.created` | — |
| GET | `/api/v1/providers/{id}` | Карточка без секрета | `providers.read` | auth | — | — | — |
| PATCH | `/api/v1/providers/{id}` | Изменить; замена или удаление секрета | `providers.manage` | auth | — | `provider.updated`, `provider.secret.rotated` | — |
| DELETE | `/api/v1/providers/{id}` | Мягкое удаление вместе с ключом и моделями; `409 provider_in_use`, пока есть embedding profile | `providers.manage` | auth | — | `provider.deleted` | — |
| POST | `/api/v1/providers/{id}/test` | Connection test (§5) | `providers.manage` | auth | — | `provider.tested` | provider_test |
| GET | `/api/v1/providers/{id}/models` | Модели (`model_configs`), без пагинации | `providers.read` | auth | — | — | — |
| POST | `/api/v1/providers/{id}/models` | Добавить model ID | `providers.manage` | auth | — | `model.created` | — |
| PATCH / DELETE | `/api/v1/providers/{id}/models/{config_id}` | Изменить / удалить; `config_id` — UUID записи `model_configs`, не model ID провайдера | `providers.manage` | auth | — | `model.updated` / `model.deleted` | — |

**Секреты — write-only** ([ADR-016](adr/0016-secret-encryption.md)): `api_key` принимается, но никогда не
возвращается, не логируется и не пишется в audit.

```json
// POST /api/v1/providers
{
  "name": "OpenAI prod",
  "provider_type": "openai",            // openai | anthropic | ollama | openai_compatible; не меняется
  "base_url": null,                     // null → адрес по умолчанию (openai, anthropic)
  "api_key": "sk-…",                    // write-only
  "settings": { "timeout_seconds": 60 }, // 1–600, по умолчанию 60
  "enabled": true
}
// 201
{
  "id": "…",
  "name": "OpenAI prod",
  "provider_type": "openai",
  "base_url": "https://api.openai.com/v1",
  "has_secret": true,
  "secret_hint": "••••a1b2",            // последние 4 символа ключа длиной ≥ 16, иначе null
  "enabled": true,
  "settings": { "timeout_seconds": 60 },
  "last_test": null,                    // или { "status": "ok"|"failed", "tested_at": "…", "error_code": "…"|null }
  "created_at": "2026-11-17T12:00:00Z",
  "updated_at": "2026-11-17T12:00:00Z"
}
```

Правила:

- `api_key` обязателен для `openai` и `anthropic` (`422`, `errors[].code = secret_required`); для `ollama` и
  `openai_compatible` — необязателен. `base_url` обязателен для `ollama` и `openai_compatible`
  (`base_url_required`); неверный URL — `invalid_url`. Без настроенного key ring сохранение ключа →
  `503 secrets_not_configured`.
- `PATCH`: поле `api_key` отсутствует → секрет не меняется; строка → замена (новый шифртекст, audit
  `provider.secret.rotated` с `change = replaced | added`); `null` → удаление (`change = removed`; для типов,
  которым ключ обязателен, — `422 secret_required`). Смена `base_url` или ключа сбрасывает `last_test`.
- `base_url` проверяется сетевой политикой инсталляции ([ADR-007](adr/0007-network-access-and-local-llm.md)) при
  создании и изменении: статическая проверка URL и одно разрешение DNS (назначение `llm`, для типов с embeddings —
  также `embedding`). Отказ → `422 endpoint_not_allowed`, причина (`scheme_not_allowed`, `address_not_allowed`,
  `address_never_allowed`, `invalid_url`) — в
  `errors[].code` поля `body.base_url`. При каждом подключении адрес проверяется снова (`egress_denied`).
- Облачные и локальные провайдеры (Ollama, локальный OpenAI-compatible) создаются одинаково; локальный адрес работает,
  только если владелец инсталляции добавил его в allowlist.
- Имя уникально без учёта регистра среди неудалённых провайдеров (`409 provider_name_taken`); model ID уникален в
  пределах провайдера (`409 model_id_taken`) и не меняется — для другой модели создаётся новая запись.

**Connection test** возвращает `200` и при неудаче: результат сохраняется в `last_test` и пишется в audit
`provider.tested` (`outcome` success/failure, код ошибки, время ответа). Ответ провайдера и его тексты ошибок клиенту
не передаются, только типизированный код и фиксированное сообщение. Сетевые вызовы выполняются вне транзакции БД.

```json
// 200
{ "status": "ok", "latency_ms": 412, "tested_at": "2026-11-17T12:00:00Z", "models": ["gpt-…"], "error": null }
// 200
{ "status": "failed", "latency_ms": 3, "tested_at": "…", "models": [],
  "error": { "code": "egress_denied", "message": "Endpoint is not allowed by instance network policy." } }
```

Коды `error.code`: `egress_denied`, `provider_auth_failed`, `provider_rate_limited`, `provider_timeout`,
`provider_unavailable`, `provider_bad_request`, `provider_model_not_found`, `provider_content_filtered`,
`provider_invalid_response`, `provider_cancelled`.

### 2.8 `embedding-profiles` (§5, §9) — реализовано

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/embedding-profiles` | Список (cursor, `sort` как у providers) | `providers.read` | auth | — | — | — |
| POST | `/api/v1/embedding-profiles` | Создать (provider, model ID, dimensions, metric, normalize) | `providers.manage` | auth | — | `embedding_profile.created` | — |
| GET | `/api/v1/embedding-profiles/{id}` | Карточка | `providers.read` | auth | — | — | — |
| PATCH | `/api/v1/embedding-profiles/{id}` | Только `name`, `status`; изменение provider/модели/размерности/метрики/normalize → `409 reindex_required` | `providers.manage` | auth | — | `embedding_profile.updated` | — |
| POST | `/api/v1/embedding-profiles/{id}/test` | Тестовый embedding, проверка размерности | `providers.manage` | auth | — | — | provider_test |

- `provider_id` должен указывать на существующего провайдера (`422`, `errors[].code = unknown_provider`) с
  embeddings API: `openai`, `ollama`, `openai_compatible` (у `anthropic` — `capability_not_supported`).
- `dimensions` 1–4000 (предел HNSW-индекса, [ADR-017](adr/0017-vector-storage.md)), `distance_metric`: `cosine` (по умолчанию) | `inner_product` | `l2`.
- `status`: `active` → `deprecated` → `retired`; из `retired` вернуться нельзя (`409 invalid_state`). Имя уникально
  без учёта регистра (`409 embedding_profile_name_taken`).
- Тест отвечает `200`: `{ "status", "latency_ms", "tested_at", "expected_dimensions", "actual_dimensions", "error" }`;
  несовпадение размерности — `error.code = dimension_mismatch`.

### 2.9 `data-sources` (§7)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/data-sources` | Список, статусы sync | `data_sources.read` | auth | — | — | — |
| POST | `/api/v1/data-sources` | Создать внешний источник (`google_drive` \| `microsoft_365`); `upload` через API не создаётся | `data_sources.manage` | auth | — | `data_source.created` | — |
| GET | `/api/v1/data-sources/{id}` | Карточка: статус, `granted_scopes`, последний sync | `data_sources.read` | auth | — | — | — |
| PATCH | `/api/v1/data-sources/{id}` | Выбранная папка/site/library, расписание | `data_sources.manage` | auth | — | `data_source.updated` | — |
| DELETE | `/api/v1/data-sources/{id}` | Удалить источник и его документы по lifecycle | `data_sources.manage` | auth | Idem | `data_source.deleted` | — |
| POST | `/api/v1/data-sources/{id}/oauth/start` | Начать OAuth (state + PKCE), вернуть `authorization_url` | `data_sources.manage` | auth | — | `data_source.oauth.started` | — |
| GET | `/api/v1/data-sources/oauth/callback` | Callback провайдера: проверка `state`, обмен кода, шифрование refresh token, redirect в UI | state | pub* | — | `data_source.connected` / `data_source.oauth.failed` | — |
| POST | `/api/v1/data-sources/{id}/validate` | `validate_connection` (§7) | `data_sources.manage` | auth | — | — | provider_test |
| GET | `/api/v1/data-sources/{id}/browse` | Folder/site/drive/library picker (`?parent_id=`) | `data_sources.manage` | auth | — | — | general |
| POST | `/api/v1/data-sources/{id}/sync` | Ручной запуск sync (`mode`: `incremental` \| `reconciliation`) | `data_sources.sync` | auth | Idem | `sync.requested` | sync |
| POST | `/api/v1/data-sources/{id}/disconnect` | Revoke у провайдера + удаление credentials | `data_sources.manage` | auth | — | `data_source.disconnected` | — |

\* Callback не имеет сессионного контекста API в обычном смысле: пользователь восстанавливается из
серверного состояния, связанного с одноразовым `state`, и сверяются с текущей сессией браузера.

Document Source типа `upload` создаётся и удаляется вместе с KB ([ADR-008](adr/0008-unified-document-ingestion.md)):
`POST` с `type = upload` → `422`; `DELETE`/`disconnect`/`sync`/`oauth` для него → `409 conflict`. В списке он
виден с `owner_knowledge_base_id`.

UI обязан показывать **фактические** `granted_scopes` и предупреждение, что выбор папки не ограничивает OAuth token
(§7).

### 2.10 `knowledge-bases` (§5) — реализованы list, create, get, patch, delete, access, reindex, search

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/knowledge-bases` | KB, доступные пользователю; `sort`: `name` (по умолчанию), `-name`, `created_at`, `-created_at` | KB `read` | auth | — | — | — |
| POST | `/api/v1/knowledge-bases` | Создать (embedding profile обязателен) | `knowledge_bases.manage` | auth | — | `kb.created` | — |
| GET | `/api/v1/knowledge-bases/{id}` | Карточка + статусы индексации | KB `read` | auth | — | — | — |
| PATCH | `/api/v1/knowledge-bases/{id}` | Изменить имя и описание (смена embedding profile пока не поддерживается) | KB `manage` | auth | — | `kb.updated` | — |
| DELETE | `/api/v1/knowledge-bases/{id}` | Удалить | `knowledge_bases.manage` | auth | Idem | `kb.deleted` | — |
| GET | `/api/v1/knowledge-bases/{id}/access` | Список доступа | KB `manage` | auth | — | — | — |
| PUT | `/api/v1/knowledge-bases/{id}/access` | Заменить список `{principal_type, principal_id, access_level}` | KB `manage` | auth | — | `kb.access.granted`, `kb.access.revoked` | — |
| GET | `/api/v1/knowledge-bases/{id}/sources` | Привязанные источники | KB `read` | auth | — | — | — |
| PUT | `/api/v1/knowledge-bases/{id}/sources/{data_source_id}` | Привязать источник | KB `manage` + `data_sources.read` | auth | — | `kb.source.attached` | — |
| DELETE | `/api/v1/knowledge-bases/{id}/sources/{data_source_id}` | Отвязать (документы источника исключаются из KB) | KB `manage` | auth | — | `kb.source.detached` | — |
| POST | `/api/v1/knowledge-bases/{id}/reindex` | Переиндексация KB | KB `manage` | auth | Idem | `kb.reindex.requested` | expensive |
| POST | `/api/v1/knowledge-bases/{id}/search` | Hybrid-поиск фрагментов без генерации ответа | KB `read` | auth | — | — | search |

Строки `sources` ещё не реализованы (кроме `upload`, других источников пока нет); `Idempotency-Key` не поддерживается.

**KB DTO (`200`/`201`):**

```json
{
  "id": "…", "name": "HR", "description": "Policies", "embedding_profile_id": "…",
  "indexing": { "documents_total": 3, "indexed": 1, "processing": 1, "failed": 1 },
  "my_access_level": "manage",
  "created_at": "…", "updated_at": "…"
}
```

`my_access_level` (`read` \| `manage`) — итоговый доступ текущего пользователя; frontend использует его только для
отображения элементов управления. Пользователь с `knowledge_bases.manage` имеет `manage` во всех KB; остальные —
по выданному доступу (прямому или через группу). KB без доступа → `404`, `read` там, где
нужен `manage` → `403`. Создание: тело `{name, description?, embedding_profile_id}`; вместе с KB создаётся её
источник `upload`. Ошибки поля
`body.embedding_profile_id` (`422 validation_error`, `errors[].code`): `unknown_embedding_profile`,
`embedding_profile_not_active`, `dimensions_not_indexable`, `provider_unavailable`; занятое имя —
`409 knowledge_base_name_taken`. Настройки chunking через API не задаются (**[Proposed]**, всегда значения по
умолчанию — [ARCHITECTURE.md §8.4](ARCHITECTURE.md)).

**Изменение (`PATCH`)** — тело `{name?, description?, embedding_profile_id?}`: меняются только переданные поля,
`description: null` очищает описание; имя нормализуется как при создании, занятое — `409 knowledge_base_name_taken`.
Вместе с именем переименовывается источник `upload`. `embedding_profile_id`, отличный от текущего, —
`422` с `errors[].code = embedding_profile_change_not_supported` (нужна переиндексация всех документов; отдельная
задача). Запрос без изменений не пишет audit.

**Удаление (`DELETE`, только `knowledge_bases.manage`)** — одна транзакция: KB, её список доступа и связи с
источниками, источник `upload` и все его документы (`deletion_reason = kb_deleted`) удаляются, незавершённые jobs
отменяются (`error_code = kb_deleted`); документы сразу исключаются из поиска, файлы, chunks и векторы удаляет purge
(при недоступном брокере — периодический sweep). Имя KB освобождается. `204`; повторно — `404`.

**Доступ (`GET`/`PUT …/access`, KB `manage`)** — `PUT` заменяет список целиком:

```json
{ "grants": [ { "principal_type": "user", "principal_id": "…", "access_level": "read" },
              { "principal_type": "group", "principal_id": "…", "access_level": "manage" } ] }
```

Ответ обоих методов — `{items: [{principal_type, principal_id, name, email, access_level, granted_by, granted_at}]}`
(`name` — display name пользователя или имя группы, `email` — только у пользователей; удалённые пользователи и группы
не показываются). Не более 500 записей. Ошибки `422 validation_error` (`errors[].field` —
`body.grants.N.principal_id`): `duplicate_principal`, `unknown_principal` (нет живого пользователя или группы);
`body.grants` — `would_lose_manage`: пользователь без `knowledge_bases.manage` не может убрать собственный `manage`
(проверяется по новому списку с учётом групп). Неизменённые записи сохраняют `granted_by`/`granted_at`; audit —
`kb.access.granted` на каждую новую запись или смену уровня, `kb.access.revoked` на каждую удалённую. Изменение
действует со следующего запроса пользователя.

**Переиндексация (`POST …/reindex`, KB `manage`, профиль `expensive`)** — `202
{jobs_created, job_ids, already_queued, without_version}`: для каждого документа KB без queued/running job
создаётся job `reindex` (как `POST /documents/{id}/reindex`); документы с активным job остаются как есть.

**Поиск (`POST /knowledge-bases/{id}/search`)** — retrieval без LLM ([ARCHITECTURE.md §9.4](ARCHITECTURE.md),
[ADR-017](adr/0017-vector-storage.md), [ADR-018](adr/0018-full-text-search-configuration.md)). Тело
`{query, mode?, top_k?}`: `query` после нормализации (NFC, управляющие символы → пробелы, пробелы схлопываются)
1–1000 символов; `mode` — `hybrid` (по умолчанию), `vector`, `keyword`; `top_k` 1–50 (по умолчанию 8). Доступ к KB
проверяется до rate limit и до обращения к провайдеру; фильтры доступа и версий — в SQL (DATABASE_SCHEMA §5.11).

```json
{
  "mode": "hybrid",
  "items": [{
    "chunk_id": "…", "document_id": "…", "document_version_id": "…", "filename": "leave.md", "chunk_index": 1,
    "content": "…", "token_count": 42, "page_start": null, "page_end": null, "section_path": ["Отпуск", "Перенос"],
    "sheet_name": null, "row_start": null, "row_end": null,
    "score": 0.032787, "vector_rank": 1, "keyword_rank": 2
  }],
  "total_tokens": 42,
  "keyword_query_empty": false
}
```

`score` — взвешенная RRF-оценка (сумма `weight/(60 + rank)` по веткам; вес vector 1,0, keyword 0,1), `vector_rank`/`keyword_rank` — место в ветке или `null`.
Keyword-ветка ищет по **любому** слову запроса (стемминг русского и английского, операторы и кавычки — обычные
символы, [ADR-018](adr/0018-full-text-search-configuration.md) §3).
Одинаковый текст (`content_hash`) возвращается один раз. `keyword_query_empty: true` — запрос состоит только из
стоп-слов, работала лишь векторная ветка. Ошибки: `404` (нет KB или доступа), `422 validation_error` (`body.query`:
`empty_query`, `query_too_long`; `body.top_k`, `body.mode`), `409 embedding_unavailable` (профиль выведен из
эксплуатации, провайдер отключён или нет ключа; `mode: keyword` при этом работает), `429 rate_limited`,
`502 provider_error`, `502 egress_denied`, `504 provider_timeout` (без вывода провайдера).

Привязка источника к KB требует подтверждения, что все участники KB имеют право видеть все документы источника
(§8 Document ACL); UI показывает предупреждение, API принимает флаг `acl_acknowledged: true` **[Proposed]**.

### 2.11 `documents` (§5, §6) — реализовано, кроме `Idempotency-Key`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/documents` | Список; обязательный фильтр `knowledge_base_id`; фильтры `processing_status`, `data_source_id`, `q`; `sort`: `-created_at` (по умолчанию), `created_at`, `filename`, `-filename` | KB `read` | auth | — | — | — |
| POST | `/api/v1/documents` | Upload (`multipart/form-data`: `knowledge_base_id`, `file`) → quarantine + ingestion job | KB `manage` | auth | Idem | `document.uploaded` | upload |
| GET | `/api/v1/documents/{id}` | Карточка: версия, MIME, checksum, размер, source ID, timestamps, processing status | KB `read` | auth | — | — | — |
| GET | `/api/v1/documents/{id}/versions` | Версии | KB `read` | auth | — | — | — |
| DELETE | `/api/v1/documents/{id}` | Удаление: немедленное исключение из retrieval + purge job | KB `manage` | auth | Idem | `document.deleted` | — |
| POST | `/api/v1/documents/{id}/reindex` | Переиндексация | KB `manage` | auth | Idem | `document.reindex.requested` | expensive |
| GET | `/api/v1/documents/{id}/versions/{version_id}/chunks/{chunk_id}/view` | Защищённый in-app source viewer (§5) | KB `read` | auth | — | — (`document.viewed` не пишется) | — |

**Upload `202`:**

```json
{
  "document": { "id": "…", "filename": "Policy.pdf", "processing_status": "pending" },
  "job": { "id": "…", "type": "ingestion", "kind": "ingest", "status": "queued" }
}
```

Upload и документы из коннекторов проходят один pipeline ([ADR-008](adr/0008-unified-document-ingestion.md)); HTTP-запрос
только принимает файл в quarantine и ставит job. Поле `knowledge_base_id` должно идти **перед** `file`: доступ и
rate limit проверяются до записи первого байта. Каждый upload создаёт **новый** документ (загрузка новой версии
существующего документа и `Idempotency-Key` — [Proposed], не реализованы). Повторный `reindex` уже обработанной
версии завершается без изменений (`result: skipped_already_processed`).

Ошибки: `413 payload_too_large`, `415 unsupported_media_type` (MVP: PDF, DOCX, XLSX, TXT, CSV, Markdown; в 0.1.0 —
PDF, DOCX), `422 validation_error` (`errors[].code = empty_file` и др.), `409 source_unavailable`, `429`. Тип
определяется по содержимому, `filename` используется только для отображения и никогда — как путь в storage.
Лимит объёма загрузок в час (§8 п.5) пока не реализован; действует лимит числа запросов.

**Document DTO (`200`):**

```json
{
  "id": "…", "filename": "Policy.pdf", "mime_type": "application/pdf",
  "data_source": { "id": "…", "type": "upload" }, "external_id": "upload:…",
  "current_version": { "id": "…", "version_number": 1, "checksum_sha256": "…", "size_bytes": 2048,
                       "mime_type": "application/pdf", "page_count": 2, "status": "ready", "error_code": null,
                       "created_at": "…", "processed_at": "…" },
  "latest_version": { "…": "та же структура" },
  "processing_status": "indexed", "source_modified_at": null,
  "created_at": "…", "updated_at": "…"
}
```

`current_version` — версия, участвующая в retrieval; `latest_version` — последняя версия (в том числе в обработке
или `failed`, с `error_code` для отображения причины). `processing_status`: `pending` \| `processing` \|
`indexed` \| `failed` \| `skipped`. Документ без доступа или удалённый → `404`.

**Delete `204`:** документ сразу исключается из retrieval и списков, его активные jobs отменяются; purge chunks,
vectors и файлов выполняется сразу задачей `maintenance` (отсрочки нет — [DATABASE_SCHEMA.md §8](DATABASE_SCHEMA.md)).

**Reindex `202`:** `{ "job": <Job DTO §2.15>, "created": true }`; если у документа уже есть `queued`/`running` job,
возвращается он с `created: false`. Нет версии для обработки → `409 no_version`.

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
Реализация — `application/documents/viewer.py` (`ViewSource`): chunk отдаётся, только если принадлежит именно
этой версии этого документа; `context` — до 2 соседних chunks того же набора с каждой стороны (по `chunk_index`).
Любое несовпадение (чужой chunk, версия другого документа, неизвестные ID) неотличимо от отсутствия доступа:
`404 not_found` с одним и тем же `detail`. `is_current` — участвует ли версия в retrieval сейчас; старая версия,
chunks которой ещё не удалены, открывается с `is_current: false`. `Cache-Control: no-store`.

### 2.12 `assistants` (§5) — реализовано

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/assistants` | Без `assistants.manage` — только доступные для чата ([ADR-005](adr/0005-assistant-access-model.md)); с `assistants.manage` — все, с полем `availability` | `chat.use` / `assistants.manage` | auth | — | — | — |
| POST | `/api/v1/assistants` | Создать: instructions, LLM, embedding/retrieval profile, KB, context limits | `assistants.manage` | auth | — | `assistant.created` | — |
| GET | `/api/v1/assistants/{id}` | Карточка (instructions видны только с `assistants.manage`) | доступ к ассистенту | auth | — | — | — |
| PATCH | `/api/v1/assistants/{id}` | Изменить | `assistants.manage` | auth | — | `assistant.updated` | — |
| DELETE | `/api/v1/assistants/{id}` | Удалить | `assistants.manage` | auth | — | `assistant.deleted` | — |

**Доступ ([ADR-005](adr/0005-assistant-access-model.md)).** Ассистент доступен пользователю, только если у него есть
доступ ко всем KB ассистента; ассистент без KB — только пользователям с `assistants.manage`. Недоступный ассистент:
`GET /assistants/{id}` → `404`. Проверка повторяется на каждом запросе.

DTO — §4 `Assistant`. `GET /assistants` — cursor-пагинация, `sort`: `name` (по умолчанию), `-name`,
`created_at`, `-created_at`. Без `assistants.manage` и без `chat.use` — `403`. С `chat.use` список и карточка
содержат только доступные ассистенты (`instructions: null`); остальные — `404`. С `assistants.manage` — все
ассистенты, `instructions` и `availability` для **текущего** пользователя:

```json
{ "availability": { "available": false, "reason": "missing_kb_access" } }   // disabled | missing_kb_access
```

Ассистент без KB для `assistants.manage` доступен (предпросмотр, ADR-005 п.4); обычным пользователям он не виден
(`no_knowledge_bases` во внешних ответах не встречается — такие пользователи получают `404`).

Тело `POST`/`PATCH` (в `PATCH` применяются только переданные поля, `description: null` очищает описание):

```json
{ "name": "HR Assistant", "description": "…", "instructions": "…",
  "model_config_id": "…", "embedding_profile_id": "…", "knowledge_base_ids": ["…"],
  "retrieval": { "top_k": 8, "vector_top_k": 40, "keyword_top_k": 40, "rrf_k": 60,
                 "vector_weight": 1.0, "keyword_weight": 0.1 },
  "limits": { "max_context_tokens": 8000, "max_history_messages": 10, "max_output_tokens": 1024 },
  "status": "active" }
```

В `POST` обязательны `name`, `model_config_id`, `embedding_profile_id`; остальное — значения по умолчанию из примера
(`instructions` — пустая строка, `knowledge_base_ids` — пусто). Ошибки (`422 validation_error`, `errors[].code`):
`body.name` — `invalid_name`; `body.instructions` — `invalid_instructions` (≤ 20 000 печатных символов);
`body.model_config_id` — `unknown_model`, `model_disabled` (модель или провайдер выключены; проверяется при
установке или смене модели); `body.embedding_profile_id` — `unknown_embedding_profile`, `embedding_profile_not_active`;
`body.knowledge_base_ids` — `unknown_knowledge_base` (нет KB или у вызывающего нет к ней доступа `read`),
`duplicate_knowledge_base`, `embedding_profile_mismatch` (все KB обязаны использовать embedding profile ассистента —
утверждено 2026-11-25), не более 20 KB; `body.retrieval` — `out_of_range` (правила retrieval: top_k 1–50,
ветки top_k–200, `rrf_k` 1–1000, веса 0–10 и не оба 0); `body.limits.*` — `out_of_range` (context 256–128 000,
история 0–100, ответ 1–128 000), `exceeds_model_limit` (больше `max_output_tokens` модели). Занятое имя —
`409 assistant_name_taken`. Удаление — soft delete, KB не затрагиваются; удалённая KB отвязывается от ассистентов.
Модель или провайдер, используемые живым ассистентом, не удаляются: `409 model_in_use` / `409 provider_in_use`.
Audit: `assistant.created`, `assistant.updated` (имена изменённых полей), `assistant.deleted` — без текста
instructions.

### 2.13 `conversations` (§5) — реализовано

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/conversations` | Мои диалоги (`assistant_id`; `sort=-last_message_at` (по умолчанию) \| `-created_at`) | `chat.use` | auth | — | — | — |
| POST | `/api/v1/conversations` | Создать диалог с ассистентом (`{ "assistant_id", "title"? }`) | `chat.use` + доступ к ассистенту | auth | — | — | — |
| GET | `/api/v1/conversations/{id}` | Диалог (только владелец) | владелец | auth | — | — | — |
| PATCH | `/api/v1/conversations/{id}` | Переименовать (`{ "title" }`, 1–200 символов) | владелец | auth | — | — | — |
| DELETE | `/api/v1/conversations/{id}` | Удалить (soft delete → purge задачей очистки) | владелец | auth | — | `conversation.deleted` | — |
| GET | `/api/v1/conversations/{id}/messages` | Сообщения с цитатами (`sort=-sequence` (по умолчанию) \| `sequence`) | владелец | auth | — | — | — |

Чужой или удалённый диалог → `404 not_found` для всех методов, в том числе для администратора. Новый диалог с
ассистентом, которым нельзя пользоваться сейчас (ADR-005; `disabled` — и для `assistants.manage`), → `404`. Название
по умолчанию — `New conversation`; первое сообщение в новом диалоге задаёт название из начала вопроса (§3). История
своего диалога читается и после потери доступа к ассистенту; `assistant.available` показывает, можно ли продолжить.

```jsonc
// Conversation
{ "id": "…", "title": "Перенос отпуска",
  "assistant": { "id": "…", "name": "HR", "available": true },   // имя сохраняется и у удалённого ассистента
  "created_at": "…", "updated_at": "…", "last_message_at": "…" }

// Message
{ "id": "…", "sequence": 2, "role": "assistant", "content": "… [c1]",
  "status": "completed",            // completed | streaming | stopped | failed
  "error_code": null,
  "citations": [ /* как событие citation, §3.3 */ ],
  "created_at": "…", "completed_at": "…" }
```

### 2.14 `chat/stream` (§5, §6) — реализовано

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| POST | `/api/v1/chat/stream` | Отправить сообщение и получить SSE-поток ответа | `chat.use` + доступ к ассистенту и его KB | auth | `client_message_id` | — (метрики в `generation_runs`) | chat |
| POST | `/api/v1/chat/runs/{generation_run_id}/stop` | Остановить генерацию (§5) | владелец диалога | auth | — | — | — |

Подробно — в разделе 3. Реализация — `application/chat/send.py` (`SendMessage`, `Generation`),
`application/chat/runs.py` (`StopGeneration`, `SweepChat`), `api/v1/chat/`.

Авторизация чата ([ADR-005](adr/0005-assistant-access-model.md)): тело запроса **не содержит** идентификаторов KB или
источников — их вычисляет backend. Недоступный ассистент: новый диалог → `404 not_found`; сообщение в собственный
диалог → `403 assistant_unavailable`.
Генерация — в процессе API ([ADR-019](adr/0019-answer-generation-in-api-process.md)): лимит `chat` и не более 2
одновременных генераций на пользователя (`429 rate_limited` до начала потока); повтор `client_message_id` в диалоге
→ `409 message_already_sent` с `user_message_id` и `assistant_message_id`, без новой генерации (повтор ищется по
всем живым диалогам пользователя, так что повтор первого сообщения с `conversation_id = null` не создаёт второй
диалог).

Ошибки до начала потока (обычный problem+json): `401`; `403 permission_denied` (нет `chat.use`), `403
assistant_unavailable`; `404 not_found` (чужой или несуществующий диалог, недоступный ассистент нового диалога);
`409 message_already_sent`, `409 model_unavailable`, `409 embedding_unavailable` (профиль KB выведен из
эксплуатации или его провайдер недоступен); `422 validation_error` (`body.content` — 1–4000 печатных символов после
обрезки пробелов, переводы строк и табуляция допустимы; `body.assistant_id` обязателен без `conversation_id`;
лишние поля, в том числе идентификаторы KB, отклоняются); `429 rate_limited` + `Retry-After` (лимит `chat` или обе
одновременные генерации заняты); `503 service_unavailable` (Redis).

Первое сообщение нового диалога (и первое сообщение диалога, созданного без заголовка) задаёт заголовок — начало
вопроса, до 80 символов.

`POST /chat/runs/{id}/stop` → `202` без тела; чужой или несуществующий run → `404`; Redis недоступен → `503`.

### 2.15 `jobs` (§11, §13 Фаза 11) — реализованы list, get, retry для `ingestion`

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/jobs` | Ingestion и sync jobs (`type`, `status`, `knowledge_base_id`, `data_source_id`); `sort`: `-queued_at` (по умолчанию), `queued_at` | `jobs.read` | auth | — | — | — |
| GET | `/api/v1/jobs/{type}/{id}` | Детали, stage, attempts, redacted error | `jobs.read` | auth | — | — | — |
| POST | `/api/v1/jobs/{type}/{id}/retry` | Повтор `failed`/`dead` job | `jobs.manage` | auth | Idem | `job.retried` | expensive |
| POST | `/api/v1/jobs/{type}/{id}/cancel` | Отмена `queued`/`running` | `jobs.manage` | auth | — | `job.cancelled` | — |

`{type}`: `ingestion` \| `sync`. Sync jobs появятся с коннекторами: сейчас `type=sync` возвращает пустой список.
`cancel` и `Idempotency-Key` для retry не реализованы (jobs отменяются удалением документа). Retry невозможен →
`409 job_not_retryable`.

**Job DTO:**

```json
{
  "id": "…", "type": "ingestion", "kind": "ingest", "status": "failed", "stage": "embed",
  "result": null, "attempt": 3, "max_attempts": 3,
  "error": { "code": "provider_auth_failed", "message": "…" },
  "document_id": "…", "document_version_id": "…",
  "queued_at": "…", "started_at": "…", "finished_at": "…"
}
```

`kind`: `ingest` \| `reindex`; `error` хранится уже redacted (стабильный код и короткое сообщение без содержимого
документа и секретов).

### 2.16 `audit` (§8 п.9)

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/audit/events` | Журнал (`action`, `actor_user_id`, `target_type`, `target_id`, `outcome`, `from`, `to`) | `audit.read` | auth | — | **[Proposed]** `audit.viewed` | general |
| GET | `/api/v1/audit/events/{id}` | Событие | `audit.read` | auth | — | — | — |

Аудит доступен только на чтение; endpoints изменения/удаления событий отсутствуют.

### 2.17 `system` ([ADR-006](adr/0006-initial-setup-and-system-configuration.md), [ADR-007](adr/0007-network-access-and-local-llm.md))

| Метод | Путь | Назначение | Permission | Scope | Idem | Audit | RL |
|---|---|---|---|---|---|---|---|
| GET | `/api/v1/system/settings` | Instance Settings только на чтение (без секретов), включая network allowlist | `system.settings.read` | auth | — | — | general |

Endpoints для **изменения** Instance Settings и allowlist **не существуют**: они меняются конфигурацией сервера и
перезапуском. Секретные значения представлены как `{ "configured": true }`.

**Реализовано сейчас** — только существующие настройки (остальные разделы появятся вместе с функциями):

```json
{
  "app_env": "production",
  "public_url": "https://kb.example.test",
  "setup_mode": "web",
  "session": { "idle_ttl_seconds": 86400, "absolute_ttl_seconds": 604800, "cookie_secure": true },
  "api_docs_enabled": false,
  "secrets": { "configured": true, "primary_key_id": "2026-11", "keys": 2 },
  "network": {
    "allowlist": [
      { "name": "local-ollama", "url": "http://ollama:11434", "purposes": ["llm", "embedding"],
        "provider_types": ["ollama"], "allowed_cidrs": ["172.30.10.0/24"] }
    ]
  }
}
```

`secrets` — состояние key ring ([ADR-016](adr/0016-secret-encryption.md)) без значений ключей; `network.allowlist` —
записи `[[network.allowlist]]` из `CONFIG_FILE` ([ADR-007](adr/0007-network-access-and-local-llm.md)), пустой
список по умолчанию.

Дополнительные разделы (**[Proposed]**, по мере появления лимитов, SMTP и storage):

```json
{
  "public_url": "https://kb.example.test",
  "setup_mode": "web",
  "limits": { "upload_max_bytes": 52428800, "max_concurrent_generations_per_user": 2 },
  "smtp": { "configured": false },
  "storage": { "backend": "local" }
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
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-store
X-Accel-Buffering: no
X-Request-ID: …
```

Формат кадра (без `id:` — `Last-Event-ID` не поддерживается, [ADR-019](adr/0019-answer-generation-in-api-process.md)
§5); `data` — одна строка JSON:

```text
event: token
data: {"delta":"Перенос возможен "}

```

### 3.3 События (§5)

| Событие | Когда | `data` |
|---|---|---|
| `message_start` | Первым, после сохранения сообщения пользователя и пустого ответа со статусом `streaming` | `{ "conversation_id", "user_message_id", "assistant_message_id", "generation_run_id", "created_at" }` |
| `token` | Фрагмент ответа | `{ "delta": "…" }` |
| `citation` | Когда в ответе впервые встречается **валидный** citation ID | см. ниже |
| `message_complete` | Последним при успехе или остановке | `{ "assistant_message_id", "finish_reason": "stop" \| "length" \| "user_cancelled", "citation_ids": ["c1","c3"], "usage": { "prompt_tokens", "completion_tokens" } }` |
| `error` | Ошибка после начала потока; поток закрывается | `{ "code", "message", "retryable", "correlation_id" }` |
| `heartbeat` | Каждые 15 с при отсутствии других событий | `{ "ts": "…" }` |

Коды `error`: `provider_timeout` (жёсткий timeout ответа `CHAT_GENERATION_TIMEOUT_SECONDS` или timeout провайдера,
`retryable: true`), `provider_rate_limited`, `provider_unavailable` (`retryable: true`), `provider_auth_failed`,
`provider_bad_request`, `provider_model_not_found`, `provider_content_filtered`, `provider_invalid_response`,
`egress_denied`, `provider_error`, `embedding_unavailable` (ошибка поиска по KB), `internal_error`. `message` —
фиксированный текст без ответа провайдера. Частичный ответ сохраняется со статусом `failed` и `error_code` = коду
события. `usage` в `message_complete` — `null`, если провайдер не сообщил токены.

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
2. Модель получает инструкцию ссылаться только на эти ID в формате `[c1]` (несколько — `[c1][c3]`)
   **[Proposed]**. Правила — в system-части запроса, chunks — только в экранированном блоке `<documents>`.
3. Backend буферизует потенциальные маркеры в потоке: валидный ID пропускается в `token` и порождает `citation`
   (один раз на ID); **невалидный ID удаляется** из текста и учитывается в
   `generation_runs.rejected_citation_count`. Групповой маркер `[c1, c3]` разбирается по одному ID и
   переписывается как `[c1][c3]`; незавершённый маркер в конце потока остаётся обычным текстом. Реализация —
   `application/retrieval/context.py` (`CitationRegistry`, `CitationFilter`).
4. В `message_citations` сохраняются только валидированные цитаты.
5. Если подходящих chunks не найдено, модель получает инструкцию корректно отказать; ответ без цитат допустим только
   как отказ/уточнение (§6).

### 3.5 Остановка генерации (§5)

`POST /api/v1/chat/runs/{generation_run_id}/stop` → `202`. Поток завершается событием `message_complete` с
`finish_reason = "user_cancelled"` (не позднее ~0,5 с: флаг в Redis проверяется между фрагментами и пока поток
ждёт провайдера); частичный ответ и уже выданные цитаты сохраняются, сообщение — `stopped`. Повторный вызов для уже
завершённой генерации → `202` без эффекта (идемпотентно).

Разрыв соединения клиентом отменяет генерацию; частичный ответ сохраняется как `stopped`. `Last-Event-ID` не
поддерживается: после обрыва клиент перечитывает сообщения диалога ([ADR-019](adr/0019-answer-generation-in-api-process.md)).

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
  "retrieval": { "top_k": 8, "vector_top_k": 40, "keyword_top_k": 40, "rrf_k": 60,
                 "vector_weight": 1.0, "keyword_weight": 0.1 },
  "limits": { "max_context_tokens": 8000, "max_history_messages": 10, "max_output_tokens": 1024 },
  "status": "active",
  "availability": { "available": true, "reason": null },   // ADR-005
  "created_at": "…", "updated_at": "…"
}

// Job
{
  "id": "…", "type": "ingestion", "kind": "ingest", "status": "failed", "stage": "parse",
  "attempt": 3, "max_attempts": 3,
  "error": { "code": "parse_timeout", "message": "Parsing exceeded time limit." },
  "queued_at": "…", "started_at": "…", "finished_at": "…"
}
```

## 5. Rate limits (§5, §8 п.5)

`login`, `setup`, `activation`, `provider_test`, `search` и `chat` реализованы ([ADR-013](adr/0013-auth-session-strategy.md),
[ADR-015](adr/0015-user-activation-links.md)); остальные значения — **[Proposed]** (по умолчанию, настраиваются).
Хранилище счётчиков — Redis; при его недоступности эти endpoints отвечают `503`.

| Профиль | Лимит | Ключ |
|---|---|---|
| `login` | 5/мин и 20/час на IP + email; 30/мин на IP; 10 неудачных попыток за 15 мин на email | IP, SHA-256(email) |
| `setup` | 5/мин | IP |
| `activation` | 10/мин | IP |
| `upload` | 30/мин, суммарный объём 500 МБ/час | пользователь |
| `chat` | 20/мин, не более 2 одновременных генераций (утверждён в [ADR-019](adr/0019-answer-generation-in-api-process.md)) | пользователь |
| `provider_test` | 10/мин | инсталляция |
| `sync` | 6/час | источник |
| `expensive` (reindex, retry) | 10/час | инсталляция |
| `search` | 30/мин (утверждён 2026-11-25) | пользователь |
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
| System Settings | `settings` (Application Settings), `system` (Instance Settings, только чтение) |

## 7. Открытые вопросы

1. Хранилище idempotency keys (PostgreSQL / Redis).
2. Пользовательские роли в MVP.
3. Нужен ли endpoint скачивания оригинала документа (и с какими заголовками безопасности).

Решено: формат ошибок ([ADR-010](adr/0010-api-error-format.md)), пагинация ([ADR-014](adr/0014-list-pagination.md)),
создание пользователей ссылкой активации ([ADR-015](adr/0015-user-activation-links.md)), setup и системные настройки ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)), доступ к
ассистентам ([ADR-005](adr/0005-assistant-access-model.md)), генерация и SSE-разрывы
([ADR-019](adr/0019-answer-generation-in-api-process.md)), сетевые ошибки провайдеров
([ADR-007](adr/0007-network-access-and-local-llm.md)), upload как Document Source
([ADR-008](adr/0008-unified-document-ingestion.md)).
