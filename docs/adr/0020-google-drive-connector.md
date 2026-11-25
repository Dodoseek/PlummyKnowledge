# ADR-020: Google Drive Connector

- **Status:** Accepted
- **Date:** 2026-11-26 (утверждено владельцем проекта вместе с планом Фазы 9)

## Context

Фаза 9 ([DEVELOPMENT_ROADMAP.md](../DEVELOPMENT_ROADMAP.md)) подключает первый внешний источник — Google Drive.
Уже решено:

- источник реализует `DataSourceConnector` и отдаёт файлы в общий ingestion pipeline со стадии 2
  ([ADR-008](0008-unified-document-ingestion.md)); код Google — только в `infrastructure/integrations/sources/`;
- OAuth client ID/secret — Instance Settings, не API ([ADR-006](0006-initial-setup-and-system-configuration.md));
- все исходящие соединения — через `SafeHttpClient`, redirects коннекторов проверяются на каждом переходе, ≤ 3, по
  списку хостов коннектора ([ADR-007](0007-network-access-and-local-llm.md) §4 п.7);
- refresh token шифруется key ring вне БД ([ADR-016](0016-secret-encryption.md));
- OAuth `state` одноразовый, связан с сессией и пользователем, redirect URI точный
  ([SECURITY_MODEL.md §15](../SECURITY_MODEL.md)).

Открытыми оставались: PKCE для Google, scope, SDK или HTTP-клиент, связь источника с KB, модель синхронизации и
заданий, правила области (scope) папки, типизированные ошибки коннектора и способ тестирования без живого Google.

## Decision

### 1. OAuth: Authorization Code + PKCE

- `POST /data-sources/{id}/oauth/start` создаёт `state` (32 байта CSPRNG) и `code_verifier` (43–128 символов,
  метод **S256**) и сохраняет в Redis `oauth:state:{sha256(state)}` → `{data_source_id, user_id, session_id,
  code_verifier}` с TTL **10 минут**. Ответ — `authorization_url` с `access_type=offline`, `prompt=consent`,
  `include_granted_scopes=false`, `code_challenge`, `state`.
- `GET /data-sources/oauth/callback` атомарно **забирает** запись (`GETDEL`: повторное использование невозможно),
  затем проверяет, что текущая cookie-сессия жива и совпадает с `session_id` и `user_id` записи, у пользователя
  по-прежнему есть `data_sources.manage` и доступ `manage` к KB источника. Любое расхождение — отказ без обмена
  кода, audit `data_source.oauth.failed`.
- Обмен кода — `POST` на token endpoint с `code_verifier` через `SafeHttpClient`. Нет `refresh_token` в ответе —
  отказ (`oauth_no_refresh_token`).
- Callback всегда отвечает `303` на **фиксированный** путь UI, построенный сервером из записи state
  (`/knowledge-bases/{kb_id}?source={id}&oauth=connected|failed[&reason=код]`); адрес возврата от клиента не
  принимается (нет open redirect). Ошибка провайдера (`error=access_denied`) отображается кодом, без текста
  провайдера.
- Redirect URI — `PUBLIC_URL` + `/api/v1/data-sources/oauth/callback`, точное совпадение с регистрацией в Google.
  Без `PUBLIC_URL` коннектор считается не настроенным. В `production` `PUBLIC_URL` и так обязан быть `https`.
- Cookie сессии `SameSite=Lax` приходит в callback при top-level переходе с Google; CSRF-защита callback — сам
  одноразовый `state`, связанный с сессией.

### 2. Конфигурация (Instance Settings)

- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` (или `GOOGLE_OAUTH_CLIENT_SECRET_FILE`). Без обоих
  `POST /data-sources` с `type = google_drive` → `409 connector_not_configured`; `GET /system/settings` сообщает
  `connectors.google_drive.configured` и `redirect_uri` (без значений).
- Адреса Google фиксированы в коде. Секция `[connectors.google_drive]` в `CONFIG_FILE` может переопределить
  `auth_url`, `token_url`, `revoke_url`, `api_url`, `download_hosts` **только при `APP_ENV != production`** — для
  E2E с фейковым сервером. В `production` такая секция — ошибка конфигурации при старте. Переопределённый адрес
  проходит обычную сетевую политику (внутренний адрес — только через запись allowlist с `purposes =
  ["connector"]`).

### 3. Scope: `drive.readonly`

- Запрашивается ровно `https://www.googleapis.com/auth/drive.readonly`. Имя учётной записи для UI берётся из
  `GET /drive/v3/about?fields=user(displayName,emailAddress)` — scope `openid`/`email` не нужны.
