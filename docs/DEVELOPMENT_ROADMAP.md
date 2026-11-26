# PlummyKnowledge — Development Roadmap

| | |
|---|---|
| Статус | v0.1 |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §13, §19, §21, §22 |
| Решения | [ADR-001…019](adr/README.md) |
| Связанные документы | [PRODUCT_SPEC.md](PRODUCT_SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md) |

Последовательность фаз 0–13 взята из blueprint без изменений. Документ добавляет к каждой фазе решения ADR-005…008,
security gate (проверки безопасности **внутри** фазы) и критерии выхода. Фаза 12 — это итоговый аудит и hardening,
а не первая проверка безопасности.

## 1. Принципы последовательности

1. Сначала работающий end-to-end сценарий, затем расширение (§22 п.1). Ядро: Docker Compose → login → provider config →
   upload PDF → parse → chunk → embed → retrieve → streamed answer → verified citation (§22).
2. Bootstrap и авторизация готовы **до** полноценного пользовательского сценария (Фазы 2–3).
3. Локальная загрузка — **первый** источник документов (Фаза 5).
4. Единый ingestion pipeline ([ADR-008](adr/0008-unified-document-ingestion.md)) появляется **до** Google Drive и
   Microsoft 365 (Фаза 5 < Фазы 9–10).
5. Полноценный RAG работает **до** сложных внешних интеграций (Фазы 6–8 < Фазы 9–10).
6. Безопасность проверяется на протяжении всей разработки: каждая фаза закрывается своим security gate.
7. Не начинать Google OAuth, Microsoft Graph и сложный RAG до стабильного запуска каркаса (§21).
8. Каждая фаза соответствует Definition of Done (§12, [../CLAUDE.md](../CLAUDE.md)).

## 2. Фазы

### Фаза 0 — Product discovery (§13)

- Vision, target users, MVP/out-of-scope — [PRODUCT_SPEC.md](PRODUCT_SPEC.md).
- Repository, README, roadmap, ADR index — [adr/README.md](adr/README.md).
- User journeys и threat model — [SECURITY_MODEL.md](SECURITY_MODEL.md).
- **Выход:** спецификации согласованы, ADR-001…008 Accepted. *(Текущее состояние: документация готова; README,
  NOTICE и GitHub Project — первая рабочая сессия §21.)*

### Фаза 1 — Repository и developer experience (§13)

- Monorepo и lockfiles; Dockerfiles и Compose: PostgreSQL+pgvector, Redis, backend, worker, frontend.
- Healthchecks, Pydantic Settings (основа Instance Settings, ADR-006), Alembic, `.env.example` без секретов.
- Ruff, pytest, mypy/pyright; ESLint, Prettier, strict TypeScript, Vitest.
- GitHub Actions, pre-commit, issue/PR templates; проверка `scripts/version.py check` в CI; версия приложений из
  `VERSION` ([ADR-009](adr/0009-automatic-versioning.md)).
- **Security gate:** нет секретов в репозитории и `.env.example`; ошибки конфигурации останавливают старт; порты
  БД/Redis не публикуются наружу; dependency scanning в CI (раннее включение пункта Фазы 12).
- **Выход:** `docker compose up` поднимает сервисы, health/readiness зелёные, CI проходит.
- *(Текущее состояние: фаза выполнена. Сессии 2–5: backend, frontend, PostgreSQL+pgvector, Redis, Alembic, Compose,
  Ruff/mypy/pytest, ESLint/Prettier/Vitest, версия из `VERSION`, Celery worker с задачей `plummy.system.ping`
  (retry с backoff, журнал жизненного цикла без payload, `plummy.cli worker check`). GitHub Actions
  (`.github/workflows/ci.yml`): тесты на Compose-стеке, ruff, mypy, frontend-проверки, `version.py check`, secret
  scan, `pip-audit`, `npm audit`, dependency review в PR; Dependabot; pre-commit в `.githooks` (secret scan и lint
  staged-файлов); шаблоны issue/PR. Сознательно отложено: Celery beat и разделение очередей — с первыми задачами
  ingestion (Фаза 5).)*

