# ADR-016: Secret Encryption at Rest

- **Status:** Accepted
- **Date:** 2026-11-18

## Context

С Фазы 4 приложение хранит секреты, которые вводит Administrator: API keys AI-провайдеров, позже — OAuth refresh
tokens и client secrets коннекторов (Фазы 9–10). Blueprint (§5, §8 п.3) требует шифровать их **до записи в БД**,
держать ключ **вне БД**, отдавать секреты в API только на запись и не писать их в логи. Черновик —
[SECURITY_MODEL.md §7](../SECURITY_MODEL.md) и [DATABASE_SCHEMA.md §4.1](../DATABASE_SCHEMA.md) (`secret_references`).

Ограничения:

- Инсталляция self-hosted, одна организация ([ADR-012](0012-single-organization-deployment.md)); внешнего KMS
  или Vault может не быть.
- Backup БД не должен раскрывать секреты; ключ резервируется отдельно ([DEPLOYMENT.md §4](../DEPLOYMENT.md)).
- Ключ должен ротироваться без простоя и без ручного перешифрования каждой записи.
- Секретов немного (десятки–сотни строк), читаются при каждом вызове провайдера.
- В стандартной библиотеке Python нет AEAD-шифров.

Существующий `SECRET_KEY` (Instance Settings) для этого не используется: у него другое назначение, нет `key_id` и нет
процедуры ротации, а совмещение ролей ключей усложняет их замену.

## Decision

### 1. Алгоритм

**AES-256-GCM** (библиотека `cryptography`, PyCA) для каждой записи `secret_references`:

- ключ — 32 случайных байта из key ring (§2);
- nonce — 96 бит из `os.urandom` на **каждое** шифрование (повторное шифрование того же значения даёт новый nonce);
- associated data — `plummy-secret-v1|<secret_id>|<kind>`: шифртекст нельзя перенести в другую запись или
  использовать как секрет другого вида; изменение `id` или `kind` делает расшифровку невозможной;
- хранятся `ciphertext` (с тегом GCM), `nonce`, `key_id`, `algorithm = "AES-256-GCM"`.

Envelope encryption (свой ключ данных на запись, зашифрованный мастер-ключом) в MVP не вводится: при малом числе
записей полное перешифрование при ротации дешевле, а колонка `backend` оставляет место для внешнего KMS/Vault.

### 2. Key ring

- Переменная `SECRETS_KEYRING_FILE` указывает на файл вне БД (Docker secret / смонтированный файл):

  ```json
  {
    "primary": "2026-11",
    "keys": {
      "2026-11": "<base64url, ровно 32 байта>",
      "2025-05": "<base64url, ровно 32 байта>"
    }
  }
  ```

- Новые и перешифрованные секреты используют ключ `primary`; расшифровка выбирает ключ по `key_id` записи.
- `key_id` — 1–32 символа `[A-Za-z0-9._-]`; ключи только случайные (не выводятся из паролей).
- Невалидный файл (не JSON, нет `primary`, ключ не 32 байта, неизвестный `primary`) → отказ старта API, worker и CLI
  с понятной ошибкой без значений ключей.
- **production:** key ring обязателен. **development:** `migrate` создаёт key ring в отдельном volume, если его нет
  (`python -m plummy.cli secrets init-keyring`). **test:** ключ генерируется в фикстуре.
- Readiness (`GET /health/ready`) сообщает `secrets: failed`, если в БД есть записи с `key_id`, которого нет в key
  ring (ключ удалили раньше перешифрования); значения ключей и секретов в ответ не попадают.

### 3. Ротация

1. Владелец добавляет новый ключ в key ring и делает его `primary`, перезапускает API и worker.
2. `python -m plummy.cli secrets rotate` перешифровывает все записи, чей `key_id` ≠ `primary`: пакетами, каждая запись
   в своей транзакции с блокировкой строки; повторный запуск безопасен. Audit `secrets.rotated` (число записей, без
   значений).
3. Когда `secrets status` показывает 0 записей на старых ключах, владелец удаляет старый ключ из файла.

Генерация ключа: `python -m plummy.cli secrets generate-key` печатает новую запись key ring (единственная команда,
которая выводит ключ; только на сервере).

### 4. Порт и использование

- Порт `SecretCipher` (application): `encrypt(secret_id, kind, plaintext) -> EncryptedSecret`,
  `decrypt(record) -> SecretValue`. Реализация — `infrastructure/security/secret_cipher.py`; `domain` и
  `application` не импортируют `cryptography`.