- `drive.file` отклонён: он даёт доступ только к файлам, открытым через Picker или созданным приложением, и не
  позволяет рекурсивно синхронизировать существующую папку и видеть изменения в ней.
- Фактически выданные scopes (поле `scope` ответа token endpoint) сохраняются в `granted_scopes`. Если среди них нет
  `drive.readonly` — отказ `oauth_scope_missing`. Если выдано больше запрошенного — сохраняется как есть и
  показывается в UI.
- UI показывает `granted_scopes`, учётную запись и предупреждение: **выбор папки не ограничивает токен** — токен
  читает весь Drive этой учётной записи. Рекомендация в DEPLOYMENT: подключать отдельную служебную учётную запись,
  которой расшарены только нужные папки.
- `drive.readonly` — restricted scope: для OAuth-клиента типа *External* Google требует верификацию (CASA); для
  *Internal* (Google Workspace) — нет. Это организационное требование установщика, описанное в DEPLOYMENT.

### 4. HTTP без Google SDK

Адаптер `GoogleDriveConnector` использует `SafeHttpClient` (httpx) напрямую, как адаптеры AI-провайдеров: без
`google-api-python-client` и `google-auth`. Новых зависимостей нет; весь трафик гарантированно идёт через сетевую
политику; поверхность — несколько REST-методов Drive v3 (`files.list`, `files.get`, `files.get?alt=media`,
`files.export`, `changes.getStartPageToken`, `changes.list`, `about.get`) и OAuth (`token`, `revoke`).

### 5. Сеть

- Новое назначение `EgressPurpose.CONNECTOR`.
- `SafeHttpClient` получает режим следования redirect (только для коннекторов): каждый переход — заново шаги 1–6
  ADR-007 §4, не более **3** переходов, только `https`, хост назначения обязан входить в список хостов коннектора
  (`www.googleapis.com`, `oauth2.googleapis.com`, `*.googleusercontent.com`). Заголовок `Authorization` на хост,
  отличный от исходного, не передаётся. Нарушение — `egress_denied` и audit `network.egress.denied`.

### 6. Источник принадлежит одной KB

- `POST /data-sources` принимает `{type: "google_drive", name, knowledge_base_id}`; требуется
  `data_sources.manage` **и** доступ `manage` к этой KB. Создаётся источник со статусом `paused` и связь в
  `knowledge_base_sources`; подключить его к другой KB нельзя (`UNIQUE (data_source_id)` в связях для внешних
  источников проверяет use case). Документы источника принадлежат этой KB, доступ к ним — обычный доступ KB
  ([ADR-012](0012-single-organization-deployment.md)); внешние ACL Google не наследуются (вне MVP).
- Видимость: `data_sources.read` + доступ `read` к KB источника (иначе `404`); `PATCH`, `oauth`, `validate`,
  `browse`, `disconnect`, `DELETE` — `data_sources.manage` + `manage`; `sync` — `data_sources.sync` + `manage`.
- `config`: `{folder_id, folder_name, drive_id?}` — выбирается через `browse` и проверяется сервером (объект
  существует, это папка, не в корзине). Смена папки после первой синхронизации запускает reconciliation.
- Источник активируется (`active`), когда есть валидные credentials и выбрана папка; тогда ставится начальная
  синхронизация.

### 7. Credentials

- Refresh token — в vault (ADR-016, `SecretKind.OAUTH_REFRESH_TOKEN`), ссылка — `data_source_credentials`
  (`account_label`, `granted_scopes`, `status`). Access token — только в памяти процесса на время задания, не
  пишется ни в БД, ни в Redis, ни в логи.
- Ответ `invalid_grant` при обновлении → credentials `revoked`, источник `error` с кодом `connector_auth_revoked`,
  синхронизация останавливается до повторного подключения.
- `disconnect`: `POST revoke` у Google (неуспех отзыва фиксируется в audit, но не мешает локальному удалению),
  удаление секрета и credentials, статус `disconnected`, отмена активных заданий. Документы остаются в KB до
  удаления источника. `DELETE` источника = disconnect + удаление документов с причиной `source_deleted`.

