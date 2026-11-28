# PlummyKnowledge — Deployment

| | |
|---|---|
| Статус | v0.5 — среда разработки, первоначальная настройка (Фазы 1–3, §0, §6) и production-стек (шаг 11.2, §1–§2, §4–§5, §7.1, §8), обновление и backup/restore (шаг 11.3, §9–§10) реализованы |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §3, §5, §8, §11, §13 (Фазы 1, 11) |
| Решения | [ADR-006](adr/0006-initial-setup-and-system-configuration.md), [ADR-007](adr/0007-network-access-and-local-llm.md), [ADR-011](adr/0011-database-roles-and-extensions.md), [ADR-024](adr/0024-database-owner-and-runtime-roles.md), [ADR-013](adr/0013-auth-session-strategy.md), [ADR-022](adr/0022-s3-compatible-storage.md), [ADR-023](adr/0023-production-deployment.md) |
| Связанные документы | [ARCHITECTURE.md §4, §10, §12](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md) |

Руководства администратора на английском — [docs/guides/](guides/README.md); этот документ — их справочная основа.
Разделы §0–§2, §4–§6, §7.1 и §8 описывают то, что реализовано. Остальные имена переменных и CLI-команд, помеченные
**[Proposed]**, фиксируются при реализации соответствующих шагов и обновляются здесь.

## 0. Среда разработки (реализовано, Фаза 1)

Запуск из корня репозитория: `docker compose up --build` (файл `compose.yaml`, `.env` не обязателен).

