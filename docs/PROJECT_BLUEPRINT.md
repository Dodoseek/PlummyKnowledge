# PlummyKnowledge

**Полная спецификация продукта, архитектуры и пошаговый план разработки**

Версия 1.1 (изменения — ADR-012) • 17 ноября 2026 года • Open-source self-hosted AI Knowledge Base

> Источник: `PlummyKnowledge_Project_Blueprint_RU.docx`. Документ перенесён в Markdown без изменения содержания.
> Рабочие спецификации, выведенные из него: [PRODUCT_SPEC.md](PRODUCT_SPEC.md), [ARCHITECTURE.md](ARCHITECTURE.md),
> [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md), [API_CONTRACTS.md](API_CONTRACTS.md), [SECURITY_MODEL.md](SECURITY_MODEL.md),
> [DEVELOPMENT_ROADMAP.md](DEVELOPMENT_ROADMAP.md), [DEPLOYMENT.md](DEPLOYMENT.md), [../CLAUDE.md](../CLAUDE.md).
> Уточнения и решения, принятые после v1.0, фиксируются в [ADR](adr/README.md) и имеют приоритет над этим документом.
>
> **Изменение v1.1 ([ADR-012](adr/0012-single-organization-deployment.md)).** По решению владельца проекта инсталляция
> принадлежит **одной** организации и работает на её серверах или в её облаке. Multi-tenancy упразднена: сущности
> Organization и `organization_id` нет, роли Platform Owner и Organization Admin объединены в Administrator,
> Organization Settings стали Application Settings, «tenant isolation» заменена изоляцией данных по KB и пользователям.
> Затронутые места ниже изменены по смыслу и помечены «изменено ADR-012».

---

## 1. Назначение и видение

PlummyKnowledge — self-hosted платформа корпоративной базы знаний с AI-интерфейсом. Она подключает внутренние источники документов, индексирует их, предоставляет безопасный поиск и отвечает на вопросы сотрудников с проверяемыми цитатами на исходные материалы.

Документ является базовой спецификацией и roadmap. Архитектурные изменения следует фиксировать в Architecture Decision Records (ADR).

### Основные ценности

- **Data ownership:** документы, индексы, диалоги и настройки остаются под контролем владельца инсталляции.
- **Provider independence:** облачные и локальные LLM, отдельные embedding-модели.
- **Source-grounded answers:** цитаты проверяются backend, а не принимаются на веру от модели.
- **Least privilege:** пользователь получает доступ только к разрешённым базам знаний и документам.
- **Deployability:** воспроизводимый запуск локально и на собственном сервере.
- **Extensibility:** новые модели, хранилища и коннекторы добавляются через стабильные интерфейсы.

## 2. Продуктовые границы

### Целевая аудитория

- Малые и средние компании, которым нужна внутренняя AI-база знаний.
- Организации с требованиями к self-hosted/private deployment.
- Команды Google Workspace и Microsoft 365.
- Разработчики и интеграторы корпоративных AI-систем.

### Роли

- **Administrator** — пользователи, группы, провайдеры, источники и KB; видит настройки инсталляции (меняются на сервере). *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- **Knowledge Manager** — документы, базы знаний и синхронизация.
- **Member** — работа с разрешёнными ассистентами.
- **Auditor** — доступ к журналам аудита в рамках полномочий.

### Основные сценарии

1. Администратор разворачивает систему через Docker Compose и создаёт первого пользователя.
2. Настраивает LLM и embedding provider.
3. Загружает PDF/DOCX/XLSX или подключает Google Drive / Microsoft 365.
4. Система извлекает структуру, создаёт версии, chunks и embeddings.
5. Администратор создаёт Knowledge Base и Assistant.
6. Сотрудник задаёт вопрос; backend проверяет права, выполняет поиск и вызывает модель.
7. Пользователь получает потоковый ответ с цитатами и открывает защищённый фрагмент источника.
8. Администратор видит состояние синхронизации, ошибки и аудит.

Не включать в MVP: автономных агентов с действиями, визуальный workflow builder, billing, multi-region, SAML/SCIM, полное наследование внешних document ACL и сложный OCR.

