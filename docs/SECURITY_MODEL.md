# PlummyKnowledge — Security Model

| | |
|---|---|
| Статус | v0.3 — аутентификация, сессии и bootstrap (Фазы 2–3), шифрование секретов и сетевая политика (Фаза 4) реализованы |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §1, §2, §5, §7, §8, §11, §12, §13 (Фазы 3, 9, 10, 12), §18, §20 |
| Решения | [ADR-005](adr/0005-assistant-access-model.md) … [ADR-008](adr/0008-unified-document-ingestion.md), [ADR-012](adr/0012-single-organization-deployment.md), [ADR-013](adr/0013-auth-session-strategy.md) (Accepted) |
| Связанные документы | [ARCHITECTURE.md](ARCHITECTURE.md), [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md), [API_CONTRACTS.md](API_CONTRACTS.md), [DEPLOYMENT.md](DEPLOYMENT.md) |

**Как читать.** Threat model, обязательные меры, правила prompt injection, Document ACL и Retention (§8) —
нормативны. Конкретные параметры (TTL, лимиты, алгоритмы) — **[Proposed]**; изменение auth/session strategy или
модели развёртывания (ADR-012) требует ADR (§15).

---

## 1. Принципы

1. **Сначала безопасность и границы доступа, затем удобства** (§22 п.2).
2. **Least privilege** — пользователь получает доступ только к разрешённым KB и документам (§1).
3. **Deny by default** — отсутствие явного разрешения = запрет.
4. **Backend — единственная security boundary**; frontend ею не является (§5).
5. **Права проверяются до retrieval и до передачи контекста LLM** (§8 п.2, §22 п.8).
6. **Документы — недоверенный ввод** (§8).
7. **Цитаты проверяются сервером** (§22 п.7).
8. **Секреты никогда не покидают backend в открытом виде**: не возвращаются API, не пишутся в логи, не попадают в
   prompt (§8, §12).

## 2. Активы, акторы и границы доверия

### 2.1 Активы

| Актив | Где | Угроза |
|---|---|---|
| Документы, extracted text, chunks, embeddings | PostgreSQL, object storage | Утечка между KB и пользователями, retrieval удалённых данных |
| Диалоги и ответы | PostgreSQL | Доступ чужих пользователей |
| API keys AI providers, OAuth refresh tokens | `secret_references` (зашифровано) | Утечка через API/логи/backup/prompt |
| Ключ шифрования секретов | Вне БД (env/файл/secret mount) | Компрометация всех секретов |
| Пароли (хэши), сессии (хэши токенов) | PostgreSQL | Захват аккаунта |
| Audit log | PostgreSQL | Подмена/удаление следов |
| Инфраструктура (сеть, metadata endpoints) | Хост/облако | SSRF |

### 2.2 Акторы

Анонимный пользователь интернета; аутентифицированный пользователь любой из ролей (§2) — в том числе злонамеренный
сотрудник, пытающийся получить данные KB без доступа или чужие диалоги; автор документа с prompt
injection; скомпрометированный внешний провайдер/источник; оператор инсталляции.

### 2.3 Границы доверия

```text
[Browser] ──TLS── [Nginx] ── [API] ──┬── [PostgreSQL/pgvector]
                                     ├── [Redis]
                                     ├── [Object storage]   ← quarantine (недоверенные файлы)
                                     └── [AI providers]     ← внешние/пользовательские endpoints (SSRF)
                    [Worker] ────────┴── [Google Drive / Microsoft Graph]
                       └── [Parser (Docling)] ← обрабатывает недоверенные файлы
```

Недоверенными считаются: любой ввод клиента, содержимое документов и их metadata (имена файлов, заголовки),
ответы AI providers и источников, ответ модели (до валидации цитат и при рендеринге).

## 3. Roles и permissions

Инсталляция принадлежит одной организации ([ADR-012](adr/0012-single-organization-deployment.md)): ролей уровня
«платформы» и «организации» нет. Каталог permissions и системные роли создаются миграцией `0003`; роли назначаются
пользователям и группам через API (`roles.assign`, API_CONTRACTS.md §2.4–2.5).

### 3.1 Роли (§2)

| Роль (`key`) | Назначение |
|---|---|
| Administrator (`admin`) | Пользователи, группы, роли, провайдеры, источники, KB, ассистенты, настройки приложения, аудит; просмотр Instance Settings |
| Knowledge Manager (`knowledge_manager`) | Документы, базы знаний и синхронизация |
| Member (`member`) | Работа с разрешёнными ассистентами |
| Auditor (`auditor`) | Доступ к журналу аудита |

Первый Administrator создаётся при первоначальной настройке (§4.5). Владелец инсталляции (человек с доступом к
серверу) — не роль приложения: Instance Settings меняются только на сервере (ADR-006).

Защита от потери управления (реализовано):

- В инсталляции всегда остаётся хотя бы один **активный** пользователь с прямой ролью `admin`: снятие роли,
  отключение или удаление, которые нарушили бы это, отклоняются (`409 last_administrator`). Все такие изменения
  берут блокировку строки роли `admin` (`SELECT … FOR UPDATE`), поэтому два администратора не могут одновременно
  снять роль друг с друга.
- Роль `admin` назначается **только напрямую**, не через группы: каждое назначение явно, а инвариант не зависит от
  состава групп.
- Администратор не может отключить или удалить себя (`409 cannot_modify_self`).
- Создание пользователя с ролями требует `roles.assign` в дополнение к `users.manage` — `users.manage` не даёт
  выдать себе или другим больше прав.

### 3.2 Каталог permissions

