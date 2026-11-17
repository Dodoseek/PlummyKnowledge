# CLAUDE.md — PlummyKnowledge

Основная рабочая инструкция Claude Code в этом репозитории. Основана на
[docs/PROJECT_BLUEPRINT.md](docs/PROJECT_BLUEPRINT.md) (§15 «Работа с Claude Code», §22 «Итоговые принципы») и
утверждённых [ADR](docs/adr/README.md).

## Проект

PlummyKnowledge — open-source **self-hosted** платформа корпоративной базы знаний с AI-интерфейсом: подключает
источники документов, индексирует их, даёт безопасный поиск и отвечает на вопросы с **проверяемыми цитатами**.
Лицензия — Apache License 2.0.

**Текущее состояние:** Этап 1 (Foundation) — каркас: `apps/backend` (FastAPI, Pydantic Settings, SQLAlchemy async,
Alembic с расширениями `vector`/`citext`, Redis, health live/ready), `apps/frontend` (React 18, Chakra UI, Redux
Toolkit/RTK Query, router, технический экран статуса), `compose.yaml` для разработки. Бизнес-сущностей, Celery,
Nginx и prod-compose ещё нет. Следующие шаги — сессии 5–7 ([DEVELOPMENT_ROADMAP.md §4](docs/DEVELOPMENT_ROADMAP.md)).
Не начинать Google OAuth, Microsoft Graph и сложный RAG до стабильного запуска каркаса.

## Приоритет источников

При противоречиях действует порядок:

1. Явные требования владельца проекта и согласованные архитектурные решения.
2. Утверждённые (Accepted) ADR — [docs/adr/](docs/adr/README.md).
3. `docs/` — продуктовые, архитектурные и технические спецификации.
4. Этот `CLAUDE.md`.
5. Глобальные инструкции AI-агента и общие предпочтения.

Глобальные инструкции не отменяют обязательные требования проекта (например, к тестированию). Если глобальная
инструкция конфликтует с требованием проекта — не выбирать молча: явно сообщить о конфликте владельцу в плане или
отчёте.

## Документы — читать перед задачей

| Документ | Когда читать |
|---|---|
| [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) | Назначение, роли, сценарии, границы MVP, что отложено |
| [docs/PROJECT_BLUEPRINT.md](docs/PROJECT_BLUEPRINT.md) | Исходные требования; при сомнениях в scope |
| [docs/adr/](docs/adr/README.md) | Всегда перед изменением архитектуры, доступа, setup, сети, ingestion |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Слои, модули, интерфейсы, pipelines, конфигурация |
| [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) | Любые модели, миграции, запросы |
| [docs/API_CONTRACTS.md](docs/API_CONTRACTS.md) | Любые endpoints, DTO, SSE, frontend-интеграция |
| [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) | Auth, permissions, tenant isolation, uploads, секреты, сеть, LLM — практически всегда |
| [docs/DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md) | Порядок работ, security gate фазы, критерии выхода |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Compose, окружение, секреты, volumes, bootstrap, сеть |

Пометка **[Proposed]** в спецификациях = решение не утверждено. Не реализовывать его молча как окончательное:
указать в плане и получить согласование (или ADR).

## Процесс выполнения задачи (§15)

Для каждой задачи:

1. Изучить контекст и документацию (этот файл, релевантные docs и ADR).
2. Проверить существующую реализацию и тесты.
3. Сформировать план.
4. Перечислить файлы, которые будут изменены.
5. Указать риски и спорные решения. **Не писать код до согласования плана.**
6. Реализовать только согласованный scope.
7. Добавить тесты (см. «Тестирование»).
8. Запустить тесты, lint и typecheck, если они применимы.
9. Обновить документацию.
10. Подготовить отчёт: изменённые файлы, принятые решения, риски, ограничения, документы, которые нужно обновить.
    Каждая задача — **отдельный commit**.

Задачи должны быть ограниченными и иметь критерии готовности; не строить «всю платформу одним промптом» (§15).

## Работа с архитектурой

- Перед реализацией задачи изучать соответствующие документы и ADR.
- Не нарушать утверждённые ADR. При необходимости архитектурного изменения сначала предложить ADR (статус
  `Proposed`) и получить согласование.
- ADR обязателен **до** изменения framework, DB, queue, tenant model, auth/session strategy или публичного API (§15).
- Соблюдать модульный монолит ([ADR-002](docs/adr/0002-modular-monolith-and-layering.md)); не переходить к
  микросервисам без отдельного архитектурного решения.
- Слои: `domain` → `application` → `infrastructure`; входы — `api` и `workers`.
  - `domain` не импортирует FastAPI, Celery, SQLAlchemy, SDK OpenAI/Anthropic, Google/Microsoft SDK.
  - `application` зависит только от интерфейсов (ports); не связывать domain/application напрямую с SDK.
  - Бизнес-логика **не** в routers и **не** в Celery tasks; API и worker вызывают одни и те же use cases.
  - ORM entities не отдаются из API — только Pydantic DTO (§10).
