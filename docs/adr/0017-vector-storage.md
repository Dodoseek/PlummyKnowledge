# ADR-017: Vector Storage in pgvector

- **Status:** Accepted
- **Date:** 2026-11-19

## Context

Фаза 5 ([DEVELOPMENT_ROADMAP.md](../DEVELOPMENT_ROADMAP.md)) начинает писать embeddings в PostgreSQL + pgvector
([ADR-003](0003-technology-stack.md)). Черновик — [DATABASE_SCHEMA.md §5.10](../DATABASE_SCHEMA.md) (`embeddings`),
помечен **[Proposed]**.

Факты и ограничения:

- Embedding profile ([API_CONTRACTS.md §2.8](../API_CONTRACTS.md)) фиксирует модель, размерность (`dimensions`),
  метрику (`cosine` | `inner_product` | `l2`) и `normalize`; профиль неизменяем, смена модели = новый профиль и
  переиндексация (§5). В одной инсталляции одновременно живут несколько профилей разной размерности: у разных KB,
  а во время переиндексации — у одной KB.
- Каждая строка с вектором обязана ссылаться на профиль (blueprint §9).
- HNSW и IVFFlat в pgvector индексируют только столбец **фиксированной** размерности. Предел индекса: `vector` —
  2000 измерений, `halfvec` — 4000. Распространённые модели укладываются в 4000 (768, 1024, 1536, 3072), но не все
  в 2000.
- Используемый образ — `pgvector/pgvector:0.8.7-pg17`: есть `halfvec` и итеративные index scans
  (`hnsw.iterative_scan`), которые нужны для поиска с фильтром по доступным KB (§8 п.2).
- Retrieval всегда фильтрует по доступным источникам, текущей версии, активному `chunk_set` и профилю
  ([DATABASE_SCHEMA.md §5.11](../DATABASE_SCHEMA.md)). У KB с малым числом документов полный перебор быстрее индекса.
- Миграции и приложение работают под одной ролью-владельцем таблиц ([ADR-011](0011-database-roles-and-extensions.md));
  разделение ролей решено в [ADR-024](0024-database-owner-and-runtime-roles.md) (шаг 12.1).
- Asyncpg использует prepared statements; generic plan PostgreSQL не применяет partial index, если предикат
  индекса совпадает с запросом только через параметр.

## Decision

### 1. Одна таблица, столбец без фиксированной размерности

```sql
CREATE TABLE embeddings (
  id                   uuid PRIMARY KEY,
  chunk_id             uuid NOT NULL REFERENCES document_chunks (id) ON DELETE CASCADE,
  embedding_config_id  uuid NOT NULL REFERENCES embedding_provider_configs (id) ON DELETE RESTRICT,
  document_version_id  uuid NOT NULL,          -- денормализация для удаления и фильтров
  data_source_id       uuid NOT NULL,          -- денормализация для фильтра по KB
  dimensions           integer NOT NULL,       -- копия dimensions профиля
  embedding            vector NOT NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (chunk_id, embedding_config_id),
  CHECK (vector_dims(embedding) = dimensions),
  CHECK (dimensions BETWEEN 1 AND 4000)
);
CREATE INDEX ix_embeddings_config_version ON embeddings (embedding_config_id, document_version_id);
```

- Векторы хранятся в полной точности (`vector`, float32); `halfvec` используется только в выражении индекса (§2).
- Use case проверяет длину каждого вектора против профиля **до** записи; `CHECK` — второй рубеж. Если
  `normalize = true`, векторы нормализуются (L2) до записи.
- Таблиц на размерность или на профиль нет: схема управляется Alembic, ORM-модель одна.

### 2. Partial expression HNSW-индекс на профиль

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_embeddings_hnsw_<profile_id_hex>
  ON embeddings USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)
  WHERE embedding_config_id = '<profile-uuid>';