| Сервис | Образ / сборка | Назначение | Порты на хосте | Сети |
|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:0.8.7-pg17` | PostgreSQL 17 + pgvector | — | `data` |
| `redis` | `redis:8.6-alpine` | Redis (AOF) | — | `data` |
| `migrate` | `apps/backend/Dockerfile` (`dev`) | Одноразово: `python -m plummy.cli secrets init-keyring` (только dev, §4), `alembic upgrade head`, затем `python -m plummy.cli setup init-token` (§6); API стартует только после успешного завершения | — | `data` |
| `backend` | `apps/backend/Dockerfile` (`dev`) | FastAPI, `uvicorn --reload`; healthcheck = `/api/v1/health/ready` | `127.0.0.1:8000` | `data`, `edge` |
| `worker` | `apps/backend/Dockerfile` (`dev`) | Celery worker, очереди `default`, `embeddings`, `maintenance`, `connectors` (стадии 8–12 ingestion, purge, cleanup, синхронизация внешних источников — ADR-020); healthcheck = `celery inspect ping`. Вызывает embedding-провайдеров и Google Drive, поэтому в сети `edge` (каждое соединение — через `SafeHttpClient`, ADR-007). Изменения кода — `docker compose restart worker` (без auto-reload) | — | `data`, `edge` |
| `worker-ingestion` | `apps/backend/Dockerfile` (`ingestion-dev`: образ с Docling и его моделями, ~4 ГБ) | Celery worker, только очередь `ingestion` (стадии 2–7: проверки, тип, безопасность файла, storage, parsing), concurrency 1; parser — в дочернем процессе с timeout и rlimits; `mem_limit` = `INGESTION_MEMORY_LIMIT` (по умолчанию `4g`). Без key ring, секретов провайдеров и setup token. `docker compose restart worker-ingestion` после изменения кода | — | `data` |
| `scheduler` | `apps/backend/Dockerfile` (`dev`) | Celery beat, ровно один экземпляр: cleanup quarantine (1 ч), повторная публикация `queued` jobs, не дошедших до брокера (5 мин), sweep удалённых документов для purge (10 мин), sweep чата — зависшие `streaming`-ответы и удалённые диалоги (5 мин), планировщик синхронизации источников (1 мин: sync jobs по `next_sync_at`, раз в сутки reconciliation, повтор потерянных и зависших jobs) — в очередь `maintenance`. Только Redis; файл состояния расписания временный (`/tmp`) | — | `data` |
| `s3` | `versity/versitygw:v1.11.0` | S3-совместимый сервер (backend `posix` на volume `s3-data`) только для integration-тестов S3-адаптера (`S3_TEST_URL` у `backend`); dev-стек хранит документы локально | — | `data` |
| `frontend` | `apps/frontend/Dockerfile` (`dev`) | Vite dev server; `/api` проксируется на `backend` (same-origin, без CORS) | `127.0.0.1:5173` | `edge` |

- Сеть `data` — `internal: true`; порты PostgreSQL и Redis не публикуются. Учётные данные БД в `compose.yaml` —
  только для разработки (значения по умолчанию, переопределяются через `.env`).
- Роли PostgreSQL ([ADR-011](adr/0011-database-roles-and-extensions.md),
  [ADR-024](adr/0024-database-owner-and-runtime-roles.md)): суперпользователь `postgres` (`POSTGRES_PASSWORD`)
  используется только init-скриптом `deploy/postgres/initdb` при создании volume. Скрипт создаёт владельца схемы
  `PLUMMY_DB_USER` (без `SUPERUSER`; `CREATEDB` — только для integration-тестов) и его БД `PLUMMY_DB_NAME`,
  runtime-роль `PLUMMY_DB_RUNTIME_USER` (`plummy_app`, без собственных прав) и устанавливает расширения из
  `deploy/postgres/extensions.sql`. `migrate` подключается владельцем и выдаёт runtime-роли права
  (`db grant-runtime`); `backend` и workers — под runtime-ролью. Integration-тесты создают и мигрируют БД владельцем
  (`TEST_OWNER_DATABASE_URL` у `backend`, только dev), а приложение в тестах работает под runtime-ролью. Volume,
  созданный до ADR-024, получает runtime-роль командой
  `COMPOSE_FILE=compose.yaml PLUMMY_DB_RUNTIME_PASSWORD_FILE=<файл с plummy_app_dev_password> sh deploy/scripts/split-db-roles.sh`
  или пересоздаётся (`docker compose down -v`).
- `backend` (dev) перед `uvicorn --reload` выполняет `python -m plummy.check_config`: при невалидной конфигурации
  контейнер завершается с кодом 1 и не перезапускается (как `prod`-таргет).
- Volumes: `postgres-data`, `redis-data`, `bootstrap-data` (файл setup token; монтируется только в `migrate`, не в
  `backend`), `secrets-data` (key ring секретов; `migrate` — запись, `backend` и `worker` — только чтение; в
  `worker-ingestion` и `scheduler` не монтируется), `storage-data` (оригиналы и quarantine; `backend`, `worker`,
  `worker-ingestion`). Исходный код монтируется read-only (`src`, `alembic`, `tests`).
- `backend` (dev) доверяет `X-Forwarded-For` от Vite-прокси (`FORWARDED_ALLOW_IPS=*`, только разработка): по нему
  считаются лимиты попыток входа. В production доверенным прокси настраивается только Nginx.
- Dockerfile backend содержит таргеты `dev` и `prod` (non-root, только runtime-зависимости); Dockerfile frontend —
  `dev`, `build` и `dist` (статические файлы для Nginx Фазы 11). Контекст сборки — корень репозитория (файл
  `VERSION`, ADR-009).
- Миграции не выполняются при старте API; readiness возвращает `503`, пока ревизия БД не совпадает с head.
- Celery ([ARCHITECTURE.md §3](ARCHITECTURE.md)): брокер и хранилище результатов — тот же Redis, ключи с префиксом
  `plummy:celery:`, только JSON (pickle отклоняется), результаты хранятся 24 ч, `acks_late`. Состояние задач в Redis —
  не источник истины: состояние ingestion jobs хранится в PostgreSQL (`ingestion_jobs`). Проверка worker:
  `docker compose exec worker python -m plummy.cli worker check [--simulate-failures N]`.
- Requeue находит только jobs, которые не дошли до брокера (`queued` без `celery_task_id`); сообщение, потерянное
  внутри Redis после публикации, не обнаруживается: такой job остаётся `queued` (reindex возвращает его же, retry
  принимает только `failed`/`dead`), автоматического восстановления пока нет. Redis хранит данные в AOF.
- Production-стек (Nginx/TLS, встроенный S3, секреты файлами) — `compose.prod.yaml`, §1–§2, §8.

**Переменные окружения backend (реализовано).** Имена утверждены в задаче Фазы 1 и заменяют предложенные ниже
`PLUMMY_ENV`, `PLUMMY_LOG_LEVEL`, `PLUMMY_DATABASE_URL_FILE`, `PLUMMY_REDIS_URL_FILE`, `PLUMMY_DB_POOL_SIZE`,
`PLUMMY_CORS_ORIGINS`.

| Переменная | Значение по умолчанию | Описание |
|---|---|---|
| `APP_ENV` | `production` | `development` \| `test` \| `production`; без значения — самый строгий режим |
| `APP_DEBUG` | `false` | В `production` запрещено `true` |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL`; логи — JSON в stdout, SQL-логирование отключено |
| `SECRET_KEY` / `SECRET_KEY_FILE` | — | ≥ 32 символов; обязателен в `production` для `PROCESS_ROLE` `api`/`worker`; в `development`/`test` без значения генерируется временный ключ |
| `PROCESS_ROLE` | `api` | `api` \| `worker` \| `ingestion` \| `scheduler` \| `migrate` (шаг 11.2): в `production` `SECRET_KEY`, `PUBLIC_URL` и `SECRETS_KEYRING_FILE` обязательны только для `api` и `worker`; `APP_DEBUG=false`, `SESSION_COOKIE_SECURE=true` и `https` в `PUBLIC_URL` — для всех ролей |
| `DATABASE_URL` / `DATABASE_URL_FILE` | — (обязателен) | `postgresql+asyncpg://…` |
| `REDIS_URL` / `REDIS_URL_FILE` | — (обязателен) | `redis://…` или `rediss://…` |
| `CORS_ORIGINS` | пусто (CORS выключен) | Список origins через запятую; `*`, пути и credentials отклоняются; в `production` — только `https` (шаг 12.5) |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `5`, `5` | Ограничение пула соединений на процесс (§11) |
| `API_DOCS_ENABLED` | вкл., кроме `production` | Swagger UI `/api/v1/docs` загружает JS/CSS с публичного CDN, поэтому в `production` выключен; `true` включает явно. `/api/v1/openapi.json` доступен всегда |
| `PUBLIC_URL` | — (обязателен в `production`, только `https`) | Origin web-интерфейса. Небезопасные запросы (`POST`/`PUT`/`PATCH`/`DELETE`) принимаются только с `Origin`/`Referer` из `PUBLIC_URL` или `CORS_ORIGINS` (ADR-013). Dev compose: `http://localhost:5173` |
| `SESSION_COOKIE_SECURE` | `true` (обязательно в `production`) | `true` → cookie `__Host-pk_session` с `Secure`; `false` → `pk_session` без `Secure` (только HTTP-разработка; dev compose задаёт `false`) |
| `SESSION_IDLE_TTL` | `86400` (24 ч) | Срок простоя сессии: секунды или ISO 8601 (`PT24H`); от 5 мин до 30 дней |
| `SESSION_ABSOLUTE_TTL` | `604800` (7 дней) | Абсолютный срок сессии; от 1 ч до 90 дней, не меньше idle |
| `SETUP_MODE` | `web` | `web` — мастер `/setup` с bootstrap token; `cli_only` — web-инициализация отключена (`404`), первый администратор создаётся CLI |
| `BOOTSTRAP_DIR` | `/var/lib/plummy/bootstrap` | Каталог файла `setup-token` (`0700`, файл `0600`) |
| `BOOTSTRAP_TOKEN_TTL` | `86400` (24 ч) | Срок действия bootstrap token; от 10 мин до 30 дней |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` (uvicorn) | Адреса прокси, которым доверяется `X-Forwarded-For` (IP клиента для rate limit и audit) |
| `SECRETS_KEYRING_FILE` | — (обязателен в `production` для `api`/`worker`; dev compose: `/var/lib/plummy/secrets/keyring.json`) | Key ring шифрования секретов ([ADR-016](adr/0016-secret-encryption.md), §4); без него сохранение API keys отвечает `503 secrets_not_configured` |
| `CONFIG_FILE` | — (пустой allowlist) | TOML-файл Instance Settings со структурой, сейчас `[[network.allowlist]]` ([ADR-007](adr/0007-network-access-and-local-llm.md), §7.3); ≤ 256 КБ, невалидный файл → отказ старта |
| `WORKER_CONCURRENCY` | `2` (dev compose) | Число процессов Celery worker (флаг `--concurrency` в команде сервиса, не настройка приложения) |
| `INGESTION_MEMORY_LIMIT` | `4g` (dev compose) | `mem_limit` контейнера `worker-ingestion` (Compose, не настройка приложения) |
| `STORAGE_BACKEND` | `local` | `local` (каталог `STORAGE_PATH`) или `s3` ([ADR-022](adr/0022-s3-compatible-storage.md)) |
| `S3_ENDPOINT_URL`, `S3_BUCKET` | — (обязательны при `s3`) | `http(s)://host[:port]` без пути; бакет создаёт администратор. Plain `http` и внутренние адреса — только с записью allowlist `purposes = ["storage"]` (проверяется при старте; CIDR — адрес S3-сервера `/32`, чтобы не включить PostgreSQL и Redis) |
| `S3_ACCESS_KEY_ID` / `_FILE`, `S3_SECRET_ACCESS_KEY` / `_FILE` | — (обязательны при `s3`) | Ключи учётной записи с доступом только к бакету; секрет — файлом |
| `S3_REGION`, `S3_PREFIX` | `us-east-1`, пусто | Регион подписи SigV4; префикс ключей (`a-z0-9-` сегменты, каждый с `/`), чтобы делить бакет |
| `STORAGE_PATH` | `/var/lib/plummy/storage` | Корень storage (области `quarantine` и `documents`); заменяет черновые `PLUMMY_STORAGE_PATH`, `PLUMMY_QUARANTINE_PATH` |
| `QUARANTINE_TTL` | `86400` (24 ч) | Срок хранения файлов в quarantine до cleanup; от 10 мин до 7 дней |
| `UPLOAD_MAX_BYTES` | `52428800` (50 МБ) | Наибольший принимаемый upload (проверяется при потоковой записи); от 1 КБ до 2 ГБ; заменяет черновое `PLUMMY_UPLOAD_MAX_BYTES` |
| `RATE_LIMIT_GENERAL_PER_MINUTE` | `300` | Общий лимит запросов к API в минуту: на пользователя, без сессии — на IP; health и SSE-поток чата не считаются; от 10 до 100 000 (шаг 12.5, SECURITY_MODEL §13) |
| `CHAT_GENERATION_TIMEOUT_SECONDS` | `120` | Жёсткий предел одного ответа чата вместе с retrieval ([ADR-019](adr/0019-answer-generation-in-api-process.md)); от 10 до 600 с. Прокси перед API должен держать SSE-соединение не меньше этого |

