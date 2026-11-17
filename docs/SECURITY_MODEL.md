# PlummyKnowledge — Security Model

| | |
|---|---|
| Статус | Draft v0.1 (до реализации) |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §1, §2, §5, §7, §8, §11, §12, §13 (Фазы 3, 9, 10, 12), §18, §20 |
| Решения | [ADR-004](adr/0004-tenant-isolation-and-kb-level-access.md) … [ADR-008](adr/0008-unified-document-ingestion.md) (Accepted) |
| Связанные документы | [ARCHITECTURE.md](ARCHITECTURE.md), [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md), [API_CONTRACTS.md](API_CONTRACTS.md), [DEPLOYMENT.md](DEPLOYMENT.md) |

**Как читать.** Threat model, обязательные меры, правила prompt injection, Document ACL и Retention (§8) —
нормативны. Конкретные параметры (TTL, лимиты, алгоритмы) — **[Proposed]**; изменение auth/session strategy или
tenant model требует ADR (§15).

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
| Документы, extracted text, chunks, embeddings | PostgreSQL, object storage | Утечка между tenant/KB, retrieval удалённых данных |
| Диалоги и ответы | PostgreSQL | Доступ чужих пользователей |
| API keys AI providers, OAuth refresh tokens | `secret_references` (зашифровано) | Утечка через API/логи/backup/prompt |
| Ключ шифрования секретов | Вне БД (env/файл/secret mount) | Компрометация всех секретов |
| Пароли (хэши), сессии, refresh tokens | PostgreSQL | Захват аккаунта |
| Audit log | PostgreSQL | Подмена/удаление следов |
| Инфраструктура (сеть, metadata endpoints) | Хост/облако | SSRF |

### 2.2 Акторы

Анонимный пользователь интернета; аутентифицированный пользователь любой из ролей (§2) — в том числе злонамеренный
участник одной организации, пытающийся получить данные другой KB или организации; автор документа с prompt
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

### 3.1 Роли (§2)

| Роль | Scope | Назначение |
|---|---|---|
| Platform Owner | platform | Настройки инсталляции и организаций |
| Organization Admin | organization | Пользователи, группы, провайдеры, источники и KB |
| Knowledge Manager | organization | Документы, базы знаний и синхронизация |
| Member | organization | Работа с разрешёнными ассистентами |
| Auditor | organization | Доступ к журналам аудита в рамках полномочий |

### 3.2 Каталог permissions **[Proposed]**

| Permission | Описание |
|---|---|
| `platform.organizations.manage` | Создание, изменение, приостановка организаций |
| `platform.settings.read` | Просмотр Instance Settings без секретов. Изменение Instance Settings через API не предусмотрено ([ADR-006](adr/0006-initial-setup-and-system-configuration.md)) |
| `platform.audit.read` | События уровня инсталляции |
| `org.settings.manage` | Настройки организации |
| `users.read` / `users.manage` | Просмотр / управление пользователями |
| `groups.read` / `groups.manage` | Просмотр / управление группами |
| `roles.read` / `roles.assign` / `roles.manage` | Просмотр ролей / назначение / пользовательские роли (не в 0.1.0) |
| `providers.read` / `providers.manage` | AI providers, models, embedding profiles |
| `data_sources.read` / `data_sources.manage` | Источники: просмотр / подключение, OAuth, disconnect |
| `data_sources.sync` | Запуск синхронизации |
| `knowledge_bases.manage` | Создание KB и управление **всеми** KB организации |
| `assistants.manage` | Создание и настройка ассистентов |
| `jobs.read` / `jobs.manage` | Просмотр / retry, cancel jobs |
| `audit.read` | Журнал аудита организации |
| `chat.use` | Работа с доступными ассистентами |

### 3.3 Матрица ролей **[Proposed]**

