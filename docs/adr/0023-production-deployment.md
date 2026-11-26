# ADR-023: Production Deployment with Docker Compose

- **Status:** Accepted
- **Date:** 2026-11-26 (утверждено владельцем проекта вместе с планом Фазы 11: CSP `style-src 'self' 'unsafe-inline'`,
  retention audit logs — Фаза 12)

## Context

До Фазы 11 есть только dev-стек (`compose.yaml`): Vite dev server, `uvicorn --reload`, исходники в bind mounts,
учётные данные по умолчанию, без TLS, CSP и Nginx. В Dockerfile backend и frontend уже есть таргеты `prod`,
`ingestion` и `dist`, но они нигде не используются. [ADR-003](0003-technology-stack.md) фиксирует Docker Compose и
Nginx; [DEPLOYMENT.md](../DEPLOYMENT.md) §5–§10 описывает целевую модель (volumes, сети, TLS, backup), но многое в ней
помечено [Proposed]. [SECURITY_MODEL.md §12](../SECURITY_MODEL.md) оставил открытым, как совместить CSP со стилями
Chakra UI/emotion; §16.2 — как повторно применить удаления после restore.

## Decision

### 1. Отдельный `compose.prod.yaml`

- Самостоятельный файл (не overlay на `compose.yaml`): dev-файл содержит bind mounts, порты `127.0.0.1` и значения
  по умолчанию, которые в production недопустимы. Запуск: `docker compose -f compose.prod.yaml --env-file
  deploy/prod/.env up -d`.
- Образы — только prod-таргеты: `backend`/`worker`/`scheduler`/`migrate` из `prod`, `worker-ingestion` из `ingestion`,
  `nginx` — свой таргет со статикой frontend из `dist` и конфигурацией `deploy/nginx/`. Код в контейнеры не
  монтируется; файловые системы приложений — `read_only` с `tmpfs` для `/tmp`.
- `APP_ENV=production`; `migrate` — одноразовый шаг (`alembic upgrade head`, `setup init-token`), без
  `secrets init-keyring` (key ring создаёт администратор, §3).
- Profile `s3` включает встроенный S3-сервер (versitygw, [ADR-022](0022-s3-compatible-storage.md) Implementation
  notes) и его init-шаг (бакет, учётная запись приложения только с этим бакетом); без него используется внешний S3 ([ADR-022](0022-s3-compatible-storage.md)). PostgreSQL и Redis входят
  в стек; внешний управляемый PostgreSQL — через override-файл (DEPLOYMENT).
- Resource limits (`cpus`, `mem_limit`, `pids_limit`) у каждого сервиса; значения по умолчанию задаются переменными.
- Healthchecks у всех долгоживущих сервисов; `restart: unless-stopped`, кроме `migrate` и init-шага S3-сервера.
- Масштабирование: `backend` и `worker` — любое число реплик, `worker-ingestion` — по памяти, `scheduler` — ровно один.

### 2. Сети

| Сеть | `internal` | Участники |
|---|---|---|
| `public` | нет | `nginx` (единственный сервис с опубликованными портами 80/443) |
| `app` | да, фиксированная подсеть | `nginx`, `backend` |
| `data` | да, фиксированная подсеть | `backend`, `worker`, `worker-ingestion`, `scheduler`, `migrate`, `postgres`, `redis`, `s3` |
| `egress` | нет | `backend`, `worker` (LLM, embeddings, коннекторы — через `SafeHttpClient`) |

`worker-ingestion`, `scheduler`, `postgres`, `redis` и `s3` выхода в интернет не имеют. Локальная модель
подключается сетью `llm` по DEPLOYMENT §7.3. Фильтрация metadata endpoints на хосте (`DOCKER-USER`) остаётся
обязанностью администратора (DEPLOYMENT §7.2).

### 3. Секреты

- Все секреты — Docker secrets (файлы `0400` в `deploy/prod/secrets/`, каталог в `.gitignore`): `SECRET_KEY_FILE`,
  `DATABASE_URL_FILE`, `REDIS_URL_FILE`, `SECRETS_KEYRING_FILE`, `S3_*_FILE`, OAuth client secrets, пароли
  `postgres`, роли приложения, Redis и ключи S3-сервера.
- Скрипт `deploy/prod/init-secrets.sh` один раз генерирует случайные значения (не перезаписывает существующие файлы)
  и key ring; значения не печатаются.
