# ADR-010: API Error Format (RFC 9457)

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Blueprint требует единый формат ошибок API (§10). [API_CONTRACTS.md §1.5](../API_CONTRACTS.md) содержал черновик
на базе RFC 9457 (Problem Details for HTTP APIs) со статусом **[Proposed]**. Каркас Фазы 1 уже отдаёт ошибки в этом
формате: 404/405 неизвестных маршрутов, 422 валидации, 500 необработанных исключений, 503 readiness. Frontend
(`toApiError`) их разбирает. Формат — часть публичного API, поэтому его нужно утвердить до появления бизнес-endpoints
(Фаза 2), а не после.

Пагинация и хранилище idempotency keys остаются отдельной планируемой темой: этот ADR их не решает.

## Decision

1. Все ответы с ошибкой (`4xx`, `5xx`) от API `/api/v1` имеют `Content-Type: application/problem+json` и тело:

   ```json
   {
     "type": "about:blank",
     "title": "Unprocessable Content",
     "status": 422,
     "code": "validation_error",
     "detail": "Request validation failed.",
     "correlation_id": "4f1c2a9e0b7d4e6f8a1b2c3d4e5f6a7b",
     "errors": [
       { "field": "query.limit", "code": "less_than_equal", "message": "Input should be less than or equal to 100" }
     ]
   }
   ```

2. Члены RFC 9457:
   - `type` — `about:blank`. Собственные URI типов не вводятся, пока нет документации ошибок по URL.
   - `title` — стандартная фраза HTTP-статуса (англ.); не локализуется и не уточняется.
   - `status` — совпадает с HTTP-статусом ответа.
   - `detail` — необязательное пояснение для человека (англ.). Отсутствует у `5xx`.
   - `instance` не используется.
3. Расширения:
   - `code` — **обязательный**, стабильный машиночитаемый идентификатор в `snake_case`. Клиенты ветвятся только по
     `code` (и `status`), а не по `title`/`detail`. Таблица кодов — [API_CONTRACTS.md §1.5](../API_CONTRACTS.md).
     Удаление или переименование кода — несовместимое изменение API.
   - `correlation_id` — **всегда** присутствует и равен заголовку ответа `X-Request-ID`: значение клиента, если оно
     соответствует `^[A-Za-z0-9._-]{1,128}$`, иначе сгенерированный сервером идентификатор. Пользователь передаёт
     его в поддержку; по нему находятся записи логов.
   - `errors` — только у `validation_error` и `invalid_query_parameter`: список
     `{ "field", "code", "message" }`.
     - `field` — путь через точку, начиная с источника: `body.name`, `query.limit`, `path.id`, `header.x-…`.
     - `code` — тип ошибки. Для ошибок схемы DTO используется тип ошибки Pydantic (`missing`, `string_too_long`,
       `int_parsing`, …); для прикладных проверок — коды из API_CONTRACTS.
     - `message` — текст для человека (англ.).
4. Коды для статусов без прикладного кода: `400 bad_request`, `401 unauthenticated`, `403 permission_denied`,
   `404 not_found`, `405 method_not_allowed` (с заголовком `Allow`), `409 conflict`, `413 payload_too_large`,
   `415 unsupported_media_type`, `422 validation_error`, `429 rate_limited`, `503 service_unavailable`. Прочие `4xx`
   получают код `bad_request`, прочие `5xx` — `internal_error`.
5. **Исключение — readiness.** `GET /api/v1/health/ready` при `503` возвращает `application/json` со статусом
   проверок (`{"status": "not_ready", "checks": {...}}`). Этот ответ предназначен для оркестратора, а не для
   клиентов API ([API_CONTRACTS.md §2.1](../API_CONTRACTS.md)).
6. **SSE.** Ошибки до начала потока — problem+json. Ошибка после начала потока — событие `error` (§3.3) с тем же
   словарём `code` и тем же `correlation_id`.
7. Новые необязательные члены добавляются обратно совместимо, с обновлением API_CONTRACTS. Изменение смысла
   существующих членов требует нового ADR.

## Alternatives

| Вариант | Почему не выбран |
|---|---|
| Формат FastAPI по умолчанию (`{"detail": ...}`) | `detail` — то строка, то список; нет стабильного кода и correlation ID |
| Собственный конверт (`{"error": {"code", "message"}}`) | Нестандартный; RFC 9457 даёт то же самое плюс совместимость с инструментами и прокси |
| Уникальные URI в `type` вместо `code` | Требует опубликованной документации по URL; строковый `code` проще для клиентов. Не исключено позже — `code` останется |
| Локализованные `title`/`detail` | Локализация — задача frontend по `code`; сервер не знает язык интерфейса пользователя |

## Consequences

- Frontend показывает сообщения по `code` (с fallback на `detail`) и выводит `correlation_id` для обращения в
  поддержку.
- Каждый новый прикладной код добавляется в таблицу API_CONTRACTS §1.5 вместе с реализацией.
- Контрактные тесты проверяют `Content-Type`, обязательные члены и соответствие `correlation_id` заголовку
  `X-Request-ID`.

## Security considerations

- `detail` и `errors[].message` не содержат секретов, stack traces, SQL, путей файловой системы, содержимого
  документов, переданных значений полей и сведений об объектах других организаций.
- `404 not_found` используется и для «нет доступа» к объекту, чтобы не раскрывать его существование
  ([ADR-004](0004-tenant-isolation-and-kb-level-access.md)).
- `500 internal_error` никогда не содержит `detail`; подробности — только в серверном логе с тем же
  `correlation_id`.
- `APP_DEBUG=true` (запрещён в `production`) заменяет ответ `500` трассировкой. Это допустимо только для локальной
  отладки.

## Implementation notes

- `apps/backend/src/plummy/api/errors.py` — `problem_response` и обработчики `HTTPException`,
  `RequestValidationError`, `Exception`.
- `apps/backend/src/plummy/api/middleware.py` — `RequestContextMiddleware` (`X-Request-ID`).
- `apps/frontend/src/shared/api/errors.ts` — `toApiError` (`code`, `detail`, `correlation_id`).
- Тесты: `apps/backend/tests/api/test_errors_and_middleware.py`, `apps/frontend/src/shared/api/errors.test.ts`.

## Related documents

[API_CONTRACTS.md §1.5, §1.8, §3.3](../API_CONTRACTS.md) · [SECURITY_MODEL.md](../SECURITY_MODEL.md) ·
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)