```

- Тип выражения: `vector(n)` при `n ≤ 2000`, `halfvec(n)` при `2000 < n ≤ 4000`. Operator class — по метрике профиля:
  `*_cosine_ops`, `*_ip_ops`, `*_l2_ops`. Параметры HNSW — по умолчанию pgvector (`m = 16`, `ef_construction = 64`).
- Имя индекса детерминировано: `ix_embeddings_hnsw_` + 32 hex-символа UUID профиля (51 символ, меньше лимита 63).
- Индекс создаёт идемпотентная задача очереди `maintenance` (`ensure_vector_index`) — при первом использовании
  профиля KB, то есть **до** первой записи векторов. Индекс на пустом наборе строится мгновенно и дальше
  поддерживается вставками. Задача проверяет `pg_index.indisvalid` и пересоздаёт индекс после прерванной
  `CREATE INDEX CONCURRENTLY`. Профиль `retired` — индекс удаляется той же задачей после удаления его векторов.
- Alembic-миграции и сравнение моделей с БД игнорируют индексы `ix_embeddings_hnsw_*` (`include_object`): ими
  управляет приложение, не схема.
- Все значения в DDL берутся из записи профиля в БД (UUID и целое число, проверенные доменом), а не из запроса
  клиента; DDL собирается только из этих значений.

### 3. Правила запроса

- Vector-ветка retrieval использует **то же выражение и предикат**, что и индекс: `embedding::halfvec(3072) <=>
  :query::halfvec(3072)` и `embedding_config_id = '<profile-uuid>'`. UUID и размерность подставляются литералами из
  записи профиля (не параметрами), чтобы planner сопоставил partial index и в generic plan; остальные значения —
  параметры.
- Запрос включает все фильтры [DATABASE_SCHEMA.md §5.11](../DATABASE_SCHEMA.md) (доступные источники, текущая
  версия, активный `chunk_set`, `status = 'ready'`, `deleted_at IS NULL`).
- Для фильтрованного поиска включается `SET LOCAL hnsw.iterative_scan = relaxed_order` и ограничение
  `hnsw.max_scan_tuples`, а итог пересортировывается по точному расстоянию. Если число кандидатов после фильтров
  меньше порога (по умолчанию 10 000 chunks), выполняется точный перебор без индекса. Конкретные значения и метрики
  качества фиксирует Фаза 6.

### 4. Ограничение профилей

- `dimensions` embedding profile ограничивается **1–4000** (было 1–16000): профиль с большей размерностью нельзя
  проиндексировать, а поиск полным перебором по всем KB не масштабируется. Ранее созданный профиль с `dimensions >
  4000` не может стать профилем KB (`422 dimensions_not_indexable`); миграция таких профилей не меняет.
- `embedding_config_id` векторов совпадает с профилем KB; переиндексация на новый профиль пишет векторы рядом со
  старыми, переключение KB — атомарно после завершения, старые векторы удаляются (ADR-008 §4).

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| Таблица на размерность (`embeddings_768`, `embeddings_1536`, …) | Нужна миграция на каждую новую размерность или DDL в runtime; метрика всё равно различается по профилям, поэтому partial index нужен и здесь |
| Таблица на профиль, создаваемая приложением | Схема вне Alembic, динамические имена таблиц в ORM и запросах, сложнее удаление и backup-проверки |
| Одна фиксированная размерность для всей инсталляции | Ограничивает выбор моделей и делает смену модели с другой размерностью миграцией схемы |
| Хранить `halfvec` вместо `vector` | Вдвое меньше места, но теряется точность для повторной оценки и перехода на другой индекс; при объёмах MVP экономия не нужна |
| IVFFlat вместо HNSW | Требует данных для обучения списков до построения индекса и перестроения при росте; HNSW можно создать на пустом наборе |
| Отдельная векторная БД (Qdrant, Weaviate, Milvus) | Новый сервис, backup и сетевой периметр; фильтры доступа пришлось бы дублировать вне PostgreSQL. Противоречит ADR-003 |

## Consequences

- Миграция Фазы 5 создаёт `embeddings` по §1; DATABASE_SCHEMA §5.10 и API_CONTRACTS §2.8 обновляются (`dimensions`
  1–4000, код `dimensions_not_indexable`), домен `MAX_DIMENSIONS` = 4000.
- Приложению нужен `CREATE INDEX` на своей таблице — сейчас роль приложения владеет таблицами (ADR-011). Если в
  Фазе 12 роли разделят, создание индексов перейдёт в задачу под ролью миграций; это учитывается в том решении.
- `CREATE INDEX CONCURRENTLY` нельзя выполнить в транзакции: задача использует отдельное соединение в autocommit.
- Retrieval-запрос собирается с литералами профиля; Фаза 6 добавляет тест, что planner использует индекс
  (`EXPLAIN`) и что точный перебор включается ниже порога.
- Индекс на каждый активный профиль занимает место; число одновременно используемых профилей в MVP невелико.

## Security considerations

- DDL и литералы запроса строятся только из записи профиля, прошедшей доменную валидацию (UUID, целое 1–4000,
  метрика из перечисления); значения от клиента в DDL не попадают — нет SQL-инъекции через профиль.
- Фильтры доступа (§8 п.2) остаются в том же SQL-запросе, что и векторный поиск; индекс не ослабляет их.
  Итеративный scan и точный перебор меняют только полноту, не набор разрешённых строк.
- Векторы считаются производными данными документа: удаляются вместе с chunks и версией (ADR-008), в логи не пишутся.
