# PlummyKnowledge — Deployment

| | |
|---|---|
| Статус | v0.1 — целевая модель; Docker-файлы и Compose создаются в Фазах 1 и 11 |
| Источник | [PROJECT_BLUEPRINT.md](PROJECT_BLUEPRINT.md) §3, §5, §8, §11, §13 (Фазы 1, 11) |
| Решения | [ADR-006](adr/0006-initial-setup-and-system-configuration.md), [ADR-007](adr/0007-network-access-and-local-llm.md) |
| Связанные документы | [ARCHITECTURE.md §4, §10, §12](ARCHITECTURE.md), [SECURITY_MODEL.md](SECURITY_MODEL.md) |

Имена переменных, сервисов, volumes и CLI-команд ниже — **[Proposed]**; они фиксируются при реализации Фазы 1 и
обновляются здесь.

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
2. Запустить `postgres` и `redis`.
3. Выполнить `migrate`: миграции Alembic и `plummy setup init-token`. Токен (≥ 256 бит, срок по умолчанию 24 ч)
   записывается в `bootstrap/setup-token`; в лог попадает только путь.
4. Запустить `api`, worker-сервисы, `scheduler`, `nginx`; дождаться readiness.
5. Прочитать токен на сервере и открыть `PLUMMY_PUBLIC_URL` — мастер настройки (`/setup`).
6. Ввести токен, название организации, данные администратора. После успеха web-setup закрыт навсегда.
7. Удалить файл токена; настроить провайдеров и (при необходимости) allowlist.

Варианты: `PLUMMY_SETUP_MODE=cli_only` — шаги 5–6 заменяет `plummy admin create`. Токен истёк — повторить
`plummy setup init-token`. Потерян доступ администратора — `plummy admin reset-password` / `plummy admin create` на
сервере.

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
| `setup_token_invalid` | Токен истёк или погашен → `plummy setup init-token` |
| Провайдер: `endpoint_not_allowed` / `egress_denied` | Запись allowlist: URL, `purposes`, `allowed_cidrs`; фактический IP сервиса |
| Ответ чата приходит одним куском | Буферизация SSE в Nginx |
| Документы зависли в `processing` | Jobs/Sync: очередь, состояние worker, лимиты ресурсов |