## 3. Архитектурные решения

Базовый стиль — Modular Monolith. Микросервисы не нужны до появления доказанной потребности в независимом масштабировании, отдельных security boundaries или отдельных командах.

- **Backend:** Python, FastAPI, SQLAlchemy 2.x async, Pydantic v2, Alembic.
- **Database:** PostgreSQL + pgvector.
- **Jobs/cache:** Redis + Celery.
- **Frontend:** React 18, TypeScript, Vite, Chakra UI 2.8+, Redux Toolkit, RTK Query.
- **API:** REST; streaming: Server-Sent Events (SSE).
- **Parsing:** Docling за интерфейсом ParserProvider.
- **Storage:** local filesystem и S3-compatible abstraction (MinIO).
- **Deployment:** Docker, Docker Compose, Nginx.
- **Tests:** Pytest, Vitest, React Testing Library, Playwright.
- **License:** Apache License 2.0.

### Принцип зависимостей

- Domain не импортирует FastAPI, SDK OpenAI/Anthropic, Celery или Google SDK.
- Application layer реализует use cases и зависит от интерфейсов.
- Infrastructure реализует интерфейсы через SQLAlchemy, SDK, Redis и storage.
- API отвечает за HTTP и DTO; worker вызывает те же application use cases.
- Не дублировать бизнес-логику в routers и Celery tasks.

## 4. Логическая архитектура

Browser → Nginx/TLS → React → FastAPI. FastAPI взаимодействует с PostgreSQL/pgvector, Redis, object storage и Celery workers. Workers выполняют parsing, chunking, embeddings, source synchronization и cleanup. Внешние адаптеры общаются с Google Drive, Microsoft Graph и AI providers.

### Модули backend

- **core** — settings, logging, exceptions, security primitives.
- auth, users, groups, permissions. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- providers, assistants, conversations.
- knowledge_bases, documents, data_sources.
- ingestion, retrieval, synchronization, audit.
- integrations/ai, integrations/embeddings, integrations/storage, integrations/sources.
- **workers** — Celery tasks и orchestration.

### Рекомендуемая структура репозитория

plummy-knowledge/ → apps/backend, apps/frontend, packages/ai-providers, packages/document-processing, packages/retrieval, packages/connectors, deploy/{docker,compose,nginx}, docs/{architecture,development,deployment,integrations,security,adr}, scripts, .github/workflows, CLAUDE.md, README.md, LICENSE, NOTICE, CONTRIBUTING.md, SECURITY.md.

## 5. Функциональные требования

### Установка

- Docker Compose quick start, healthchecks, readiness, миграции и создание первого администратора.
- Pydantic Settings, .env.example без секретов, безопасные defaults.
- Setup wizard и понятная диагностика старта.

### Authentication и authorization

- Локальная аутентификация, Argon2id, короткий access token, refresh token rotation/revocation.
- Безопасные cookies HttpOnly/Secure/SameSite и CSRF strategy.
- Rate limiting login; password reset при наличии email infrastructure.
- OIDC — следующая фаза; SAML/SCIM позднее.
- Контекст пользователя (роли, права) устанавливается сервером из проверенной сессии. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- Backend проверяет permissions на каждом use case; frontend не является security boundary.

### AI providers

- CRUD конфигураций, connection test, model ID, endpoint и credentials.
- Отдельные LLMProvider и EmbeddingProvider интерфейсы.
- OpenAI, Anthropic, Ollama и OpenAI-compatible adapter.
- Секреты шифруются до записи в БД; ключ шифрования хранится вне БД.
- Смена embedding model/profile требует переиндексации.

### Knowledge Base, Documents, Assistants

- CRUD KB, привязка источников, доступ групп/пользователей, статусы индексации.
- Document versions, checksum, MIME, source ID, timestamps и processing status.
- Assistant: instructions, LLM, embedding/retrieval profile, доступные KB и context limits.
- Удаление документа должно исключать его из retrieval и очищать производные данные по retention policy.

### Chat