### 8. Синхронизация

- **Задания** — таблица `sync_jobs` (DATABASE_SCHEMA §7.2), одно активное задание на источник (partial unique
  index); вторая попытка запуска — `409 sync_in_progress`. Выполняет Celery-задача в `worker` (сеть и секреты),
  очередь `connectors`. Скачанный файл кладётся в quarantine и проходит стадии 2–12 как upload: парсинг —
  `worker-ingestion` без сети и секретов.
- **Начальная:** сначала `changes.getStartPageToken`, затем рекурсивный обход папки (`files.list`,
  `q = '<id>' in parents and trashed = false`, `supportsAllDrives`, `includeItemsFromAllDrives`), страницы по
  1000. По окончании курсор сохраняется в `data_sources.sync_cursor`. Изменения во время обхода догоняются
  следующей инкрементальной синхронизацией.
- **Инкрементальная:** `changes.list` от сохранённого курсора; курсор сохраняется после каждой полностью
  обработанной страницы (checkpoint), поэтому повтор задания не теряет и не дублирует изменения.
- **Область папки:** файл в области, если цепочка `parents` доходит до выбранной папки (поиск вверх через
  `files.get` с кэшем на задание, глубина ≤ 64). Изменение **папки** (переименование, перенос, удаление) запускает
  reconciliation, поскольку меняет область для потомков. Ярлыки (shortcuts) не разыменовываются — они могли бы
  вывести за пределы папки.
- **Правила:** новый файл → документ и версия; изменился `md5Checksum` (обычный файл) или `version` (Google-формат)
  → новая версия; изменились только имя или путь → обновление метаданных без переиндексации; файл удалён или в
  корзине → удаление документа с причиной `remote_deleted`; вышел из области → `out_of_scope`.
- **Reconciliation:** полный обход и сверка множеств `external_id`; запускается вручную, раз в сутки по расписанию,
  при изменении папок и при `CursorInvalid`.
- **Форматы:** Google Docs → DOCX, Sheets → XLSX, Slides → PDF (`files.export`, лимит экспорта Google 10 МБ);
  остальные — как есть. Неподдерживаемые типы (Forms, Sites, Maps, папки, ярлыки) пропускаются с учётом в
  `stats.skipped`. Размер файла — тот же предел `UPLOAD_MAX_BYTES`, что и у загрузки; превышение — пропуск.
- **Расписание:** Celery beat раз в минуту ставит инкрементальную синхронизацию источникам, у которых наступил
  `next_sync_at` (интервал `sync_interval_minutes`: по умолчанию 15, допустимо 5–1440, `null` — только вручную).
- **Ограничения скорости:** `429` и `403` с причиной `rateLimitExceeded`/`userRateLimitExceeded` → повтор с
  экспоненциальной задержкой и jitter, с учётом `Retry-After`, до 5 попыток на запрос; дальше — повтор задания
  позже. `403 dailyLimitExceeded`/`storageQuotaExceeded` → `QuotaExceeded`, задание `failed`, источник не
  переводится в `error`.

### 9. Ошибки коннектора

Утверждается набор ARCHITECTURE §8.6: `ConnectorAuthRevoked`, `RateLimited(retry_after)`, `QuotaExceeded`,
`NotFound`, `PermissionDenied`, `Transient`, `CursorInvalid`. Ошибки содержат только код и безопасное сообщение;
тела ответов Google не логируются и не сохраняются (там могут быть имена файлов и адреса).

### 10. Тестирование без живого Google

- Unit и integration: фейковый Drive/OAuth на уровне httpx transport внутри `SafeHttpClient` (сетевая политика
  выполняется как обычно), настоящие PostgreSQL и Redis.