### Фаза 2 — Core backend и data model (§13)

- Async SQLAlchemy session lifecycle и transaction boundaries.
- User, Group, Role, Permission и связи; системные роли и permissions — миграцией. Сущности Organization нет:
  инсталляция принадлежит одной организации ([ADR-012](adr/0012-single-organization-deployment.md)).
- `instance_state` и CLI `python -m plummy.cli admin create` — серверный способ создать первого администратора
  (ADR-006); остаётся процедурой восстановления.
- Query patterns с фильтрами доступа, audit events, migrations.
- **Предварительно:** ADR-012 (Accepted).
- **Security gate:** тесты «чужие данные недоступны» для первых ресурсов; SC-05 — с появлением документов (Фаза 5).
- **Выход:** модели и миграции проверены на чистой БД и upgrade path.
- *(Текущее состояние: **фаза завершена.** Этапы «Bootstrap & Authentication» и «Users & RBAC» реализовали
  `users`, `user_activation_tokens`, `groups`, `user_groups`, `roles`, `permissions`, `role_permissions`,
  `user_roles`, `group_roles`, `sessions`, `instance_state`, `audit_logs` (миграции `0002`–`0006`, проверены на
  чистой БД и upgrade/downgrade), UnitOfWork, CLI, cursor-пагинацию ([ADR-014](adr/0014-list-pagination.md)) и
  тесты «чужие данные недоступны» для сессий пользователей.)*

### Фаза 3 — Authentication/authorization и bootstrap (§13, ADR-006)

- Password hashing (Argon2id), login/logout, серверные сессии с отзывом и сроками, rate limiting
  ([ADR-013](adr/0013-auth-session-strategy.md)); управление своими сессиями и смена пароля.
- Permission policy service и проверки в use cases; CSRF/cookie strategy.
- Группа `setup`: bootstrap token, `GET /setup/status`, `POST /setup/initialize`, режим `cli_only`.
- Instance Settings только на чтение (`GET /system/settings`).
- Управление пользователями, группами и ролями; создание пользователей ссылкой активации
  ([ADR-015](adr/0015-user-activation-links.md)); защита последнего администратора.
- **Предварительно:** ADR-013 (Accepted).
- **Security gate:** negative RBAC tests; revoked sessions отклоняются; SC-03, SC-04; тела setup-запросов не
  логируются.
- **Выход:** первый администратор создаётся только с bootstrap token или CLI; защищённый endpoint работает.
- *(Текущее состояние: **фаза завершена.** Реализованы bootstrap token и CLI (`setup init-token`, `admin create`,
  `admin reset-password`), `GET /setup/status`, `POST /setup/initialize`, режим `cli_only`, login/logout/me,
  сессии, CSRF, лимиты входа, `SecurityContext.require`, `require_permission`; свои сессии
  (`GET/DELETE /auth/sessions`), смена пароля, активация (`POST /auth/activate`), `GET /system/settings`;
  API пользователей, групп и ролей (эффективные права = прямые роли ∪ роли групп); frontend: setup, login, guards,
  `/account`, `/activate`, `/admin/users`, `/admin/groups`. Тесты: SC-03, SC-04, revoked sessions, negative RBAC,
  последний администратор (в том числе параллельно), одноразовость активации.
  **Перенесено:** Application Settings (`GET/PATCH /settings`) и SC-02 в части Application Settings — вместе с
  первыми настройками, которые в них появятся (лимиты чата, retention), не позднее Фазы 7; сброс пароля по email —
  с появлением email-инфраструктуры.)*

### Фаза 4 — AI provider abstraction и сетевая политика (§13, ADR-007)

