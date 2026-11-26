# ADR-024: Database Owner and Runtime Roles, Audit Retention

- **Status:** Accepted
- **Date:** 2026-11-27
- **Amends:** [ADR-011](0011-database-roles-and-extensions.md) §3 (роли) и пункт «Отложено»

## Context

По [ADR-011](0011-database-roles-and-extensions.md) одна роль приложения владеет базой. Под ней работают и миграции,
и сервисы: `api`, `worker`, `worker-ingestion`, `connectors`, `scheduler`. Владелец таблицы может выполнять
`UPDATE`, `DELETE`, `TRUNCATE`, `DROP` и `ALTER` над ней. Отозвать эти права у самого себя он не может: `REVOKE`
у владельца снимается тем же владельцем обратно.

Отсюда расхождение со спецификациями. [DATABASE_SCHEMA.md §7.3](../DATABASE_SCHEMA.md) и
[SECURITY_MODEL.md §11](../SECURITY_MODEL.md) называют `audit_logs` append-only. Фактически это свойство держится
только на коде: приложение выполняет лишь `INSERT`. При SQL injection, RCE в зависимости API или worker либо утечке
`DATABASE_URL` атакующий может стереть следы в журнале и изменить схему, например удалить ограничения или создать
функции.

Retention audit logs (1 год, [DATABASE_SCHEMA.md §8](../DATABASE_SCHEMA.md)) отложен в
[ADR-023](0023-production-deployment.md) §7 по той же причине. Удаление старых записей требует `DELETE` на
`audit_logs`. Выдать его роли, под которой работают сервисы, — значит отказаться от append-only.

Решение владельца проекта (Фаза 12): разделить роли. Retention выполняет CLI под ролью владельца, по расписанию
хоста.

## Decision

### 1. Две роли

| Роль | Переменная | По умолчанию | Кто использует | Права |
|---|---|---|---|---|
| **Владелец схемы** | `PLUMMY_DB_USER` | `plummy` | Только `migrate` (Alembic, выдача прав, `setup init-token`), backup/restore и CLI обслуживания на сервере | Владеет БД и всеми объектами схемы `public`; DDL. Атрибуты как в ADR-011: `NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS`, в production `NOCREATEDB` |
| **Runtime** | `PLUMMY_DB_RUNTIME_USER` | `plummy_app` | `backend`, `worker`, `worker-ingestion`, `connectors`, `scheduler` | `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT`; ничем не владеет и не входит в роль владельца; только DML по списку ниже |

Владелец сохраняет прежнее имя `plummy`. Поэтому существующие базы, `backup.sh`, `restore.sh` и дампы не меняются:
объекты и так принадлежат этой роли. Новая только runtime-роль.

### 2. Права runtime-роли

- `CONNECT` на базу приложения; `REVOKE ALL ON DATABASE … FROM PUBLIC`, как сейчас в production. `TEMPORARY` не
  выдаётся.
- `USAGE` на схему `public`; `REVOKE CREATE ON SCHEMA public FROM PUBLIC`. Runtime-роль не может создавать объекты.
- `SELECT, INSERT, UPDATE, DELETE` на все таблицы схемы, кроме перечисленных ниже. `TRUNCATE`, `REFERENCES` и
  `TRIGGER` не выдаются.
- `audit_logs` — только `SELECT, INSERT`. `UPDATE`, `DELETE` и `TRUNCATE` недоступны: журнал append-only на уровне
  БД.
- `alembic_version` — только `SELECT`: readiness и проверка ревизии читают её, но не меняют.
- `USAGE, SELECT` на последовательности схемы, если они есть.
- `EXECUTE` только на две функции владельца `SECURITY DEFINER` для HNSW-индексов (§2.1); права на остальные
  функции схемы, выданные runtime-роли явно, снимаются.

### 2.1 HNSW-индексы профилей (решение владельца проекта, шаг 12.1)