- Диалоги и сообщения, SSE streaming, остановка генерации.
- События: `message_start`, `token`, `citation`, `message_complete`, `error`, `heartbeat`.
- Цитаты и защищённый in-app source viewer; не полагаться на внешнюю ссылку Drive/SharePoint.

## 6. RAG и обработка документов

### Ingestion pipeline

1. Upload/source discovery.
2. Download в quarantine/temp storage.
3. Проверка типа, размера, checksum и security limits.
4. Parsing и нормализация структуры.
5. Извлечение metadata и создание DocumentVersion.
6. Structure-aware chunking с сохранением page/section/sheet/row.
7. Генерация embeddings по выбранному profile.
8. Сохранение chunks и vectors.
9. Обновление статуса и job metadata.

Все долгие операции выполняются в Celery. Повторный запуск должен быть идемпотентным. Поддерживаемые форматы MVP: PDF, DOCX, XLSX, TXT, CSV, Markdown.

### Retrieval

- Vector search через pgvector.
- Keyword search через PostgreSQL full-text search.
- Hybrid fusion (например, Reciprocal Rank Fusion).
- Фильтрация по KB, документам и версиям до передачи контекста LLM. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- Top-k, context budget, deduplication; reranker — следующая фаза.

### Citations

Backend создаёт временные citation IDs для реально найденных chunks. Модель может сослаться только на них; backend валидирует каждый ID. Metadata: document/version, chunk, filename, page, section, sheet/row, snippet и internal viewer route.

### Оценка качества

- Собрать 30–50 репрезентативных документов и 50–100 вопросов.
- Включить вопросы без ответа, похожие документы и конфликтующие версии.
- Измерять Recall@K, MRR/nDCG, citation precision/coverage и groundedness.
- Проверять корректный отказ, когда источники не содержат ответа.

## 7. Коннекторы

### Общий интерфейс

DataSourceConnector должен предоставлять `validate_connection`, `list_files`, `download_file`, `get_changes` и `get_metadata`. Реальный контракт включает pagination, cancellation, retries, rate limits, typed errors, ETag/version и capabilities.

### Google Drive

- OAuth, folder picker, recursive listing, download/export, initial sync.
- Incremental sync через Changes API и устойчивое хранение cursor/page token.
- Обработка rename, move, delete, quota, retries, revoke/disconnect.
- **Критично:** выбор папки в UI не ограничивает OAuth token этой папкой. Application-level filtering не равно OAuth-level isolation.
- До production изучить scopes, Google verification, Workspace delegated access и минимальные полномочия; честно отображать реальные scopes.

### Microsoft 365

- Entra ID OAuth auth code flow + PKCE.
- Выбор SharePoint site, drive, document library или папки.
- Graph listing/download, initial sync, Delta Query.
- Обработка move/delete/rename, throttling, retries и revoke.
- `Files.Read.All` и подобные широкие разрешения не ограничиваются выбранной папкой; отдельно исследовать Selected permissions.

### Синхронизация

- Job per source, cursor/checkpoint, lock от параллельного sync.
- Backoff, retry, dead-letter/error state, ручной запуск и расписание.
- Периодический reconciliation для выявления пропущенных изменений.
- Удаление/изменение remote file корректно отражается в индексе.

## 8. Безопасность и privacy

### Threat model

- Утечка данных между KB и пользователями и RBAC bypass. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- Утечка API keys/OAuth tokens.
- SSRF, malicious uploads, parser exploits, archive bombs.
- Prompt injection в документах.
- XSS/CSRF/session theft, resource exhaustion и утечки через логи.
- Ошибки удаления и чрезмерные OAuth permissions.

### Обязательные меры

1. Проверка доступа к данным (KB, личные данные пользователя) в каждом API request и background job. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
2. Authorization до retrieval и до передачи текста LLM.
3. Шифрование credentials at rest; ключ вне БД; secret redaction.
4. TLS, secure cookies, CSRF strategy, security headers.
5. Rate limits для login, upload, chat и дорогих операций.
6. Лимиты размера, страниц, архивов, времени парсинга и ресурсов worker.
7. SSRF controls, запрет доступа к metadata/private endpoints по умолчанию.
8. Quarantine/temp storage с ограниченными правами и cleanup.
9. Audit log для критичных действий.
10. Backup/restore drills и проверка lifecycle удаления.