- Use cases сохраняют секрет и запись-владельца (например `ai_providers`) в одной транзакции.
- Расшифровка — только в момент использования (сборка адаптера провайдера, connection test, вызов модели);
  значение живёт в `SecretValue` (как `SecretStr`: `repr`/`str` не раскрывают его) и не кэшируется в Redis или БД.
- Ошибка расшифровки (неверный ключ, повреждение, подмена AAD) → `ServiceUnavailable`/ошибка провайдера без
  подробностей в ответе; в лог — только `secret_id`, `key_id` и тип ошибки.

### 5. API

- Секреты **write-only**: в запросах `api_key` — `SecretStr`; в ответах только `has_secret: bool` и `secret_hint`.
- `secret_hint` — последние 4 символа, если секрет длиной ≥ 16 символов, иначе `null`; хранится открыто в колонке
  `hint` и не позволяет восстановить секрет.
- Замена секрета создаёт новый шифртекст (новый nonce); старое значение не сохраняется.
- `GET /system/settings` показывает `secrets: { "configured": true, "primary_key_id": "2026-11", "keys": 2 }`.

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| Fernet (`cryptography`) | AES-128-CBC + HMAC без associated data: нельзя привязать шифртекст к записи; встроенная ротация (`MultiFernet`) не даёт `key_id` |
| XChaCha20-Poly1305 (PyNaCl / libsodium) | Ещё одна нативная зависимость; при случайном 96-битном nonce и малом числе записей AES-GCM достаточно |
| `pgcrypto` в PostgreSQL | Ключ передаётся в SQL и может попасть в логи БД и `pg_stat_statements`; нарушает «ключ вне БД» |
| Ключ из `SECRET_KEY` (HKDF) | Нет `key_id` и ротации; смешение назначений ключей |
| Внешний Vault / KMS обязательно | Усложняет self-hosted установку; остаётся опцией через `backend` |
| Envelope encryption с DEK на запись | Нужна при больших объёмах или KMS; сейчас лишняя сложность |

## Consequences

- Новая прямая зависимость `cryptography` (PyCA, стандарт де-факто для AEAD в Python; сейчас её нет в `uv.lock`),
  проверяется `pip-audit`.
- Новая обязательная переменная в production `SECRETS_KEYRING_FILE`; документация DEPLOYMENT (§3 переименование из
  черновика `PLUMMY_SECRETS_KEY_FILE`, §4 процедуры), `.env.example`, dev compose (volume и `init-keyring`).
- CLI-группа `secrets`: `init-keyring`, `generate-key`, `status`, `rotate`.
- Потеря key ring = потеря всех сохранённых секретов (их надо ввести заново); восстанавливать БД без ключа бессмысленно.

## Security considerations

- Компрометация БД или её backup без key ring не раскрывает секреты; подмена шифртекста или AAD обнаруживается тегом
  GCM.
- Компрометация процесса API/worker раскрывает ключи — это вне модели угроз шифрования в покое; смягчается сетевой
  изоляцией ([ADR-007](0007-network-access-and-local-llm.md)) и минимальными правами контейнеров.
- Случайный 96-битный nonce: при числе шифрований на ключ ≪ 2³² вероятность повтора пренебрежима; ротация ключа
  дополнительно ограничивает объём.
- Ключи и секреты не логируются, не попадают в тексты ошибок, в audit (`details`) и в prompt; тесты проверяют логи и
  ответы API.
- Файл key ring читается один раз при старте; рекомендуемые права `0400`; при более широких правах — предупреждение в
  логе.

## Implementation notes

`application/ports.py` — `SecretCipher`, `EncryptedSecret`; `core/config.py` — `SECRETS_KEYRING_FILE`;
`infrastructure/security/secret_cipher.py` — AES-GCM и key ring; `infrastructure/db` — `secret_references`
(миграция Фазы 4), `SqlSecretRepository`; `cli` — группа `secrets`; `infrastructure/db/checks.py` — проверка
`key_id`. Тесты: round-trip, AAD-подмена, неизвестный `key_id`, ротация (идемпотентность), невалидный key ring,
отсутствие секретов в логах и ответах.

## Related documents

[SECURITY_MODEL.md §7](../SECURITY_MODEL.md) · [DATABASE_SCHEMA.md §4.1](../DATABASE_SCHEMA.md) ·
[DEPLOYMENT.md §3–4](../DEPLOYMENT.md) · [ARCHITECTURE.md §8](../ARCHITECTURE.md) ·
[ADR-006](0006-initial-setup-and-system-configuration.md) · [ADR-007](0007-network-access-and-local-llm.md)