`CREATE INDEX` требует владельца таблицы, а worker строит индекс профиля, когда KB начинает его использовать
([ADR-017](0017-vector-storage.md) §2). Ревизия `0017` создаёт функции владельца `plummy_ensure_vector_index(uuid)` и
`plummy_drop_vector_index(uuid)` (`SECURITY DEFINER`, `search_path = pg_catalog, public, pg_temp`, `EXECUTE` у
`PUBLIC` отозван). Они принимают только id профиля и строят выражение из сохранённой строки профиля
(CHECK-ограничения на размерность и метрику): то же выражение и предикат, что `index_spec`, по которым retrieval
выбирает индекс (тест сравнивает `pg_get_indexdef` с эталонным DDL). Сборка транзакционная, без `CONCURRENTLY`:
на время сборки вставки в `embeddings` ждут, чтение и поиск не блокируются; невалидный индекс прежней
конкурентной сборки удаляется и строится заново. Отклонены: отдельный сервис-индексатор с ролью владельца
(учётные данные владельца в работающем сервисе) и сборка индексов `migrate` и таймером хоста (до запуска таймера
поиск по новому профилю шёл бы полным перебором).

Права выдаёт не Alembic, а идемпотентная команда `python -m plummy.cli db grant-runtime`. Её выполняет `migrate`
после `alembic upgrade head` при каждом запуске: `alembic upgrade head && python -m plummy.cli db grant-runtime &&
python -m plummy.cli setup init-token`. Команда сначала отзывает все права runtime-роли на объекты схемы, затем
выдаёт их по списку. Так права восстанавливаются после `pg_restore --no-privileges` ([ADR-023](0023-production-deployment.md)
§6) и после ревизий, добавивших таблицы; лишние права, выданные вручную, снимаются. Список исключений из полного DML
(`audit_logs`, `alembic_version`) хранится в коде рядом с командой и покрыт тестом. Новая таблица по умолчанию
получает DML — append-only таблица должна попасть в список явно.

Имя runtime-роли команда берёт из настройки `DATABASE_RUNTIME_ROLE` контейнера `migrate`. Если роли нет, команда
завершается ошибкой со ссылкой на шаг администратора ([DEPLOYMENT.md §6.1](../DEPLOYMENT.md)). Тогда `migrate`
завершается с ненулевым кодом, и сервисы не стартуют.

### 3. Учётные данные

- Production: секрет `database_url` — подключение **runtime-роли**, его получают все сервисы, кроме `migrate`.
  Новый секрет `migration_database_url` — подключение владельца, только для `migrate`. Пароли — `db_password`
  (владелец) и новый `db_runtime_password`. `init-secrets.sh` создаёт недостающие файлы. Если найден прежний
  `database_url` с ролью владельца, скрипт переносит его в `migration_database_url` и создаёт новый `database_url`.
- Development (`compose.yaml`): та же модель. Dev init-скрипт создаёт обе роли; `CREATEDB` для integration-тестов
  остаётся только у владельца. Существующий dev-volume обновляется скриптом из §5 или пересоздаётся
  (`docker compose down -v`).
- Роль администратора БД (суперпользователь) по-прежнему не передаётся ни одному сервису (ADR-011).

### 4. Проверка при запуске

При старте процессов с `PROCESS_ROLE` из `api`, `worker`, `scheduler` проверяется собственная роль подключения.
Если роль является владельцем `audit_logs`, суперпользователем или может выполнить `UPDATE`/`DELETE` над
`audit_logs` (`has_table_privilege`), в production процесс завершается с понятной ошибкой: разделение ролей не
выполнено. В development и test выводится предупреждение. Проверка только читает каталоги и не раскрывает
учётных данных.

### 5. Существующие инсталляции

Однократный шаг администратора при обновлении на 0.5.0. Скрипт `deploy/scripts/split-db-roles.sh`:

