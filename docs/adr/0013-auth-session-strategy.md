# ADR-013: Auth and Session Strategy

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Blueprint требует локальную аутентификацию, Argon2id, cookies `HttpOnly/Secure/SameSite`, CSRF strategy, отзыв
сессий и rate limiting login (§5, §13 Фаза 3). Черновик [SECURITY_MODEL.md §4](../SECURITY_MODEL.md) v0.1 предлагал
короткий access token (15 мин) и refresh token с ротацией и reuse detection. Но тот же черновик требовал проверять на
**каждом** запросе, что сессия не отозвана (Redis + БД): access token всё равно становился stateful, а пара токенов
добавляла `/auth/refresh`, гонки ротации между вкладками и таблицу `refresh_tokens`. Клиент — только браузерное SPA
той же инсталляции (ADR-012); сторонних API-клиентов и мобильных приложений в MVP нет.

## Decision

### 1. Серверные сессии с непрозрачным идентификатором

- При входе сервер создаёт запись `sessions` и случайный токен `secrets.token_urlsafe(32)` (256 бит). В БД хранится
  только SHA-256 токена (`token_hash`); сам токен не хранится и не логируется.
- Токен передаётся только в cookie:

  | Режим | Cookie | Атрибуты |
  |---|---|---|
  | `SESSION_COOKIE_SECURE=true` (по умолчанию, обязательно в `production`) | `__Host-pk_session` | `HttpOnly; Secure; SameSite=Lax; Path=/` |
  | `SESSION_COOKIE_SECURE=false` (только `development`/`test`, HTTP без TLS) | `pk_session` | `HttpOnly; SameSite=Lax; Path=/` |

  `Max-Age` — до абсолютного истечения сессии. Заголовок `Authorization` не используется.
- **Проверка на каждом запросе** — один индексированный запрос к PostgreSQL (источник истины). Сессия отклоняется,
  если: не найдена; `revoked_at` задан; истёк абсолютный срок (`expires_at`); истёк срок простоя
  (`last_seen_at + SESSION_IDLE_TTL`); пользователь не `active` или удалён; сессия создана раньше
  `password_changed_at`. Отзыв действует немедленно. Кэш сессий в Redis не используется, пока нет измеренной
  потребности.
- **Сроки** (Instance Settings): простой `SESSION_IDLE_TTL` = 24 ч (от 5 мин до 30 дней), абсолютный
  `SESSION_ABSOLUTE_TTL` = 7 дней (от 1 ч до 90 дней, не меньше idle). `last_seen_at` обновляется не чаще раза в
  60 с.
- **Session fixation:** вход (login, setup) всегда создаёт новую сессию и новый токен. Если запрос пришёл с cookie
  существующей сессии, она отзывается (`revoke_reason = superseded`). Токен, присланный клиентом, никогда не
  «принимается» как новый.
- **Logout** отзывает текущую сессию (`logout`) и удаляет cookie; без действующей сессии — только удаляет cookie
  (идемпотентно, `204`).
- **Массовый отзыв** (смена/сброс пароля, отключение пользователя, действие администратора) — `revoked_at` у всех
  активных сессий пользователя; дополнительно `password_changed_at` отсекает сессии, созданные до смены пароля.
- Refresh tokens, `/auth/refresh` и таблица `refresh_tokens` **не вводятся**.

### 2. CSRF

- **Токен, привязанный к сессии:** `csrf_token = base64url(HMAC-SHA256(key = session_token, msg = "plummy-csrf-v1"))`.
  Он детерминирован для сессии, не хранится, не позволяет восстановить токен сессии и не зависит от `SECRET_KEY`.
- Сервер отдаёт `csrf_token` в теле ответов `POST /setup/initialize`, `POST /auth/login` и `GET /auth/me`. Frontend
  держит его только в памяти (Redux) и отправляет в заголовке `X-CSRF-Token` с `POST/PUT/PATCH/DELETE`. После
  перезагрузки страницы токен снова приходит из `/auth/me`. Читаемой JS-cookie нет.
- Для небезопасного метода с действующей сессией: нет или неверный `X-CSRF-Token` (сравнение constant-time) →
  `403 csrf_failed`.
- **Проверка `Origin`** для всех небезопасных методов: `Origin` (а без него — origin из `Referer`) должен совпадать с
  origin `PUBLIC_URL` или входить в `CORS_ORIGINS`; иначе `403 csrf_failed`. Если нет обоих заголовков — отказ.
  Для `POST /auth/login` и `POST /setup/initialize` сессии ещё нет, поэтому `Origin` — основная защита (в том числе
  от login CSRF).
- `SameSite=Lax` — дополнительный рубеж; `GET` не имеет побочных эффектов.

### 3. Пароли

- Argon2id (`argon2-cffi`), профиль RFC 9106 low-memory: t = 3, m = 64 MiB, p = 4 — выше минимума OWASP
  (m = 19 MiB, t = 2, p = 1). Параметры хранятся в строке хэша; при входе с устаревшими параметрами хэш
  пересчитывается. Хэширование выполняется вне event loop (`asyncio.to_thread`).
