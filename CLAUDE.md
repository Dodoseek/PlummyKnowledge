# CLAUDE.md — PlummyKnowledge

Основная рабочая инструкция Claude Code в этом репозитории. Основана на
[docs/PROJECT_BLUEPRINT.md](docs/PROJECT_BLUEPRINT.md) (§15 «Работа с Claude Code», §22 «Итоговые принципы») и
утверждённых [ADR](docs/adr/README.md).

## Проект

PlummyKnowledge — open-source **self-hosted** платформа корпоративной базы знаний с AI-интерфейсом: подключает
источники документов, индексирует их, даёт безопасный поиск и отвечает на вопросы с **проверяемыми цитатами**.
Лицензия — Apache License 2.0. Одна инсталляция принадлежит **одной** организации и работает на её серверах или в её
облаке: multi-tenancy, сущности Organization и `organization_id` нет ([ADR-012](docs/adr/0012-single-organization-deployment.md)).

**Текущее состояние:** Фазы 1–7 roadmap завершены (Foundation, Bootstrap & Authentication, Users & RBAC, AI providers
& network policy, Local document ingestion, Retrieval & RAG evaluation, Knowledge Bases & Assistants). Backend:
FastAPI, Pydantic Settings, SQLAlchemy async, Alembic (`vector`/`citext`, users, activation tokens, groups,
RBAC-каталог и назначения ролей, sessions, audit_logs), Redis (rate limit, брокер Celery), health live/ready; Celery
worker с диагностической задачей; первоначальная настройка по bootstrap token и CLI `python -m plummy.cli`;
login/logout/me, свои сессии, смена пароля на серверных сессиях ([ADR-013](docs/adr/0013-auth-session-strategy.md));
`SecurityContext` и `require_permission`; API пользователей (создание ссылкой активации,
[ADR-015](docs/adr/0015-user-activation-links.md)), групп и ролей с защитой последнего администратора;
cursor-пагинация ([ADR-014](docs/adr/0014-list-pagination.md)); `GET /system/settings`. Фаза 4: шифрование секретов
с key ring вне БД ([ADR-016](docs/adr/0016-secret-encryption.md), CLI `secrets`), network allowlist в `CONFIG_FILE` и
`SafeHttpClient` ([ADR-007](docs/adr/0007-network-access-and-local-llm.md)), порты `LLMProvider`/`EmbeddingProvider`
и адаптеры OpenAI, Anthropic, Ollama, OpenAI-compatible (httpx, без SDK), API `providers`, моделей и
`embedding-profiles` с connection test. Фаза 5: хранение векторов ([ADR-017](docs/adr/0017-vector-storage.md)),
local storage с quarantine, KB с источником `upload`, upload документов, единый ingestion pipeline (стадии 2–7 —
`worker-ingestion` с Docling без сети и секретов, стадии 8–12 — `worker`: chunking, embeddings, атомарное
переключение версии), API `knowledge-bases`, `documents`, `jobs`, удаление с purge, Celery beat `scheduler`.
Frontend: React 18, Chakra UI, RTK Query, страницы `/setup`, `/login`, `/activate`, `/account`, `/admin/users`,
`/admin/groups`, `/admin/providers`, `/admin/embedding-profiles`, `/admin/assistants`, `/assistants`, `/knowledge-bases`,
`/knowledge-bases/:id`, guard, выход.
CI (GitHub Actions), pre-commit (secret scan, lint), Dependabot. Фаза 6: FTS ([ADR-018](docs/adr/0018-full-text-search-configuration.md), миграции `0011`/`0012`),
hybrid retrieval (`application/retrieval`: vector + FTS, RRF, dedup, бюджет), context assembly и citation IDs
`c1…cn` с потоковой валидацией, `POST /knowledge-bases/{id}/search` и поиск на странице KB, evaluation
(`synthetic-v1`, `eval run`, baseline — [docs/evaluation/](docs/evaluation/README.md)). Фаза 7: управление KB (`PATCH`,
`DELETE` с purge, `/access`, `/reindex`), миграция `0013` и ассистенты с политикой доступа ADR-005
(`application/assistants/access.py`), CRUD `/assistants` с `availability`, страницы `/admin/assistants` и `/assistants`.
Application Settings, chat, Nginx и prod-compose ещё нет ([DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md)).
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
| [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) | Auth, sessions, permissions, изоляция данных, uploads, секреты, сеть, LLM — практически всегда |
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
- Пользователь, роли и права — только из проверенной сессии (`SecurityContext`); от клиента не принимаются.
  Данные KB фильтруются по доступу пользователя, личные данные (диалоги, сессии) — по владельцу
  ([ADR-012](docs/adr/0012-single-organization-deployment.md), [ADR-013](docs/adr/0013-auth-session-strategy.md)).
