# PlummyKnowledge

Open-source **self-hosted** платформа корпоративной базы знаний с AI-интерфейсом. PlummyKnowledge подключает
источники документов, индексирует их, даёт безопасный поиск и отвечает на вопросы сотрудников со **ссылками на
исходные материалы, которые проверяет backend**.

- **Данные остаются у вас.** Документы, индексы, диалоги и настройки хранятся только в вашей инсталляции.
- **Любые модели.** Можно подключить облачные или локальные LLM (OpenAI, Anthropic, Ollama, OpenAI-compatible) и
  отдельно выбрать модель для embeddings.
- **Ответы с проверенными цитатами.** Модель может сослаться только на фрагменты, которые backend выдал ей сам.
- **Минимум привилегий.** Сотрудник видит только те базы знаний, документы и ассистентов, к которым у него есть доступ.

> **Статус: ранняя разработка (`0.1.0-dev`).** Готов технический фундамент (Этап 1): backend, frontend,
> PostgreSQL + pgvector, Redis, миграции и среда разработки в Docker Compose. Пользовательских функций (вход,
> загрузка документов, чат) пока нет — план работ описан в [roadmap](docs/DEVELOPMENT_ROADMAP.md). Не используйте
> проект в production.

## Быстрый старт (разработка)

Нужен Docker с Docker Compose v2.

```bash
git clone <repository-url> plummy-knowledge && cd plummy-knowledge
docker compose up --build
```

После запуска доступны:

| Что | Адрес |
|---|---|
| Web-интерфейс (технический экран статуса) | <http://localhost:5173> |
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

## Архитектура вкратце

Модульный монолит ([ADR-002](docs/adr/0002-modular-monolith-and-layering.md)): один backend-артефакт со слоями
`domain → application → infrastructure` и входами `api` (FastAPI) и `workers` (Celery).

| Слой | Технологии ([ADR-003](docs/adr/0003-technology-stack.md)) |
|---|---|
| Backend | Python 3.13, FastAPI, SQLAlchemy 2 (async, asyncpg), Pydantic v2 / pydantic-settings, Alembic |
| Данные | PostgreSQL 17 + pgvector, Redis |
| Frontend | React 18, TypeScript (strict), Vite, Chakra UI 2, Redux Toolkit + RTK Query, React Router |
| Качество | Ruff, mypy (strict), pytest; ESLint, Prettier, Vitest + Testing Library |
| Развёртывание | Docker, Docker Compose (Nginx и production-профиль — Фаза 11) |

```text
apps/backend/     FastAPI-приложение, Alembic-миграции, тесты
apps/frontend/    React SPA
docs/             спецификации и ADR
scripts/          служебные скрипты (версионирование)
deploy/postgres/  SQL расширений и init-скрипт dev-БД
compose.yaml      среда разработки
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

Изменения в `apps/backend/src` и `apps/frontend/src` подхватываются без перезапуска: работают uvicorn reload и
Vite HMR. После изменения зависимостей (`pyproject.toml`/`uv.lock`, `package.json`/`package-lock.json`) выполните
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

Версия хранится в файле [`VERSION`](VERSION) и автоматически повышается при каждом коммите (`0.1.0-dev.N`). Backend,
frontend и Docker-образы берут её из этого файла. Подробнее — [ADR-009](docs/adr/0009-automatic-versioning.md).

## Лицензия

[Apache License 2.0](LICENSE).