- E2E: stdlib-сервер `deploy/e2e/fake_google.py` с переопределёнными адресами (§2) и записью allowlist `connector`.
- Опциональный ручной live-тест (`-m live`) с настоящей учётной записью; в CI не запускается.

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| `drive.file` + Google Picker | Нет рекурсивной синхронизации папки и изменений в ней; Picker требует API key и стороннего JS во frontend |
| Service account + domain-wide delegation | Только Google Workspace, даёт доступ ко всем пользователям домена; вне MVP |
| Google SDK (`google-api-python-client`) | Свой HTTP-транспорт в обход `SafeHttpClient`, тяжёлые зависимости ради 9 методов |
| Без PKCE (только `state`) | PKCE дешёв и защищает от перехвата кода; Google его поддерживает для web-клиентов |
| Хранить access token | Не нужен между заданиями; лишний секрет в хранилище |
| Источник на несколько KB | Дублирование документов и неоднозначный доступ; можно добавить позже без смены схемы |
| Синхронизация в API-процессе | Долгая сетевая операция — по §11 только в Celery |

## Consequences

- Миграция `0015`: `data_source_credentials`, `sync_jobs`; новый вид секрета; новое назначение сети; очередь
  `connectors`.
- Публичный API `data-sources` реализуется по API_CONTRACTS §2.9 с уточнениями этого ADR.
- Коннектор Microsoft 365 (Фаза 10) переиспользует OAuth-поток, задания, правила области и ошибки.

## Security considerations

- `state` одноразовый, хешируется в ключе Redis, связан с сессией и пользователем; PKCE S256; точный redirect URI;
  нет open redirect.
- Refresh token зашифрован, write-only; access token не хранится; `disconnect` отзывает токен у Google.
- Честный показ scopes и предупреждение о том, что папка не ограничивает токен.
- Скачивание: redirects проверяются на каждом переходе по хостам коннектора; `Authorization` не уходит на чужой хост.
- Внешние документы — недоверенный ввод (SC-08): они проходят тот же pipeline и prompt-изоляцию, что и загрузки.
- Удаление в источнике отражается в индексе; удалённые документы не находятся поиском.
- В логах и audit нет токенов, кодов авторизации, `code_verifier`, содержимого файлов и тел ответов Google.

## Implementation notes

Шаги 9.0–9.5 roadmap: ADR и спецификации; схема, домен и redirects; OAuth и API; адаптер; синхронизация;
UI, E2E и security gate.

- 9.3: верхний уровень picker'а добавляет `drives.list` (первые 100 shared drives) к перечисленным в §4 методам;
  корень shared drive можно выбрать папкой (`config.drive_id`). В процессе API запрос к Drive повторяется не более
  2 раз (ответ пользователю не ждёт минутами), в worker — до 5. `401` на только что полученный токен —
  `ConnectorAccessTokenRejected` (не отзыв grant): отзыв фиксируется только по `invalid_grant` token endpoint.
  Если Google выдаёт при refresh новый refresh token, он заменяет сохранённый в vault.
- 9.4: миграция `0016` — `external_id` уникален только среди живых документов (восстановленный файл получает новый
  документ, пока удалённый ждёт purge). Full listing (initial и reconciliation) удаляет непоказанные документы с
  причиной `out_of_scope`. Путь документа — цепочка ID папок в `source_metadata.ancestors`: изменение папки без
  проиндексированных документов под ней обходит только её поддерево (папка создана или перенесена внутрь), папка с
  документами, перенесённая или удалённая, запускает reconciliation в том же job, переименование ничего не требует.
  Новая revision с содержимым существующей версии не создаёт копию: текущая и последняя версии не трогаются, более
  старая (откат) переиндексируется. Новая версия отменяет незавершённые ingestion jobs документа
  (`superseded_by_sync`). Job переживает падение worker: scheduler возвращает в очередь `running` jobs без checkpoint
  2 часа и публикует `queued` jobs, не дошедшие до брокера. Лимит задачи — 100 минут; Celery повторяет job с backoff
  30 с – 1 ч или с `Retry-After`, попытки считает `sync_jobs.attempt` (до 5).

## Related documents

[ADR-006](0006-initial-setup-and-system-configuration.md) · [ADR-007](0007-network-access-and-local-llm.md) ·
[ADR-008](0008-unified-document-ingestion.md) · [ADR-016](0016-secret-encryption.md) ·
[API_CONTRACTS.md §2.9](../API_CONTRACTS.md) · [DATABASE_SCHEMA.md §5.4, §5.5, §7.2](../DATABASE_SCHEMA.md) ·
[ARCHITECTURE.md §8.6, §9.5](../ARCHITECTURE.md) · [SECURITY_MODEL.md §15](../SECURITY_MODEL.md)