`NAME` и `NAME_FILE` одновременно — ошибка конфигурации. Невалидная конфигурация → сообщение без значений секретов
и exit code 1. Frontend: `VITE_API_BASE_URL` (по умолчанию `/api/v1`) — публичное значение, попадает в bundle.

**Первый запуск в разработке.** После `docker compose up --build` шаг `migrate` выпускает setup token. Прочитать его
и открыть http://localhost:5173 (откроется `/setup`):

```bash
docker compose run --rm migrate cat /var/lib/plummy/bootstrap/setup-token
```

Повторные `up` действующий токен не меняют; после настройки `init-token` ничего не выпускает и удаляет файл.
Полный сброс локальной инсталляции — `docker compose down -v` (удаляет все данные разработки).

**Зависимости frontend в dev.** Контейнер `frontend` монтирует `src/`, а `node_modules` берёт из образа. При
старте он сравнивает смонтированный `apps/frontend/package-lock.json` с тем, из которого собран образ; если они
различаются, контейнер останавливается с подсказкой `docker compose up --build frontend` (без проверки Vite падал бы
на импорте нового пакета).

**E2E-стек (Playwright, Фаза 8).** `compose.e2e.yaml` накладывается на `compose.yaml` в отдельном проекте
`plummy-e2e`: порты не публикуются (стек работает рядом с dev-стеком), фейковый OpenAI-compatible провайдер
`fake-llm` (`deploy/e2e/fake_llm.py`, только stdlib) живёт во внутренней сети `llm` с фиксированной подсетью, и
backend/worker достигают его только через запись allowlist в тестовом `CONFIG_FILE` (`deploy/e2e/plummy.toml`,
[ADR-007](adr/0007-network-access-and-local-llm.md)). `worker-ingestion` собирается из образа `dev` (тесты
загружают Markdown; Docling проверяет job CI «Ingestion»). Раннер `e2e` (образ Playwright) работает в сетевом
пространстве `frontend`, поэтому браузер открывает доверенный origin `http://localhost:5173`; том bootstrap
смонтирован ему только для чтения — global setup создаёт первого администратора на свежем стеке. С Фазы 9 в той же
сети работает `fake-google` (`deploy/e2e/fake_google.py`, только stdlib): OAuth с проверкой client secret,
redirect URI и PKCE и Drive v3 с фиксированным деревом; backend и worker получают тестовый OAuth client
(`GOOGLE_OAUTH_CLIENT_ID`/`GOOGLE_OAUTH_CLIENT_SECRET` — значения фейка) и адреса Google из секции
`[connectors.google_drive]` тестового `CONFIG_FILE`, браузер открывает страницу согласия фейка по имени сервиса. С
Фазы 10 рядом работает `fake-microsoft` (`deploy/e2e/fake_microsoft.py`, только stdlib): Entra ID одного tenant
(PKCE, client secret, ротация refresh token) и Microsoft Graph с сайтом, библиотекой и delta; backend и worker
получают `MICROSOFT_OAUTH_*` фейка и адреса из секции `[connectors.microsoft_365]`. Всё это — тестовая
инфраструктура, в production не используется.

```bash
export COMPOSE_PROJECT_NAME=plummy-e2e COMPOSE_FILE=compose.yaml:compose.e2e.yaml COMPOSE_PROFILES=e2e
docker compose up --build --detach --wait backend worker worker-ingestion scheduler frontend fake-llm fake-google fake-microsoft
docker compose run --rm -T e2e
docker compose down --volumes
```

## 1. Модель развёртывания (реализовано, шаг 11.2)

- Docker + Docker Compose + Nginx ([ADR-023](adr/0023-production-deployment.md)). Два самостоятельных файла:
  `compose.yaml` — разработка (§0), `compose.prod.yaml` — production (не overlay: в dev-файле bind mounts кода, порты и
  значения по умолчанию, недопустимые в production). Один хост для MVP; `backend` и `worker` масштабируются
  независимо (§9).
- Наружу публикуется **только** Nginx (`80` → редирект, `443`). PostgreSQL, Redis, S3, API и worker портов не
  публикуют. Образы — prod-таргеты Dockerfile, код не монтируется, корневые ФС сервисов приложения `read_only`
  (`tmpfs` для `/tmp`), `no-new-privileges`, `cap_drop: ALL`, лимиты `cpus`/`mem_limit`/`pids_limit`.

### 1.1 Установка

```bash
sudo sh deploy/prod/init-secrets.sh                  # секреты и key ring в deploy/prod/secrets/ (значения не печатаются)
cp deploy/prod/prod.env.example deploy/prod/.env     # PUBLIC_URL, имя сервера, storage, лимиты
# сертификат: deploy/prod/certs/fullchain.pem и privkey.pem (или PLUMMY_TLS_CERT / PLUMMY_TLS_KEY в .env)
docker compose -f compose.prod.yaml --env-file deploy/prod/.env --profile s3 up -d --build --wait
docker compose -f compose.prod.yaml --env-file deploy/prod/.env run --rm migrate \
  cat /var/lib/plummy/bootstrap/setup-token          # токен первоначальной настройки (§6)
```

Затем открыть `PUBLIC_URL`, создать первого администратора по токену. **Сразу** скопировать
`deploy/prod/secrets/keyring.json` в отдельное защищённое место (§4). `--profile s3` включает встроенный S3-сервер
(versitygw) и одноразовый `s3-init` (учётная запись приложения с ролью `user` и бакет `S3_BUCKET`, идемпотентно); с
внешним S3 профиль не указывается, а в `.env` задаются `S3_ENDPOINT_URL` (`https`), `S3_BUCKET`, `S3_REGION`, ключи —
в `secrets/s3_access_key_id` и `secrets/s3_secret_access_key` до первого запуска. Свой `CONFIG_FILE` (локальная
модель, §7.3) — переменная `PLUMMY_CONFIG_FILE` (по умолчанию `deploy/prod/plummy.toml`, в нём запись allowlist
встроенного S3).

Сертификат и ключ монтируются как Docker secrets; Nginx читает их как uid `101`, поэтому ключ — `0400` с владельцем
`101` (или `0444` в каталоге `0700`). Получение и продление сертификата (ACME) — вне стека: после продления
`docker compose … restart nginx`.

## 2. Сервисы (`compose.prod.yaml`)

