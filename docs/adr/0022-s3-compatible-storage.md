# ADR-022: S3-Compatible Document Storage

- **Status:** Accepted
- **Date:** 2026-11-26 (утверждено владельцем проекта вместе с планом Фазы 11: собственная подпись SigV4 на `httpx`)

## Context

[ADR-003](0003-technology-stack.md) задаёт хранилище документов как local filesystem и S3-compatible (MinIO) за
абстракцией. Порт `StorageProvider` ([ARCHITECTURE.md §8.5](../ARCHITECTURE.md)) и реализация `LocalFileStorage`
есть с Фазы 5: две области (`quarantine`, `documents`), ключи генерирует сервер, `put` никогда не перезаписывает,
`move` переносит объект между областями без перезаписи, `delete` идемпотентен, `local_path` даёт парсеру
read-only файл, `purge_expired` чистит quarantine по TTL.

Production-профилю Фазы 11 нужно объектное хранилище: несколько реплик API и worker на разных хостах не могут
делить локальный volume, а backup оригиналов должен быть отдельным от БД и согласованным по времени. Открытыми
были: клиент S3 (SDK или собственная подпись), как сохранить инварианты порта (без перезаписи, без частичных
объектов), куда идут соединения с точки зрения [ADR-007](0007-network-access-and-local-llm.md) и какие права
получает `worker-ingestion`.

## Decision

### 1. Клиент: SigV4 на `httpx` через `SafeHttpClient`

- Адаптер `S3Storage` (`infrastructure/storage/s3.py`) реализует тот же `StorageProvider`; бизнес-код не меняется.
- Запросы подписываются AWS Signature Version 4 собственным модулем (`infrastructure/storage/sigv4.py`, только
  стандартная библиотека: `hmac`, `hashlib`), отправляются через `SafeHttpClient` — как адаптеры LLM и коннекторов,
  без SDK и без собственного HTTP-стека в обход политики egress.
- Path-style адресация (`{endpoint}/{bucket}/{key}`) — её поддерживают MinIO и AWS; virtual-hosted style не нужен.
- Используемые операции: `PutObject`, `GetObject`, `HeadObject`, `CopyObject`, `DeleteObject`, `ListObjectsV2`.
  Multipart upload не нужен: наибольший upload — `UPLOAD_MAX_BYTES` ≤ 2 ГБ, предел одного `PutObject` — 5 ГБ.

### 2. Инварианты порта

- **Без перезаписи:** `PutObject` и `CopyObject` отправляются с `If-None-Match: *`; ответ `412` → 
  `StorageObjectExistsError`. Хранилище, которое игнорирует условную запись, не поддерживается: contract-тесты
  проверяют её на MinIO, readiness — нет (проверка требовала бы записи).
- **Без частичных объектов и с лимитом:** `put` сначала пишет поток во временный файл (каталог `0700`, файл `0600`,
  удаляется в любом случае), прерывая запись при превышении `max_bytes` (`StorageLimitExceededError`), считает
  SHA-256 и размер, затем один `PutObject` с `Content-Length` и подписанным `x-amz-content-sha256`. Объект появляется
  в бакете атомарно и целиком.
- **`move`:** `CopyObject` в целевую область с `If-None-Match: *`, затем `DeleteObject` источника. Сбой между шагами
  оставляет копию в quarantine — её удалит cleanup по TTL; повтор `move` после успешного копирования видит целевой
  объект и только удаляет источник.
- **`local_path`:** объект скачивается во временный файл (`0600`, каталог `0700`), который удаляется при выходе из
  контекста; парсер по-прежнему получает путь и не знает о S3.
- **`purge_expired`:** `ListObjectsV2` по префиксу области и `DeleteObject` объектов с `LastModified` старше границы.
- **Ключи объектов:** `{prefix}{area}/{последние два символа ключа}/{key}` — та же раскладка, что у local; ключ
  проходит ту же проверку формата, имя файла пользователя в ключ не попадает. `S3_PREFIX` (необязательный, `[a-z0-9-/]`,
  оканчивается на `/`) позволяет делить бакет.

### 3. Конфигурация (Instance Settings)

- `STORAGE_BACKEND=s3`; `S3_ENDPOINT_URL` (`http(s)://host[:port]`, без пути), `S3_BUCKET`, `S3_REGION` (по
  умолчанию `us-east-1`), `S3_PREFIX`, `S3_ACCESS_KEY_ID` и `S3_SECRET_ACCESS_KEY` (или `*_FILE`). Заменяют черновые
  `PLUMMY_S3_*` из DEPLOYMENT.md. Без любого обязательного значения при `STORAGE_BACKEND=s3` — отказ старта.