- Redis требует пароль (`requirepass` из секрета); PostgreSQL — роли по [ADR-011](0011-database-roles-and-extensions.md).
- Сервис получает только свои секреты: `worker-ingestion` — БД, Redis, S3; `scheduler` — только Redis и БД; key ring —
  только `backend` и `worker`.

### 4. Nginx

- TLS 1.2+ с сертификатом администратора (`deploy/prod/certs/`); HTTP → `301` на HTTPS; HSTS `max-age=31536000`
  (без `preload` по умолчанию). Получение сертификата (ACME) — вне стека, описано в DEPLOYMENT.
- Статика SPA с fallback на `index.html`; `/api/` проксируется на `backend` с `X-Forwarded-For $remote_addr`
  (перезапись, не дописывание) и `X-Forwarded-Proto https`; `backend` доверяет `X-Forwarded-For` только с адреса
  Nginx в сети `app` (`FORWARDED_ALLOW_IPS`).
- SSE (`/api/v1/chat/stream`): `proxy_buffering off`, `proxy_read_timeout` больше `CHAT_GENERATION_TIMEOUT_SECONDS`.
- `client_max_body_size` немного больше `UPLOAD_MAX_BYTES`; `proxy_request_buffering off` для upload.
- Swagger UI выключен (`API_DOCS_ENABLED` пусто в production); `server_tokens off`.

### 5. Security headers и CSP

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none';
  frame-ancestors 'none'; form-action 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