- выполняется на сервере и подключается к контейнеру `postgres` суперпользователем через локальный сокет;
- создаёт runtime-роль с паролем из `secrets/db_runtime_password`;
- выдаёт ей `CONNECT`, отзывает `CREATE` на схеме `public` у `PUBLIC`;
- идемпотентен: повторный запуск не меняет пароль и не падает.

Порядок обновления: backup → `init-secrets.sh` (новые файлы) → `split-db-roles.sh` → `up` (`migrate` выдаёт права,
сервисы подключаются runtime-ролью). Для внешнего или управляемого PostgreSQL в [DEPLOYMENT.md §6.1](../DEPLOYMENT.md)
приведены эквивалентные SQL-команды. CI проверяет upgrade path с `v0.4.0` через этот скрипт.

### 6. Retention audit logs

- CLI `python -m plummy.cli maintenance prune-audit` удаляет записи `audit_logs` старше срока хранения. Срок задаёт
  `AUDIT_RETENTION_DAYS`, по умолчанию 365; значения меньше 30 отклоняются. Ключ `--dry-run` только считает записи.
- Команда работает под ролью **владельца** (`migration_database_url`) и запускается как одноразовый контейнер:
  `docker compose run --rm migrate python -m plummy.cli maintenance prune-audit`. Расписание задаёт хост: cron или
  systemd timer, пример — [DEPLOYMENT.md](../DEPLOYMENT.md). `scheduler` эту задачу не выполняет: он работает под
  runtime-ролью, а держать учётные данные владельца в долгоживущем сервисе — значит снова дать сервису `DELETE` на
  журнал.
- Удаление идёт пачками (по `created_at`, ограниченный размер транзакции). После удаления команда записывает
  событие `maintenance.audit_pruned` (`actor_type = system`). Детали: число удалённых записей и граница по времени,
  без содержимого записей.
- Retention не трогает записи моложе срока; повторный запуск безопасен.

## Alternatives

| Вариант | Почему не выбран |
|---|---|
| Оставить одну роль, append-only держать кодом | Не защищает журнал при компрометации API или worker — то, ради чего журнал существует |
| Триггер `BEFORE UPDATE OR DELETE` на `audit_logs`, запрещающий изменения | Владелец таблицы может отключить или удалить триггер (`ALTER TABLE … DISABLE TRIGGER`); без разделения ролей это не рубеж |
| Отдельная роль только для retention (`DELETE` на `audit_logs`) | Третья роль и ещё один секрет ради одной команды, которую и так выполняет администратор на сервере; роль владельца уже доступна `migrate` и CLI обслуживания |
| Retention в `scheduler` под ролью с `DELETE` на `audit_logs` | Долгоживущий сервис с правом стирать журнал повторяет исходную проблему |
| Права через `ALTER DEFAULT PRIVILEGES` вместо команды | Не восстанавливаются после `pg_restore --no-privileges` и не снимают лишние права; оставлены бы как неявное состояние каталога |
| Выдача прав в ревизии Alembic | Права зависят от имени роли в окружении, а ревизии не редактируются после попадания в `main`; повторная выдача после restore потребовала бы отдельного механизма всё равно |
| Переименовать владельца (`plummy_owner`), а `plummy` отдать runtime | Существующие объекты принадлежат `plummy`; потребовался бы `REASSIGN OWNED` суперпользователем и изменение backup/restore |

## Consequences

- Компрометация API или worker больше не даёт DDL, `TRUNCATE` и изменения журнала аудита. Данные приложения
  по-прежнему доступны на чтение и запись: это рубеж для журнала и схемы, не для данных. Второй рубеж для данных
  KB — RLS, отдельный открытый вопрос ([SECURITY_MODEL.md §20](../SECURITY_MODEL.md)).
- Обновление существующей инсталляции требует однократного действия суперпользователя (§5). Без него `migrate`
  завершается ошибкой, и стек не стартует.
- Секретов становится два для БД вместо одного; `migrate` получает другой URL, чем сервисы.
- Integration-тесты, которые проверяют права, работают под runtime-ролью; служебные SQL-запросы тестов (подготовка
  данных) остаются под владельцем.