- Не дублировать бизнес-логику между модулями.
- Не создавать новые абстракции без реальной необходимости. Провайдеры, парсеры, хранилища и коннекторы — только через
  существующие интерфейсы (`LLMProvider`, `EmbeddingProvider`, `ParserProvider`, `ChunkingStrategy`,
  `StorageProvider`, `DataSourceConnector`, `SafeHttpClient`).
- Один ingestion pipeline для всех источников ([ADR-008](docs/adr/0008-unified-document-ingestion.md)); один
  retrieval pipeline (§22). Долгие операции — только в Celery; jobs идемпотентны и наблюдаемы (§11).
- Не расширять MVP. Вне MVP (§2): автономные агенты с действиями, визуальный workflow builder, billing,
  multi-region, SAML/SCIM, полное наследование внешних document ACL, сложный OCR. Без подтверждённой стратегии —
  никакого billing и SaaS complexity (§14).

## Безопасность — не обходить никогда

- Никогда не обходить backend authorization; проверка прав — в каждом use case.
- Не доверять frontend permissions: frontend не является security boundary.
- Проверять tenant isolation на backend: tenant только из проверенной сессии; каждый запрос к БД и каждый job
  фильтруется по `organization_id` ([ADR-004](docs/adr/0004-tenant-isolation-and-kb-level-access.md)).
- Не передавать недоступные документы в LLM: авторизация **до** retrieval и **до** передачи текста LLM; фильтры
  tenant/KB/document/version — в SQL; удалённые, failed и outdated версии не участвуют.
- Доступ к ассистенту — только при доступе ко всем его KB, проверка на каждом запросе; идентификаторы KB от frontend
  не принимаются ([ADR-005](docs/adr/0005-assistant-access-model.md)).
- Citation IDs выдаёт и валидирует backend; выдуманные ID отклоняются.
- Не логировать секреты, токены, пароли, содержимое документов и приватные prompts. Секреты шифруются до записи в БД,
  ключ — вне БД; в API секреты write-only.
- Не ослаблять SSRF-защиту ради удобства интеграций: все исходящие соединения — через `SafeHttpClient`; внутренние
  адреса — только через allowlist в Instance Settings ([ADR-007](docs/adr/0007-network-access-and-local-llm.md)).
- Первоначальная настройка — только с bootstrap token или через CLI на сервере
  ([ADR-006](docs/adr/0006-initial-setup-and-system-configuration.md)); Instance Settings не меняются через API.
- Считать документы недоверенным вводом; отделять инструкции от retrieved context; не помещать secrets в prompt.
- Не предоставлять LLM произвольные инструменты (tools) без отдельного архитектурного решения.
- Имена загруженных файлов никогда не используются как пути в storage.

## Работа с кодом

- Не изменять файлы вне scope задачи без необходимости.
- Не выполнять масштабный рефакторинг без согласования.
- Не добавлять и не обновлять зависимости без причины и обоснования.
- Не заменять существующие библиотеки новыми без ADR.
- Не создавать дублирующие сервисы, модели и компоненты.
- Сохранять обратную совместимость API, если она требуется существующими контрактами
  ([API_CONTRACTS.md §1.1](docs/API_CONTRACTS.md)).

## Тестирование (§12)

Тестирование — обязательная часть каждой функциональной задачи. Для каждой новой функциональности:

- позитивные тесты;
- негативные тесты;
- граничные случаи;
- сценарии ошибок;
- проверка авторизации, если задача связана с доступом к данным;
- проверка отсутствия доступа к чужим tenant-данным;
- обновление существующих тестов, если поведение намеренно изменилось.

Нельзя считать задачу завершённой только потому, что код написан. Если тест невозможно запустить — явно сообщить
причину и не утверждать, что он пройден.

Уровни (§12): unit, integration, contract (AI providers, connectors), E2E, security, performance, recovery.
Обязательные негативные тесты: чужой tenant/KB/document недоступен; unauthorized chunks не попадают в LLM; revoked
sessions отклоняются; secrets не возвращаются; malicious filenames не выходят из storage; повторные jobs не дублируют
chunks; deleted docs не находятся; выдуманные citation IDs отклоняются. Контрольные сценарии SC-01…SC-09 —
[SECURITY_MODEL.md §18.1](docs/SECURITY_MODEL.md).

**Документационные задачи:** достаточно проверки ссылок, согласованности терминологии, структуры и отсутствия
противоречий. Не создавать программные тесты ради формального соблюдения правила.

## Технологии ([ADR-003](docs/adr/0003-technology-stack.md))