```

`'unsafe-inline'` разрешён **только** для стилей: emotion вставляет `<style>` во время работы, а nonce потребовал бы
генерировать HTML на сервере для каждого запроса. Скрипты — только `'self'`, без `'unsafe-inline'` и
`'unsafe-eval'`. Ответы API дополнительно сохраняют свои заголовки (`Cache-Control: no-store` приватных ответов).
OAuth-провайдеры открываются переходом верхнего уровня, а не из `form-action`/`connect-src`.

### 6. Backup, restore и повторное применение удалений

- `deploy/scripts/backup.sh`: `pg_dump --format=custom` под ролью приложения, затем архив объектов storage
  (`maintenance export-objects`, см. Implementation notes 11.3) в каталог с меткой времени UTC и манифестом (версия
  приложения, ревизия Alembic, время начала дампа, SHA-256). Key ring и
  bootstrap token в backup **не** попадают — скрипт отказывается работать, если они оказались в целевом каталоге.
  Шифрование и вынос копии — обязанность администратора (DEPLOYMENT описывает `age`/`gpg`).
- Порядок «сначала БД, потом объекты» гарантирует, что объекты всех документов, живых на момент дампа, есть в копии,
  кроме удалённых после дампа — а их удаляет следующий шаг restore.
- Удаления после момента дампа записаны только в audit исходной БД. Перед restore (если исходная БД доступна —
  повреждение данных, откат) администратор выполняет на ней `python -m plummy.cli maintenance export-deletions
  --since <время дампа из манифеста> --output deletions.json`: идентификаторы и типы из событий `document.deleted`,
  `kb.deleted`, `data_source.deleted`, `data_source.disconnected`, `conversation.deleted` и `user.deleted`, без
  содержимого.
- `deploy/scripts/restore.sh` на чистую инсталляцию (пустые volumes): `pg_restore`, объекты обратно,
  `migrate` (upgrade до текущей версии), затем `maintenance reapply-deletions --input deletions.json` — те же use
  cases удаления, что у API (soft delete, purge, удаление credentials), от имени системного действия с audit
  `maintenance.deletion_reapplied`. Повтор идемпотентен. Restore без файла удалений требует явного
  `--no-deletions` и предупреждает, что удаления после дампа не применены: если исходная БД потеряна целиком, окно
  равно интервалу между backup (DEPLOYMENT рекомендует частоту).
- Restore drill — job CI: backup → удаление документа → `down -v` → restore → reapply → проверка входа, поиска,
  цитаты и того, что удалённый документ не находится.
- Обновление: backup → `migrate` новой версии → перезапуск сервисов → readiness. CI проверяет upgrade path: БД,
  созданная миграциями предыдущего релиза, обновляется до head.

### 7. Retention

Purge устаревших версий документов (хранить последние 3 `outdated`, DATABASE_SCHEMA §8) — задача `maintenance`
Фазы 11. Retention audit logs (1 год) откладывается до Фазы 12: роль приложения не имеет `DELETE` на `audit_logs`,
нужна отдельная роль БД и решение о ней.

## Alternatives

- **Profiles в одном `compose.yaml`.** Отклонено: dev-сервисы с bind mounts и значениями по умолчанию оставались бы в
  том же файле, ошибка выбора profile запускала бы небезопасную конфигурацию.
- **Kubernetes/Helm.** Вне MVP ([PRODUCT_SPEC.md](../PRODUCT_SPEC.md)); Compose остаётся целевой платформой до 1.0.
- **CSP с nonce для стилей.** Требует SSR или `sub_filter` с генерацией nonce в Nginx и настройку кэша emotion;
  отложено, пересматривается в Фазе 12.
- **Физический backup PostgreSQL (`pg_basebackup`, PITR).** Сложнее для одного хоста; DEPLOYMENT упоминает его как
  вариант для управляемых БД. Логический дамп достаточен для MVP-объёмов.

## Consequences

- Два compose-файла поддерживаются параллельно; CI поднимает оба (dev — тесты и E2E, prod — smoke и restore drill).
- Администратор отвечает за TLS-сертификат, фильтрацию egress на хосте, шифрование и вынос backup.
- `'unsafe-inline'` в `style-src` допускает внедрение стилей при XSS в разметку; защита от XSS по-прежнему на
  React-экранировании и Markdown без сырого HTML.

## Security considerations

- Единственная точка входа — Nginx; `backend` недоступен с хоста.
- Секреты не передаются переменными окружения, только файлами; `docker inspect` их не показывает.
- Key ring хранится и резервируется отдельно от backup БД; restore без key ring восстанавливает всё, кроме
  сохранённых секретов (их вводят заново), что документировано.
- Security gate Фазы 11: restore drill, проверка lifecycle удаления, key ring вне backup, заголовки и CSP проверяются
  в CI.

## Implementation notes

Шаги 11.2 (compose, Nginx, секреты, сети, smoke в CI), 11.3 (backup/restore, reapply-deletions, upgrade path,
drill), 11.5 (retention, security gate).

**Шаг 11.2 (реализовано).**

- `compose.prod.yaml`, `deploy/prod/` (`init-secrets.sh`, `postgres-initdb/`, `s3-init.sh`, `plummy.toml`,
  `prod.env.example`), `deploy/nginx/` и таргет `web` в `apps/frontend/Dockerfile` (`nginx-unprivileged`, порты 8080/8443
  в контейнере). Порядок установки — [DEPLOYMENT.md §1.1](../DEPLOYMENT.md).
- **`PROCESS_ROLE`** (`api`, `worker`, `ingestion`, `scheduler`, `migrate`): раньше `production` требовал
  `SECRET_KEY`, `PUBLIC_URL` и key ring от каждого процесса, что противоречило §3. Теперь их требуют только `api` и
  `worker`; проверки `APP_DEBUG`, `SESSION_COOKIE_SECURE` и `https` в `PUBLIC_URL` — для всех ролей.
- **Права секретов (уточнение §3).** Compose без swarm монтирует файл-секрет как есть, с владельцем и режимом хоста,
  а сервисы читают его своим uid. Поэтому `init-secrets.sh` под `root` назначает каждому файлу uid читающего сервиса
  и `0400`; без `root` (рабочая станция) — `0444` в каталоге `0700`, и API предупреждает о доступном key ring. В CI —
  вариант с `root`, проверка отсутствия предупреждения. TLS-сертификат и ключ — тоже Docker secrets (uid Nginx `101`).
- **Встроенный S3:** versitygw с IAM в volume `s3-iam`; admin API слушает только `127.0.0.1:7071`, `s3-init`
  использует сетевое пространство `s3` (`network_mode: service:s3`) и создаёт учётную запись с ролью `user`
  (доступ только к своему бакету) и бакет. Фиксированные адреса Nginx (`172.30.200.10`) и S3 (`172.30.201.10`) —
  вне `ip_range` динамических адресов.
- `migrate` не монтирует key ring и не получает `SECRET_KEY`; `scheduler` — только БД и Redis; `worker-ingestion` —
  БД, Redis, S3 и `CONFIG_FILE` (запись allowlist storage), `HOME=/tmp`. Спул загрузок — анонимный volume `/var/tmp`, а
  не `tmpfs`.
- CI-job «Production stack»: prod-образы, профиль `s3`, override `deploy/prod/smoke/compose.smoke.yaml` с фейковым
  провайдером E2E; `deploy/prod/smoke/smoke.py` (stdlib) проходит основной сценарий через Nginx, `checks.sh`
  проверяет порты, `docker inspect`, read-only ФС и внутренние сети.
- Сопутствующее: фейковый провайдер E2E (`deploy/e2e/fake_llm.py`) мог вернуть нулевой embedding, если хэши слов
  взаимно гасились; теперь такой вектор получает ненулевой компонент.
- Вне 11.2: OAuth client secrets коннекторов подключаются override-файлом (DEPLOYMENT §4); внешний управляемый
  PostgreSQL — override-файлом с `database_url` (без сервиса `postgres`).

**Шаг 11.3 (реализовано).**

- **Объекты — через `StorageProvider`, а не зеркалом бакета (уточнение §6).** versitygw хранит метаданные объекта
  (ETag, content type, контрольные суммы) в xattrs файлов volume: `tar` volume их теряет, а `mc mirror` потребовал бы
  ещё одного образа с доступом к S3. `python -m plummy.cli maintenance export-objects --output -` пишет в stdout tar
  (PAX) с записями `documents/<key>` и `quarantine/<key>` (новый метод порта `keys(area)`; ключи — только имена
  storage, никогда имена файлов пользователей); `import-objects --input -` принимает только такие обычные файлы и не
  перезаписывает существующие объекты. Одинаково работает для local, встроенного S3 и внешнего S3 и не требует
  доступа к volume с хоста (read-only контейнеры, uid сервисов).
- **`user.deleted` тоже применяется повторно** (в §6 его не было): удалённый после backup пользователь иначе вернулся
  бы со своими сессиями и диалогами. Защита последнего администратора действует и здесь — такое удаление
  отклоняется (`refused`) с сообщением администратору.
- Изменения после дампа, которые не являются удалениями (отключение пользователя, изменение пользователя, отзыв роли,
  доступа к KB и сессий, смена и сброс пароля, изменение групп, удаление ассистента, провайдера, модели, ротация
  секретов), не применяются автоматически: `export-deletions` считает их в `not_reapplied`, CLI выводит счётчики для
  ручной проверки по audit исходной инсталляции.
- Use cases удаления разделены на «часть в транзакции» (`delete_document_in`, `delete_knowledge_base_in`,
  `delete_source_in`, `disconnect_source_in`, `delete_user_in`) и «действия после commit» (purge, отзыв токена); API и
  `reapply-deletions` вызывают одни и те же функции. Каждое удаление — отдельная транзакция с audit
  `maintenance.deletion_reapplied` (`actor_type=system`, details: исходное действие и время). Экспорт записывает
  `maintenance.deletions_exported`. API для этих операций нет — только CLI на сервере.
- `restore.sh` отбрасывает из списка `pg_restore` записи `EXTENSION` и комментарии к ним: расширения ставит initdb под
  bootstrap superuser (ADR-011), а роль приложения не может их комментировать. Restore отказывается работать на
  непустой БД. Удаления применяются после старта API, но до старта Nginx.
- CI: restore drill (DEPLOYMENT §10) и upgrade path — схему создаёт `alembic upgrade head` образа тега `UPGRADE_FROM`
  (`v0.4.0`), затем стек коммита мигрирует её и проходит smoke и drill. Новых миграций после `0016` пока нет, поэтому
  сейчас upgrade path проверяет совместимость кода с схемой релиза; данные релиза не создаются (у `v0.4.0` нет
  production-стека).

## Related documents

[ADR-003](0003-technology-stack.md) · [ADR-006](0006-initial-setup-and-system-configuration.md) ·
[ADR-007](0007-network-access-and-local-llm.md) · [ADR-011](0011-database-roles-and-extensions.md) ·
[ADR-013](0013-auth-session-strategy.md) · [ADR-016](0016-secret-encryption.md) · [ADR-022](0022-s3-compatible-storage.md) ·
[DEPLOYMENT.md](../DEPLOYMENT.md) · [SECURITY_MODEL.md §12, §16](../SECURITY_MODEL.md)