| Сервис | Назначение | Сети | Секреты | Volumes |
|---|---|---|---|---|
| `nginx` | TLS, SPA, reverse proxy на API, SSE без буферизации (таргет `web` образа frontend) | `public`, `app` (`172.30.200.10`) | TLS cert/key | — |
| `backend` | FastAPI (`PROCESS_ROLE=api`) | `app`, `data`, `egress` | БД (runtime-роль), Redis, `SECRET_KEY`, key ring, S3 | `storage-data`*, анонимный `/var/tmp` |
| `worker` | Embeddings, sync, maintenance (`default`, `embeddings`, `maintenance`, `connectors`) | `data`, `egress` | как у `backend` | как у `backend` |
| `worker-ingestion` | Parsing (очередь `ingestion`, образ `ingestion` с Docling), без интернета | `data` | БД (runtime-роль), Redis, S3 | как у `backend` |
| `scheduler` | Celery beat, ровно один | `data` | БД (runtime-роль), Redis | — |
| `migrate` | Одноразовый шаг: `alembic upgrade head` + `db grant-runtime` + `setup init-token`; retention аудита по таймеру хоста (§6.1) | `data` | БД (владелец схемы, `migration_database_url`), Redis | `bootstrap-data` |
| `postgres` | PostgreSQL + pgvector; роли по ADR-011 и ADR-024 (`deploy/prod/postgres-initdb`) | `data` | пароли `postgres`, владельца и runtime-роли | `postgres-data` |
| `redis` | Брокер, rate limits, locks; `requirepass` из секрета | `data` | `redis_conf` | `redis-data` |
| `s3`, `s3-init` | Встроенный S3-сервер (profile `s3`): versitygw, admin API только на loopback | `data` (`172.30.201.10`) | ключи root и приложения | `s3-data`, `s3-iam` |

\* только при `STORAGE_BACKEND=local` (один хост). `PROCESS_ROLE` (`api`, `worker`, `ingestion`, `scheduler`,
`migrate`) определяет, какие секреты процессу обязательны в production: `SECRET_KEY`, `PUBLIC_URL` и
`SECRETS_KEYRING_FILE` — только `api` и `worker`; `APP_DEBUG=false`, `SESSION_COOKIE_SECURE=true` и `https` в
`PUBLIC_URL` — для всех. Загрузки и входные файлы парсера спулятся в `TMPDIR=/var/tmp` — анонимный volume контейнера
(место на диске не меньше `UPLOAD_MAX_BYTES` на одновременную операцию), а не в `tmpfs`.

## 3. Конфигурация окружения (Instance Settings, ADR-006)

Источники по приоритету: переменные окружения → `*_FILE` (Docker secrets) → конфигурационный файл
`CONFIG_FILE` (TOML) для структурированных значений. Невалидная конфигурация → отказ старта с понятной
ошибкой. `.env.example` содержит все переменные без секретных значений.

