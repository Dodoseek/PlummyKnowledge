# PlummyKnowledge

Open-source **self-hosted** платформа корпоративной базы знаний с AI-интерфейсом. PlummyKnowledge подключает
источники документов, индексирует их, даёт безопасный поиск и отвечает на вопросы сотрудников со **ссылками на
исходные материалы, которые проверяет backend**.

- **Данные остаются у вас.** Документы, индексы, диалоги и настройки хранятся только в вашей инсталляции.
- **Любые модели.** Можно подключить облачные или локальные LLM (OpenAI, Anthropic, Ollama, OpenAI-compatible) и
  отдельно выбрать модель для embeddings.
- **Ответы с проверенными цитатами.** Модель может сослаться только на фрагменты, которые backend выдал ей сам.
- **Минимум привилегий.** Сотрудник видит только те базы знаний, документы и ассистентов, к которым у него есть доступ.

> **Статус: 0.5.0 — первый релиз с production-профилем.** Работает весь базовый сценарий: первоначальная настройка,
> вход и сессии, пользователи, группы и роли, подключение LLM-провайдеров (облачных и локальных), базы знаний с
> доступом по пользователям и группам, загрузка документов (гарантированно PDF и DOCX; также TXT, Markdown, CSV,
> XLSX) с асинхронной индексацией, hybrid-поиск, ассистенты, чат с потоковым ответом, остановкой и историей,
> проверенные цитаты и защищённый просмотр источника, синхронизация папок Google Drive и Microsoft 365
> (SharePoint, OneDrive).
>
> В 0.5.0 добавлены ([roadmap](docs/DEVELOPMENT_ROADMAP.md), Фазы 11–12): production-профиль
> `compose.prod.yaml` (Nginx, TLS, CSP, S3-совместимое хранилище, секреты файлами), backup/restore и обновление,
> страница фоновых задач, разделение ролей БД, журнал аудита для аудитора, общий rate limit и проверка образов
> Trivy в CI. Production-развёртывание — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md); при обновлении БД, созданной до
> 0.5.0, однократно выполните `deploy/scripts/split-db-roles.sh` ([DEPLOYMENT.md](docs/DEPLOYMENT.md) §6.1).

## Быстрый старт (разработка)

Нужен Docker с Docker Compose v2.

```bash
git clone <repository-url> plummy-knowledge && cd plummy-knowledge
docker compose up --build
```

После запуска доступны:

| Что | Адрес |
|---|---|
| Web-интерфейс | <http://localhost:5173> |
| API | <http://localhost:8000/api/v1> |
| Документация API (Swagger UI; в `production` выключена, см. `API_DOCS_ENABLED`) | <http://localhost:8000/api/v1/docs> |
| OpenAPI-схема | <http://localhost:8000/api/v1/openapi.json> |
| Liveness / readiness | `/api/v1/health/live`, `/api/v1/health/ready` |

Файл `.env` для разработки не обязателен. Чтобы переопределить значения по умолчанию, скопируйте
[`.env.example`](.env.example) в `.env`. Порты PostgreSQL и Redis на хост не публикуются, backend и frontend
слушают только `127.0.0.1`.

Приложение и миграции подключаются к PostgreSQL под ролью без прав суперпользователя. Расширения `vector` и
`citext` в среде разработки устанавливает init-скрипт контейнера, в production — администратор БД
([ADR-011](docs/adr/0011-database-roles-and-extensions.md), [DEPLOYMENT.md §6.1](docs/DEPLOYMENT.md)). Если volume
БД создан до этого изменения, пересоздайте его: `docker compose down -v`.

Остановить стек: `docker compose down`. Удалить данные (БД, Redis): `docker compose down -v`.

### Первые шаги

1. Прочитайте одноразовый setup token и откройте <http://localhost:5173> — откроется мастер `/setup`, где создаётся
   первый администратор ([ADR-006](docs/adr/0006-initial-setup-and-system-configuration.md)):

   ```bash
   docker compose run --rm migrate cat /var/lib/plummy/bootstrap/setup-token
   ```

2. **AI providers** → добавьте провайдера (OpenAI, Anthropic, Ollama или OpenAI-compatible), проверьте соединение,
   добавьте chat-модель. Ключи шифруются и никогда не возвращаются API. Локальная модель (например, Ollama) доступна
   только после записи в allowlist конфигурационного файла — [DEPLOYMENT.md §7.3](docs/DEPLOYMENT.md).
3. **AI providers → Embedding profiles** → профиль на модели embeddings (облачной или локальной).
4. **Knowledge bases** → создайте базу знаний, загрузите документы и дождитесь статуса `indexed`; выдайте доступ
   пользователям или группам.
5. **Manage assistants** → ассистент с моделью, embedding profile и базами знаний. Пользователь видит ассистента,
   только если у него есть доступ ко **всем** его базам ([ADR-005](docs/adr/0005-assistant-access-model.md)).
6. **Users** → пригласите сотрудников: ссылка активации показывается один раз
   ([ADR-015](docs/adr/0015-user-activation-links.md)).
7. **Chat** → задайте вопрос: ответ приходит потоком, номера в квадратных скобках ведут к цитируемому фрагменту.

