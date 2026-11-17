# PlummyKnowledge — Deployment

| | |
|---|---|
| Статус | v0.2 — среда разработки реализована (Фаза 1, §0); production-профиль — целевая модель Фазы 11 |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §3, §5, §8, §11, §13 (Фазы 1, 11) |
| Решения | [ADR-006](adr/0006-initial-setup-and-system-configuration.md), [ADR-007](adr/0007-network-access-and-local-llm.md), [ADR-011](adr/0011-database-roles-and-extensions.md) |
| Связанные документы | [ARCHITECTURE.md §4, §10, §12](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md) |

Раздел §0 описывает то, что реализовано. Остальные имена переменных, сервисов, volumes и CLI-команд — **[Proposed]**;
они фиксируются при реализации соответствующих фаз и обновляются здесь.

## 0. Среда разработки (реализовано, Фаза 1)

Запуск из корня репозитория: `docker compose up --build` (файл `compose.yaml`, `.env` не обязателен).

| Сервис | Образ / сборка | Назначение | Порты на хосте | Сети |
|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:0.8.7-pg17` | PostgreSQL 17 + pgvector | — | `data` |
| `redis` | `redis:8.6-alpine` | Redis (AOF) | — | `data` |
| `migrate` | `apps/backend/Dockerfile` (`dev`) | Одноразово: `alembic upgrade head`; API стартует только после успешного завершения | — | `data` |
| `backend` | `apps/backend/Dockerfile` (`dev`) | FastAPI, `uvicorn --reload`; healthcheck = `/api/v1/health/ready` | `127.0.0.1:8000` | `data`, `edge` |
| `frontend` | `apps/frontend/Dockerfile` (`dev`) | Vite dev server; `/api` проксируется на `backend` (same-origin, без CORS) | `127.0.0.1:5173` | `edge` |

- Сеть `data` — `internal: true`; порты PostgreSQL и Redis не публикуются. Учётные данные БД в `compose.yaml` —
  только для разработки (значения по умолчанию, переопределяются через `.env`).
- Роли PostgreSQL ([ADR-011](adr/0011-database-roles-and-extensions.md)): суперпользователь `postgres`
  (`POSTGRES_PASSWORD`) используется только init-скриптом `deploy/postgres/initdb` при создании volume. Скрипт
  создаёт роль приложения `PLUMMY_DB_USER` (без `SUPERUSER`; `CREATEDB` — только для integration-тестов) и её БД
  `PLUMMY_DB_NAME`, устанавливает расширения из `deploy/postgres/extensions.sql`. `migrate` и `backend`
  подключаются под ролью приложения. Volume, созданный до ADR-011, пересоздаётся: `docker compose down -v`.
- `backend` (dev) перед `uvicorn --reload` выполняет `python -m plummy.check_config`: при невалидной конфигурации
  контейнер завершается с кодом 1 и не перезапускается (как `prod`-таргет).
- Volumes: `postgres-data`, `redis-data`. Исходный код монтируется read-only (`src`, `alembic`, `tests`).
- Dockerfile backend содержит таргеты `dev` и `prod` (non-root, только runtime-зависимости); Dockerfile frontend —
  `dev`, `build` и `dist` (статические файлы для Nginx Фазы 11). Контекст сборки — корень репозитория (файл
  `VERSION`, ADR-009).
- Миграции не выполняются при старте API; readiness возвращает `503`, пока ревизия БД не совпадает с head.
- Celery worker, scheduler, Nginx, MinIO и prod-compose не входят в Фазу 1 (сессия 5 и Фаза 11).

**Переменные окружения backend (реализовано).** Имена утверждены в задаче Фазы 1 и заменяют предложенные ниже
`PLUMMY_ENV`, `PLUMMY_LOG_LEVEL`, `PLUMMY_DATABASE_URL_FILE`, `PLUMMY_REDIS_URL_FILE`, `PLUMMY_DB_POOL_SIZE`,
`PLUMMY_CORS_ORIGINS`.

| Переменная | Значение по умолчанию | Описание |
|---|---|---|
| `APP_ENV` | `production` | `development` \| `test` \| `production`; без значения — самый строгий режим |
| `APP_DEBUG` | `false` | В `production` запрещено `true` |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL`; логи — JSON в stdout, SQL-логирование отключено |
| `SECRET_KEY` / `SECRET_KEY_FILE` | — | ≥ 32 символов; обязателен в `production`; в `development`/`test` без значения генерируется временный ключ |
| `DATABASE_URL` / `DATABASE_URL_FILE` | — (обязателен) | `postgresql+asyncpg://…` |
| `REDIS_URL` / `REDIS_URL_FILE` | — (обязателен) | `redis://…` или `rediss://…` |
| `CORS_ORIGINS` | пусто (CORS выключен) | Список origins через запятую; `*`, пути и credentials отклоняются |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `5`, `5` | Ограничение пула соединений на процесс (§11) |
| `API_DOCS_ENABLED` | вкл., кроме `production` | Swagger UI `/api/v1/docs` загружает JS/CSS с публичного CDN, поэтому в `production` выключен; `true` включает явно. `/api/v1/openapi.json` доступен всегда |