| Группа | Переменные | Секрет |
|---|---|---|
| Общие | `PUBLIC_URL`, `APP_ENV`, `LOG_LEVEL` (реализовано, §0) | — |
| БД | `DATABASE_URL_FILE` (у сервисов — runtime-роль, у `migrate` — владелец схемы), `DB_POOL_SIZE`; `DATABASE_RUNTIME_ROLE` (только `migrate`, `db grant-runtime`); `AUDIT_RETENTION_DAYS` (365, минимум 30; `maintenance prune-audit`) — [ADR-024](adr/0024-database-owner-and-runtime-roles.md) | да (URL) |
| Redis | `PLUMMY_REDIS_URL_FILE` | да |
| Шифрование | `SECRETS_KEYRING_FILE` (key ring с `key_id`, реализовано, §0; заменяет черновое `PLUMMY_SECRETS_KEY_FILE`) | **да, хранится вне БД и отдельно от backup** |
| Storage | `STORAGE_BACKEND`, `STORAGE_PATH`, `QUARANTINE_TTL` (реализовано, §0); `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION`, `S3_PREFIX`, `S3_ACCESS_KEY_ID_FILE`, `S3_SECRET_ACCESS_KEY_FILE` — шаг 11.1, [ADR-022](adr/0022-s3-compatible-storage.md) | частично |
| Setup | `SETUP_MODE` (`web` \| `cli_only`), `BOOTSTRAP_DIR`, `BOOTSTRAP_TOKEN_TTL` (реализовано, §0) | — |
| Безопасность | `CORS_ORIGINS`, `SESSION_IDLE_TTL`, `SESSION_ABSOLUTE_TTL`, `SESSION_COOKIE_SECURE` (реализовано, §0); `RATE_LIMIT_GENERAL_PER_MINUTE` (шаг 12.5); остальные лимиты rate limit — константы (ADR-013 §4) | — |
| Лимиты | `UPLOAD_MAX_BYTES`, `CHAT_GENERATION_TIMEOUT_SECONDS` (реализовано, §0); лимиты страниц/распаковки/времени парсинга — пока константы `ParseLimits`; одновременные генерации (2 на пользователя) — константа ([ADR-019](adr/0019-answer-generation-in-api-process.md)) | — |
| Jobs | `WORKER_CONCURRENCY` (реализовано в dev compose, §0); retry/backoff — в определении каждой задачи | — |
| SMTP | `PLUMMY_SMTP_HOST`, `PLUMMY_SMTP_USER`, `PLUMMY_SMTP_PASSWORD_FILE` | да |
| Коннекторы | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` / `GOOGLE_OAUTH_CLIENT_SECRET_FILE` (Фаза 9, §7.4, [ADR-020](adr/0020-google-drive-connector.md)); `MICROSOFT_OAUTH_TENANT_ID`, `MICROSOFT_OAUTH_CLIENT_ID`, `MICROSOFT_OAUTH_CLIENT_SECRET` / `MICROSOFT_OAUTH_CLIENT_SECRET_FILE` (Фаза 10, §7.5, [ADR-021](adr/0021-microsoft-365-connector.md)) | да (secret) |
| Сеть | `[[network.allowlist]]` в `CONFIG_FILE` (ADR-007, реализовано; заменяет черновое `PLUMMY_CONFIG_FILE`) | — |

Изменение Instance Settings → перезапуск `api` и worker-сервисов. Через web-интерфейс Instance Settings не меняются.

## 4. Секреты

- Все секреты — файлы (Docker secrets), а не значения в `.env` или переменных окружения: `docker inspect` их не
  показывает (проверяется в CI). `deploy/prod/init-secrets.sh` один раз генерирует случайные пароли PostgreSQL, Redis и
  S3-сервера, `SECRET_KEY`, key ring, `database_url` и `redis_url`; существующие файлы не перезаписывает. Для БД —
  две роли (ADR-024): `db_password` и `migration_database_url` — владелец схемы, только для `migrate`;
  `db_runtime_password` и `database_url` — runtime-роль всех остальных сервисов. Прежний `database_url` с ролью
  владельца (до 0.5.0) скрипт переносит в `migration_database_url`. Под `root`
  каждому файлу назначается uid читающего сервиса (`10001` — приложение, `999` — PostgreSQL и Redis, `0` — S3-сервер) и
  режим `0400`; без `root` (рабочая станция) — `0444` в каталоге `0700`, и API предупреждает о доступном key ring.
  Каталог `deploy/prod/secrets/` — в `.gitignore`.
- OAuth client secrets коннекторов: файл в `deploy/prod/secrets/` и override-файл с секретом и переменной
  `GOOGLE_OAUTH_CLIENT_SECRET_FILE` / `MICROSOFT_OAUTH_CLIENT_SECRET_FILE` для `backend` и `worker` (client ID — в
  `.env`).
- **Key ring секретов** ([ADR-016](adr/0016-secret-encryption.md)) — JSON-файл `SECRETS_KEYRING_FILE`:
  `{"primary": "<key id>", "keys": {"<key id>": "<base64url, 32 байта>"}}`. В production создаётся при установке и
  монтируется файлом с правами `0400`; в dev compose его создаёт `migrate` (`secrets init-keyring`, существующий файл
  не перезаписывается). Потеря key ring делает недоступными все сохранённые API keys (их придётся ввести заново);
  резервная копия хранится **отдельно** от backup БД.
- Команды CLI (на сервере; в dev — `docker compose run --rm migrate python -m plummy.cli …`):
  `secrets generate-key` (печатает новую запись key ring — единственная команда, выводящая ключ), `secrets status`
  (число записей по `key_id`), `secrets rotate` (перешифровать записи на старых ключах), `secrets init-keyring`.
- Ротация ключа: добавить новый ключ в key ring и сделать его `primary` → перезапустить `api` и worker →
  `secrets rotate` → когда `secrets status` показывает 0 записей на старых ключах, удалить старый ключ из файла.
  Readiness сообщает `secrets: fail`, если ключ удалили раньше.
- Секреты никогда не пишутся в логи; `GET /api/v1/system/settings` показывает только `configured`, `primary_key_id` и
  число ключей.

## 5. Persistent volumes

| Volume | Содержимое | Backup | Примечание |
|---|---|---|---|
| `postgres-data` | БД, векторы, audit | **Да** (`pg_dump`, §10) | Основной источник истины |
| `s3-data` | Бакеты встроенного S3-сервера (оригиналы, extracted text, quarantine) | **Да** (через `objects.tar`, §10) | Не копировать файлы volume: метаданные в xattrs |
| `s3-iam` | Учётные записи встроенного S3-сервера | Нет | Пересоздаётся `s3-init` из секретов |
| `storage-data` | Документы при `STORAGE_BACKEND=local` | **Да** (через `objects.tar`, §10) | Только один хост |
| `redis-data` | Очереди и кэш | Нет | Состояние jobs восстанавливается из БД |
| `bootstrap-data` | Файл bootstrap token (`0600`, каталог `0700`) | Нет | Монтируется только в `migrate`, не в `backend`; после настройки файл удаляет следующий запуск `init-token` |
| `deploy/prod/secrets/` | Секреты и key ring (каталог хоста) | **Key ring — отдельно** от backup данных | §4 |

## 6. Bootstrap (ADR-006, реализовано)

1. Подготовить окружение и секреты (`.env.example`); в production задать `PUBLIC_URL` (https) и `SECRET_KEY`.
2. Подготовить роль приложения, БД и расширения (§6.1); запустить `postgres` и `redis`.
3. Выполнить `migrate`: `alembic upgrade head` и `python -m plummy.cli setup init-token`. Токен (256 бит, срок
   `BOOTSTRAP_TOKEN_TTL`, по умолчанию 24 ч) записывается в `BOOTSTRAP_DIR/setup-token` (`0600`); в БД хранится
   только SHA-256, в вывод и логи попадает только путь к файлу.
4. Запустить `api` (и остальные сервисы); дождаться readiness.
5. Прочитать токен на сервере (`cat` файла в контейнере `migrate`) и открыть `PUBLIC_URL` — frontend перенаправит
   на `/setup`.
6. Ввести токен и данные первого администратора (email, имя, пароль 12–128 символов). Backend в одной транзакции с
   блокировкой `instance_state` создаёт пользователя с ролью `admin`, отмечает инсталляцию настроенной, гасит токен
   и открывает сессию. После этого `POST /setup/initialize` всегда отвечает `409`.
7. Файл токена удаляется следующим запуском `init-token` (каждый `migrate`); вручную — `rm` в volume.

**CLI на сервере** (`python -m plummy.cli`, запускается в контейнере `migrate`/`backend`; пароли — скрытым вводом или
`--password-stdin`, никогда аргументом):

| Команда | Назначение |
|---|---|
| `setup init-token` | Выпустить токен, если инсталляция не настроена; действующий токен, совпадающий с файлом, не меняется; потерянный файл или истёкший токен — перевыпуск |
| `setup init-token --rotate` | Принудительно выпустить новый токен (старый перестаёт действовать) — токен утёк или потерян |
| `setup status` | Настроена ли инсталляция, включён ли web-setup, есть ли файл токена |
| `admin create --email … --display-name … [--password-stdin]` | Создать администратора. На ненастроенной инсталляции это и есть первоначальная настройка (токен гасится) |
| `admin reset-password --email … [--password-stdin]` | Новый пароль; все сессии пользователя отзываются |

Пример (dev compose): `docker compose run --rm migrate python -m plummy.cli setup init-token --rotate`.

**Потерян токен до завершения настройки.** Публичного механизма нет: только доступ к серверу. Выполнить
`setup init-token --rotate` и прочитать новый файл, либо создать администратора `admin create` (в том числе при
`SETUP_MODE=cli_only`). Каждое действие пишется в audit (`setup.token_issued`, `setup.completed`,
`admin.created_via_cli`).

**Потерян доступ администратора после настройки.** `admin reset-password` или `admin create` на сервере (audit
`admin.password_reset_via_cli` / `admin.created_via_cli`). Web-setup повторно не открывается: состояние хранится
флагом `instance_state.initialized_at`, а не выводится из числа пользователей.

### 6.1 PostgreSQL: роли и расширения (ADR-011, ADR-024, реализовано)

Выполняется администратором БД **до** первого запуска `migrate`. Ни приложение, ни миграции не получают прав
суперпользователя. Ролей две ([ADR-024](adr/0024-database-owner-and-runtime-roles.md)):

- **владелец схемы** (`PLUMMY_DB_USER`, по умолчанию `plummy`) — владеет БД и таблицами; под ним работают только
  `migrate`, backup/restore и `maintenance prune-audit`;
- **runtime-роль** (`PLUMMY_DB_RUNTIME_USER`, по умолчанию `plummy_app`) — API и workers: чтение и запись данных, на
  `audit_logs` только `SELECT`/`INSERT`, без DDL, `TRUNCATE` и `TEMPORARY`. Права выдаёт `migrate` после каждого
  `alembic upgrade` (`python -m plummy.cli db grant-runtime`): команда сбрасывает их к списку ADR-024 §2, в том числе
  после restore и новых ревизий. В production API и workers отказываются стартовать, если их роль может изменить
  журнал аудита.

`compose.prod.yaml` делает всё это сам (`deploy/prod/postgres-initdb`). Для внешнего или управляемого PostgreSQL:

1. Ролью администратора (суперпользователь или администратор управляемого PostgreSQL) создать роли и БД:

   ```sql
   CREATE ROLE plummy LOGIN PASSWORD '<из менеджера секретов>'
     NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
   CREATE ROLE plummy_app LOGIN PASSWORD '<из менеджера секретов>'
     NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
   CREATE DATABASE plummy OWNER plummy;
   REVOKE ALL ON DATABASE plummy FROM PUBLIC;
   GRANT CONNECT ON DATABASE plummy TO plummy_app;
   ```

   `plummy_app` не должна входить в роль `plummy` (`GRANT plummy TO plummy_app` запрещён): `db grant-runtime` это
   проверяет и отказывает.
2. Подключившись к этой БД той же ролью администратора, установить расширения:

   ```bash
   psql "postgresql://<admin>@<host>:5432/plummy" -v ON_ERROR_STOP=1 -f deploy/postgres/extensions.sql
   ```

   В управляемых сервисах сначала разрешить расширения `vector` и `citext` в настройках провайдера.
3. `migrate`: `DATABASE_URL_FILE` с ролью `plummy` и `DATABASE_RUNTIME_ROLE=plummy_app`. Остальным сервисам —
   `DATABASE_URL_FILE` с ролью `plummy_app`. Учётные данные администратора в окружение приложения не передаются.
4. Запустить `migrate`. Если расширения не установлены, ревизия `0001` завершается ошибкой со списком недостающих
   расширений и ссылкой на `extensions.sql`; если нет runtime-роли — `db grant-runtime` завершается ошибкой со
   ссылкой на этот раздел. В обоих случаях сервисы не стартуют.

**Обновление инсталляции, созданной до 0.5.0** (одна роль приложения), — однократно: backup (§10) →
`sudo sh deploy/prod/init-secrets.sh` (создаёт `db_runtime_password`, переносит прежний `database_url` в
`migration_database_url` и создаёт новый `database_url`) → `sudo sh deploy/scripts/split-db-roles.sh` (создаёт
runtime-роль суперпользователем контейнера `postgres` через локальный сокет; повтор безопасен) → `docker compose …
up -d --build --wait`. Для внешнего PostgreSQL вместо скрипта выполнить `CREATE ROLE plummy_app …`, `REVOKE` и
`GRANT CONNECT` из п.1. С 1.0.0 обновление с версий до 0.5.0 — через 0.5.0 ([ADR-025](adr/0025-semver-and-contract-stability.md) §4).

**Retention журнала аудита** (1 год, `AUDIT_RETENTION_DAYS`) — задача хоста, не `scheduler`: удалять записи
может только владелец схемы. Пример systemd timer (или строка cron с той же командой):

```ini
# /etc/systemd/system/plummy-audit-retention.service
[Service]
Type=oneshot
WorkingDirectory=/opt/plummy
ExecStart=/usr/bin/docker compose -f compose.prod.yaml --env-file deploy/prod/.env run --rm --no-deps migrate python -m plummy.cli maintenance prune-audit