### Prompt injection

Документы всегда считаются недоверенным вводом. Их текст не может переопределять system policies. В MVP не предоставлять модели инструменты для произвольных действий; не помещать secrets в prompt; отделять инструкции от retrieved context; тестировать adversarial documents.

### Document ACL

MVP поддерживает доступ на уровне Knowledge Base. Если в одной KB лежат документы с разными внешними ACL, нельзя импортировать их всем участникам KB. До реализации document-level ACL использовать только источники, где все участники KB имеют право видеть все документы. Позже добавить ACL sync, identity mapping, inheritance и propagation of revocations.

### Retention

Определить lifecycle и сроки для original files, extracted text, versions, chunks, embeddings, chat history, audit logs и credentials. Soft delete не должен оставлять документ доступным для retrieval. Документировать влияние backup retention.

## 9. Модель данных

- **Identity:** User, Group, UserGroup, Role, Permission, RolePermission, UserRole/GroupRole, Session. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- **AI:** AIProvider, EmbeddingProviderConfig, ModelConfig, SecretReference.
- **Knowledge:** KnowledgeBase, KnowledgeBaseAccess, Assistant, AssistantKnowledgeBase, DataSource, DataSourceCredential, KnowledgeBaseSource, Document, DocumentVersion, DocumentChunk, Embedding.
- **Chat:** Conversation, Message, MessageCitation, GenerationRun.
- **Jobs/audit:** IngestionJob, SyncJob, AuditLog, позднее UsageEvent.

Использовать UUID, foreign keys, индексы по status/source_id/document_id/timestamps *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*, unique constraints для external IDs внутри source. Исключать deleted/failed/outdated versions из retrieval. Каждая vector row должна быть связана с embedding profile.

## 10. API и frontend

API versioning: `/api/v1`. Использовать Pydantic DTO, единый формат ошибок, pagination, allowlisted filters/sorting, idempotency keys для чувствительных повторяемых операций. Не отдавать ORM entities напрямую.

Группы endpoints: auth, users, groups, roles, providers, embedding-profiles, data-sources, knowledge-bases, documents, assistants, conversations, chat/stream, jobs, audit, health; дополнительно setup, settings, system (ADR-006). *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*

Для каждого endpoint документировать назначение, permission, scope (`auth`/`public`), schemas, ошибки, side effects, idempotency, audit event и rate limits.

Frontend sections: Login/Setup, Dashboard, Chat, Assistants, Knowledge Bases, Documents, Data Sources, Providers & Models, Users & Groups, Roles & Permissions, Jobs/Sync, Audit, System Settings.

## 11. Нефункциональные требования

- Идемпотентные jobs, retry/backoff, отсутствие долгих операций в HTTP workers.
- Измерять API latency, retrieval latency, time-to-first-token, ingestion throughput, queue depth, worker memory и DB latency.
- Независимый запуск API и worker; регулируемое число workers; ограниченные DB pools.
- Структурированные logs, correlation IDs, job IDs, health/readiness и метрики.
- Не писать содержимое документов, токены и приватные prompts в обычные логи.

## 12. Тестирование и Definition of Done

- **Unit:** permissions, chunking, citation validation, domain rules.
- **Integration:** PostgreSQL/pgvector, Redis, storage, provider adapters.
- **Contract:** AI providers и connectors.
- **E2E:** основные пользовательские сценарии.
- **Security:** изоляция KB и пользователей, RBAC, SSRF, uploads, prompt injection. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- **Performance:** retrieval, ingestion, concurrent chat.
- **Recovery:** backup restore, worker restart, interrupted sync.

Негативные тесты обязательны: KB/document без доступа и чужой диалог недоступны *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*; unauthorized chunks не попадают в LLM; revoked sessions отклоняются; secrets не возвращаются; malicious filenames не выходят из storage; повторные jobs не дублируют chunks; deleted docs не находятся; выдуманные citation IDs отклоняются.