`NAME` и `NAME_FILE` одновременно — ошибка конфигурации. Невалидная конфигурация → сообщение без значений секретов
и exit code 1. Frontend: `VITE_API_BASE_URL` (по умолчанию `/api/v1`) — публичное значение, попадает в bundle.

## 1. Модель развёртывания

- Docker + Docker Compose + Nginx (§3). Один хост для MVP; API и worker масштабируются независимо (§11).
- Профили Compose (§13 Фаза 11): `dev` (локальная разработка, local storage) и `prod` (TLS, MinIO, resource limits).
- Наружу публикуется **только** Nginx (443, редирект с 80). PostgreSQL, Redis, MinIO, API и worker внешних портов не
  публикуют.

## 2. Сервисы

| Сервис | Назначение | Сети | Volumes |
|---|---|---|---|
| `nginx` | TLS, SPA, reverse proxy на API, SSE без буферизации | `edge` | `certs` (ro) |
| `api` | FastAPI | `edge`, `data`, `egress`, `llm`* | — |
| `worker-ingestion` | Parsing, chunking (очередь `ingestion`) | `data` | `quarantine`, `documents` (при local storage) |
| `worker-io` | Embeddings, sync, maintenance (очереди `embeddings`, `sync`, `maintenance`) | `data`, `egress`, `llm`* | `quarantine`, `documents` |
| `scheduler` | Расписания sync, reconciliation, cleanup | `data` | — |
| `migrate` | Одноразовый шаг: Alembic + `plummy setup init-token` | `data` | `bootstrap` |
| `postgres` | PostgreSQL + pgvector | `data` | `postgres-data` |
| `redis` | Брокер, кэш, rate limits, locks | `data` | `redis-data` |
| `minio` | S3-compatible storage (`prod`) | `data` | `minio-data` |

\* только если владелец подключает локальную модель (§6). Разделение worker на `ingestion` (без исходящего
интернета) и `io` — **[Proposed]**: парсер недоверенных файлов не должен иметь сетевого выхода.

## 3. Конфигурация окружения (Instance Settings, ADR-006)

Источники по приоритету: переменные окружения → `*_FILE` (Docker secrets) → конфигурационный файл
`PLUMMY_CONFIG_FILE` (TOML) для структурированных значений. Невалидная конфигурация → отказ старта с понятной
ошибкой. `.env.example` содержит все переменные без секретных значений.