| Permission | Platform Owner | Org Admin | Knowledge Manager | Member | Auditor |
|---|:-:|:-:|:-:|:-:|:-:|
| `platform.*` | ✓ | | | | |
| `org.settings.manage` | | ✓ | | | |
| `users.*`, `groups.*` | | ✓ | `read` | | |
| `roles.read`, `roles.assign` | | ✓ | | | |
| `providers.read` | | ✓ | ✓ | | |
| `providers.manage` | | ✓ | | | |
| `data_sources.read` | | ✓ | ✓ | | |
| `data_sources.manage` | | ✓ | | | |
| `data_sources.sync` | | ✓ | ✓ | | |
| `knowledge_bases.manage` | | ✓ | ✓ | | |
| `assistants.manage` | | ✓ | | | |
| `jobs.read` | | ✓ | ✓ | | |
| `jobs.manage` | | ✓ | ✓ | | |
| `audit.read` | | ✓ | | | ✓ |
| `chat.use` | | ✓ | ✓ | ✓ | |

Обоснование: Organization Admin создаёт KB и Assistant и видит аудит (§2, сценарии 5 и 8); Knowledge Manager —
документы, KB и синхронизация (§2). Platform Owner **не** получает автоматического доступа к данным организаций; для
работы с данными ему назначается организационная роль явно. Auditor видит только журнал своей организации
(«в рамках полномочий», §2).

### 3.4 Уровни доступа к KB (§5, §8)

| Уровень | Даёт |
|---|---|
| `read` | Поиск, чат через ассистентов, просмотр документов и source viewer |
| `manage` | `read` + загрузка/удаление документов, привязка источников, reindex, управление доступом к этой KB |

Выдаётся пользователю или группе (`knowledge_base_access`). Пользователи с `knowledge_bases.manage` имеют `manage`
ко всем KB организации.

### 3.5 Вычисление прав

- Эффективные permissions = объединение permissions ролей пользователя и ролей его групп.
- Эффективный доступ к KB = максимум из прямого доступа, доступа через группы и `knowledge_bases.manage`.
- Права загружаются **на каждый запрос** (допустим короткий кэш, инвалидируемый при изменении ролей/групп/доступа);
  отзыв доступа вступает в силу не позже следующего запроса **[Proposed]**.

## 4. Authentication и сессии

Требования (§5, §13 Фаза 3): локальная аутентификация, Argon2id, короткий access token, refresh token
rotation/revocation, cookies HttpOnly/Secure/SameSite, CSRF strategy, rate limiting login, password reset только при
наличии email infrastructure; OIDC — следующая фаза, SAML/SCIM — позднее.

### 4.1 Пароли

- Argon2id; параметры не ниже рекомендаций OWASP (m = 19 MiB, t = 2, p = 1) и калибруются под сервер;
  параметры хранятся в строке хэша, при входе с устаревшими параметрами хэш пересчитывается **[Proposed]**.
- Минимальная длина 12 символов, без требований к составу; проверка по списку распространённых паролей
  **[Proposed]**.
- Сообщения об ошибке входа не различают «нет пользователя» / «неверный пароль» / «отключён» (`invalid_credentials`).
- Время ответа на неудачный логин не зависит от существования пользователя (фиктивная проверка хэша).

### 4.2 Сессии и токены **[Proposed — ADR «Auth/session strategy» (планируется)]**

| Объект | Срок | Хранение |
|---|---|---|
| Access token | 15 мин | Cookie; при **каждом** запросе сервер проверяет, что сессия не отозвана (Redis-кэш + БД) |
| Refresh token | 7 дней (idle) | Cookie, ограниченная путём refresh; в БД — только SHA-256 |
| Сессия | 30 дней (абсолютный срок) | `sessions` |

- Refresh rotation: каждый refresh выдаёт новый токен, старый помечается использованным. Повторное предъявление
  использованного токена → отзыв всей сессии и audit event `auth.refresh.reuse_detected` (§5).
- Отзыв всех сессий пользователя: logout-all, смена пароля, отключение/удаление пользователя, действие администратора.
- Отозванная сессия отклоняется немедленно (§12), даже если access token ещё не истёк.

### 4.3 Cookies (§5) **[Proposed]**

| Cookie | Атрибуты |
|---|---|
| `__Host-pk_access` | `HttpOnly; Secure; SameSite=Lax; Path=/` |
| `__Secure-pk_refresh` | `HttpOnly; Secure; SameSite=Strict; Path=/api/v1/auth/refresh` |
| `__Host-pk_csrf` | `Secure; SameSite=Strict; Path=/` (читается JS для double-submit) |

