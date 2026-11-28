# PlummyKnowledge — Security Model

| | |
|---|---|
| Статус | v0.4 — меры Фаз 1–11 реализованы; threat model пересмотрена в Фазе 12 (§17) |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §1, §2, §5, §7, §8, §11, §12, §13 (Фазы 3, 9, 10, 12), §18, §20 |
| Решения | [ADR-005](adr/0005-assistant-access-model.md) … [ADR-008](adr/0008-unified-document-ingestion.md), [ADR-012](adr/0012-single-organization-deployment.md), [ADR-013](adr/0013-auth-session-strategy.md), [ADR-016](adr/0016-secret-encryption.md), [ADR-023](adr/0023-production-deployment.md), [ADR-024](adr/0024-database-owner-and-runtime-roles.md) (Accepted) |
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

### 3.6 Инвентаризация маршрутов (шаг 12.2)

Все маршруты API (88 на шаг 12.2) сведены в проверенную таблицу `apps/backend/tests/route_inventory.py`. В ней для
каждого метода и пути указан вид доступа и нужное право или уровень доступа к KB. Изменения в таблице проходят
review как изменения кода.

| Вид | Маршруты | Проверка |
|---|---|---|
| Без сессии | `health/*`, `setup/*`, `auth/login`, `auth/logout`, `auth/activate`, `data-sources/oauth/callback` | bootstrap token (§4.5), токен активации (§4.6), OAuth state, привязанный к сессии (§15), или ничего (health) |
| Своё (`own`) | `auth/me`, `auth/sessions*`, `auth/password/change`, `conversations*`, `chat/*` | Данные владельца сессии, чужие — `404`; диалоги и чат — ещё `chat.use` и доступность ассистента (§6.3). Администратор чужие диалоги тоже не видит |
| Право (`permission`) | `users*`, `groups*`, `roles*`, `providers*`, `embedding-profiles*`, `data-sources*`, `jobs*`, `system/settings`, `POST`/`DELETE knowledge-bases` | `require_permission` в маршруте (ранний отказ) и повторно в use case; отказ — `403 permission_denied` |
| Доступ к KB | Остальные `knowledge-bases/*`, `documents/*` | Уровень доступа к KB (§3.4) в use case: без доступа — `404 not_found`, `read` вместо `manage` — `403 permission_denied`; список KB — только доступные |
| Ассистент | `assistants*` | ADR-005 (§6.3) |

Все маршруты проверяют `Origin` (§4.4); изменяющие маршруты с сессией — ещё CSRF-токен. Права `settings.manage`
(Application Settings ещё нет) и `audit.read` (до `GET /audit/events`, шаг 12.5) маршрутами пока не используются.

Проверки:

- `tests/unit/test_route_inventory.py`. Тест падает, если:
  - в приложении есть маршрут без записи в таблице, или в таблице есть запись для несуществующего маршрута;
  - маршрут без сессии требует сессию, или маршрут с сессией её не требует;
  - `require_permission` маршрута проверяет не то право, что указано в таблице;
  - маршрут не проверяет `Origin`;
  - право из каталога не используется и не зарезервировано с причиной;
  - системные роли не совпадают с матрицей §3.3.
- `tests/integration/test_rbac_matrix.py` (настоящие PostgreSQL и Redis):
  - каждый маршрут с правом проверяется для каждой системной роли и для пользователя без ролей. Второй прогон идёт
    с отключёнными проверками в маршрутах, поэтому отказывает сам use case (§6.1);
  - каждый маршрут KB и документов проверяется для прямого `read`, `read` через группу, прямого `manage`,
    `knowledge_bases.manage` и пользователя без доступа;
  - чужой диалог, поток чата и сессия отвечают `404` другому пользователю и администратору;
  - без `chat.use` диалоги и чат отвечают `403`;
  - без сессии все маршруты, кроме публичных, отвечают `401`;
  - каждый изменяющий маршрут с сессией, но без CSRF-токена отвечает `403 csrf_failed`.

Найденные расхождения: не найдено.

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