**Definition of Done:** требования и permissions описаны; миграции проверены; реализация завершена; unit/integration tests добавлены; ошибки и пустые состояния обработаны; audit/observability учтены; документация обновлена; security review выполнен; lint/typecheck/CI проходят.

## 13. Пошаговый roadmap

### Фаза 0 — Product discovery

- Зафиксировать vision, target users, MVP/out-of-scope.
- Создать repository, README, roadmap, ADR index.
- Описать user journeys и threat model.

### Фаза 1 — Repository и developer experience

- Создать monorepo и lockfiles.
- Dockerfiles и Compose: PostgreSQL+pgvector, Redis, backend, worker, frontend.
- Healthchecks, Pydantic Settings, Alembic, .env.example.
- Ruff, pytest, mypy/pyright; ESLint, Prettier, strict TypeScript, Vitest.
- GitHub Actions, pre-commit, issue/PR templates.

### Фаза 2 — Core backend и data model

- Async SQLAlchemy session lifecycle и transaction boundaries.
- User, Group, Role, Permission и связи.
- Query patterns с фильтрами доступа, audit events, migrations.
- Тесты «чужие данные недоступны». *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*

### Фаза 3 — Authentication/authorization

- Password hashing, login/logout, access/refresh strategy.
- Refresh rotation/revocation, session management, rate limiting.
- Permission policy service и проверки в use cases.
- CSRF/cookie strategy и negative RBAC tests.

### Фаза 4 — AI provider abstraction

- LLMProvider, EmbeddingProvider, capabilities, typed errors.
- OpenAI, Anthropic, Ollama, OpenAI-compatible adapters.
- Connection tests, encrypted secrets, timeout/retry/cancellation.
- Contract tests.

### Фаза 5 — Local document ingestion

- StorageProvider и local storage; quarantine upload.
- File validation, Document/DocumentVersion.
- ParserProvider + Docling, normalized document structure.
- ChunkingStrategy, Celery tasks, idempotency/retry.
- Embeddings, pgvector, job status, reindex/delete.
- Tests PDF/DOCX/XLSX/TXT/CSV/MD.

### Фаза 6 — Retrieval и RAG evaluation

- Vector search и PostgreSQL full-text search.
- Hybrid fusion, KB/version filters, deduplication. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- Bounded context assembly и citation ID validation.
- Evaluation dataset и метрики Recall@K/citation precision.
- Prompt injection test cases.

### Фаза 7 — Knowledge Bases и Assistants

- CRUD KB, sources, KB-level access.
- CRUD Assistant, instructions, model/retrieval profiles.
- Проверки доступа и тесты комбинаций permissions.

### Фаза 8 — Chat UI и streaming

- Login/setup/chat/history UI.
- SSE client и typed events, stop generation.
- Citation display и protected in-app viewer.
- Loading/error/empty states, accessibility, E2E.

### Фаза 9 — Google Drive

- Изучить scopes и verification requirements.
- OAuth state validation, encrypted refresh tokens, folder picker.
- Recursive listing, download/export, initial sync.
- Changes API cursor, incremental sync, rename/move/delete.
- Throttle/backoff, revoke, reconciliation, tests и docs.

### Фаза 10 — Microsoft 365

- Entra app registration и OAuth PKCE.
- Delegated/app permissions и Selected permissions research.
- Site/drive/library/folder picker, Graph listing/download.
- Initial sync, Delta Query, changes, retries, revoke, tests.

### Фаза 11 — Production deployment

- S3-compatible storage и MinIO.
- Compose profiles, Nginx TLS, production secrets/resource limits.
- Backup/restore, upgrades, retention, worker scaling.
- Admin jobs/sync dashboard и troubleshooting.

### Фаза 12 — Security hardening

- Threat model review и RBAC/KB-access audit. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- OAuth, SSRF, upload/parser, archive bomb и prompt injection tests.
- XSS/CSRF/CORS, rate limits, logs, deletion lifecycle.
- Dependency/container scanning и security reporting.