| Permission | Описание |
|---|---|
| `system.settings.read` | Просмотр Instance Settings без секретов. Изменение Instance Settings через API не предусмотрено ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| `settings.manage` | Application Settings (в пределах instance policy) |
| `users.read` / `users.manage` | Просмотр / управление пользователями |
| `groups.read` / `groups.manage` | Просмотр / управление группами |
| `roles.read` / `roles.assign` | Просмотр ролей / назначение ролей (пользовательские роли — не в 0.1.0) |
| `providers.read` / `providers.manage` | AI providers, models, embedding profiles |
| `data_sources.read` / `data_sources.manage` | Источники: просмотр / подключение, OAuth, disconnect |
| `data_sources.sync` | Запуск синхронизации |
| `knowledge_bases.manage` | Создание KB и управление **всеми** KB |
| `assistants.manage` | Создание и настройка ассистентов |
| `jobs.read` / `jobs.manage` | Просмотр / retry, cancel jobs |
| `audit.read` | Журнал аудита |
| `chat.use` | Работа с доступными ассистентами |

### 3.3 Матрица ролей

| Permission | Administrator | Knowledge Manager | Member | Auditor |
|---|:-:|:-:|:-:|:-:|
| `system.settings.read`, `settings.manage` | ✓ | | | |
| `users.*`, `groups.*` | ✓ | `read` | | |
| `roles.read`, `roles.assign` | ✓ | | | |
| `providers.read` | ✓ | ✓ | | |
| `providers.manage` | ✓ | | | |
| `data_sources.read` | ✓ | ✓ | | |
| `data_sources.manage` | ✓ | | | |
| `data_sources.sync` | ✓ | ✓ | | |
| `knowledge_bases.manage` | ✓ | ✓ | | |
| `assistants.manage` | ✓ | | | |
| `jobs.read`, `jobs.manage` | ✓ | ✓ | | |
| `audit.read` | ✓ | | | ✓ |
| `chat.use` | ✓ | ✓ | ✓ | |

Обоснование: Administrator создаёт KB и Assistant и видит аудит (§2, сценарии 5 и 8); Knowledge Manager —
документы, KB и синхронизация (§2); Auditor видит только журнал аудита.

### 3.4 Уровни доступа к KB (§5, §8)

| Уровень | Даёт |
|---|---|
| `read` | Поиск, чат через ассистентов, просмотр документов и source viewer |
| `manage` | `read` + загрузка/удаление документов, привязка источников, reindex, управление доступом к этой KB |

Выдаётся пользователю или группе (`knowledge_base_access`). Пользователи с `knowledge_bases.manage` имеют `manage`
ко всем KB.

### 3.5 Вычисление прав

- Эффективные permissions = объединение permissions прямых ролей пользователя и ролей его неудалённых групп
  (реализовано).
- Эффективный доступ к KB = максимум из прямого доступа, доступа через группы и `knowledge_bases.manage`.
- Права загружаются **на каждый запрос** вместе с проверкой сессии (реализовано: один запрос к `user_roles`,
  `group_roles`, `user_groups` и `role_permissions`); отзыв роли, исключение из группы или удаление группы вступают
  в силу со следующего запроса.

## 4. Authentication и сессии ([ADR-013](adr/0013-auth-session-strategy.md))

Требования (§5, §13 Фаза 3): локальная аутентификация, Argon2id, cookies HttpOnly/Secure/SameSite, CSRF strategy,
отзыв сессий, rate limiting login, password reset только при наличии email infrastructure; OIDC — следующая фаза,
SAML/SCIM — позднее. Автоматической регистрации нет: пользователей создаёт Administrator (§4.6) или
первоначальная настройка.

### 4.1 Пароли

- Argon2id (`argon2-cffi`), профиль RFC 9106 low-memory: t = 3, m = 64 MiB, p = 4 (выше минимума OWASP m = 19 MiB,
  t = 2, p = 1). Параметры хранятся в строке хэша; при входе с устаревшими параметрами хэш пересчитывается.
  Хэширование — вне event loop.
- Длина 12–128 символов, без требований к составу; пароль не совпадает с email и не состоит из одного
  повторяющегося символа. Проверка по списку распространённых паролей — **[Proposed]**, отдельная задача.
- Ошибка входа не различает «нет пользователя» / «неверный пароль» / «отключён» / «удалён» (`invalid_credentials`).
- Время ответа на неудачный логин не зависит от существования пользователя (проверка фиктивного хэша).

### 4.2 Сессии

| Объект | Срок | Хранение |
|---|---|---|
| Сессия: простой | `SESSION_IDLE_TTL`, по умолчанию 24 ч | `sessions.last_seen_at` (обновляется не чаще раза в 60 с) |
| Сессия: абсолютный срок | `SESSION_ABSOLUTE_TTL`, по умолчанию 7 дней | `sessions.expires_at` |
| Токен сессии | Как сессия | Cookie; в БД только SHA-256 (`sessions.token_hash`) |

- Непрозрачный токен 256 бит; проверка на **каждом** запросе в PostgreSQL: не отозвана, не истекла, пользователь
  `active` и не удалён, сессия не старше `password_changed_at`. Отозванная сессия отклоняется немедленно (§12).
- Login и setup всегда создают **новую** сессию; сессия из прежней cookie отзывается (`superseded`) — защита от
  session fixation.
- Logout отзывает текущую сессию (`logout`); без сессии — только удаляет cookie (идемпотентно).
- Отзыв всех сессий пользователя: смена пароля самим пользователем (`password_change`; этому браузеру сразу
  выдаётся новая сессия), сброс через CLI, отключение (`user_disabled`) и удаление пользователя, действие
  администратора (`POST /users/{id}/sessions/revoke`, `admin`).
- Пользователь видит и отзывает **только свои** сессии (`GET/DELETE /auth/sessions`); чужая сессия неотличима от
  несуществующей (`404`).
- Смена пароля требует текущий пароль; проверка идёт через лимиты `login`, неудачи считаются неудачными входами.
- Refresh tokens не используются (ADR-013).

### 4.3 Cookies (§5)

| Cookie | Атрибуты |
|---|---|
| `__Host-pk_session` | `HttpOnly; Secure; SameSite=Lax; Path=/`; `Max-Age` до абсолютного истечения |
| `pk_session` | Только при `SESSION_COOKIE_SECURE=false` (разработка без TLS; запрещено в `production`): `HttpOnly; SameSite=Lax; Path=/` |

### 4.4 CSRF (§5)