- `LLMProvider`, `EmbeddingProvider`, capabilities, typed errors.
- **`SafeHttpClient`** и network allowlist в Instance Settings — до первого адаптера.
- Адаптеры OpenAI, Anthropic, Ollama, OpenAI-compatible — через одни и те же интерфейсы.
- Connection tests, encrypted secrets, timeout/retry/cancellation; contract tests.
- **Предварительно:** ADR «Шифрование секретов» — [ADR-016](adr/0016-secret-encryption.md) (Accepted).
- **Security gate:** SSRF-тесты по каждому классу адресов и альтернативным записям IP; SC-02 (`endpoint_not_allowed`),
  SC-06, SC-07; секреты не возвращаются и не логируются.
- **Выход:** облачный и локальный (Ollama через allowlist) провайдеры проходят connection test и contract tests.
- *(Текущее состояние: **фаза реализована.** Секреты: AES-256-GCM, key ring вне БД (`SECRETS_KEYRING_FILE`), CLI
  `secrets init-keyring | generate-key | status | rotate`, проверка readiness ([ADR-016](adr/0016-secret-encryption.md),
  миграция `0007`). Сеть: `[[network.allowlist]]` в `CONFIG_FILE`, `SafeHttpClient` с pinning проверенного IP,
  audit `network.egress.denied`. Порты `LLMProvider`/`EmbeddingProvider` с typed errors, capabilities,
  retry/timeout/cancellation; адаптеры OpenAI, Anthropic, Ollama, OpenAI-compatible на `httpx` без SDK. API
  `providers`, моделей и `embedding-profiles` (миграция `0008`), connection test с лимитом `provider_test`;
  frontend `/admin/providers`, `/admin/providers/:id`, `/admin/embedding-profiles`. Тесты: SSRF по классам адресов и
  записям IP, SC-02, SC-06, SC-07, секреты не возвращаются; contract tests адаптеров — против локальных
  fake-серверов. Живые проверки с реальными провайдерами и Ollama — `LIVE_PROVIDER_TESTS=1` с ключами, в CI не
  запускаются.)*

### Фаза 5 — Local document ingestion (§13, ADR-008)

- `StorageProvider` и local storage; quarantine upload.
- Document Source `upload` (создаётся с KB); `Document`/`DocumentVersion`; file validation.
- `ParserProvider` + Docling, normalized document structure.
- `ChunkingStrategy`, Celery tasks, idempotency/retry; embeddings, pgvector, job status, reindex/delete.
- Все 12 стадий единого pipeline; стадии 1–2 сразу спроектированы под коннекторы.
- Tests PDF/DOCX/XLSX/TXT/CSV/MD.
- **Предварительно:** ADR «Хранение векторов».
- **Security gate:** лимиты размера/страниц/распаковки, archive bombs, malicious filenames, изоляция парсера; SC-05
  для документов; SC-09; deleted docs не находятся.
- **Выход:** загруженный PDF/DOCX проходит pipeline до `ready`; повторы не создают дубликатов.
- *(Текущее состояние: **фаза реализована** (5.0–5.7). Хранение векторов — [ADR-017](adr/0017-vector-storage.md);
  миграции `0009` (KB, источники, документы, версии, chunks, embeddings, `ingestion_jobs`) и `0010`. Local storage с
  областями `quarantine`/`documents`, ключи генерирует сервер. Upload с потоковой проверкой размера и rate limit;
  стадии 2–7 — в `worker-ingestion` (отдельный образ с Docling, без сети и секретов, parser в дочернем процессе с
  timeout/rlimits), стадии 8–12 — в `worker` (structure-aware chunking 512/64, embeddings через `EmbeddingProvider`,
  атомарное переключение версии). API KB (list/create/get), documents (list/upload/get/versions/delete/reindex),
  jobs (list/get/retry); purge сразу после удаления; Celery beat `scheduler` (cleanup quarantine, requeue,
  purge sweep). Frontend `/knowledge-bases`, `/knowledge-bases/:id`. Parsing проверен с реальным Docling
  (`tests/ingestion`, образ `worker-ingestion`), стадии 8–12 — integration-тестами на PostgreSQL с fake embedder;
  прогон до `ready` с реальным embedding-провайдером не выполнен. Не реализовано в фазе: `Idempotency-Key` upload, новые версии существующего
  документа через upload, лимит объёма загрузок в час, cancel jobs, API chunking settings, grace period purge.)*