### 4.4 CSRF (§5)

- Double-submit token: значение cookie `pk_csrf` передаётся в заголовке `X-CSRF-Token` для `POST/PUT/PATCH/DELETE`.
- Дополнительно проверяется заголовок `Origin` (должен совпадать с настроенным публичным origin).
- `GET`-запросы не имеют побочных эффектов (кроме OAuth callback, защищённого `state`).

### 4.5 Первоначальная настройка и восстановление администратора ([ADR-006](adr/0006-initial-setup-and-system-configuration.md))

- `POST /api/v1/setup/initialize` требует **bootstrap token**: ≥ 256 бит, генерируется init-командой при установке, в
  БД хранится только хэш, открытое значение — в файле `0600` на сервере; в логи попадает только путь. Срок действия
  ограничен (по умолчанию 24 ч **[Proposed]**), токен одноразовый.
- Инициализация атомарна: блокировка строки `instance_state`, проверка флага и токена, создание организации,
  пользователя и ролей, установка флага, погашение токена, audit — одна транзакция. Второй параллельный запрос
  получает `409 setup_already_completed`.
- Состояние «настроено» хранится явным флагом и через API не сбрасывается; удаление всех пользователей не открывает
  setup повторно.
- Нет сессии и CSRF cookie → обязательна проверка `Origin`; rate limit `setup`; сравнение токена constant-time; тела
  запросов setup не логируются.
- Режим `cli_only` отключает web-initialize.
- Повторное создание или восстановление администратора — только CLI с доступом к серверу (`plummy admin create`,
  `plummy admin reset-password`), с audit-событием.

## 5. Tenant isolation (§8 п.1)

1. Tenant context устанавливается сервером из проверенной сессии (§5); `organization_id` от клиента не принимается.
2. Каждый repository-метод требует `organization_id`; запросы без tenant-фильтра запрещены (code review + тесты).
3. Составные внешние ключи `(organization_id, id)` не дают связать объекты разных организаций на уровне БД
   (DATABASE_SCHEMA.md §1).
4. **Background jobs:** payload содержит `organization_id`; worker загружает ресурсы только с этим фильтром и
   проверяет их состояние (не удалены, принадлежат tenant) (§8 п.1).
5. Ключи Redis (кэш, locks, rate limits) и ключи object storage содержат `organization_id` в префиксе.
6. Ответы на запросы к ресурсам чужой организации — `404` (не раскрывать существование).
7. **[Proposed — ADR «PostgreSQL RLS» (планируется)]** PostgreSQL RLS как второй рубеж.
8. Тесты изоляции двух организаций обязательны для каждого endpoint и job (§13 Фаза 2, §12).

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

### 6.4 Защита RAG (сводка)

1. Доступность ассистента (§6.3) → 2. разрешённые источники вычисляет backend → 3. фильтры tenant / источник / текущая
`ready`-версия / активный `chunk_set` / profile в SQL → 4. повторная проверка отобранных chunks перед сборкой
контекста → 5. инструкции отделены от недоверенного контекста (§10) → 6. валидация citation IDs → 7. source viewer
проверяет доступ заново.

## 7. Секреты и шифрование (§5, §8 п.3)

- Секреты (API keys провайдеров, OAuth refresh tokens и client secrets) шифруются **до записи в БД** (§5).
- Ключ шифрования хранится **вне БД** (§5): переменная окружения или смонтированный файл; отсутствие ключа при
  старте → отказ запуска с понятной диагностикой.
- **[Proposed — ADR «Шифрование секретов» (планируется)]** AES-256-GCM, уникальный nonce на запись, associated data = `organization_id` +
  `secret_id` + `kind`; key ring с `key_id` для ротации ключа без простоя.
- Секреты в API — **write-only**; в ответах только `has_secret` и `secret_hint` (§12).
- Секреты не передаются в prompt (§8), не включаются в тексты ошибок, не пишутся в логи (secret redaction, §8 п.3):
  `SecretStr` в DTO/настройках, фильтр логирования, санитизация исключений SDK.