- Токен, привязанный к сессии: `HMAC-SHA256(key = токен сессии, "plummy-csrf-v1")`, base64url. Приходит в теле ответов
  `POST /setup/initialize`, `POST /auth/login`, `GET /auth/me`; frontend хранит его только в памяти и передаёт в
  `X-CSRF-Token` для `POST/PUT/PATCH/DELETE`. Неверный или отсутствующий токен при действующей сессии →
  `403 csrf_failed`.
- Для всех небезопасных методов проверяется `Origin` (без него — `Referer`): должен совпадать с origin `PUBLIC_URL`
  или входить в `CORS_ORIGINS`; иначе (в том числе без обоих заголовков) → `403 csrf_failed`. Для login и setup это
  основная защита (сессии ещё нет).
- `GET`-запросы не имеют побочных эффектов (кроме OAuth callback, защищённого `state`).

### 4.5 Первоначальная настройка и восстановление администратора ([ADR-006](adr/0006-initial-setup-and-system-configuration.md))

- `POST /api/v1/setup/initialize` требует **bootstrap token**: 256 бит, генерируется командой
  `python -m plummy.cli setup init-token` в шаге `migrate`; в БД хранится только SHA-256, открытое значение — в файле
  `BOOTSTRAP_DIR/setup-token` (`0600`, каталог `0700`) на сервере; в логи попадает только путь. Срок действия —
  `BOOTSTRAP_TOKEN_TTL` (по умолчанию 24 ч), токен одноразовый.
- Инициализация атомарна: хэш пароля вычисляется до транзакции; затем блокировка строки `instance_state`
  (`SELECT … FOR UPDATE`), проверка флага и токена (сравнение constant-time), создание первого пользователя и
  назначение роли `admin`, установка флага, погашение токена (хэш обнуляется), audit `setup.completed`, создание
  сессии — одна транзакция. Второй параллельный запрос ждёт блокировку и получает `409 setup_already_completed`.
  Отклонённые попытки пишут `setup.initialize.rejected` отдельной транзакцией.
- Состояние «настроено» хранится явным флагом и через API не сбрасывается; удаление всех пользователей не открывает
  setup повторно.
- Сессии ещё нет → обязательна проверка `Origin`; rate limit `setup` (5/мин на IP); тела запросов setup не
  логируются; токен и пароль — `SecretStr`.
- Режим `SETUP_MODE=cli_only` отключает web-initialize (`404`); первый администратор создаётся
  `python -m plummy.cli admin create`.
- **Токен потерян или истёк** до завершения настройки — только с доступом к серверу:
  `python -m plummy.cli setup init-token --rotate` выпускает новый токен, старый перестаёт действовать. Повторные
  запуски `migrate` действующий токен не меняют. После инициализации команда ничего не выпускает и удаляет
  оставшийся файл токена.
- Восстановление доступа администратора — только CLI на сервере (`python -m plummy.cli admin reset-password`;
  отзывает все сессии пользователя), с audit-событием. Публичного механизма повторного создания администратора нет.

### 4.6 Создание пользователей и активация ([ADR-015](adr/0015-user-activation-links.md))

- Administrator создаёт пользователя в статусе `invited` **без пароля** и получает одноразовую ссылку
  `{PUBLIC_URL}/activate#token=…`; пароль знает только пользователь.
- Токен — 256 бит, в БД только SHA-256 (`user_activation_tokens`), срок 72 ч, один действующий токен на
  пользователя; перевыпуск заменяет прежний. Токен находится во фрагменте URL: браузер не отправляет его на сервер
  при открытии страницы и в `Referer`; frontend сразу убирает его из адресной строки и не сохраняет.
- `POST /auth/activate` — публичный, с проверкой `Origin` и лимитом 10/мин на IP (Redis, fail closed). Пароль
  проверяется политикой §4.1 (нарушение не тратит ссылку). Установка пароля, статус `active`, удаление токена и
  создание сессии — одна транзакция с блокировкой строки пользователя; повторная или параллельная активация
  отклоняется.
- Любая причина отказа (неверный, истёкший, использованный токен, пользователь отключён, удалён или уже активен) →
  одинаковый `400 activation_token_invalid`; причина пишется только в audit (`user.activation.rejected`).
- Пользователь `invited` не может войти (`invalid_credentials`, как для неизвестного email).

## 5. Изоляция данных ([ADR-012](adr/0012-single-organization-deployment.md))

Инсталляция принадлежит одной организации; изоляция **разных** организаций обеспечивается отдельными инсталляциями
(свои БД, storage, ключи, сети), а не кодом. Внутри инсталляции действуют правила:

1. Контекст безопасности (`SecurityContext`: пользователь, сессия, роли, permissions) устанавливается сервером только
   из проверенной сессии (§5); идентификаторы пользователя, ролей и прав от клиента не принимаются (DTO с
   `extra="forbid"`).
2. Доступ к документам — на уровне Knowledge Base (§3.4, §6); фильтры доступа к KB, источникам, документам и версиям
   применяются в SQL **до** передачи контекста LLM.
3. Личные данные пользователя (диалоги, сессии) доступны только ему.
4. **Background jobs:** payload содержит идентификаторы ресурса и `correlation_id`; worker заново загружает ресурс и
   проверяет его состояние (не удалён, источник активен) (§8 п.1).
5. Ответы на запросы к ресурсам без доступа (чужой диалог, KB без доступа, документ такой KB) — `404`
   (не раскрывать существование).
6. **[Proposed — ADR «PostgreSQL RLS» (планируется)]** RLS по доступным KB как второй рубеж — решается в Фазе 12.
7. Тесты «чужой диалог / KB без доступа / документ без доступа недоступны» обязательны для каждого endpoint и job
   (§12).

## 6. Authorization и Document ACL

### 6.1 Где проверяются права (§5, §8)

- `PermissionPolicy` вызывается **в каждом use case** (§5). Router-зависимости допустимы только как ранний отказ,
  но не заменяют проверку в use case.
- Retrieval получает список разрешённых `data_source_id`, вычисленный policy service, и применяет его **в SQL**
  (не пост-фильтром) (§8 п.2).