# /etc/systemd/system/plummy-audit-retention.timer
[Timer]
OnCalendar=weekly
Persistent=true
[Install]
WantedBy=timers.target
```

`--dry-run` только считает записи. Каждый запуск оставляет событие `maintenance.audit_pruned` (число и граница).
Под runtime-ролью (например, в контейнере `backend`) команда отказывает с подсказкой.

Новые версии могут добавить расширение или потребовать `ALTER EXTENSION … UPDATE`. Это отражается в release notes
как шаг администратора перед `migrate`.

## 7. Сетевая конфигурация (ADR-007)

### 7.1 Сегментация (реализовано, `compose.prod.yaml`)

| Сеть | Участники | Исходящий интернет |
|---|---|---|
| `public` | `nginx` | — (опубликованные порты) |
| `app` | `nginx` (`172.30.200.10`), `backend` | Нет (`internal`, `172.30.200.0/24`) |
| `data` | `backend`, `worker`, `worker-ingestion`, `scheduler`, `migrate`, `postgres`, `redis`, `s3` (`172.30.201.10`) | Нет (`internal`, `172.30.201.0/24`) |
| `egress` | `backend`, `worker` | Да — облачные провайдеры, Google/Microsoft (через `SafeHttpClient`) |
| `llm` | `backend`, `worker`, сервис локальной модели (override, §7.3) | Нет (`internal`), фиксированная подсеть |

Фиксированные адреса берутся из нижней половины подсетей, динамические — из верхней (`ip_range`), поэтому адрес
Nginx и S3-сервера никогда не достаётся другому контейнеру. `backend` доверяет `X-Forwarded-For` только от адреса Nginx
(`FORWARDED_ALLOW_IPS`); Nginx перезаписывает заголовок адресом клиента. Встроенный S3 разрешён записью allowlist
`deploy/prod/plummy.toml` с `allowed_cidrs = ["172.30.201.10/32"]` и `purposes = ["storage"]`: более широкий CIDR
включил бы PostgreSQL и Redis, что backend отклоняет при старте.

### 7.2 Второй рубеж egress

Приложение проверяет каждое соединение само (`SafeHttpClient`), но инфраструктура должна дополнительно:

- запретить контейнерам доступ к `169.254.169.254` и другим metadata endpoints (например, правила в цепочке
  `DOCKER-USER` или egress-прокси); в облаке — IMDSv2 с hop limit 1;
- не давать `worker-ingestion` выхода в интернет;
- не публиковать порт локальной модели на хосте во внешнюю сеть.

### 7.3 Локальная модель (Ollama или OpenAI-compatible)

1. Запустить сервис модели в сети `llm` с фиксированной подсетью (или на хосте) без публикации порта наружу.
2. Добавить запись в конфигурационный файл:

   ```toml
   [[network.allowlist]]
   name           = "local-ollama"
   url            = "http://ollama:11434"
   purposes       = ["llm", "embedding"]
   provider_types = ["ollama"]
   allowed_cidrs  = ["172.30.10.0/24"]   # подсеть сети llm
   ```

   Для модели на хосте (`http://host.docker.internal:11434`) на Linux нужно сопоставление host-gateway и CIDR шлюза
   Docker bridge.
3. Перезапустить `backend` и `worker` (пример override-файла с сетью `llm` — [docs/guides/local-models.md](guides/local-models.md)).
4. В приложении (Administrator) создать провайдера с тем же `base_url` и выполнить connection test.

Allowlist по умолчанию пуст; примеры выше не являются значениями по умолчанию.

### 7.4 Google Drive ([ADR-020](adr/0020-google-drive-connector.md))

1. В Google Cloud Console создать проект, включить **Google Drive API** и настроить OAuth consent screen:
   - тип **Internal** (Google Workspace) — верификация Google не нужна;
   - тип **External** — scope `drive.readonly` относится к restricted: до верификации (включая оценку безопасности
     CASA) приложение работает только для тестовых пользователей, а refresh token тестового приложения истекает
     через 7 дней.
2. Создать OAuth client типа **Web application** с Authorized redirect URI ровно
   `<PUBLIC_URL>/api/v1/data-sources/oauth/callback` (в production — `https`).
3. Передать `GOOGLE_OAUTH_CLIENT_ID` и `GOOGLE_OAUTH_CLIENT_SECRET_FILE` сервисам `backend` и `worker`, задать
   `PUBLIC_URL`, перезапустить их. `GET /api/v1/system/settings` покажет `connectors.google_drive.configured = true` и
   `redirect_uri`.
4. Подключать **отдельную служебную учётную запись Google**, которой расшарены только нужные папки: выбор папки в
   приложении не ограничивает токен — он читает весь Drive учётной записи.
