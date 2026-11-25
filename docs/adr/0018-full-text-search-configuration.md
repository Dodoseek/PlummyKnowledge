# ADR-018: Full-Text Search Configuration

- **Status:** Accepted
- **Date:** 2026-11-25

## Context

Фаза 6 ([DEVELOPMENT_ROADMAP.md](../DEVELOPMENT_ROADMAP.md)) добавляет keyword-ветку retrieval на PostgreSQL
full-text search рядом с vector-веткой (blueprint §6, [ARCHITECTURE.md §9.4](../ARCHITECTURE.md)). Черновик —
[DATABASE_SCHEMA.md §5.9](../DATABASE_SCHEMA.md): `document_chunks.search_vector` — `GENERATED ALWAYS AS
(to_tsvector(<config>, content)) STORED`, конфигурация — открытый вопрос; миграция `0009` колонку не создала.

Факты и ограничения:

- Корпус целевых инсталляций смешанный: русский и английский текст, часто в одном документе и даже в одном абзаце
  (термины, названия продуктов, коды). Язык документа не хранится и при ingestion не определяется.
- Расширения ограничены `vector` и `citext` ([ADR-011](0011-database-roles-and-extensions.md)); `unaccent`,
  внешние словари (hunspell) и сторонние расширения недоступны без отдельного решения.
- Встроенная конфигурация `russian` (проверено на `pgvector/pgvector:0.8.7-pg17`) применяет `russian_stem` к
  кириллическим словам и `english_stem` к латинским (`asciiword`, `asciihword`); английские стоп-слова
  отбрасываются словарём `english_stem`, русские — `russian_stem`. `ё` и `е` нормализуются одинаково; числа и
  коды остаются лексемами (`simple`).
- `to_tsvector(regconfig, text)` с константной конфигурацией — `IMMUTABLE`, поэтому пригодна для generated column.
- Chunks ограничены `ChunkingSettings.max_tokens` (по умолчанию 512): лимиты `tsvector` (1 МБ, позиции до 16383)
  недостижимы.
- Retrieval обязан фильтровать в SQL по доступным источникам, текущей `ready`-версии и активному `chunk_set`
  ([DATABASE_SCHEMA.md §5.11](../DATABASE_SCHEMA.md)); FTS-ветка подчиняется тем же правилам, что и vector-ветка.

## Decision

### 1. Одна generated-колонка с конфигурацией `russian`

```sql
ALTER TABLE document_chunks
  ADD COLUMN search_vector tsvector
  GENERATED ALWAYS AS (to_tsvector('russian'::regconfig, content)) STORED;
CREATE INDEX CONCURRENTLY ix_document_chunks_search_vector ON document_chunks USING gin (search_vector);
```

- Колонку вычисляет PostgreSQL: приложение её не пишет, и повтор ingestion не может рассинхронизировать её с
  `content`.
- Индекс создаётся отдельной ревизией `CONCURRENTLY` ([DATABASE_SCHEMA.md §9](../DATABASE_SCHEMA.md)).
- Конфигурация — константа схемы, а не настройка инсталляции: смена конфигурации меняет выражение колонки и
  требует миграции (пересчёт колонки и индекса), а не reindex документов.

### 2. Запрос

- Текст пользователя разбирается только `websearch_to_tsquery('russian', :query)`: синтаксис web-поиска
  (фразы в кавычках, `or`, `-слово`) без ошибок разбора на любом вводе; строка передаётся параметром.
- Ранжирование — `ts_rank_cd(search_vector, query)` (учитывает близость лексем); его значение используется только
  для порядка внутри FTS-ветки — слияние с vector-веткой идёт по рангам (RRF), а не по score.
- Запрос без лексем (только стоп-слова или знаки) даёт пустой `tsquery`: FTS-ветка пропускается, retrieval
  продолжается по vector-ветке.
- В запросе обязательны все фильтры доступа и версий §5.11; фильтр `search_vector @@ query` использует GIN-индекс.

## Alternatives

- **`simple`.** Без стемминга и стоп-слов: «отпуска» не находит «отпуск», английские формы не сводятся. Для
  русского — сильная потеря recall. Отклонено.
- **Два `tsvector` (`russian` и `english`) или конкатенация.** Для английского `russian` уже применяет
  `english_stem`, поэтому второй вектор почти дублирует первый, удваивает размер индекса и усложняет запрос.
  Отклонено.
- **Определение языка chunk и `regconfig` в строке.** Нужен детектор языка при ingestion (зависимость), а
  смешанные chunks всё равно получают один язык. Выигрыш появится только для третьих языков. Отложено (см.
  Consequences).
- **Отдельный поисковый движок (OpenSearch, Meilisearch).** Новая инфраструктура и второй источник истины для
  фильтров доступа (blueprint §3 выбирает PostgreSQL FTS). Отклонено.

## Consequences

- Один индекс и один запрос покрывают русский и английский; качество фиксируется baseline evaluation Фазы 6.
- Прочие языки (немецкий, французский…) на латинице проходят через `english_stem` — стемминг неточный; языки
  с иным алфавитом индексируются без стемминга. Поддержка дополнительных языков — новое ADR (вероятно, `regconfig`
  на KB или chunk).
- Колонка `STORED` увеличивает размер `document_chunks` (порядка размера текста); добавление колонки переписывает
  таблицу — на больших инсталляциях миграция занимает время и выполняется в окно обслуживания.
- Синонимы, морфология составных слов и опечатки не поддерживаются; их компенсирует vector-ветка.

## Security considerations

- Пользовательский запрос никогда не подставляется в SQL: только параметр `websearch_to_tsquery`, ошибки синтаксиса
  невозможны.
- FTS-ветка — тот же контур авторизации, что vector-ветка: фильтр по `allowed_source_ids`, вычисленным сервером, и
  по текущей `ready`-версии — в том же SQL-запросе, до выдачи результатов (§8 п.2).
- `ts_headline`/сниппеты строятся только по уже авторизованным chunks; содержимое документов и запросов не
  логируется.

## Implementation notes

- Миграции `0011` (колонка) и `0012` (GIN-индекс `CONCURRENTLY`, вне транзакции Alembic).
- ORM-модель объявляет колонку как `Computed(..., persisted=True)`; запись в неё запрещена.
- Тесты: стемминг русского и английского, `ё`/`е`, пустой `tsquery`, фильтры доступа и версий в FTS-ветке.

## Related documents

- [DATABASE_SCHEMA.md §5.9, §5.11](../DATABASE_SCHEMA.md)
- [ARCHITECTURE.md §9.4](../ARCHITECTURE.md)
- [ADR-011](0011-database-roles-and-extensions.md), [ADR-017](0017-vector-storage.md)