- Перед передачей контекста LLM повторно проверяется, что каждый chunk принадлежит разрешённому источнику, текущей
  `ready`-версии и неудалённому документу (defense in depth) (§12: unauthorized chunks не попадают в LLM).
- Source viewer и открытие цитат из истории проверяют доступ заново при каждом запросе.

### 6.2 Document ACL (§8)

- MVP поддерживает доступ **только на уровне Knowledge Base**.
- Если в одной KB лежат документы с разными внешними ACL, их **нельзя** импортировать всем участникам KB.
- До реализации document-level ACL использовать только источники, где **все участники KB имеют право видеть все
  документы**. UI при привязке источника к KB показывает предупреждение, API требует явного подтверждения
  **[Proposed]**.
- Позже (§8, §14): ACL sync, identity mapping, inheritance, propagation of revocations.

### 6.3 Доступ к ассистентам ([ADR-005](adr/0005-assistant-access-model.md))

- Ассистент доступен, только если у пользователя есть доступ (`read`+) **ко всем** KB ассистента. Нет доступа хотя
  бы к одной → ассистент недоступен целиком.
- Ассистент без KB — только для `assistants.manage` (предпросмотр); `disabled` — никому.
- Проверка — в use case на **каждом** запросе (список, карточка, новый диалог, каждое сообщение, открытие цитаты).
- Идентификаторы KB/источников от клиента не используются; разрешённые источники вычисляет backend и применяет в SQL
  retrieval, дополнительно пересекая их с KB, доступными пользователю.
- Отзыв доступа к KB (прямой, через группу, удаление KB) сразу делает недоступными связанных ассистентов.
- Не раскрывать существование: недоступный ассистент → `404`; в собственном диалоге → `403 assistant_unavailable`.

**Реализовано в фазе 7:** политика — `application/assistants/access.py` (`availability_of`, `require_assistant`), список
для чата фильтруется тем же правилом в SQL (`SqlAssistantRepository.page`). `assistants.manage` видит все ассистенты с
`availability` и причиной (`disabled`, `missing_kb_access`); остальные — только доступные и только с `chat.use`.
Инструкции ассистента возвращаются только `assistants.manage`. Матрица и отзыв доступа —
`tests/integration/test_assistant_access.py`; `403 assistant_unavailable` и проверка в чате — Фаза 8.

### 6.4 Защита RAG (сводка)

1. Доступность ассистента (§6.3) → 2. разрешённые источники вычисляет backend → 3. фильтры источник / текущая
`ready`-версия / активный `chunk_set` / profile в SQL → 4. повторная проверка отобранных chunks перед сборкой
контекста → 5. инструкции отделены от недоверенного контекста (§10) → 6. валидация citation IDs → 7. source viewer
проверяет доступ заново.

## 7. Секреты и шифрование (§5, §8 п.3, [ADR-016](adr/0016-secret-encryption.md) — реализовано)

- Секреты (API keys провайдеров, позднее OAuth refresh tokens и client secrets) шифруются **до записи в БД** (§5)
  и хранятся только в `secret_references` ([DATABASE_SCHEMA.md §4.1](DATABASE_SCHEMA.md)).
- AES-256-GCM (`cryptography`), новый 96-битный nonce на каждое шифрование, associated data
  `plummy-secret-v1|<secret_id>|<kind>`: шифртекст нельзя перенести в другую запись или выдать за секрет другого вида.
- Ключи — в **key ring вне БД** (`SECRETS_KEYRING_FILE`, JSON `{primary, keys}`), каждый ключ с `key_id`. В
  `production` key ring обязателен; невалидный файл → отказ старта API, worker и CLI без вывода значений ключей. Без
  key ring операции, сохраняющие секрет, отвечают `503 secrets_not_configured`.
- Ротация без простоя: новый ключ становится `primary` → `python -m plummy.cli secrets rotate` перешифровывает записи
  на старых ключах (идемпотентно, audit `secrets.rotated`) → старый ключ удаляется. Readiness падает (`secrets`),
  если ключ удалили раньше перешифрования.
- Расшифровка — только в момент использования (connection test, вызов модели); значение живёт в `SecretValue`
  (`repr`/`str` не раскрывают его) и не кэшируется. Ошибка расшифровки → `503 secret_unavailable` без подробностей; в
  лог — только `secret_id`, `key_id` и тип ошибки.
- Секреты в API — **write-only**; в ответах только `has_secret` и `secret_hint` (последние 4 символа ключа длиной ≥ 16)
  (§12). Frontend держит введённый ключ только в состоянии формы: он не попадает в Redux store, `localStorage` и
  `sessionStorage`.
- Секреты не передаются в prompt (§8), не включаются в тексты ошибок, не пишутся в логи (secret redaction, §8 п.3):
  `SecretStr` в DTO/настройках, фильтр логирования, санитизация исключений SDK.
- Backup БД содержит только шифртексты; ключ хранится и резервируется **отдельно** от backup БД.

## 8. SSRF, сетевые разрешения и локальные LLM (§8 п.7, [ADR-007](adr/0007-network-access-and-local-llm.md))

Пользовательские URL появляются в конфигурации провайдеров (`openai_compatible`, `ollama`, кастомный `base_url`
OpenAI/Anthropic). Полный алгоритм — в ADR-007; здесь — обязательные свойства.

**Модель: Explicit Network Allowlist.**

- Все внутренние и опасные адреса запрещены по умолчанию; allowlist по умолчанию пуст.
- Разрешения задаёт только владелец инсталляции в Instance Settings (конфигурация сервера). Administrator приложения
  не может расширить allowlist: такого API нет, Application Settings не содержат сетевых параметров.
- Запись allowlist ограничена `scheme + host + port` (+ префикс пути), назначением (`llm`, `embedding`, …), типами
  провайдеров и **обязательными `allowed_cidrs`** для непубличных адресов. Разрешение hostname не
  открывает все адреса, в которые он может разрешиться.
- Link-local, cloud metadata, unspecified, multicast/broadcast и адреса собственных сервисов платформы (PostgreSQL,
  Redis, storage) не разрешаются никогда.