Backend: Python, FastAPI, SQLAlchemy 2.x async, Pydantic v2, Alembic · DB: PostgreSQL + pgvector ·
Jobs: Redis + Celery · Frontend: React 18, TypeScript (strict), Vite, Chakra UI 2.8+, Redux Toolkit, RTK Query ·
API: REST `/api/v1` + SSE · Parsing: Docling за `ParserProvider` · Storage: local FS / S3-compatible (MinIO) ·
Deploy: Docker, Docker Compose, Nginx · Quality: Ruff, mypy/pyright, ESLint, Prettier · Tests: Pytest, Vitest,
React Testing Library, Playwright.

## Структура репозитория (§4)

```text
apps/backend, apps/frontend
packages/{ai-providers,document-processing,retrieval,connectors}
deploy/{docker,compose,nginx}
docs/   PRODUCT_SPEC, PROJECT_BLUEPRINT, ARCHITECTURE, DATABASE_SCHEMA, API_CONTRACTS,
        SECURITY_MODEL, DEVELOPMENT_ROADMAP, DEPLOYMENT, adr/
scripts, .githooks, .github/workflows
CLAUDE.md, VERSION, README.md, LICENSE, NOTICE, CONTRIBUTING.md, SECURITY.md
```

## Версионирование ([ADR-009](docs/adr/0009-automatic-versioning.md))

- Версия — в файле `VERSION`; каждый коммит автоматически повышает её (`0.1.0-dev.N` → `0.1.0-dev.N+1`) через
  `.githooks/pre-commit`.
- Не редактировать `VERSION` вручную и не обходить hook (`--no-verify`) без явной просьбы владельца.
- Релиз и смена целевой версии — только по решению владельца: `python3 scripts/version.py release`,
  `python3 scripts/version.py set <version>`.

## Команды

Уже доступны:

```bash
sh scripts/setup-git-hooks.sh          # один раз на клон: hooks и merge-driver версии
python3 scripts/version.py show        # текущая версия
python3 scripts/version.py check       # VERSION валиден и больше, чем в HEAD~1
python3 scripts/version.py release     # релиз X.Y.Z + тег vX.Y.Z (только по решению владельца)
```

Среда разработки (из корня репозитория):

```bash
docker compose up --build                                   # postgres, redis, migrate, backend, frontend
docker compose exec backend pytest                          # backend: unit + API + integration
docker compose exec backend ruff check src tests alembic    # backend lint
docker compose exec backend ruff format --check src tests alembic
docker compose exec backend mypy                            # backend typecheck (strict)
docker compose run --rm migrate                             # alembic upgrade head
```

Новая миграция создаётся локально (в контейнере `alembic/` смонтирован read-only):
`cd apps/backend && uv run alembic revision -m "..."`.

Frontend (в `apps/frontend`, Node ^20.19 / ^22.13 / >=24, npm из `packageManager` — 11.21.2; `.npmrc` с
`engine-strict` отклоняет npm 10; `package-lock.json` обновлять только этой версией npm):

```bash
npm ci && npm test && npm run lint && npm run typecheck && npm run format:check && npm run build
```

Backend локально без Docker (в `apps/backend`): `uv sync` и `uv run pytest` (integration-тесты пропускаются без
`DATABASE_URL`/`REDIS_URL`), `uv run ruff check .`, `uv run mypy`.

Ещё не существуют: E2E (Playwright), CI (GitHub Actions), CLI `plummy`, Celery worker. **Не выдумывать команды** —
сверяться с фактическими конфигурациями в репозитории.

## Definition of Done (§12)

- [ ] Требования и permissions описаны
- [ ] Миграции проверены (чистая БД и upgrade path)
- [ ] Реализация завершена в рамках согласованного scope
- [ ] Unit/integration tests добавлены (позитивные, негативные, граничные, ошибки, tenant isolation)
- [ ] Ошибки и пустые состояния обработаны
- [ ] Audit/observability учтены
- [ ] Документация обновлена
- [ ] Security review выполнен
- [ ] Lint, typecheck и CI проходят

## Шаблон задачи (§15)

> Ты работаешь над PlummyKnowledge. Прочитай CLAUDE.md и релевантные документы. Изучи существующий код. Сначала
> предложи план файлов и изменений, не пиши код до согласования. Реализуй только конкретную задачу. Не добавляй
> зависимости без обоснования; не помещай бизнес-логику в routers; не связывай domain/application напрямую с SDK;
> не обходи permission checks; не логируй secrets. Добавь позитивные и негативные тесты. После реализации запусти
> тесты, lint и typecheck, перечисли изменённые файлы, решения, риски и документы, которые нужно обновить.

## Ориентир: вертикальный срез ядра (§22)

Docker Compose → login → provider config → upload PDF → parse → chunk → embed → retrieve → streamed answer →
verified citation. Сначала работающий end-to-end сценарий, затем расширение.