- Backup БД содержит только шифртексты; ключ хранится и резервируется **отдельно** от backup БД.

## 8. SSRF, сетевые разрешения и локальные LLM (§8 п.7, [ADR-007](adr/0007-network-access-and-local-llm.md))

Пользовательские URL появляются в конфигурации провайдеров (`openai_compatible`, `ollama`, кастомный `base_url`
OpenAI/Anthropic). Полный алгоритм — в ADR-007; здесь — обязательные свойства.

**Модель: Explicit Network Allowlist.**

- Все внутренние и опасные адреса запрещены по умолчанию; allowlist по умолчанию пуст.
- Разрешения задаёт только владелец инсталляции в Instance Settings (конфигурация сервера). Администратор организации
  не может расширить allowlist: такого API нет, Organization Settings не содержат сетевых параметров.
- Запись allowlist ограничена `scheme + host + port` (+ префикс пути), назначением (`llm`, `embedding`, …), типами
  провайдеров, организациями и **обязательными `allowed_cidrs`** для непубличных адресов. Разрешение hostname не
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
- Коннекторы обращаются только к фиксированным хостам провайдеров (Google APIs, Microsoft Graph и login endpoints).
- Парсер не выполняет сетевых запросов (внешние ресурсы в документах не загружаются).
- Тела ответов внешних сервисов ограничены по размеру и времени.
- **Второй рубеж** — сегментация Docker networks и egress-фильтрация на уровне хоста ([DEPLOYMENT.md](DEPLOYMENT.md)).

**Локальные LLM** используют те же `LLMProvider`/`EmbeddingProvider`. Безопасная настройка (владелец): изолированная
сеть для сервиса модели без публикации порта → запись allowlist с назначением и CIDR → перезапуск API/worker →
провайдер в организации с тем же `base_url` → connection test. Удобство обеспечивается одной записью конфигурации, а не
ослаблением проверок.

## 9. Uploads, parsing и storage (§8 п.6, п.8)

Все источники ([ADR-008](adr/0008-unified-document-ingestion.md)) — upload, Google Drive, Microsoft 365 — проходят одни и
те же стадии 2–4 ingestion pipeline: проверка прав и tenant, типа и размера, безопасности файла. Worker повторно
проверяет tenant и состояние ресурсов, не доверяя payload задачи.

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

## 11. Audit log (§8 п.9)

Журнал append-only (DATABASE_SCHEMA.md §7.3); не содержит секретов, токенов и содержимого документов.

**[Proposed] Критичные действия:**

| Область | События |
|---|---|
| Setup / организации | `setup.completed`, `setup.initialize.rejected`, `admin.created_via_cli`, `admin.password_reset_via_cli`, `organization.created`, `organization.updated`, `organization.suspended`, `organization.settings.updated` |
| Аутентификация | `auth.login.succeeded`, `auth.login.failed`, `auth.logout`, `auth.session.revoked`, `auth.session.revoked_by_admin`, `auth.refresh.reuse_detected`, `auth.password.changed`, `auth.password.reset_requested`, `auth.password.reset` |
| Пользователи и группы | `user.created`, `user.updated`, `user.disabled`, `user.deleted`, `group.created`, `group.updated`, `group.deleted`, `group.members.changed` |
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
  документов и ответы модели рендерятся как недоверенные данные.
- **CORS:** same-origin; при необходимости — явный allowlist origins, никогда `*` вместе с credentials.
- **Session theft:** HttpOnly cookies, короткий access token, rotation, reuse detection, отзыв сессий.

## 13. Rate limiting и resource exhaustion (§8 п.5, п.6)

- Rate limits для login, upload, chat и дорогих операций (§8 п.5); значения — [API_CONTRACTS.md](API_CONTRACTS.md), раздел 5 «Rate limits».
- Лимит одновременных генераций на пользователя и общий лимит на инсталляцию.
- Ограниченные DB pools, лимиты очередей, timeouts на всех внешних вызовах (§11).
- Ограничение длины сообщения чата, числа сообщений истории и context budget (§5, §6).

## 14. Логи и privacy (§11)