**Проверка каждого соединения (`SafeHttpClient`):**

| Угроза | Мера |
|---|---|
| SSRF | Проверяется **фактический IP** соединения, а не только URL |
| DNS rebinding / повторное разрешение | Одно разрешение → проверка всех адресов → подключение к проверенному IP (pinning), без повторного DNS |
| DNS spoofing | Для `https` — обязательная проверка сертификата; для внутренних адресов — только внутри `allowed_cidrs`; рекомендация: IP-литерал или изолированная Docker network |
| Redirects | Для AI-провайдеров не выполняются; для коннекторов — каждый переход проверяется заново (≤ 3, допустимые хосты коннектора) |
| IPv4/IPv6-обходы | IPv4-mapped/compatible, NAT64, 6to4, Teredo разворачиваются и проверяются как IPv4 |
| Альтернативная запись IP | Десятичные, восьмеричные, шестнадцатеричные, сокращённые формы и zone ID отклоняются |
| Loopback, private, link-local, metadata | Запрет по классам IANA special-purpose (ADR-007 §3) |
| Обход через прокси | `HTTP(S)_PROXY` из окружения игнорируется без явной настройки |
| userinfo/парсинг URL | Строгий разбор; `user@host` и нестандартные формы отклоняются |

- Отказ → `egress_denied`, audit `network.egress.denied` (без тела запроса).
- `base_url` провайдера проверяется уже при создании и изменении (статическая проверка и одно разрешение DNS для
  каждого назначения типа провайдера) → `422 endpoint_not_allowed`; соединение всё равно проверяется заново при
  каждом вызове.
- Адаптеры AI-провайдеров — без SDK, только через `SafeHttpClient` (redirects не выполняются, ответы ограничены по
  размеру и времени); тексты ошибок и тела ответов провайдера наружу не передаются — только типизированные коды
  (`provider_auth_failed`, `provider_timeout`, …) с фиксированными сообщениями.
- Connection test ограничен лимитом `provider_test` (10/мин на инсталляцию) и не держит транзакцию БД во время сетевого
  вызова.
- Коннекторы обращаются только к фиксированным хостам провайдеров (Google APIs, Microsoft Graph и login endpoints).
- Парсер не выполняет сетевых запросов (внешние ресурсы в документах не загружаются).
- Тела ответов внешних сервисов ограничены по размеру и времени.
- **Второй рубеж** — сегментация Docker networks и egress-фильтрация на уровне хоста ([DEPLOYMENT.md](DEPLOYMENT.md)).

**Локальные LLM** используют те же `LLMProvider`/`EmbeddingProvider`. Безопасная настройка (владелец): изолированная
сеть для сервиса модели без публикации порта → запись allowlist с назначением и CIDR → перезапуск API/worker →
провайдер в приложении с тем же `base_url` → connection test. Удобство обеспечивается одной записью конфигурации, а не
ослаблением проверок.

## 9. Uploads, parsing и storage (§8 п.6, п.8)

Все источники ([ADR-008](adr/0008-unified-document-ingestion.md)) — upload, Google Drive, Microsoft 365 — проходят одни и
те же стадии 2–4 ingestion pipeline: проверка прав и состояния ресурсов, типа и размера, безопасности файла. Worker повторно
проверяет состояние ресурсов, не доверяя payload задачи.

| Мера | Реализация **[Proposed — значения]** |
|---|---|
| Quarantine | Файл сначала попадает в quarantine/temp storage с ограниченными правами (каталог `0700`, без exec), cleanup по TTL (§8 п.8) |
| Тип | По содержимому (magic bytes), а не расширению/`Content-Type`; только форматы MVP (§6); macro-enabled (`.docm`, `.xlsm`) отклоняются |
| Размер | Лимит на файл (по умолчанию 50 МБ) — проверяется потоково, до полной загрузки |
| Страницы / листы / строки | Лимиты страниц PDF, листов и строк XLSX/CSV |
| Archive bombs | DOCX и XLSX — ZIP-контейнеры: лимит суммарного распакованного размера, числа записей, коэффициента сжатия, вложенности |
| Время парсинга | Жёсткий timeout на документ; превышение → `failed` с кодом `parse_timeout` |
| Ресурсы worker | Парсинг в отдельном процессе/контейнере с лимитами CPU/памяти (cgroups, rlimits); отдельная очередь |
| Имена файлов | Используются только для отображения; ключи storage генерирует сервер; `Content-Disposition` с безопасным кодированием. Malicious filenames не выходят из storage (§12) |
| Checksum | SHA-256 для идемпотентности и целостности |
| Antivirus | Открытый вопрос (например, интеграция ClamAV как опциональный шаг) |

## 10. Prompt injection и безопасность LLM (§8)

- Документы **всегда** считаются недоверенным вводом; их текст не может переопределять system policies.
- В MVP модель **не получает инструментов** для произвольных действий (§8, §18).
- Secrets не помещаются в prompt.
- Инструкции отделены от retrieved context: системные правила и instructions ассистента — в system-части; retrieved
  chunks — в явно размеченном блоке «данные, не инструкции» с citation IDs.
- Модель может сослаться только на citation IDs, выданные backend; остальные отклоняются (§6, §12).
- **Рендеринг ответа:** Markdown без сырого HTML; внешние изображения в ответе не загружаются автоматически
  (защита от эксфильтрации через URL изображений); ссылки — `rel="noopener noreferrer nofollow"` с отображением
  домена **[Proposed]**.
- Тестирование adversarial documents: документы с инструкциями «игнорируй правила», попытками выдать чужие
  citation IDs, раскрыть system prompt, вставить ссылки/изображения для эксфильтрации (§8, §13 Фаза 6).