### Фаза 13 — Open-source release

- README, diagrams, quick start, configuration/deployment guides.
- Connector/provider development guides, backup/restore, troubleshooting.
- CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, LICENSE, NOTICE.
- CHANGELOG, SemVer, GitHub Release, demo, roadmap, Discussions.

## 14. Следующие версии

- **Enterprise access:** document ACL, permission inheritance, OIDC/SAML, SCIM.
- **RAG quality:** rerankers, OCR, table-aware retrieval, multimodal parsing, query rewriting.
- **Platform:** MCP, agent tools с allowlist/approval, webhooks, connector SDK, analytics.
- Не включать billing и SaaS complexity без подтверждённой стратегии.

## 15. Работа с Claude Code

Claude Code должен выполнять ограниченные задачи, опираясь на спецификации. Не поручать ему создание всей платформы одним промптом.

1. Прочитать CLAUDE.md и релевантные docs.
2. Изучить текущий код и тесты.
3. Предложить план и список файлов до написания кода.
4. После согласования реализовать только заданный scope.
5. Добавить позитивные и негативные тесты.
6. Запустить tests, lint и typecheck.
7. Объяснить изменения, риски и ограничения.
8. Обновить документацию и сделать отдельный commit.

### Файлы документации

- `CLAUDE.md`
- `docs/PRODUCT_SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/DATABASE_SCHEMA.md`
- `docs/API_CONTRACTS.md`
- `docs/SECURITY_MODEL.md`
- `docs/DEVELOPMENT_ROADMAP.md`
- `docs/adr/`

### Шаблон задачи

> Ты работаешь над PlummyKnowledge. Прочитай CLAUDE.md и релевантные документы. Изучи существующий код. Сначала предложи план файлов и изменений, не пиши код до согласования. Реализуй только конкретную задачу. Не добавляй зависимости без обоснования; не помещай бизнес-логику в routers; не связывай domain/application напрямую с SDK; не обходи permission checks; не логируй secrets. Добавь позитивные и негативные тесты. После реализации запусти тесты, lint и typecheck, перечисли изменённые файлы, решения, риски и документы, которые нужно обновить.

Любое изменение framework, DB, queue, tenant model, auth/session strategy или публичного API требует предварительного ADR.

## 16. Лицензия и авторство

### Apache License 2.0

Рекомендованная лицензия — Apache License 2.0. Она разрешает свободное использование, изменение, распространение и коммерческое применение при соблюдении условий лицензии, включая сохранение copyright и license notices и обозначение изменённых файлов в предусмотренных случаях. Она содержит явные положения о патентных правах.

Apache 2.0 не требует раскрывать исходный код производных проектов. Это permissive open-source лицензия, а не copyleft. Не добавляй собственные ограничения поверх неё, если хочешь сохранить стандартную совместимость с open-source экосистемой.

### NOTICE и авторство

Укажи корректного правообладателя и год в LICENSE/NOTICE. В README добавь Author/Maintainer и ссылку на официальный репозиторий. Пример шаблона:

```text
Copyright 2026 [Copyright Holder]
PlummyKnowledge
Official repository: [repository URL]
This product includes software developed for the PlummyKnowledge project.
```

Не указывай фиктивное юридическое лицо. Для contributions определить DCO или CLA; проверять лицензии сторонних зависимостей. Для юридически значимых вопросов оформить права и уведомления с юристом.

## 17. Open-source рост и услуги

- Публиковать архитектуру, roadmap и работающий skeleton до полного MVP.
- Писать технические материалы о secure RAG, pgvector, connectors, self-hosted deployment.
- Использовать GitHub Discussions, Hacker News Show HN, Reddit r/selfhosted/r/LocalLLaMA/r/RAG, Dev.to.
- Измерять реальные deployments, contributors, issue quality, installation success и внедрения, а не только stars.
- Возможные услуги: установка, поддержка, корпоративные коннекторы, SSO/ACL, интеграции, аудит, сопровождение.

## 18. Риски