- Запрещено в обычных логах (§11): содержимое документов и chunks, prompts и ответы модели, токены, пароли,
  API keys, cookies, заголовки `Authorization`, OAuth codes.
- Ошибки внешних SDK санитизируются: они могут содержать фрагменты запроса или ключа.
- В логах — идентификаторы (`user_id`, `organization_id`, `job_id`, `correlation_id`), а не email/имена, где возможно.
- Уровень DEBUG в production не включает payload-логирование.

## 15. OAuth-коннекторы (§7, §13 Фазы 9, 10)

- `state`: случайный, одноразовый, привязан к сессии и организации, TTL ≈ 10 мин; проверяется в callback.
- PKCE (S256) — обязательно для Microsoft Entra ID (§7), **[Proposed]** и для Google.
- Redirect URI — точное совпадение, только HTTPS (кроме localhost в dev).
- Refresh tokens шифруются (§13 Фаза 9); access tokens провайдеров не сохраняются или сохраняются только зашифрованными с коротким сроком.
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
| Cross-tenant leakage | Tenant из сессии, обязательный фильтр, составные FK, tenant в job payload, префиксы Redis/storage, (RLS) | Тесты двух организаций для каждого endpoint/job |
| RBAC bypass | Policy в каждом use case, deny by default, 404/403, frontend не boundary | Negative RBAC tests, тесты комбинаций permissions (§13 Фаза 7) |
| Утечка API keys / OAuth tokens | Шифрование, ключ вне БД, write-only API, redaction, отсутствие в prompt | Тесты: secrets не возвращаются и не логируются |
| SSRF | Валидация URL и IP, запрет private/metadata, allowlist уровня инсталляции, pinning | SSRF-тесты (§12) |
| Malicious uploads / parser exploits | Quarantine, magic bytes, лимиты, изолированный парсер, timeout | Upload/parser tests (§13 Фаза 12) |
| Archive bombs | Лимиты распаковки ZIP-контейнеров | Archive bomb tests |
| Prompt injection | Недоверенный контент, разделение инструкций и контекста, нет tools, валидация цитат, безопасный рендеринг | Adversarial documents (§13 Фаза 6) |
| XSS | Экранирование, CSP, запрет сырого HTML | XSS tests |
| CSRF | Double-submit + Origin, SameSite | CSRF tests |
| Session theft | HttpOnly/Secure, короткий access, rotation, reuse detection | Тесты revoked sessions (§12) |
| Resource exhaustion | Rate limits, лимиты upload/парсинга/генераций, pools, timeouts | Performance tests (§12) |
| Утечки через логи | Запрет payload-логирования, redaction | Проверка логов в тестах |
| Ошибки удаления | Немедленное исключение из retrieval, purge jobs, lifecycle drills | Тест «deleted docs не находятся» (§12) |
| Захват неинициализированной инсталляции | Bootstrap token, атомарная инициализация, флаг без сброса через API, CLI-восстановление (ADR-006) | SC-03, SC-04 |
| Обход сетевой политики администратором организации | Allowlist только в Instance Settings, нет API изменения (ADR-007) | SC-02 |
| Доступ к ассистенту без доступа ко всем его KB | ADR-005, проверка на каждом запросе | SC-01 |
| Чрезмерные OAuth permissions | Минимальные scopes, честное отображение, исследование Selected permissions | Документация и ревью перед production |

## 18. Обязательные security-тесты (§12)

Негативные тесты обязательны:

- [ ] чужой tenant / KB / document недоступен;
- [ ] unauthorized chunks не попадают в LLM;
- [ ] revoked sessions отклоняются;
- [ ] secrets не возвращаются;
- [ ] malicious filenames не выходят из storage;
- [ ] повторные jobs не дублируют chunks;
- [ ] deleted docs не находятся;
- [ ] выдуманные citation IDs отклоняются.

Плюс категории (§12): tenant isolation, RBAC, SSRF, uploads, prompt injection; recovery: backup restore, worker
restart, interrupted sync.

### 18.1 Контрольные сценарии

Обязательны для реализации и тестирования соответствующих фаз ([DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md)).