| Группа | Переменные | Секрет |
|---|---|---|
| Общие | `PLUMMY_PUBLIC_URL`, `PLUMMY_ENV`, `PLUMMY_LOG_LEVEL` | — |
| БД | `PLUMMY_DATABASE_URL_FILE`, `PLUMMY_DB_POOL_SIZE` | да |
| Redis | `PLUMMY_REDIS_URL_FILE` | да |
| Шифрование | `PLUMMY_SECRETS_KEY_FILE` (key ring с `key_id`) | **да, хранится вне БД и отдельно от backup** |
| Storage | `PLUMMY_STORAGE_BACKEND` (`local` \| `s3`), `PLUMMY_STORAGE_PATH`, `PLUMMY_QUARANTINE_PATH`, `PLUMMY_S3_ENDPOINT`, `PLUMMY_S3_BUCKET`, `PLUMMY_S3_ACCESS_KEY_FILE`, `PLUMMY_S3_SECRET_KEY_FILE` | частично |
| Setup | `PLUMMY_SETUP_MODE` (`web` \| `cli_only`), `PLUMMY_BOOTSTRAP_DIR`, `PLUMMY_BOOTSTRAP_TOKEN_TTL` | — |
| Безопасность | `PLUMMY_CORS_ORIGINS`, TTL сессий, лимиты rate limit | — |
| Лимиты | `PLUMMY_UPLOAD_MAX_BYTES`, лимиты страниц/распаковки/времени парсинга, `PLUMMY_MAX_CONCURRENT_GENERATIONS_PER_USER` | — |
| Jobs | `PLUMMY_WORKER_CONCURRENCY`, параметры retry/backoff | — |
| SMTP | `PLUMMY_SMTP_HOST`, `PLUMMY_SMTP_USER`, `PLUMMY_SMTP_PASSWORD_FILE` | да |
| Коннекторы | `PLUMMY_GOOGLE_CLIENT_ID`, `PLUMMY_GOOGLE_CLIENT_SECRET_FILE`, `PLUMMY_MS_CLIENT_ID`, `PLUMMY_MS_CLIENT_SECRET_FILE` | да |
| Сеть | `[[network.allowlist]]` в конфигурационном файле (ADR-007) | — |

Изменение Instance Settings → перезапуск `api` и worker-сервисов. Через web-интерфейс Instance Settings не меняются.

## 4. Секреты

- Все секреты — файлы (Docker secrets / смонтированные файлы с правами `0400`), а не значения в `.env`.
- Ключ шифрования секретов генерируется при установке; его потеря делает недоступными все сохранённые API keys и
  OAuth tokens. Резервная копия ключа хранится отдельно от backup БД.
- Ротация ключа: добавить новый ключ в key ring → перешифровать секреты служебной командой → удалить старый ключ.
- Секреты никогда не пишутся в логи; `GET /api/v1/system/settings` показывает только `configured: true|false`.

## 5. Persistent volumes

| Volume | Содержимое | Backup | Примечание |
|---|---|---|---|
| `postgres-data` | БД, векторы, audit | **Да** | Основной источник истины |
| `documents` | Оригиналы и extracted text (local storage) | **Да** | В `prod` вместо него — MinIO/S3 |
| `minio-data` | Object storage (`prod`) | **Да** | |
| `quarantine` | Недоверенные файлы до проверки | Нет | Отдельный volume, `noexec`, ограниченные права, cleanup по TTL |
| `redis-data` | Очереди и кэш | Нет | Состояние jobs восстанавливается из БД |
| `bootstrap` | Файл bootstrap token (`0600`) | Нет | Монтируется только в `migrate`/CLI; удаляется после настройки |
| `certs` | TLS-сертификаты | По политике владельца | |

## 6. Bootstrap (ADR-006)

1. Подготовить `.env` из `.env.example` и файлы секретов; сгенерировать ключ шифрования.
2. Запустить `postgres` и `redis`; подготовить роль приложения, БД и расширения (§6.1).
3. Выполнить `migrate`: миграции Alembic и `plummy setup init-token`. Токен (≥ 256 бит, срок по умолчанию 24 ч)
   записывается в `bootstrap/setup-token`; в лог попадает только путь.
4. Запустить `api`, worker-сервисы, `scheduler`, `nginx`; дождаться readiness.
5. Прочитать токен на сервере и открыть `PLUMMY_PUBLIC_URL` — мастер настройки (`/setup`).
6. Ввести токен, название организации, данные администратора. После успеха web-setup закрыт навсегда.
7. Удалить файл токена; настроить провайдеров и (при необходимости) allowlist.

Варианты: `PLUMMY_SETUP_MODE=cli_only` — шаги 5–6 заменяет `plummy admin create`. Токен истёк — повторить
`plummy setup init-token`. Потерян доступ администратора — `plummy admin reset-password` / `plummy admin create` на
сервере.

### 6.1 PostgreSQL: роли и расширения (ADR-011, реализовано)