### Фаза 6 — Retrieval и RAG evaluation (§13)

- Vector search и PostgreSQL full-text search; hybrid fusion (RRF); KB/source/version filters; deduplication.
- Bounded context assembly и citation ID validation.
- Evaluation dataset (30–50 документов, 50–100 вопросов) и метрики Recall@K/citation precision.
- Prompt injection test cases.
- **Предварительно:** ADR «FTS-конфигурация».
- **Security gate:** unauthorized chunks не попадают в LLM; выдуманные citation IDs отклоняются; SC-08.
- **Выход:** метрики evaluation зафиксированы как baseline.
- *(Текущее состояние: **фаза реализована** (6.0–6.6). FTS — [ADR-018](adr/0018-full-text-search-configuration.md)
  (поправка §3 по итогам baseline, утверждена: совпадение по любому слову, `ts_rank`), миграции `0011`/`0012`. Hybrid
  retrieval (`RetrieveChunks`): авторизация KB до embedding, фильтры §5.11 в SQL обеих веток, RRF, dedup, бюджет;
  context assembly и citation IDs `c1…cn` с потоковой валидацией; SC-08 — детерминированные тесты prompt layout.
  `POST /knowledge-bases/{id}/search` и поиск на странице KB. Evaluation: набор `synthetic-v1` (36 документов,
  87 вопросов), Recall@K/MRR/nDCG, `python -m plummy.cli eval run`; baseline на `bge-m3` (Ollama) —
  [docs/evaluation/baseline-2026-11.md](evaluation/baseline-2026-11.md): vector R@10 0,99 / MRR 0,89, hybrid
  R@10 0,99 / MRR 0,81, keyword R@10 0,94 / MRR 0,70. **Отклонения от плана:** citation precision и прочие
  метрики генерации перенесены в harness Фазы 8 (нужны ответы LLM); равновесный RRF уступал vector в top-1 —
  после перебора fusion взвешена (vector 1,0, keyword 0,1): hybrid R@1 0,82 / MRR 0,90, на уровне vector; rate limit поиска — новый профиль `search`
  (30/мин на пользователя, утверждён 2026-11-25).)*

### Фаза 7 — Knowledge Bases и Assistants (§13, ADR-005)

- CRUD KB, sources, KB-level access.
- CRUD Assistant, instructions, model/retrieval profiles.
- Assistant Access Model: доступ ко всем KB ассистента; ассистент без KB — только `assistants.manage`.
- Проверки доступа и тесты комбинаций permissions.
- **Security gate:** SC-01; отзыв доступа к KB (прямого и через группу) немедленно скрывает ассистента.
- **Выход:** матрица комбинаций «пользователь × KB × ассистент» покрыта тестами.
- *(Текущее состояние: **фаза реализована** (7.1–7.5). Управление KB: `PATCH` (имя, описание; смена embedding
  profile → `422 embedding_profile_change_not_supported`), `DELETE` с purge (только `knowledge_bases.manage`),
  `GET`/`PUT /access` с audit и защитой от потери собственного `manage`, `POST /reindex`. Миграция `0013`
  (`assistants`, `assistant_knowledge_bases`); ассистенты: домен, политика доступа ADR-005 (`application/assistants/
  access.py`; список для чата фильтруется в SQL, карточка — через политику), CRUD `/assistants` с `availability`;
  все KB ассистента используют его embedding profile. Frontend: настройки и доступ на странице KB,
  `/admin/assistants`, `/assistants` (без чата). Security gate: `tests/integration/test_assistant_access.py` —
  матрица (прямой доступ, группа, оба пути, `knowledge_bases.manage`, без доступа, без `chat.use`, ассистент без KB,
  `disabled`), SC-01, немедленный отзыв (прямой grant, grant группы, выход из группы, удаление группы, снятие роли,
  удаление KB, отключение ассистента); каждый тест сверяет список с карточками. **Отложено:** предупреждение о
  пользователях, теряющих доступ при добавлении KB, `acl_acknowledged`, смена profile у KB, `Idempotency-Key`.)*