- Retention журнала зависит от расписания хоста. Если таймер не настроен, журнал растёт. Readiness этого не видит;
  DEPLOYMENT описывает таймер в шагах установки.

## Security considerations

- Наименьшие привилегии для сервисов: без DDL, `TRUNCATE`, `TEMPORARY`, без владения объектами и членства в роли
  владельца (`NOINHERIT` и отсутствие `GRANT owner TO runtime`).
- Пароль владельца не попадает в контейнеры сервисов: `migration_database_url` монтируется только в `migrate`.
- Проверка при запуске (§4) не даёт тихо работать с ролью владельца после неполного обновления.
- Retention журнала — действие администратора на сервере, само оставляет запись в журнале.
- `pg_read_file` и подобные функции недоступны обеим ролям (ADR-011, тест сохраняется).

## Implementation notes

Реализовано в шаге 12.1 ([DEVELOPMENT_ROADMAP.md](../DEVELOPMENT_ROADMAP.md)). Уточнения при реализации:

- `db grant-runtime` (`infrastructure/db/roles.py`) кроме прав на таблицы сам выдаёт `CONNECT` на базу и отзывает
  у `PUBLIC` права на базу и `CREATE` на схеме: это может владелец, поэтому шаг администратора сводится к созданию
  роли. Команда отказывает, если роль не существует, является владельцем или входит в его роль, либо имеет
  `SUPERUSER`/`BYPASSRLS`/`CREATEROLE`/`CREATEDB`.
- Каждая `SECURITY DEFINER` функция схемы должна быть в `EXECUTABLE_FUNCTIONS` и не исполняться `PUBLIC` — это
  проверяет тест: новая функция-повышение прав не появится незаметно.
- Проверка при запуске (§4) выполняется в `python -m plummy.check_config` (workers, scheduler) и в lifespan API;
  при недоступной БД — предупреждение, решение оставлено readiness.
- Development: `compose.yaml` передаёт integration-тестам URL владельца в `TEST_OWNER_DATABASE_URL` (только
  контейнер `backend`, сам API её не читает). Тесты создают и мигрируют БД владельцем, выдают права runtime-роли и
  запускают приложение под ней — весь набор тестов проверяет, что сервисам хватает DML.

Изначальный план шага:

- `deploy/prod/postgres-initdb/10-roles-and-extensions.sh` и `deploy/postgres/initdb/10-dev-roles-and-extensions.sh`
  — вторая роль; `deploy/scripts/split-db-roles.sh` — для существующих инсталляций;
- `deploy/prod/init-secrets.sh` — `db_runtime_password`, `migration_database_url`, перенос прежнего `database_url`;
- `compose.prod.yaml`, `compose.yaml`, `compose.e2e.yaml` — `migrate` с URL владельца, сервисы с URL runtime-роли;
- CLI `db grant-runtime`, `maintenance prune-audit`, событие `maintenance.audit_pruned`, проверка при запуске;
- тесты: runtime-роль не может `UPDATE`/`DELETE`/`TRUNCATE` над `audit_logs`, `ALTER`/`CREATE` объектов и `DROP`;
  `grant-runtime` идемпотентна и снимает лишние права; prune-audit удаляет только старые записи и пишет событие;
  проверка при запуске; CI — upgrade path с `v0.4.0` через `split-db-roles.sh` и restore drill с повторной выдачей
  прав.

## Related documents

[ADR-011](0011-database-roles-and-extensions.md) · [ADR-023](0023-production-deployment.md) §6, §7 ·
[DATABASE_SCHEMA.md §7.3, §8, §9](../DATABASE_SCHEMA.md) · [SECURITY_MODEL.md §11, §17](../SECURITY_MODEL.md) ·
[DEPLOYMENT.md §4, §6.1, §10](../DEPLOYMENT.md)