**Реализовано в фазе 6** (`application/retrieval/context.py`): system rules идут первыми в `instructions` и не
могут быть отменены ни документом, ни инструкциями администратора ассистента; chunks передаются только как
`ChatRequest.context` (экранированный блок `<documents>`); citation IDs `c1…cn` выдаются только отобранным после
авторизации chunks и действуют одну генерацию; `CitationFilter` удаляет из ответа любой невыданный ID (в том
числе ID прошлых ответов и разбитые между фрагментами потока маркеры). Детерминированные тесты SC-08
(`tests/unit/test_retrieval_context.py`): «ignore all previous instructions», поддельные `</documents>` и
`<document id=…>`, выдуманные `[c99]`, ссылки и изображения для эксфильтрации, отсутствие секретов в теле
запроса к провайдеру. Проверка поведения реальной модели на adversarial documents — в harness фазы 8.

## 11. Audit log (§8 п.9)

Журнал append-only (DATABASE_SCHEMA.md §7.3); не содержит секретов, токенов и содержимого документов.

Реализованы события setup, аутентификации, пользователей, групп, ролей, провайдеров, моделей, embedding profiles,
`secrets.rotated` и `network.egress.denied`; остальные — **[Proposed]**. В audit провайдеров нет значений ключей:
`provider.secret.rotated` содержит только `change = added | replaced | removed`.

**Критичные действия:**

| Область | События |
|---|---|
| Setup / настройки | `setup.completed`, `setup.initialize.rejected`, `setup.token_issued`, `admin.created_via_cli`, `admin.password_reset_via_cli`, `secrets.rotated`, `settings.updated` |
| Аутентификация | `auth.login.succeeded`, `auth.login.failed`, `auth.logout`, `auth.session.revoked`, `auth.session.revoked_by_admin`, `auth.password.changed`, `auth.password.reset_requested`, `auth.password.reset` |
| Пользователи и группы | `user.created`, `user.updated`, `user.disabled`, `user.deleted`, `user.activated`, `user.activation.reissued`, `user.activation.rejected`, `group.created`, `group.updated`, `group.deleted`, `group.members.changed` |
| Роли и доступ | `role.assigned`, `role.revoked`, `role.created`, `role.updated`, `role.deleted`, `kb.access.granted`, `kb.access.revoked` |
| Провайдеры | `provider.created`, `provider.updated`, `provider.deleted`, `provider.secret.rotated`, `provider.tested`, `model.created`, `model.updated`, `model.deleted`, `embedding_profile.created`, `embedding_profile.updated` |
| Источники | `data_source.created`, `data_source.updated`, `data_source.deleted`, `data_source.oauth.started`, `data_source.connected`, `data_source.oauth.failed`, `data_source.disconnected`, `sync.requested` |
| KB и документы | `kb.created`, `kb.updated`, `kb.deleted`, `kb.source.attached`, `kb.source.detached`, `kb.reindex.requested`, `document.uploaded`, `document.deleted`, `document.reindex.requested` |
| Ассистенты и чат | `assistant.created`, `assistant.updated`, `assistant.deleted`, `conversation.deleted` |
| Jobs | `job.retried`, `job.cancelled` |
| Безопасность | Отказы доступа (`outcome = denied`) на чувствительных операциях; срабатывание rate limit на login; `network.egress.denied` |
| Опционально (открытый вопрос 6) | `document.viewed`, `audit.viewed` |

## 12. Web security (§8 п.4, §13 Фаза 12)