### Фаза 8 — Chat UI и streaming (§13)

- Login/setup/chat/history UI; setup wizard (ADR-006).
- SSE client и typed events, stop generation.
- Citation display и protected in-app viewer.
- Loading/error/empty states, accessibility, E2E.
- **Предварительно:** ADR «Генерация в API или worker» — [ADR-019](adr/0019-answer-generation-in-api-process.md)
  (генерация в процессе API).
- **Security gate:** XSS-тесты рендеринга ответа (Markdown без сырого HTML, без автозагрузки внешних изображений);
  CSRF; viewer повторно проверяет доступ; E2E для SC-01.
- **Выход:** вертикальный сценарий §22 работает end-to-end → **релиз 0.1.0** (вместе с минимальным open-source
  пакетом, см. §3).
- *(Текущее состояние: **фаза реализована** (8.0–8.7); релиз **0.1.0** (тег `v0.1.0`) выпущен по решению владельца вместе с
  минимальным open-source пакетом: README с quick start, LICENSE, NOTICE, CONTRIBUTING, SECURITY. ADR-019: генерация в
  процессе API, Redis-семафор (2 генерации на пользователя), stop-флаг, жёсткий таймаут, heartbeat, разрыв
  соединения сохраняет ответ как `stopped`; миграция `0014` (диалоги, сообщения, цитаты, generation runs).
  API: `/conversations` (список, карточка, история, rename, delete с purge), `POST /chat/stream` (SSE, ADR-005 на
  каждом сообщении, потоковый фильтр citation IDs), `POST /chat/runs/{id}/stop`, viewer
  `GET /documents/{id}/versions/{vid}/chunks/{cid}/view` (±2 фрагмента, повторная проверка доступа), sweep
  зависших ответов в `scheduler`. Frontend: `/chat` (fetch + `ReadableStream`, Stop, карточки цитат, история),
  `/viewer`, ссылка «Chat», «Start chat» на странице ассистентов; Markdown ответа без сырого HTML и без загрузки
  изображений. E2E: `apps/e2e` (Playwright) на `compose.e2e.yaml` с фейковым OpenAI-compatible провайдером,
  job CI «E2E». Security gate: XSS — Vitest `AnswerMarkdown.test.tsx`/`viewer.test.tsx`; CSRF/Origin, `no-store`,
  отсутствие текста диалога в логах — `test_chat_security_gate.py`; viewer — `test_source_viewer.py`; SC-01 из
  браузера — `apps/e2e/tests/access.spec.ts`. **Отложено:** метрики качества генерации (citation precision,
  groundedness) — нужен отдельный harness с настоящей моделью; `Idempotency-Key`; audit `document.viewed` и
  отдельный rate limit viewer; формальный аудит accessibility; CSP и Nginx — Фаза 11. Snippet цитаты и имя
  удалённого ассистента остаются в истории диалога после потери доступа (сам фрагмент открывается только при
  доступе).)*

### Фаза 9 — Google Drive (§13)

- Scopes и verification requirements; OAuth state validation, encrypted refresh tokens, folder picker.
- Recursive listing, download/export, initial sync; Changes API cursor, incremental sync, rename/move/delete.
- Throttle/backoff, revoke, reconciliation, tests и docs.
- Коннектор только реализует `DataSourceConnector`; документы идут в общий pipeline (ADR-008).
- Решения — [ADR-020](adr/0020-google-drive-connector.md). Шаги: 9.0 ADR и спецификации; 9.1 схема (`0015`), домен,
  redirects в `SafeHttpClient`; 9.2 OAuth и API `data-sources`; 9.3 адаптер Google Drive; 9.4 синхронизация и
  расписание (`0016`, `POST /data-sources/{id}/sync`, очередь `connectors`); 9.5 UI, E2E и security gate. Выполнены
  9.0–9.5.