5. Отзыв: «Disconnect» в приложении отзывает токен у Google; дополнительно доступ можно отозвать на
   <https://myaccount.google.com/permissions>.

Записи allowlist для Google не нужны: адреса публичные, а хосты API и загрузки зафиксированы в коде. Секция
`[connectors.google_drive]` в `CONFIG_FILE` (подмена адресов Google) допустима только вне production — для E2E с
фейковым сервером.

### 7.5 Microsoft 365 ([ADR-021](adr/0021-microsoft-365-connector.md)) — с шага 10.1

1. В Entra admin center зарегистрировать приложение (*Single tenant*), платформа **Web**, Redirect URI ровно
   `<PUBLIC_URL>/api/v1/data-sources/oauth/callback`; создать client secret.
2. API permissions → Microsoft Graph → **Delegated**: `Files.Read.All`, `Sites.Read.All`, `User.Read`,
   `offline_access`; выдать **Grant admin consent** (`Sites.Read.All` без согласия администратора не работает).
3. Передать `MICROSOFT_OAUTH_TENANT_ID` (GUID или домен; `common`/`organizations`/`consumers` отклоняются),
   `MICROSOFT_OAUTH_CLIENT_ID` и `MICROSOFT_OAUTH_CLIENT_SECRET_FILE` сервисам `backend` и `worker`, задать
   `PUBLIC_URL`, перезапустить их.
4. Подключать **отдельную служебную учётную запись**, у которой есть доступ только к нужным библиотекам: выбор папки
   не ограничивает токен — он читает всё, что видит учётная запись во всех сайтах и OneDrive tenant.
5. Отзыв: «Disconnect» удаляет токен в приложении, но Microsoft не позволяет отозвать отдельный токен. Отзовите
   согласие в <https://myapps.microsoft.com> (учётная запись) или удалите согласие корпоративного приложения в Entra
   (администратор).

Client secret Entra имеет срок действия: продлите его до истечения, иначе обновление токенов перестанет работать.

## 8. TLS и Nginx (реализовано, шаг 11.2)

Образ — таргет `web` в `apps/frontend/Dockerfile` (официальный `nginx` на Alpine от uid 101, порты `8080`/`8443` внутри контейнера, статика
из `dist`), конфигурация — `deploy/nginx/`:

- TLS 1.2/1.3, `ssl_session_tickets off`; порт 80 отвечает только `301` на HTTPS (и `/nginx-health` для healthcheck).
- Заголовки и CSP — `deploy/nginx/security-headers.conf` ([ADR-023](adr/0023-production-deployment.md) §5): HSTS
  `max-age=31536000`, CSP со `style-src 'self' 'unsafe-inline'` только для стилей emotion/Chakra UI, `nosniff`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy`, COOP, CORP, `X-Frame-Options: DENY`; `server_tokens off`.
- `/api/` — прокси на `backend` без буферизации тела запроса (upload идёт потоком, лимит проверяет API);
  `/api/v1/chat/stream` — без буферизации ответа, `proxy_read_timeout` = `PLUMMY_STREAM_READ_TIMEOUT` (больше
  `CHAT_GENERATION_TIMEOUT_SECONDS`).
- `client_max_body_size` = `PLUMMY_CLIENT_MAX_BODY_SIZE` (немного больше `UPLOAD_MAX_BYTES`).
- `/assets/` кэшируется на год (имена с хэшем), оболочка SPA — `Cache-Control: no-cache`, остальные пути — fallback на
  `index.html`. В access log нет query string (OAuth codes, курсоры, поисковые запросы).
- Проверка в CI (job «Production stack»): `deploy/prod/smoke/smoke.py` — редирект, заголовки и CSP, readiness, Swagger
  выключен, setup → провайдер → KB → upload в S3 → индексация → поиск → SSE-ответ с проверенной цитатой → logout,
  всё через Nginx; `deploy/prod/smoke/checks.sh` — порты публикует только Nginx, секретов нет в `docker inspect`,
  key ring не доступен другим, ФС read-only, сервисы без интернета только во внутренних сетях.

## 9. Health, обновления, масштабирование

- `GET /api/v1/health/live`, `GET /api/v1/health/ready` используются healthcheck Compose; readiness не проходит, пока
  не применены миграции.
- Обновление (реализовано, шаг 11.3): backup (§10) → `git checkout` новой версии → `docker compose up -d --build
  --wait` (сервис `migrate` применяет миграции до head, остальные ждут его завершения) → проверка readiness, входа и
  поиска. Откат — restore из backup, сделанного перед обновлением: миграции вниз не поддерживаются.
- Upgrade path проверяется в CI (job «Production stack»): схему БД предыдущего релиза (`UPGRADE_FROM`, сейчас
  `v0.5.0`) создают его миграции; `split-db-roles.sh` (§6.1) на ней ничего не меняет; стек текущего коммита мигрирует её
  до head и проходит весь smoke-сценарий, проверки ролей (`checks.sh`) и restore drill.

### 9.1 Масштабирование (шаг 11.5)

Один хост с Compose — модель развёртывания MVP (ADR-023 §1); сервисы масштабируются независимо. Лимиты CPU и
памяти — переменные `*_CPUS` и `*_MEMORY_LIMIT` в `deploy/prod/.env` (`prod.env.example`), они обязательны для
worker-сервисов (§8 п.6).

| Сервис | Как масштабировать | Ограничения |
|---|---|---|
| `backend` (API, SSE) | `docker compose up -d --scale backend=N`, затем `docker compose restart nginx` | Nginx разрешает имя `backend` при старте, новые реплики видит после перезапуска. Лимит одновременных генераций на пользователя и stop-флаг — в Redis, общие для всех реплик (ADR-019). Одна реплика держит до `DB_POOL_SIZE + DB_MAX_OVERFLOW` соединений с БД (по умолчанию 5 + 5) |
| `worker` (embeddings, maintenance, sync) | `WORKER_CONCURRENCY` (процессов в контейнере) и/или `--scale worker=N` | Каждый процесс — свой пул соединений; квоты embedding-провайдера ограничивают пользу от параллелизма |
| `worker-ingestion` (Docling) | Только `--scale worker-ingestion=N`; concurrency внутри контейнера — 1 | Память: `INGESTION_MEMORY_LIMIT` (4 ГБ по умолчанию) **на реплику** |
| `scheduler` (Celery beat) | **Ровно один** | Второй экземпляр продублирует периодические задачи |
| `migrate` | Однократный | — |
| `postgres`, `redis`, `s3` | Вертикально: `POSTGRES_CPUS`, `POSTGRES_MEMORY_LIMIT` и т. д. | Сумма пулов всех процессов должна оставаться ниже `max_connections` PostgreSQL (100 по умолчанию): `реплики backend × 10 + процессы worker × 10 + реплики worker-ingestion × 10 + scheduler` |

Признаки нехватки: jobs долго в `queued` на `/admin/jobs` — мало worker (`ingestion` — `worker-ingestion`,
`embeddings` — `worker`); `429 rate_limited` в чате — лимит одновременных генераций пользователя (2) или rate limit, а не число реплик API; `OOMKilled` в
`docker inspect` — поднять `*_MEMORY_LIMIT`. Несколько хостов, внешний PostgreSQL/Redis и оркестратор — вне MVP:
внешние PostgreSQL и S3 подключаются override-файлом (§4, ADR-022).

## 10. Backup и restore (реализовано, шаг 11.3)

Решение — [ADR-023](adr/0023-production-deployment.md) §6. Скрипты работают из корня репозитория с
`compose.prod.yaml` и `deploy/prod/.env` (или с заданными `COMPOSE_FILE`/`COMPOSE_ENV_FILES`); стек должен быть
запущен.

**Backup** — `sh deploy/scripts/backup.sh /srv/plummy-backups` создаёт каталог `plummy-<время UTC>/` (режим `0700`):

| Файл | Содержимое |
|---|---|
| `database.dump` | `pg_dump --format=custom` базы приложения под ролью приложения |
| `objects.tar` | все объекты storage (`documents/`, `quarantine/`) через `StorageProvider`: одинаково для local, встроенного S3 и внешнего S3 |
| `manifest.json` | версия, ревизия Alembic, `dump_started_at`, SHA-256 обоих файлов |

- Сначала БД, потом объекты: объекты всех документов, живых на момент дампа, есть в копии.
- **Key ring (`deploy/prod/secrets/keyring.json`) и bootstrap token в backup не входят** — скрипт отказывается
  завершиться, если они попали в каталог. Key ring копируется отдельно, хранится отдельно от backup; без него
  сохранённые API-ключи и OAuth-токены не расшифровать (их придётся ввести заново).
- Шифрование и вынос копии — обязанность администратора, например:
  `tar -C /srv/plummy-backups -cf - plummy-<время> | age -r <recipient> > plummy-<время>.tar.age`. Частота backup —
  не реже раза в сутки: при потере исходной БД удаления после последнего backup вернутся (ниже).
- Объекты storage не копируются как файлы volume: встроенный S3-сервер хранит метаданные объекта (ETag, тип,
  контрольные суммы) в xattrs, которые обычный архив теряет; через порт storage метаданные пересоздаются при `put`.

**Удаления после backup.** Всё, что удалено после дампа (документы, KB, источники и их отключение, диалоги,
пользователи), после restore вернулось бы. Пока исходная инсталляция доступна, на ней:

```bash
docker compose -f compose.prod.yaml --env-file deploy/prod/.env exec -T backend \
  python -m plummy.cli maintenance export-deletions --since <dump_started_at из manifest.json> --output - > deletions.json