| ID | Сценарий | Ожидаемое поведение | Решение | Фаза |
|---|---|---|---|---|
| SC-01 | Пользователь имеет доступ к KB A, но не к KB B; обе подключены к одному ассистенту | Ассистента нет в списке; `GET /assistants/{id}` → `404`; `POST /chat/stream` → `404` (или `403 assistant_unavailable` в собственном старом диалоге); ни один chunk из A или B не уходит в LLM | ADR-005 | 7–8 |
| SC-02 | Администратор организации пытается расширить сетевой allowlist | API изменения allowlist отсутствует (`404/405`); `PATCH /organizations/current/settings` с сетевыми полями → `422`; провайдер с непозволенным `base_url` → `422 endpoint_not_allowed` | ADR-006, ADR-007 | 3–4 |
| SC-03 | Злоумышленник повторно вызывает первоначальную настройку | После инициализации `POST /setup/initialize` → `409 setup_already_completed` при любом токене; до инициализации без валидного токена → `403 setup_token_invalid`; событие `setup.initialize.rejected` | ADR-006 | 3 |
| SC-04 | Два запроса одновременно создают первого администратора | Ровно один `201`; второй ждёт блокировку и получает `409`; в БД одна организация и один первый администратор | ADR-006 | 3 |
| SC-05 | Пользователь запрашивает документ другого tenant напрямую по ID (карточка, viewer, chunk, reindex, delete) | `404 not_found` для всех методов; данные и факт существования не раскрываются | ADR-004, ADR-008 | 2, 5 |
| SC-06 | Локальный Ollama доступен по явно разрешённому адресу | Провайдер `ollama` с `base_url`, совпадающим с записью allowlist, проходит connection test и используется в чате/embeddings; тот же адрес для другого назначения или организации вне записи → отказ | ADR-007 | 4 |
| SC-07 | Разрешённый hostname после DNS-разрешения указывает на запрещённый адрес (вне `allowed_cidrs`, metadata, loopback) | Соединение не устанавливается; `egress_denied`; `network.egress.denied` в audit; повторного DNS-разрешения нет | ADR-007 | 4 |
| SC-08 | Внешний документ содержит prompt injection | Инструкции из документа не меняют правила; модель без tools; выдуманные citation IDs отклоняются; внешние изображения в ответе не загружаются; секреты в prompt отсутствуют | §10, ADR-008 | 6 |
| SC-09 | Повторная задача ingestion для уже обработанной версии | Job завершается `succeeded` с `result = skipped_already_processed`; число chunks и embeddings не меняется; при гонке двух jobs дубликатов нет (unique + lock) | ADR-008 | 5 |

## 19. Процессы (§12, §13 Фаза 12, §20)

- **Definition of Done** включает security review (§12).
- **Dependency и container scanning** в CI (§13 Фаза 12); инструменты — **[Proposed]**: pip-audit, npm audit /
  Dependabot, Trivy.
- **Security reporting:** `SECURITY.md` с процессом ответственного раскрытия (§4, §13 Фаза 13).
- **Перед релизом** (§20): нет известных critical/high issues; tenant isolation и permission tests проходят; secrets
  не попадают в logs/API responses; upload/parser limits и ingestion idempotency работают; deleted docs исключены
  из retrieval; citations валидируются backend; backup/restore проверены.
- Threat model review и tenant/RBAC audit — Фаза 12 и при значимых изменениях.

## 20. Открытые вопросы

1. Auth/session strategy: TTL, формат access token, точная CSRF-схема (планируемый ADR).
2. Алгоритм и ротация ключа шифрования секретов (планируемый ADR).
3. RLS как второй рубеж (планируемый ADR).
4. Antivirus-сканирование загрузок.
5. CSP для стилей Chakra UI (nonce vs `unsafe-inline`).
6. Нужно ли аудировать просмотр документов и чтение аудита.
7. Срок действия bootstrap token и процедура повторной генерации.

Решено: сетевой allowlist и локальные модели ([ADR-007](adr/0007-network-access-and-local-llm.md)), bootstrap
([ADR-006](adr/0006-initial-setup-and-system-configuration.md)), доступ к ассистентам
([ADR-005](adr/0005-assistant-access-model.md)).