| Мера | Реализация (Фаза 5; значения — по умолчанию) |
|---|---|
| Quarantine | Файл сначала попадает в quarantine/temp storage с ограниченными правами (каталог `0700`, без exec), cleanup по TTL (§8 п.8) |
| Тип | По содержимому (magic bytes), а не расширению/`Content-Type`; только форматы MVP (§6); macro-enabled (`.docm`, `.xlsm`) отклоняются |
| Размер | Лимит на файл (по умолчанию 50 МБ) — проверяется потоково, до полной загрузки |
| Страницы / листы / строки | 2000 страниц PDF, 100 листов, 200 000 строк XLSX/CSV |
| Archive bombs | DOCX и XLSX — ZIP-контейнеры: не более 10 000 записей, 512 МБ распакованного размера, коэффициент сжатия записи ≤ 100 (для записей больше 1 МБ); вложенные архивы и XML-сущности — проверки шага 12.4 |
| Время парсинга | Жёсткий timeout на документ (300 с, группа процессов завершается); превышение → `failed` с кодом `parse_timeout` |
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
  домена (реализовано, Фаза 8, §12).
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

Журнал append-only (DATABASE_SCHEMA.md §7.3); не содержит секретов, токенов и содержимого документов. На уровне БД
append-only обеспечивает runtime-роль без `UPDATE`/`DELETE` на `audit_logs`; записи старше срока хранения (1 год)
удаляет `maintenance prune-audit` под ролью владельца схемы ([ADR-024](adr/0024-database-owner-and-runtime-roles.md),
реализовано в шаге 12.1): событие `maintenance.audit_pruned` с числом записей и границей.

Реализованы события setup, аутентификации, пользователей, групп, ролей, провайдеров, моделей, embedding profiles,
`secrets.rotated`, `network.egress.denied`, `job.retried`, `job.cancelled` и `maintenance.*` (в том числе
`maintenance.audit_pruned`); остальные — **[Proposed]**. В audit провайдеров нет значений ключей:
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
| Обслуживание (CLI на сервере, `actor_type = system`) | `maintenance.deletions_exported`, `maintenance.deletion_reapplied` (§16.2), `maintenance.audit_pruned` (ADR-024 §6) |
| Безопасность | Отказы доступа (`outcome = denied`) на чувствительных операциях; срабатывание rate limit на login; `network.egress.denied` |
| Опционально (открытый вопрос 4, §20) | `document.viewed`, `audit.viewed` |

## 12. Web security (§8 п.4, §13 Фаза 12)