- В `production` `S3_ENDPOINT_URL` — только `https`, кроме адреса, разрешённого записью allowlist (внутренний MinIO в
  сети `data`, §4).
- Бакет создаёт администратор (в production-compose — одноразовый `minio-init`); приложение бакеты не создаёт и не
  меняет их политику. Учётная запись приложения в MinIO получает доступ только к своему бакету.
- Readiness проверяет storage `HeadBucket`-запросом (без записи); ошибка → `storage: fail`.

### 4. Сеть ([ADR-007](0007-network-access-and-local-llm.md))

- Новая цель egress `storage`. Публичный `https`-endpoint (AWS S3 и т. п.) проходит политику как публичный адрес;
  внутренний MinIO требует записи allowlist с `purposes = ["storage"]` и `allowed_cidrs` сети `data`.
- Redirects не выполняются; адрес, имя бакета и регион берутся только из конфигурации, никогда из ответов.
- `worker-ingestion` получает `CONFIG_FILE` только ради этой записи; другой выход в сеть у него по-прежнему закрыт
  инфраструктурой (сеть `data` — `internal`).

### 5. Права сервисов

Учётные данные S3 получают `backend`, `worker` и `worker-ingestion` — те же сервисы, что монтируют local storage
volume сейчас. `scheduler` и `migrate` их не получают. Отдельные ключи для `worker-ingestion` не вводятся: ему нужны
и чтение, и запись, и перенос между областями.

## Alternatives

- **`aiobotocore`/`aioboto3`.** Отклонено: тяжёлая зависимость (botocore) со своим HTTP-стеком, который обходит
  `SafeHttpClient` и DNS pinning ADR-007; ради шести операций.
- **MinIO SDK (`minio-py`).** Синхронный, свой HTTP-клиент (urllib3) — те же возражения.
- **Unsigned payload (`UNSIGNED-PAYLOAD`) и потоковая загрузка без временного файла.** Отклонено: не даёт
  атомарного лимита размера и целостности на стороне хранилища; временный файл всё равно нужен парсеру.
- **Общий сетевой том (NFS) с `LocalFileStorage`.** Не решает backup и масштабирование на несколько хостов без
  внешней инфраструктуры; остаётся вариантом для одного хоста (`STORAGE_BACKEND=local`).

## Consequences

- Собственный код подписи — зона ответственности проекта: unit-тесты на официальных примерах AWS SigV4 и
  integration-тесты на настоящем MinIO в CI.
- Временные файлы требуют места на диске API и worker не меньше `UPLOAD_MAX_BYTES` на одновременный upload/parse.
- Local storage остаётся поддерживаемым режимом для одного хоста и для разработки.
- Миграция существующих данных local → s3 — отдельная задача (утилита или `mc mirror` с той же раскладкой ключей);
  раскладка совпадает, поэтому достаточно скопировать каталоги `quarantine/` и `documents/` в бакет под `S3_PREFIX`.

## Security considerations

- Секретный ключ — только файл (`S3_SECRET_ACCESS_KEY_FILE`), не попадает в логи, ошибки и `GET /system/settings`
  (там — `backend`, `bucket`, `endpoint` и `configured`).
- Подпись запроса не логируется; ошибки S3 сводятся к typed storage errors без тела ответа.
- Шифрование at rest — средствами хранилища (SSE MinIO/KMS, SSE-S3); приложение его не настраивает, DEPLOYMENT
  описывает рекомендацию.
- Malicious filenames по-прежнему не выходят из storage: ключ формирует сервер, проверка формата одна для обоих
  адаптеров (SC и тесты Фазы 5 повторяются для S3).

## Implementation notes

Шаг 11.1. Тесты: общий contract-набор `StorageProvider` для local и S3 (MinIO в CI), unit-тесты SigV4 на примерах
AWS, лимит размера, обрыв потока, `412` при повторном ключе, повтор `move`, `purge_expired`, отказ конфигурации.

## Related documents

[ADR-003](0003-technology-stack.md) · [ADR-007](0007-network-access-and-local-llm.md) ·
[ADR-008](0008-unified-document-ingestion.md) · [ADR-023](0023-production-deployment.md) ·
[ARCHITECTURE.md §8.5](../ARCHITECTURE.md) · [SECURITY_MODEL.md §9](../SECURITY_MODEL.md) ·
[DEPLOYMENT.md](../DEPLOYMENT.md)