- **Security gate:** OAuth state/PKCE; честное отображение scopes; redirects загрузки проверяются `SafeHttpClient`;
  SC-08 на внешних документах; удаление в источнике отражается в индексе.
  *(Выполнено: state/PKCE, сверка сессии и одноразовость — `test_data_sources_api.py`, фейковый Google проверяет
  client secret, redirect URI и PKCE; фактические scopes и предупреждение — UI и `sources.test.tsx`; redirects
  загрузки — `test_safe_http_redirects.py`, `test_google_drive.py`; SC-08 на документе из Drive (враждебные имя и текст остаются
  экранированными данными, невыданные citation IDs отклоняются) — `test_source_sync.py`; удаление в Drive убирает
  документ из поиска — `test_source_sync.py` и E2E `drive.spec.ts` (OAuth в браузере → папка → sync → поиск →
  удаление → sync), там же проверено, что ни один выданный токен не попадает в страницу и ответы API.
  **Отложено:** отмена sync job через `/jobs/{type}/{id}/cancel`; dashboard jobs/sync — Фаза 11.)*
- **Выход:** **релиз 0.3.0**.

### Фаза 10 — Microsoft 365 (§13)

- Entra app registration и OAuth PKCE; delegated/app permissions и Selected permissions research.
- Site/drive/library/folder picker, Graph listing/download.
- Initial sync, Delta Query, changes, retries, revoke, tests.
- Решения — [ADR-021](adr/0021-microsoft-365-connector.md): delegated `Files.Read.All` + `Sites.Read.All`, один
  tenant, без SDK; отзыв у провайдера невозможен — disconnect локальный. Шаги: 10.0 ADR и спецификации; 10.1
  Instance Settings, OAuth Entra, обобщение портов и API; 10.2 адаптер Graph; 10.3 синхронизация через Delta Query;
  10.4 UI, E2E и security gate. Выполнены 10.0–10.1.
- **Security gate:** как в Фазе 9; предупреждение о широте `Files.Read.All`.
- **Выход:** **релиз 0.4.0**.

### Фаза 11 — Production deployment (§13)

- S3-compatible storage и MinIO; Compose profiles, Nginx TLS, production secrets/resource limits.
- Backup/restore, upgrades, retention, worker scaling; сегментация сетей и egress-фильтрация ([DEPLOYMENT.md](DEPLOYMENT.md)).
- Admin jobs/sync dashboard и troubleshooting.
- **Security gate:** backup/restore drill; проверка lifecycle удаления; ключ шифрования хранится отдельно от backup.

### Фаза 12 — Security hardening (§13)

- Threat model review и RBAC/KB-access audit.
- OAuth, SSRF, upload/parser, archive bomb и prompt injection tests — полный прогон SC-01…SC-09.
- XSS/CSRF/CORS, rate limits, logs, deletion lifecycle.
- Dependency/container scanning и security reporting.
- **Выход (вместе с Фазой 11):** **релиз 0.5.0**.

### Фаза 13 — Open-source release (§13)

- README, diagrams, quick start, configuration/deployment guides.
- Connector/provider development guides, backup/restore, troubleshooting.
- CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, LICENSE, NOTICE.
- CHANGELOG, SemVer, GitHub Release, demo, roadmap, Discussions.
- **Выход:** **релиз 1.0.0** — стабильные contracts и production readiness (§19).

## 3. Релизы (§19)