Выполняется администратором БД **до** первого запуска `migrate`. Ни приложение, ни миграции не получают прав
суперпользователя.

1. Ролью администратора (суперпользователь или администратор управляемого PostgreSQL) создать роль приложения и БД:

   ```sql
   CREATE ROLE plummy LOGIN PASSWORD '<из менеджера секретов>'
     NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
   CREATE DATABASE plummy OWNER plummy;
   ```

2. Подключившись к этой БД той же ролью администратора, установить расширения:

   ```bash
   psql "postgresql://<admin>@<host>:5432/plummy" -v ON_ERROR_STOP=1 -f deploy/postgres/extensions.sql
   ```

   В управляемых сервисах сначала разрешить расширения `vector` и `citext` в настройках провайдера.
3. В `DATABASE_URL` (лучше `DATABASE_URL_FILE`) указать роль приложения. Учётные данные администратора в
   окружение приложения не передаются.
4. Запустить `migrate`. Если расширения не установлены, ревизия `0001` завершается ошибкой со списком недостающих
   расширений и ссылкой на `extensions.sql`; API не стартует.

Новые версии могут добавить расширение или потребовать `ALTER EXTENSION … UPDATE`. Это отражается в release notes
как шаг администратора перед `migrate`.

## 7. Сетевая конфигурация (ADR-007)

### 7.1 Сегментация

| Сеть | Участники | Исходящий интернет |
|---|---|---|
| `edge` | `nginx`, `api` | — |
| `data` | `api`, worker-сервисы, `scheduler`, `migrate`, `postgres`, `redis`, `minio` | Нет (`internal: true`) |
| `egress` | `api`, `worker-io` | Да — облачные провайдеры, Google/Microsoft |
| `llm` | `api`, `worker-io`, сервис локальной модели | Нет (`internal: true`), фиксированная подсеть |

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
3. Перезапустить `api` и `worker-io`.
4. В организации создать провайдера с тем же `base_url` и выполнить connection test.

Allowlist по умолчанию пуст; примеры выше не являются значениями по умолчанию.

## 8. TLS и Nginx

- TLS терминируется в Nginx; HSTS; security headers (SECURITY_MODEL.md §12).
- Для SSE: отключённая буферизация ответа, увеличенный read timeout, корректная передача `X-Accel-Buffering: no`.
- Лимит размера тела запроса согласован с `PLUMMY_UPLOAD_MAX_BYTES`.

## 9. Health, обновления, масштабирование

- `GET /api/v1/health/live`, `GET /api/v1/health/ready` используются healthcheck Compose; readiness не проходит, пока
  не применены миграции.
- Обновление: backup → `migrate` новой версии → перезапуск `api`/worker → проверка readiness. Миграции проверены на
  upgrade path (§20).
- Масштабирование: число реплик `api` и concurrency worker настраиваются независимо; DB pool ограничен на процесс
  (§11).
- Resource limits (CPU/память) обязательны для worker-сервисов, особенно `worker-ingestion` (§8 п.6).

## 10. Backup и restore (§8 п.10)

- Backup: PostgreSQL (логический или физический) + object storage, согласованные по времени; ключ шифрования —
  отдельно.
- Restore drill регулярно и перед релизом: восстановление на чистую инсталляцию, readiness, проверка входа, поиска и
  цитат.
- После restore повторно применить удаления, выполненные после момента backup (SECURITY_MODEL.md §16.2).
- Влияние backup retention на удалённые данные документируется для администратора (§8).

## 11. Troubleshooting

| Симптом | Где смотреть |
|---|---|
| Readiness не проходит | Логи `api`: БД, Redis, storage, неприменённые миграции, ошибки конфигурации |
| `migrate`: `Required PostgreSQL extensions are not installed` | Расширения не установлены администратором → §6.1 |
| `setup_token_invalid` | Токен истёк или погашен → `plummy setup init-token` |
| Провайдер: `endpoint_not_allowed` / `egress_denied` | Запись allowlist: URL, `purposes`, `allowed_cidrs`; фактический IP сервиса |
| Ответ чата приходит одним куском | Буферизация SSE в Nginx |
| Документы зависли в `processing` | Jobs/Sync: очередь, состояние worker, лимиты ресурсов |