- **TLS** терминируется в Nginx; HTTP → HTTPS redirect; HSTS.
- **Security headers:** `Content-Security-Policy` (`default-src 'self'; script-src 'self'; object-src 'none';
  base-uri 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; connect-src 'self'`;
  стили Chakra UI/emotion требуют nonce или `style-src 'self' 'unsafe-inline'` — решить при реализации),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Cross-Origin-Opener-Policy: same-origin`.
- **XSS:** React-экранирование по умолчанию; `dangerouslySetInnerHTML` запрещён без санитизации; содержимое
  документов и ответы модели рендерятся как недоверенные данные. Реализовано (Фаза 8): ответ — Markdown без сырого
  HTML; изображения из ответа не загружаются (утечка данных через URL), остаётся ссылка; ссылки только http(s) с
  `rel="noopener noreferrer nofollow"` и показом домена; `javascript:`/`data:`/`mailto:` и прочие схемы — текст;
  ссылки на источники строятся из UUID цитат, выданных backend; текст документа в viewer — только текст
  (`AnswerMarkdown.test.tsx`, `viewer.test.tsx`). CSP — вместе с Nginx (Фаза 11).
- **CORS:** same-origin; при необходимости — явный allowlist origins, никогда `*` вместе с credentials.
- **Session theft:** HttpOnly cookies, хэш токена в БД, idle и абсолютный срок, немедленный отзыв, новая сессия при входе (ADR-013).

## 13. Rate limiting и resource exhaustion (§8 п.5, п.6)

- Rate limits для login, upload, chat и дорогих операций (§8 п.5); значения — [API_CONTRACTS.md](API_CONTRACTS.md), раздел 5 «Rate limits».
- Лимит одновременных генераций на пользователя и общий лимит на инсталляцию.
- Ограниченные DB pools, лимиты очередей, timeouts на всех внешних вызовах (§11).
- Ограничение длины сообщения чата, числа сообщений истории и context budget (§5, §6).

## 14. Логи и privacy (§11)

- Запрещено в обычных логах (§11): содержимое документов и chunks, prompts и ответы модели, токены, пароли,
  API keys, cookies, заголовки `Authorization`, OAuth codes.
- Ошибки внешних SDK санитизируются: они могут содержать фрагменты запроса или ключа.
- В логах — идентификаторы (`user_id`, `job_id`, `correlation_id`), а не email/имена, где возможно.
- Уровень DEBUG в production не включает payload-логирование.

## 15. OAuth-коннекторы (§7, §13 Фазы 9, 10)

- `state`: случайный, одноразовый, привязан к сессии и пользователю, TTL ≈ 10 мин; проверяется в callback.
- PKCE (S256) — обязательно для Microsoft Entra ID (§7) и для Google ([ADR-020](adr/0020-google-drive-connector.md)).
- Google Drive: `state` хранится в Redis под SHA-256 ключом и забирается атомарно (`GETDEL`); callback сверяет
  текущую cookie-сессию, пользователя и его права; возврат в UI — только на путь, построенный сервером (нет open
  redirect). Scope — `drive.readonly` ([ADR-020](adr/0020-google-drive-connector.md) §1, §3).
- Redirect URI — точное совпадение, только HTTPS (кроме localhost в dev).
- Refresh tokens шифруются (§13 Фаза 9, [ADR-016](adr/0016-secret-encryption.md)); access tokens провайдеров не сохраняются (только в памяти задания).
- **Выбор папки в UI не ограничивает OAuth token** (§7). Application-level filtering ≠ OAuth-level isolation.
  Утечка токена = доступ ко всему, что разрешают выданные scopes.
- `Files.Read.All` и подобные широкие разрешения Microsoft не ограничиваются выбранной папкой; исследовать
  Selected permissions (§7, §13 Фаза 10).
- До production: изучить scopes, Google verification, Workspace delegated access, минимальные полномочия (§7).
- Фактически выданные scopes сохраняются и **честно отображаются** в UI (§7).
- Revoke/disconnect: отзыв токена у провайдера, где это поддерживается; удаление credentials; остановка sync;
  инструкция по отзыву согласия у провайдера.

## 16. Retention, удаление и backup (§8)

### 16.1 Удаление данных

- Lifecycle и сроки для original files, extracted text, versions, chunks, embeddings, chat history, audit logs и
  credentials — [DATABASE_SCHEMA.md §8](DATABASE_SCHEMA.md#8-retention-и-lifecycle-удаления-8).
- Soft delete **не оставляет** документ доступным для retrieval (§8).
- Удаление документа исключает его из retrieval и очищает производные данные по retention policy (§5).
- Удаление в источнике, выход за выбранную область и удаление источника проходят тот же lifecycle (ADR-008).
- Disconnect источника удаляет credentials немедленно.

### 16.2 Резервное копирование

- Backup включает PostgreSQL и object storage (оригиналы); Redis — не источник истины.
- Ключ шифрования секретов хранится и резервируется **отдельно** от backup БД; bootstrap token в backup не нужен.
- Backup шифруется и имеет ограниченный доступ; влияние backup retention документируется: удалённые данные остаются
  в backup до истечения их срока (§8).
- После восстановления из backup повторно применяются удаления, выполненные после момента backup (по audit log), до
  открытия доступа пользователям **[Proposed]**.
- Backup/restore drills и проверка lifecycle удаления — регулярно и перед релизом (§8 п.10, §20). Процедура —
  [DEPLOYMENT.md](DEPLOYMENT.md).

## 17. Threat model (§8)

| Угроза (§8) | Меры | Проверка |
|---|---|---|
| Утечка между KB / пользователями | Контекст из сессии, фильтры доступа к KB в SQL, проверка состояния ресурсов в jobs, (RLS) | Тесты «KB без доступа / чужой диалог недоступны» для каждого endpoint/job |
| Утечка между организациями | Одна организация на инсталляцию (ADR-012): отдельные БД, storage, ключи | Не применимо внутри инсталляции |
| RBAC bypass | Policy в каждом use case, deny by default, 404/403, frontend не boundary | Negative RBAC tests, тесты комбинаций permissions (§13 Фаза 7) |
| Потеря управления и эскалация через управление пользователями | Последний активный администратор защищён блокировкой строки роли `admin`; `admin` только напрямую; запрет отключать/удалять себя; роли при создании — только с `roles.assign` (§3.1) | Тесты последнего администратора, в том числе параллельные снятия ролей и удаления |
| Перехват ссылки активации | Токен во фрагменте URL, только SHA-256 в БД, 72 ч, одноразовость под блокировкой, перевыпуск отменяет прежний, одинаковая ошибка, лимит на IP (ADR-015) | Тесты повторной/параллельной активации, истёкшего и перевыпущенного токена |
| Утечка API keys / OAuth tokens | Шифрование, ключ вне БД, write-only API, redaction, отсутствие в prompt | Тесты: secrets не возвращаются и не логируются |
| SSRF | Валидация URL и IP, запрет private/metadata, allowlist уровня инсталляции, pinning | SSRF-тесты (§12) |
| Malicious uploads / parser exploits | Quarantine, magic bytes, лимиты, изолированный парсер, timeout | Upload/parser tests (§13 Фаза 12) |
| Archive bombs | Лимиты распаковки ZIP-контейнеров | Archive bomb tests |
| Prompt injection | Недоверенный контент, разделение инструкций и контекста, нет tools, валидация цитат, безопасный рендеринг | Adversarial documents (§13 Фаза 6) |
| XSS | Экранирование, CSP, запрет сырого HTML | XSS tests |
| CSRF | Double-submit + Origin, SameSite | CSRF tests |
| Session theft | HttpOnly/Secure, хэш токена в БД, idle/абсолютный срок, отзыв, новая сессия при входе (ADR-013) | Тесты revoked/expired sessions, session fixation (§12) |
| Resource exhaustion | Rate limits, лимиты upload/парсинга/генераций, pools, timeouts | Performance tests (§12) |
| Утечки через логи | Запрет payload-логирования, redaction | Проверка логов в тестах |
| Ошибки удаления | Немедленное исключение из retrieval, purge jobs, lifecycle drills | Тест «deleted docs не находятся» (§12) |
| Захват неинициализированной инсталляции | Bootstrap token, атомарная инициализация, флаг без сброса через API, CLI-восстановление (ADR-006) | SC-03, SC-04 |
| Обход сетевой политики администратором приложения | Allowlist только в Instance Settings, нет API изменения (ADR-007) | SC-02 |
| Доступ к ассистенту без доступа ко всем его KB | ADR-005, проверка на каждом запросе | SC-01 |
| Чрезмерные OAuth permissions | Минимальные scopes, честное отображение, исследование Selected permissions | Документация и ревью перед production |

## 18. Обязательные security-тесты (§12)

Негативные тесты обязательны:

- [x] KB / документ без доступа и чужой диалог недоступны (Фазы 5–8: SC-05, `test_source_viewer.py`,
  `test_conversations_api.py`, `test_chat_stream.py`, E2E `access.spec.ts`/`chat.spec.ts`);
- [x] unauthorized chunks не попадают в LLM (Фазы 6–8: `test_retrieval.py`, `test_chat_stream.py`, E2E — журнал
  запросов фейкового провайдера);
- [x] revoked sessions отклоняются (Фаза 3);
- [x] secrets не возвращаются (Фаза 4: API провайдеров, frontend store и storage);
- [x] malicious filenames не выходят из storage (Фаза 5: `test_local_storage.py`, `test_file_inspection.py`);
- [x] повторные jobs не дублируют chunks (Фаза 5: SC-09, `test_indexing.py`);
- [x] deleted docs не находятся (Фаза 5–6: `test_retrieval.py`, viewer);
- [x] выдуманные citation IDs отклоняются (Фазы 6, 8: потоковый фильтр, `test_chat_stream.py`, E2E).

Security gate Фазы 8 — `test_chat_security_gate.py`: CSRF-токен и доверенный `Origin` на каждом изменяющем запросе
чата (поток, stop, rename, delete), отказ не занимает слот генерации и лимит, приватные ответы с `no-store`, текст
вопроса, ответа и документов не попадает в логи.

Плюс категории (§12): изоляция данных KB и пользователей, RBAC, SSRF, uploads, prompt injection; recovery: backup restore, worker
restart, interrupted sync.

### 18.1 Контрольные сценарии

Обязательны для реализации и тестирования соответствующих фаз ([DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md)).

| ID | Сценарий | Ожидаемое поведение | Решение | Фаза |
|---|---|---|---|---|
| SC-01 | Пользователь имеет доступ к KB A, но не к KB B; обе подключены к одному ассистенту | Ассистента нет в списке; `GET /assistants/{id}` → `404`; `POST /chat/stream` → `404` (или `403 assistant_unavailable` в собственном старом диалоге); ни один chunk из A или B не уходит в LLM | ADR-005 | 7–8 |
| SC-02 | Administrator приложения пытается расширить сетевой allowlist | API изменения allowlist отсутствует (`404/405`); `PATCH /settings` с сетевыми полями → `422`; провайдер с непозволенным `base_url` → `422 endpoint_not_allowed` | ADR-006, ADR-007 | 3–4 |
| SC-03 | Злоумышленник повторно вызывает первоначальную настройку | После инициализации `POST /setup/initialize` → `409 setup_already_completed` при любом токене; до инициализации без валидного токена → `403 setup_token_invalid`; событие `setup.initialize.rejected` | ADR-006 | 3 |
| SC-04 | Два запроса одновременно создают первого администратора | Ровно один `201`; второй ждёт блокировку и получает `409`; в БД один первый администратор | ADR-006 | 3 |
| SC-05 | Пользователь запрашивает документ KB, к которой у него нет доступа, напрямую по ID (карточка, viewer, chunk, reindex, delete) | `404 not_found` для всех методов; данные и факт существования не раскрываются | ADR-012, ADR-008 | 5 |
| SC-06 | Локальный Ollama доступен по явно разрешённому адресу | Провайдер `ollama` с `base_url`, совпадающим с записью allowlist, проходит connection test и используется в чате/embeddings; тот же адрес для другого назначения или типа провайдера вне записи → отказ | ADR-007 | 4 |
| SC-07 | Разрешённый hostname после DNS-разрешения указывает на запрещённый адрес (вне `allowed_cidrs`, metadata, loopback) | Соединение не устанавливается; `egress_denied`; `network.egress.denied` в audit; повторного DNS-разрешения нет | ADR-007 | 4 |
| SC-08 | Внешний документ содержит prompt injection | Инструкции из документа не меняют правила; модель без tools; выдуманные citation IDs отклоняются; внешние изображения в ответе не загружаются; секреты в prompt отсутствуют | §10, ADR-008 | 6 |
| SC-09 | Повторная задача ingestion для уже обработанной версии | Job завершается `succeeded` с `result = skipped_already_processed`; число chunks и embeddings не меняется; при гонке двух jobs дубликатов нет (unique + lock) | ADR-008 | 5 |

## 19. Процессы (§12, §13 Фаза 12, §20)

- **Definition of Done** включает security review (§12).
- **Dependency и container scanning** в CI (§13 Фаза 12); инструменты — **[Proposed]**: pip-audit, npm audit /
  Dependabot, Trivy.
- **Security reporting:** `SECURITY.md` с процессом ответственного раскрытия (§4, §13 Фаза 13).
- **Перед релизом** (§20): нет известных critical/high issues; тесты изоляции KB/пользователей и permission tests проходят; secrets
  не попадают в logs/API responses; upload/parser limits и ingestion idempotency работают; deleted docs исключены
  из retrieval; citations валидируются backend; backup/restore проверены.
- Threat model review и RBAC/KB-access audit — Фаза 12 и при значимых изменениях.

## 20. Открытые вопросы

1. Алгоритм и ротация ключа шифрования секретов (планируемый ADR).
2. RLS как второй рубеж доступа на уровне KB (планируемый ADR).
3. Antivirus-сканирование загрузок.
4. CSP для стилей Chakra UI (nonce vs `unsafe-inline`).
5. Нужно ли аудировать просмотр документов и чтение аудита.
6. Список распространённых паролей для парольной политики (источник, лицензия).

Решено: одна организация на инсталляцию ([ADR-012](adr/0012-single-organization-deployment.md)), сессии, CSRF,
пароли и лимиты входа ([ADR-013](adr/0013-auth-session-strategy.md)), создание пользователей ссылкой активации
([ADR-015](adr/0015-user-activation-links.md)), срок и повторная генерация bootstrap token
(§4.5), сетевой allowlist и локальные модели ([ADR-007](adr/0007-network-access-and-local-llm.md)), bootstrap
([ADR-006](adr/0006-initial-setup-and-system-configuration.md)), доступ к ассистентам
([ADR-005](adr/0005-assistant-access-model.md)).