| Риск | Митигация |
|---|---|
| Слишком широкий MVP | Строгий scope и вертикальные срезы. |
| Широкие OAuth scopes | Документировать реальные права и исследовать ограниченные permissions. |
| Некачественный parsing | Evaluation corpus и parser abstraction. |
| Утечка retrieval | Authorization до LLM context и negative tests. |
| Prompt injection | Недоверенный контент, отсутствие tools в MVP. |
| Несовместимые embeddings | Profiles и reindex. |
| Дубли jobs | Idempotency, locks, unique constraints. |
| Сложный deployment | Compose quick start, healthchecks и troubleshooting. |
| Архитектурный дрейф от AI coding | CLAUDE.md, ADR и code review. |

## 19. Первый публичный релиз 0.1.0

0.1.0 должен доказать главную ценность, а не содержать все enterprise функции.

- Docker Compose deployment.
- Локальная authentication и базовая KB-level authorization.
- Один или несколько LLM providers.
- Upload PDF/DOCX и асинхронная индексация.
- Hybrid retrieval и streamed chat.
- Проверяемые citations и in-app source view.
- Тесты изоляции KB и пользователей, базовый audit, документация и quick start. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*

Дальнейшая примерная последовательность:

| Версия | Содержание |
|---|---|
| 0.2.0 | XLSX/CSV и RAG improvements |
| 0.3.0 | Google Drive |
| 0.4.0 | Microsoft 365 |
| 0.5.0 | Hardening/admin/backups |
| 1.0.0 | Стабильные contracts и production readiness |

## 20. Контрольный список релиза

- [ ] Миграции проверены на чистой БД и upgrade path.
- [ ] Нет известных критических/high security issues.
- [ ] Тесты изоляции KB/пользователей и permission tests проходят. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
- [ ] Secrets не попадают в logs/API responses.
- [ ] Upload/parser limits и ingestion idempotency работают.
- [ ] Deleted docs исключены из retrieval.
- [ ] Citations валидируются backend.
- [ ] Backup/restore проверены.
- [ ] Docker images, healthchecks и CI проходят.
- [ ] README, .env.example, changelog, LICENSE и NOTICE обновлены.

## 21. Первые семь рабочих сессий

1. Создать GitHub repository, README, Apache 2.0 LICENSE, NOTICE, .gitignore, docs и GitHub Project.
2. Инициализировать FastAPI backend, Pydantic Settings, logging, health endpoints, pytest и Ruff.
3. Инициализировать React + TypeScript + Vite, Chakra UI, Redux Toolkit, RTK Query и router.
4. Поднять PostgreSQL + pgvector и Redis через Compose; настроить SQLAlchemy async и Alembic.
5. Поднять Celery worker, тестовую задачу, retry и наблюдаемое состояние.
6. Реализовать User и роли, миграции и тесты доступа. *(изменено [ADR-012](adr/0012-single-organization-deployment.md))*
7. Реализовать минимальный auth flow и защищённый endpoint; обновить docs и зафиксировать milestone.

Не начинать Google OAuth, Microsoft Graph и сложный RAG до стабильного запуска каркаса.

## 22. Итоговые принципы

1. Сначала работающий end-to-end сценарий, затем расширение.
2. Сначала безопасность и границы доступа, затем удобства.
3. Один ingestion pipeline для локальных файлов и внешних коннекторов.
4. Один retrieval pipeline для всех источников.
5. Провайдеры подключаются через интерфейсы.
6. Фоновые задачи идемпотентны и наблюдаемы.
7. Цитаты проверяются сервером.
8. Права проверяются до передачи контекста LLM.
9. Значимые решения фиксируются в ADR.
10. Claude Code получает ограниченные задачи с критериями готовности.

**Главная рекомендация:** начни с вертикального сценария Docker Compose → login → provider config → upload PDF → parse → chunk → embed → retrieve → streamed answer → verified citation. Это техническое ядро PlummyKnowledge, на которое затем можно безопасно наращивать Google Drive, Microsoft 365, enterprise permissions и дополнительные провайдеры.