- **TLS** терминируется в Nginx; HTTP → HTTPS redirect; HSTS.
- **Security headers:** `Content-Security-Policy` (`default-src 'self'; script-src 'self'; object-src 'none';
  base-uri 'none'; frame-ancestors 'none'; form-action 'self'; img-src 'self' data:; connect-src 'self'`;
  `style-src 'self' 'unsafe-inline'` — только для стилей emotion/Chakra UI, скрипты без `'unsafe-inline'`;
  решение и полный набор заголовков — [ADR-023](adr/0023-production-deployment.md) §5, Фаза 11),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Cross-Origin-Opener-Policy: same-origin`.
- **XSS:** React-экранирование по умолчанию; `dangerouslySetInnerHTML` запрещён без санитизации; содержимое
  документов и ответы модели рендерятся как недоверенные данные. Реализовано (Фаза 8): ответ — Markdown без сырого
  HTML; изображения из ответа не загружаются (утечка данных через URL), остаётся ссылка; ссылки только http(s) с
  `rel="noopener noreferrer nofollow"` и показом домена; `javascript:`/`data:`/`mailto:` и прочие схемы — текст;
  ссылки на источники строятся из UUID цитат, выданных backend; текст документа в viewer — только текст
  (`AnswerMarkdown.test.tsx`, `viewer.test.tsx`). CSP задаёт Nginx (реализовано, Фаза 11).
- **CORS:** same-origin; при необходимости — явный allowlist origins, никогда `*` вместе с credentials.
- **Session theft:** HttpOnly cookies, хэш токена в БД, idle и абсолютный срок, немедленный отзыв, новая сессия при входе (ADR-013).

## 13. Rate limiting и resource exhaustion (§8 п.5, п.6)

- Rate limits для login, setup, activation, upload, chat, provider test, search, sync и дорогих операций (§8 п.5);
  общий лимит `general` (300 запросов в минуту на пользователя, без сессии — на IP; без health и SSE-потока) — шаг
  12.5. Значения — [API_CONTRACTS.md](API_CONTRACTS.md), раздел 5 «Rate limits».
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
- Microsoft 365 ([ADR-021](adr/0021-microsoft-365-connector.md)): delegated `Files.Read.All` + `Sites.Read.All`,
  authority одного tenant организации, PKCE; app-only `Sites.Selected` рассмотрен и отложен (отдельный ADR при
  потребности в изоляции на уровне OAuth). Отзыва отдельного токена у Microsoft нет: disconnect удаляет секрет
  локально, UI предлагает отозвать согласие в Microsoft. Адреса следующих страниц Graph не берутся из ответа.
- До production: изучить scopes, Google verification, Workspace delegated access, минимальные полномочия (§7).
- Фактически выданные scopes сохраняются и **честно отображаются** в UI (§7).
- Revoke/disconnect: отзыв токена у провайдера, где это поддерживается; удаление credentials; остановка sync;
  инструкция по отзыву согласия у провайдера.
- **Реализовано для Google Drive (Фаза 9):** UI показывает фактические `granted_scopes` и предупреждение о широте
  токена; токены не попадают в браузер (E2E `drive.spec.ts` сверяет страницу и ответы API со всеми выданными
  фейковым Google токенами); документы из Drive проходят тот же pipeline и ту же сборку контекста, что и загрузки:
  враждебные имя файла и текст остаются экранированными данными внутри блока `<documents>` (SC-08,
  `test_source_sync.py`); удалённый в Drive файл после sync не находится поиском (`test_source_sync.py`, E2E).
- **Реализовано для Microsoft 365 (Фаза 10, [ADR-021](adr/0021-microsoft-365-connector.md)):** authority одного
  tenant (`common`/`organizations`/`consumers` отклоняются конфигурацией), PKCE и одноразовый `state` — общий код с
  Google; подключение без `Files.Read.All` или `Sites.Read.All` отклоняется и ничего не сохраняет
  (`test_microsoft_sources_api.py`); ротируемый refresh token заменяет сохранённый; `invalid_grant` /
  `interaction_required` помечают источник отозванным и останавливают расписание (`test_microsoft_sync.py`).
  UI показывает фактические scopes и предупреждение о широте: токен читает **все** файлы и сайты SharePoint,
  доступные учётной записи, выбор папки этого не ограничивает. Disconnect удаляет токен локально без сетевого
  вызова и показывает, где отозвать согласие в Microsoft. Адреса `@odata.nextLink`/`deltaLink` принимаются только
  в пределах `api_url`, подделанный cursor ведёт к reconciliation, а не к запросу на чужой хост
  (`test_microsoft_graph.py`, `test_microsoft_sync.py`). E2E `microsoft.spec.ts` против `deploy/e2e/fake_microsoft.py`:
  OAuth в браузере → сайт → библиотека → папка → sync → поиск → удаление в SharePoint → sync → disconnect; файл
  вне папки не индексируется, ни один выданный токен не попадает в страницу и ответы API.

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
- После восстановления из backup повторно применяются удаления, выполненные после момента backup, до открытия
  доступа пользователям: `maintenance export-deletions` на исходной БД и `maintenance reapply-deletions` на
  восстановленной ([ADR-023](adr/0023-production-deployment.md) §6, реализовано в шаге 11.3): документы, KB,
  источники (удаление и отключение), диалоги, пользователи. Файл удалений содержит только идентификаторы, типы и время.
  `restore.sh` применяет их до запуска Nginx. Прочие изменения после backup (отключение пользователя, отзыв ролей,
  доступа и сессий, смена пароля) не применяются автоматически: экспорт считает их для ручной проверки. Если исходная
  БД потеряна, окно потерянных удалений и отзывов равно интервалу между backup.
- Backup/restore drills и проверка lifecycle удаления — регулярно и перед релизом (§8 п.10, §20). Процедура —
  [DEPLOYMENT.md](DEPLOYMENT.md).

## 17. Threat model (§8)

Пересмотрена в шаге 12.0: меры сверены с реализацией Фаз 1–11. Колонка «Фаза 12» называет шаг
([DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md)), который закрывает пробел или сводит проверки в один прогон; «—» —
пробелов не найдено.

| Угроза (§8) | Меры (реализовано) | Проверка | Фаза 12 |
|---|---|---|---|
| Утечка между KB / пользователями | Контекст из сессии; фильтры доступа к KB, документам и версиям в SQL до retrieval и LLM; jobs заново загружают ресурс и проверяют состояние; `404` без раскрытия существования. RLS нет (§5 п.6, §20) | SC-05, `test_authorization.py`, `test_rbac_matrix.py` и `test_route_inventory.py` (§3.6), `test_source_viewer.py`, `test_conversations_api.py`, E2E `access.spec.ts` | 12.2 (выполнен): инвентаризация маршрутов и матрица ролей, расхождений нет; 12.3 (выполнен): SC-прогон, §18.2 |
| Утечка между организациями | Одна организация на инсталляцию (ADR-012): отдельные БД, storage, ключи | Не применимо внутри инсталляции | — |
| RBAC bypass | `require_permission` и policy в каждом use case, deny by default, frontend не boundary | Negative RBAC tests по модулям (`tests/integration/rbac.py`); инвентаризация и матрица ролей (§3.6) | 12.2 (выполнен): тест падает, если маршрут не описан в таблице прав; `401` без сессии, `403` без права и в use case без проверки маршрута |
| Потеря управления и эскалация через управление пользователями | Последний активный администратор защищён блокировкой строки роли `admin`; запрет отключать/удалять себя; роли при создании — только с `roles.assign` (§3.1) | Тесты последнего администратора, в том числе параллельные | — |
| Перехват ссылки активации | Токен во фрагменте URL, только SHA-256 в БД, 72 ч, одноразовость под блокировкой, лимит на IP (ADR-015) | Тесты повторной, параллельной, истёкшей и перевыпущенной активации | — |
| Утечка API keys / OAuth tokens | AES-256-GCM, key ring вне БД и вне backup, write-only API, redaction ошибок, нет в prompt (ADR-016) | `test_secrets.py`, тесты API провайдеров и источников, E2E: токены фейковых Google/Microsoft не попадают в страницу | 12.5: сквозной тест логов на DEBUG |
| Подмена или удаление audit log, DDL при компрометации API/worker | Runtime-роль сервисов без DDL, `TRUNCATE`, `TEMPORARY` и без `UPDATE`/`DELETE` на `audit_logs`; владелец схемы — только у `migrate`; единственный путь к DDL — две функции `SECURITY DEFINER` для HNSW-индексов профиля (ADR-024 §2.1); отказ старта в production с ролью владельца; retention — CLI владельца ([ADR-024](adr/0024-database-owner-and-runtime-roles.md), шаг 12.1) | `test_database_roles.py`, `checks.sh` в CI (роли подключений и права runtime-роли) | — (закрыто в 12.1) |
| SSRF | `SafeHttpClient`: валидация URL и IP, запрет private/metadata/loopback, allowlist в Instance Settings, pinning адреса, проверка redirect (ADR-007); сегментация сетей Compose | `test_safe_http_client.py`, `test_safe_http_redirects.py`, `test_network_policy.py`, `test_network_egress.py` (SC-06, SC-07) | 12.3 (выполнен): SC-прогон, §18.2 |
| Malicious uploads / parser exploits | Quarantine, magic bytes, отказ от macro-enabled, лимиты размера/страниц/листов/строк, парсер в отдельном контейнере без сети и секретов, процесс с rlimits и timeout | `test_file_inspection.py`, `test_isolated_parser.py`, `test_document_upload.py` | 12.4: XXE и billion laughs в DOCX/XLSX, вложенные архивы, polyglot, несовпадение MIME — в `worker-ingestion` |
| Archive bombs | Лимиты числа записей, распакованного размера и коэффициента сжатия ZIP | `test_file_inspection.py` | 12.4: вложенные архивы |
| Prompt injection | Документы — данные в блоке `<documents>`; нет tools; system rules первыми; citation IDs выдаёт и фильтрует backend; Markdown без HTML и без загрузки изображений | SC-08: `test_retrieval_context.py`, `test_chat_stream.py`, `test_source_sync.py` | 12.4: набор враждебных документов через весь pipeline |
| XSS | React-экранирование, Markdown без сырого HTML, только http(s)-ссылки, CSP без `unsafe-inline` для скриптов (ADR-023 §5) | `AnswerMarkdown.test.tsx`, `viewer.test.tsx`, проверка заголовков в CI-smoke | — (nonce для стилей — §20) |
| CSRF | Double-submit token + проверка `Origin`, SameSite (ADR-013) | `test_chat_security_gate.py`, тесты auth, `test_rbac_matrix.py` (каждый изменяющий маршрут без CSRF → `403 csrf_failed`) | 12.2 (выполнен) |
| CORS | Same-origin; allowlist origins, никогда `*` с credentials | Тесты конфигурации | 12.5: тесты preflight и ответов на чужой origin |
| Session theft | HttpOnly/Secure, хэш токена в БД, idle/абсолютный срок, отзыв, новая сессия при входе (ADR-013) | Тесты revoked/expired sessions, session fixation | — |
| Resource exhaustion | Лимиты login, setup, activation, upload, chat, provider_test, search, sync, expensive; семафор генераций; лимиты парсинга; pools и timeouts | `test_rate_limiter.py`, `test_generation_control.py` | 12.5: общий лимит `general` |
| Утечки через логи | Запрет payload-логирования, redaction | `test_chat_security_gate.py` (чат), `test_logging_and_version.py` | 12.5: сквозной тест загрузки, провайдеров и коннекторов на DEBUG |
| Ошибки удаления | Немедленное исключение из retrieval, purge, retention версий, повторное применение удалений после restore (ADR-023 §6) | «deleted docs не находятся», `test_restore_deletions.py`, restore drill в CI | — |
| Захват неинициализированной инсталляции | Bootstrap token, атомарная инициализация, флаг без сброса через API, CLI-восстановление (ADR-006) | SC-03, SC-04 (`test_setup_api.py`) | 12.3 (выполнен): SC-прогон, §18.2 |
| Обход сетевой политики администратором приложения | Allowlist только в Instance Settings, нет API изменения (ADR-007) | SC-02 | 12.3 (выполнен): SC-прогон, §18.2 |
| Доступ к ассистенту без доступа ко всем его KB | ADR-005, проверка на каждом запросе | SC-01 (`test_assistant_access.py`, `test_chat_stream.py`) | 12.3 (выполнен): SC-прогон, §18.2 |
| Чрезмерные OAuth permissions | Минимальные scopes, фактические scopes в UI, предупреждение о широте токена (§15) | `test_microsoft_sources_api.py`, E2E `drive.spec.ts`, `microsoft.spec.ts` | — (Selected permissions — отдельный ADR при потребности) |
| Уязвимые зависимости и образы | pip-audit, npm audit, Dependabot, dependency review в CI | CI | 12.6: Trivy для production-образов |
| Компрометация backup | Ключ шифрования и setup token не попадают в backup (`backup.sh` отказывается), каталог `0700`, checksums в manifest (ADR-023 §6) | Restore drill в CI | — |

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
- [x] deleted docs не находятся (Фаза 5–6: `test_retrieval.py`, viewer; Фаза 9 — удаление в Google Drive:
  `test_source_sync.py`, E2E `drive.spec.ts`; Фаза 10 — удаление в SharePoint: `test_microsoft_sync.py`, E2E
  `microsoft.spec.ts`);
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
| SC-08 | Внешний документ содержит prompt injection | Инструкции из документа не меняют правила; модель без tools; выдуманные citation IDs отклоняются; внешние изображения в ответе не загружаются; секреты в prompt отсутствуют | §10, ADR-008 | 6, 9 |
| SC-09 | Повторная задача ingestion для уже обработанной версии | Job завершается `succeeded` с `result = skipped_already_processed`; число chunks и embeddings не меняется; при гонке двух jobs дубликатов нет (unique + lock) | ADR-008 | 5 |

### 18.2 Трассировка и прогон (шаг 12.3)

Каждый пункт ожидаемого поведения SC-01…SC-09 связан с тестами в
`apps/backend/tests/security_scenarios.py`. Тест `scripts/tests/test_security_scenarios.py` (CI-job «Repository»)
проверяет, что набор сценариев совпадает с §18.1, у каждого пункта есть тесты и каждый указанный тест существует.
Backend-тесты всех сценариев запускаются одной командой в контейнере backend:
`pytest $(python -m tests.security_scenarios)`.

| ID | Чем проверено |
|---|---|
| SC-01 | `test_assistant_access.py` (список и карточка), `test_chat_stream.py` (`404`, `403 assistant_unavailable`, до модели не доходит ни один запрос), E2E `access.spec.ts` |
| SC-02 | `test_route_inventory.py` (кроме `GET /system/settings`, маршрутов `/system` и `/settings` нет), `test_groups_api.py` (`PATCH` → `405`), `test_providers_api.py` (`422 endpoint_not_allowed`). Пункт о `PATCH /settings` → `422` не применим: Application Settings ещё нет, маршрута нет |
| SC-03 | `test_setup_api.py`: `409` с любым токеном после инициализации, `403` до неё; событие `setup.initialize.rejected` в обоих случаях |
| SC-04 | `test_setup_api.py`: параллельная инициализация, откат при сбое |
| SC-05 | `test_documents_api.py`, `test_rbac_matrix.py` (все маршруты KB и документов), `test_source_viewer.py`, `test_retrieval.py`, E2E `access.spec.ts` |
| SC-06 | `test_network_egress.py`, `test_safe_http_client.py`, contract `test_ai_providers.py`, `test_providers_api.py` |
| SC-07 | `test_safe_http_client.py` (адрес после DNS, DNS rebinding), `test_network_egress.py` (`network.egress.denied`) |
| SC-08 | `test_retrieval_context.py`, `test_source_sync.py`, contract `test_ai_providers.py` (без tools), `test_chat_stream.py` (citation IDs), frontend `AnswerMarkdown.test.tsx` (без изображений и HTML) |
| SC-09 | `test_indexing.py`: повтор и гонка jobs |

Прогон 12.3 (2026-11-28): backend — 52 passed; frontend `AnswerMarkdown.test.tsx` — 8 passed; E2E `access.spec.ts`
— в CI-job «E2E». В ходе прогона добавлены тесты:

- SC-01 через чат: ассистент на двух KB при доступе к одной из них;
- SC-02: в инвентаризации нет маршрутов, меняющих Instance Settings;
- SC-03: событие `setup.initialize.rejected` после инициализации.

Расхождений с реализацией не найдено.

## 19. Процессы (§12, §13 Фаза 12, §20)

- **Definition of Done** включает security review (§12).
- **Dependency и container scanning** в CI (§13 Фаза 12): pip-audit, npm audit, Dependabot и dependency review
  (реализовано); Trivy для production-образов — шаг 12.6 (сбой на CRITICAL/HIGH, для которых есть исправление;
  исключения — в `.trivyignore` с причиной и сроком).
- **Security reporting:** `SECURITY.md` с процессом ответственного раскрытия (§4, §13 Фаза 13).
- **Перед релизом** (§20): нет известных critical/high issues; тесты изоляции KB/пользователей и permission tests проходят; secrets
  не попадают в logs/API responses; upload/parser limits и ingestion idempotency работают; deleted docs исключены
  из retrieval; citations валидируются backend; backup/restore проверены.
- Threat model review и RBAC/KB-access audit — Фаза 12 и при значимых изменениях.

## 20. Открытые вопросы

1. RLS как второй рубеж доступа на уровне KB (планируемый ADR). Разделение ролей БД
   ([ADR-024](adr/0024-database-owner-and-runtime-roles.md)) защищает схему и журнал, но не данные KB.
2. Antivirus-сканирование загрузок.
3. CSP для стилей Chakra UI: nonce вместо `unsafe-inline` (пока `unsafe-inline`, ADR-023 §5).
4. Нужно ли аудировать просмотр документов и чтение аудита.
5. Список распространённых паролей для парольной политики (источник, лицензия).

Решено: алгоритм и ротация ключа шифрования секретов ([ADR-016](adr/0016-secret-encryption.md)), роли БД и
retention журнала аудита ([ADR-024](adr/0024-database-owner-and-runtime-roles.md)), одна организация на
инсталляцию ([ADR-012](adr/0012-single-organization-deployment.md)), сессии, CSRF,
пароли и лимиты входа ([ADR-013](adr/0013-auth-session-strategy.md)), создание пользователей ссылкой активации
([ADR-015](adr/0015-user-activation-links.md)), срок и повторная генерация bootstrap token
(§4.5), сетевой allowlist и локальные модели ([ADR-007](adr/0007-network-access-and-local-llm.md)), bootstrap
([ADR-006](adr/0006-initial-setup-and-system-configuration.md)), доступ к ассистентам
([ADR-005](adr/0005-assistant-access-model.md)).