## Архитектура вкратце

Модульный монолит ([ADR-002](docs/adr/0002-modular-monolith-and-layering.md)): один backend-артефакт со слоями
`domain → application → infrastructure` и входами `api` (FastAPI) и `workers` (Celery).

| Слой | Технологии ([ADR-003](docs/adr/0003-technology-stack.md)) |
|---|---|
| Backend | Python 3.13, FastAPI, SQLAlchemy 2 (async, asyncpg), Pydantic v2 / pydantic-settings, Alembic |
| Данные | PostgreSQL 17 + pgvector, Redis |
| Frontend | React 18, TypeScript (strict), Vite, Chakra UI 2, Redux Toolkit + RTK Query, React Router |
| Качество | Ruff, mypy (strict), pytest; ESLint, Prettier, Vitest + Testing Library |
| Развёртывание | Docker, Docker Compose; production — `compose.prod.yaml` с Nginx/TLS и встроенным S3 ([DEPLOYMENT.md](docs/DEPLOYMENT.md)) |

```text
apps/backend/      FastAPI-приложение, Celery workers, Alembic-миграции, тесты
apps/frontend/     React SPA
apps/e2e/          end-to-end тесты (Playwright)
docs/              спецификации и ADR
scripts/           служебные скрипты (версионирование, secret scan)
deploy/postgres/   SQL расширений и init-скрипт dev-БД
deploy/e2e/        фейковый OpenAI-compatible провайдер и конфигурация E2E-стека
compose.yaml       среда разработки
compose.e2e.yaml   E2E-стек поверх compose.yaml
```

## Разработка

Полный список команд — в [CLAUDE.md](CLAUDE.md#команды). Основные:

```bash
# один раз после клонирования: git hooks автоматического версионирования (ADR-009)
sh scripts/setup-git-hooks.sh

# backend (внутри запущенного стека)
docker compose exec backend pytest            # unit, API и integration-тесты
docker compose exec backend ruff check src tests alembic
docker compose exec backend mypy

# frontend (в apps/frontend)
npm ci
npm test && npm run lint && npm run typecheck && npm run build
```

E2E-тесты (Playwright) — в отдельном Compose-проекте рядом с dev-стеком, команды —
[DEPLOYMENT.md](docs/DEPLOYMENT.md) («E2E-стек»).

Изменения в `apps/backend/src` и `apps/frontend/src` подхватываются API и frontend без перезапуска: работают uvicorn
reload и Vite HMR. Workers и scheduler нужно перезапустить: `docker compose restart worker worker-ingestion scheduler`. После изменения зависимостей (`pyproject.toml`/`uv.lock`, `package.json`/`package-lock.json`) выполните
`docker compose up --build`.

Frontend требует npm 11: версия зафиксирована в `package.json` (`packageManager`, `engines`), а `.npmrc`
(`engine-strict`) не даёт установить зависимости другим npm. Docker-образ ставит нужную версию сам. Локально:
`npm install -g npm@11.21.2` или разовый запуск `npx npm@11.21.2 ci`.

Миграции применяет отдельный сервис `migrate` перед стартом API, сам API схему не меняет. Повторно применить
миграции можно командой `docker compose run --rm migrate`.

## Конфигурация

Backend настраивается переменными окружения, их проверяет Pydantic Settings. При невалидной конфигурации процесс
не стартует. Секреты можно передавать файлами (`DATABASE_URL_FILE`, `REDIS_URL_FILE`, `SECRET_KEY_FILE`). Если
`APP_ENV` не задан, применяется самый строгий режим (`production`): `SECRET_KEY` обязателен, `APP_DEBUG=true`
запрещён. Подробности — [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#0-среда-разработки-реализовано-фаза-1).

## Документация

| Документ | Содержание |
|---|---|
| [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) | Назначение, роли, сценарии, границы MVP |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Слои, модули, интерфейсы, pipelines |
| [DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) | Модель данных |
| [API_CONTRACTS.md](docs/API_CONTRACTS.md) | REST API и SSE |
| [SECURITY_MODEL.md](docs/SECURITY_MODEL.md) | Модель безопасности и threat model |
| [DEVELOPMENT_ROADMAP.md](docs/DEVELOPMENT_ROADMAP.md) | Фазы разработки и релизы |
| [DEPLOYMENT.md](docs/DEPLOYMENT.md) | Окружение, секреты, сеть, bootstrap |
| [docs/adr/](docs/adr/README.md) | Архитектурные решения (ADR) |

## Версионирование

Версия хранится в файле [`VERSION`](VERSION) и автоматически повышается при каждом коммите (`X.Y.Z-dev.N`); релизы
помечаются тегами `vX.Y.Z`. Backend,
frontend и Docker-образы берут её из этого файла. Подробнее — [ADR-009](docs/adr/0009-automatic-versioning.md).

## Участие и безопасность

Как предложить изменение — [CONTRIBUTING.md](CONTRIBUTING.md). Об уязвимостях сообщайте приватно —
[SECURITY.md](SECURITY.md).

## Лицензия

[Apache License 2.0](LICENSE), см. также [NOTICE](NOTICE).