- Политика: длина 12–128 символов, без требований к составу; пароль не совпадает с email и не состоит из одного
  повторяющегося символа. Проверка по списку распространённых паролей — отдельная задача.
- Ошибка входа одинакова для неизвестного email, неверного пароля, отключённого или удалённого пользователя:
  `401 invalid_credentials`. Для неизвестного email выполняется проверка фиктивного хэша, чтобы время ответа не
  раскрывало существование учётной записи.

### 4. Ограничение попыток (Redis)

| Лимит | Значение | Ключ |
|---|---|---|
| login: IP + email | 5/мин и 20/час | IP + SHA-256(email) |
| login: IP | 30/мин | IP |
| login: неудачные попытки на email | 10 за 15 мин | SHA-256(email) |
| setup initialize | 5/мин | IP |

- Превышение → `429 rate_limited` + `Retry-After`. Ключи не содержат email в открытом виде.
- Redis недоступен → `503 service_unavailable` для login и setup (fail closed).
- Лимит по email считается одинаково для существующих и несуществующих адресов.
- IP клиента — адрес соединения после обработки `X-Forwarded-For` доверенными прокси (uvicorn,
  `FORWARDED_ALLOW_IPS`). В production доверенным прокси настраивается только Nginx.

### 5. Ответы

- `401 unauthenticated` — нет сессии, сессия не найдена, истекла или пользователь неактивен; `401 session_revoked`
  — сессия отозвана; `403 permission_denied` — недостаточно прав; `403 csrf_failed`.
- Ответы `setup` и `auth` имеют `Cache-Control: no-store`.

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| Access token (15 мин) + refresh token с ротацией (черновик SECURITY_MODEL v0.1) | Проверка отзыва на каждом запросе всё равно нужна; больше кода, гонки ротации между вкладками, отдельный endpoint и таблица без выигрыша в безопасности для same-origin SPA |
| JWT без проверки на сервере | Невозможен немедленный отзыв (§12 «revoked sessions отклоняются») |
| Токен в `localStorage` / заголовке `Authorization` | Доступен любому XSS; противоречит §5 (HttpOnly cookies) |
| Double-submit cookie `pk_csrf` | Требует читаемой JS-cookie и уязвим к подбрасыванию cookie с соседнего поддомена без префикса `__Host-`; токен, производный от сессии, проще и привязан к ней |
| Синхронизатор CSRF, хранимый в БД | Лишняя колонка и запись; производный токен даёт те же свойства |
| Сессии только в Redis | Redis — не источник истины (DEPLOYMENT §5); потеря данных Redis не должна разлогинивать или «воскрешать» сессии |

## Consequences

- Таблица `refresh_tokens` и endpoint `/auth/refresh` удаляются из спецификаций.
- Новые Instance Settings: `PUBLIC_URL`, `SESSION_IDLE_TTL`, `SESSION_ABSOLUTE_TTL`, `SESSION_COOKIE_SECURE`
  (см. DEPLOYMENT).
- Новые зависимости: `argon2-cffi`, `email-validator` (напрямую; нормализация email — в domain). Адреса
  special-use доменов (`.local`, `.test`, `.localhost`) отклоняются как `invalid_email`.
- Каждый запрос с сессией читает PostgreSQL; при росте нагрузки возможен короткий кэш с инвалидацией при отзыве
  (отдельное решение).
- Возможна блокировка входа для известного злоумышленнику email на 15 минут (лимит неудачных попыток) — осознанный
  компромисс ради защиты от перебора.

## Security considerations

- Компрометация БД не раскрывает действующие токены (только SHA-256 256-битных значений).
- XSS может прочитать CSRF-токен из памяти страницы при любой схеме CSRF; основная защита от XSS — экранирование и
  CSP (SECURITY_MODEL §12).
- Неверная настройка доверенных прокси делает лимиты по IP неэффективными; лимиты по email действуют независимо.
- Контрольные проверки: revoked sessions отклоняются (§12), SC-03/SC-04 (setup), CSRF и brute-force тесты.

## Implementation notes

- `core/security.py` — генерация и хэш токенов, вывод CSRF-токена; `infrastructure/security/passwords.py` — Argon2id;
  `api/cookies.py` — cookie сессии; `api/security.py` — CSRF, `Origin`, зависимости `CurrentContext` и
  `require_permission`; use cases — `application/auth/`.
- `apps/backend/src/plummy/infrastructure/redis/rate_limiter.py` — фиксированные окна, атомарный `INCR`+`EXPIRE`.

## Related documents

[SECURITY_MODEL.md §4](../SECURITY_MODEL.md) · [API_CONTRACTS.md §1.4, §2.0, §2.2](../API_CONTRACTS.md) ·
[DATABASE_SCHEMA.md §3](../DATABASE_SCHEMA.md) · [DEPLOYMENT.md](../DEPLOYMENT.md) ·
[ADR-006](0006-initial-setup-and-system-configuration.md) · [ADR-012](0012-single-organization-deployment.md)