```

Файл содержит только идентификаторы, типы и время (формат `plummy-deletions/1`) и счётчик изменений, которые не
применяются автоматически (отключение пользователя, отзыв роли и доступа к KB, смена пароля, отзыв сессий и т. п.):
их администратор просматривает в audit исходной инсталляции и повторяет вручную.

**Restore** — на чистую инсталляцию (новый хост или `docker compose down --volumes`) с теми же секретами,
прежде всего key ring:

```bash
sh deploy/scripts/restore.sh /srv/plummy-backups/plummy-<время> --deletions deletions.json
```

Порядок: проверка SHA-256 → PostgreSQL и Redis → проверка, что БД пуста (иначе отказ) → `pg_restore` (без записей
расширений: их ставит initdb, ADR-011; права не восстанавливаются — их заново выдаёт `migrate`, ADR-024) → storage,
`migrate` (upgrade до текущей версии и `db grant-runtime`) и API → `maintenance
import-objects` (существующие объекты не перезаписываются, повтор безопасен) → `maintenance reapply-deletions` → все
остальные сервисы, включая Nginx. Удаления применяются **до** того, как Nginx откроет доступ пользователям; каждое —
теми же use cases, что у API, от имени системы с audit `maintenance.deletion_reapplied`; уже удалённое пропускается,
удаление последнего администратора отклоняется. Без файла удалений нужен явный `--no-deletions`.

После restore: вход, поиск и цитата в чате; при потере key ring — повторный ввод секретов провайдеров и
переподключение источников.

**Restore drill** — job CI «Production stack» при каждом push: `smoke.py run` (два документа) → `backup.sh` →
`smoke.py delete-document` → `export-deletions` → `down --volumes` → `restore.sh` → `smoke.py verify-restore`
(удалённый документ — 404 и не находится поиском, сохранённый находится и переиндексируется из восстановленного
объекта, ответ с проверенной цитатой). Вручную — перед каждым обновлением на копии.

**Backup retention.** Удалённые данные остаются в backup до истечения его срока хранения (SECURITY_MODEL §16.2):
срок хранения копий администратор выбирает с учётом этого.

## 11. Troubleshooting

| Симптом | Где смотреть |
|---|---|
| Readiness не проходит | Логи `api`: БД, Redis, storage, неприменённые миграции, ошибки конфигурации |
| `migrate`: `Required PostgreSQL extensions are not installed` | Расширения не установлены администратором → §6.1 |
| `setup_token_invalid` | Токен истёк или погашен → `python -m plummy.cli setup init-token --rotate` (§6) |
| Провайдер: `endpoint_not_allowed` / `egress_denied` | Запись allowlist: URL, `purposes`, `allowed_cidrs`; фактический IP сервиса |
| Ответ чата приходит одним куском | Буферизация SSE в Nginx |
| Документы зависли в `processing` | Страница `/admin/jobs`: статус, stage, attempts. `queued` долго — `docker compose ps` (worker, `worker-ingestion`, `scheduler` healthy?), лимиты ресурсов; `scheduler` периодически переотправляет `queued` jobs, которые брокер не принял (для sync — и зависшие `running`). Ненужный job — «Stop», затем «Retry» |
| Job `failed`: `provider_auth_failed`, `provider_model_not_found` | Ключ или модель embedding-провайдера: `/admin/providers` → Test; после исправления — «Retry» |
| Job `dead`: `attempts_exhausted` (`provider_timeout`, `provider_rate_limited`, `provider_unavailable`, `transient_error` до этого) | Доступность провайдера и квоты; allowlist (`egress_denied` в логах worker); «Retry» после восстановления |
| Job `failed`: `unsupported_media_type`, `archive_limits_exceeded`, `unsafe_content`, `no_text`, `extraction_invalid` | Файл отклонён и не хранится: повтор даст тот же результат; конвертировать файл или проверить лимиты (`UPLOAD_MAX_BYTES`, лимиты архивов) |
| Job `failed`: `embedding_dimensions_mismatch` | Размерность профиля не совпадает с моделью → новый профиль с верной размерностью и reindex KB |
| Job `cancelled`: `cancelled_by_admin` / `document_deleted`, `source_*`, `superseded_by_sync`, `resource_unavailable` | Остановлен администратором (можно «Retry») / документ или источник удалён, изменён или заменён новой версией — повтор не нужен |
| Sync `failed`: `connector_auth_revoked` | Доступ к Google/Microsoft отозван: источник в `error`, переподключить в «External sources» карточки KB |
| Restore: `refusing: the database is not empty` | Restore только на пустые volumes: `docker compose down --volumes` или новый хост (§10) |
| После restore провайдеры не работают (`secrets` в readiness) | Не тот key ring: восстановить `deploy/prod/secrets/keyring.json` исходной инсталляции или ввести секреты заново (§4, §10) |