- Не вводить multi-tenancy (`organizations`, `organization_id`, роли «платформы») без нового ADR.
- Не передавать недоступные документы в LLM: авторизация **до** retrieval и **до** передачи текста LLM; фильтры
  KB/document/version — в SQL; удалённые, failed и outdated версии не участвуют.
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
- проверка отсутствия доступа к чужим данным (KB без доступа, чужие диалоги и сессии);
- обновление существующих тестов, если поведение намеренно изменилось.

Нельзя считать задачу завершённой только потому, что код написан. Если тест невозможно запустить — явно сообщить
причину и не утверждать, что он пройден.

Уровни (§12): unit, integration, contract (AI providers, connectors), E2E, security, performance, recovery.
Обязательные негативные тесты: KB/document без доступа и чужой диалог недоступны; unauthorized chunks не попадают в LLM; revoked
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
docker compose up --build                                   # postgres, redis, migrate, backend, workers, scheduler, frontend
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

CLI на сервере (dev — в контейнере `migrate`, подробности — [DEPLOYMENT.md §6](docs/DEPLOYMENT.md)):

```bash
docker compose run --rm migrate cat /var/lib/plummy/bootstrap/setup-token        # прочитать setup token
docker compose run --rm migrate python -m plummy.cli setup status
docker compose run --rm migrate python -m plummy.cli setup init-token --rotate   # новый токен
docker compose run --rm -it migrate python -m plummy.cli admin reset-password --email admin@example.com
```

Worker и проверки репозитория:

```bash
docker compose exec worker python -m plummy.cli worker check   # задача до worker и обратно
docker compose restart worker worker-ingestion scheduler       # после изменения кода (нет auto-reload)
docker compose build worker-ingestion                          # образ с Docling и моделями (~4 ГБ)
docker compose run --rm --no-deps worker-ingestion pytest -p no:cacheprovider tests/ingestion tests/unit/test_isolated_parser.py
python3 scripts/check_secrets.py                               # secret scan всех tracked-файлов
python3 -m unittest discover -s scripts/tests                  # тесты scripts/
```

Оценка retrieval ([docs/evaluation/](docs/evaluation/README.md); индексация набора расходует токены
embedding-провайдера):

```bash
docker compose exec backend python -m plummy.cli eval run --dataset /app/evaluation/datasets/synthetic-v1 \
  --email admin@example.com --embedding-profile <profile-id> --output /tmp/report.json
```

Секреты ([ADR-016](docs/adr/0016-secret-encryption.md), dev — в контейнере `migrate`):

```bash
docker compose run --rm migrate python -m plummy.cli secrets status         # записи по key_id
docker compose run --rm migrate python -m plummy.cli secrets rotate         # перешифровать на primary
docker compose run --rm migrate python -m plummy.cli secrets generate-key   # новая запись key ring (печатает ключ)
```

Живые contract tests провайдеров (тратят токены, в CI не запускаются; в `apps/backend`):
`LIVE_PROVIDER_TESTS=1 ANTHROPIC_API_KEY=… OPENAI_API_KEY=… OPENAI_TEST_MODEL=… uv run pytest -m live`.

`.githooks/pre-commit`: secret scan staged-строк → ruff/prettier/eslint staged-файлов (если `uv`/`node_modules`
есть; `PLUMMY_SKIP_LINT=1` — пропустить lint один раз) → bump `VERSION`. CI — `.github/workflows/ci.yml`: те же
проверки на Compose-стеке, `pip-audit`, `npm audit`, dependency review в PR.

E2E (Playwright, `apps/e2e`): отдельный Compose-проект без опубликованных портов, фейковый OpenAI-compatible
провайдер (`deploy/e2e/fake_llm.py`) в allowlist тестового `CONFIG_FILE`; global setup создаёт первого
администратора на свежем стеке и всё остальное — через API:

```bash
export COMPOSE_PROJECT_NAME=plummy-e2e COMPOSE_FILE=compose.yaml:compose.e2e.yaml COMPOSE_PROFILES=e2e
docker compose up --build --detach --wait backend worker worker-ingestion scheduler frontend fake-llm
docker compose run --rm -T e2e                                  # tsc + playwright test (Chromium)
docker compose down --volumes
```

**Не выдумывать команды** — сверяться с фактическими конфигурациями в репозитории.

## Definition of Done (§12)

- [ ] Требования и permissions описаны
- [ ] Миграции проверены (чистая БД и upgrade path)
- [ ] Реализация завершена в рамках согласованного scope
- [ ] Unit/integration tests добавлены (позитивные, негативные, граничные, ошибки, изоляция данных)
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