| Релиз | Содержание | Фазы |
|---|---|---|
| 0.1.0 | Docker Compose deployment, локальная аутентификация и KB-level авторизация, LLM-провайдеры, upload PDF/DOCX и асинхронная индексация, hybrid retrieval, streamed chat, проверяемые citations и in-app viewer, тесты изоляции KB и пользователей, базовый audit, документация и quick start | 0–8 + минимальный пакет Фазы 13 (README, LICENSE, NOTICE, CONTRIBUTING, SECURITY, quick start) |
| 0.2.0 | XLSX/CSV и RAG improvements | Доработка 5–6 |
| 0.3.0 | Google Drive | 9 |
| 0.4.0 | Microsoft 365 | 10 |
| 0.5.0 | Hardening, admin, backups | 11–12 |
| 1.0.0 | Стабильные contracts и production readiness | 13 |

Перед каждым релизом — контрольный список §20 blueprint и [SECURITY_MODEL.md §19](SECURITY_MODEL.md).

**Версии ([ADR-009](adr/0009-automatic-versioning.md)).** Между релизами каждый коммит автоматически повышает
`VERSION`: `0.1.0-dev.1`, `0.1.0-dev.2`, … Релиз выпускается командой `python3 scripts/version.py release`
(→ `0.1.0`, тег `v0.1.0`), после чего разработка продолжается с `0.2.0-dev.1`. Номера релизов остаются привязанными
к таблице выше. В Фазе 1 версия backend, frontend и Docker-образов берётся из `VERSION`, а CI проверяет её рост
(`python3 scripts/version.py check`).

## 4. Первые семь рабочих сессий (§21)

| # | Сессия | Фаза |
|---|---|---|
| 1 | GitHub repository, README, Apache 2.0 LICENSE, NOTICE, .gitignore, docs, GitHub Project | 0 |
| 2 | FastAPI backend, Pydantic Settings, logging, health endpoints, pytest, Ruff | 1 |
| 3 | React + TypeScript + Vite, Chakra UI, Redux Toolkit, RTK Query, router | 1 |
| 4 | PostgreSQL + pgvector и Redis через Compose; SQLAlchemy async и Alembic | 1 |
| 5 | Celery worker, тестовая задача, retry и наблюдаемое состояние (выполнено) | 1 |
| 6 | User, роли и permissions, миграции; `instance_state` и `plummy.cli admin create` (выполнено в этапе «Bootstrap & Authentication») | 2 |
| 7 | Минимальный auth flow и защищённый endpoint; обновить docs и зафиксировать milestone (выполнено в этапе «Bootstrap & Authentication») | 3 (начало) |

## 5. Контрольные сценарии по фазам

| Сценарий ([SECURITY_MODEL.md §18.1](SECURITY_MODEL.md)) | Фаза первого теста |
|---|---|
| SC-01 Нет доступа к одной из KB ассистента | 7 (API), 8 (E2E) |
| SC-02 Administrator приложения расширяет allowlist | 3–4 |
| SC-03 Повторная первоначальная настройка | 3 |
| SC-04 Параллельное создание первого администратора | 3 |
| SC-05 Документ KB без доступа по прямому запросу | 5 |
| SC-06 Локальный Ollama по разрешённому адресу | 4 |
| SC-07 Разрешённый hostname → запрещённый адрес | 4 |
| SC-08 Prompt injection во внешнем документе | 6, 9 |
| SC-09 Повторный ingestion обработанной версии | 5 |

## 6. Уточнения к blueprint

| Вопрос | Решение в этом roadmap | Требует подтверждения |
|---|---|---|
| Фаза 5 тестирует XLSX/CSV, а релиз 0.1.0 включает только PDF/DOCX, XLSX/CSV — 0.2.0 | Pipeline Фазы 5 принимает все форматы MVP; гарантированное качество structure-aware для XLSX/CSV (sheet/row) — в 0.2.0 | Да |
| Фаза 13 (open-source release) идёт после 0.1.0, хотя 0.1.0 — первый публичный релиз, а §17 советует публиковать skeleton рано | Минимальный open-source пакет выпускается с 0.1.0; Фаза 13 завершает его к 1.0.0 | Да |
| Web-setup в blueprint не привязан к фазе | Серверный bootstrap (CLI) — Фаза 2; web-setup — Фаза 3; wizard UI — Фаза 8 | Нет (ADR-006) |
