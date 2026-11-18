# ADR-008: Unified Document Ingestion

- **Status:** Accepted
- **Date:** 2026-11-17
- **Amended by:** [ADR-012](0012-single-organization-deployment.md) — одна организация на инсталляцию; проверки tenant
  в pipeline заменены проверками доступа к KB и состояния ресурсов.

## Context

Blueprint требует один ingestion pipeline для локальных файлов и внешних коннекторов (§22 п.3), идемпотентные jobs
(§6, §11), версии документов (§5) и единый интерфейс `DataSourceConnector` (§7). Отдельные pipeline для upload,
Google Drive и Microsoft 365 утроили бы логику проверки, парсинга, chunking, удаления и тестов. Требуется
зафиксировать единую абстракцию источника и разделение «источник → документ → версия → результат обработки».

## Decision

### 1. Document Source

Вводится единая абстракция **Document Source** — это сущность `DataSource` из модели данных blueprint (§9),
таблица `data_sources`, группа API `data-sources`. Новая таблица не создаётся.

| Тип | Назначение | Состав |
|---|---|---|
| `upload` | Локальная загрузка файлов через UI/API | Создаётся автоматически для каждой KB и привязан ровно к ней |
| `google_drive` | Google Drive (Фаза 9) | OAuth credentials, выбранные папки, cursor Changes API |
| `microsoft_365` | SharePoint/OneDrive через Microsoft Graph (Фаза 10) | OAuth credentials, site/drive/library/folder, delta link |

Новые типы добавляются реализацией `DataSourceConnector` и значением `type`; pipeline не меняется.

Почему `upload`-источник создаётся на каждую KB: доступ в MVP задаётся на уровне KB (ADR-012), а источник может быть
подключён к нескольким KB. Общий upload-источник инсталляции сделал бы файл, загруженный в одну KB, видимым во всех KB,
к которым подключён источник.

### 2. Разделение понятий

| Понятие | Сущность | Идентичность | Изменяемость |
|---|---|---|---|
| Источник | `DataSource` | `id`; credentials и cursor отдельно | Конфигурация меняется |
| Документ | `Document` | `(data_source_id, external_id)` — unique | Логическая сущность; metadata источника обновляется (rename/move) |
| Версия | `DocumentVersion` | `(document_id, checksum_sha256)` — unique; `version_number` | **Неизменяема** после создания: оригинал и checksum не меняются |
| Результат обработки | `DocumentChunk`, `Embedding`, статус версии | Chunks: `(document_version_id, chunk_set, chunk_index)`; embeddings: `(chunk_id, embedding_config_id)` | Производные данные: можно пересоздать (reindex) без новой версии |
| Попытка обработки | `IngestionJob` | `idempotency_key` — unique | Журнал попыток, stage, ошибки |

### 3. Единый pipeline

Каждый документ из любого источника проходит одни и те же стадии. Нумерация согласована с blueprint §6.

| # | Стадия | Blueprint §6 | Примечание |
|---|---|---|---|
| 1 | Получение документа из источника | 1, 2 | Upload — поток от клиента; коннектор — `download_file`/export. Файл сразу попадает в quarantine |
| 2 | Проверка разрешений и состояния ресурсов | — (§8 п.1, п.2) | Источник активен и не удалён; KB/документ не удалены; для коннектора — файл внутри выбранной области; для upload — право `manage` на KB проверено при приёме и источник не удалён |
| 3 | Проверка типа и размера | 3 | Тип по содержимому; только форматы MVP |
| 4 | Проверка безопасности файла | 3 | Лимиты страниц/распаковки/архивов, macro-форматы, checksum |
| 5 | Сохранение оригинала в storage | 2, 5 | Перенос из quarantine; создание `DocumentVersion` (или повторное использование существующей по checksum) |
| 6 | Извлечение текста через `ParserProvider` | 4 | Изолированно, с timeout и лимитами ресурсов |
| 7 | Нормализация содержимого | 4, 5 | `NormalizedDocument`; извлечение metadata в версию |
| 8 | Chunking | 6 | `ChunkingStrategy`, сохранение page/section/sheet/row |
| 9 | Создание embeddings | 7 | По embedding profile KB |
| 10 | Сохранение chunks и индексов | 8 | Транзакционно; активация нового `chunk_set` |
| 11 | Обновление статуса обработки | 9 | Версия `ready`, `documents.current_version_id`, предыдущая версия `outdated` |
| 12 | События и логирование результата | 9 | Статусы job, структурированный лог с `job_id`/`correlation_id`, метрики (§11) |

### 4. Обязательные свойства

- **Идемпотентность.** Ключ job = `(document_version_id | source item, kind, chunking settings hash, embedding profile)`.
  Повторный job для версии, которая уже `ready` с тем же `chunk_set` и полными embeddings, завершается как
  `succeeded` без изменений (`skipped: already_processed`). Unique-ограничения и advisory lock на версию защищают от
  гонки двух одинаковых jobs.
- **Повторы и ошибки.** Retry с backoff для временных ошибок; постоянные ошибки (неподдерживаемый формат, лимиты) не
  повторяются; после исчерпания попыток — `dead`, ручной retry через API.
- **Версии.** Новый контент (другой checksum) → новая `DocumentVersion`. Тот же контент → существующая версия.
  Rename/move → обновление metadata документа без переиндексации.
- **Переиндексация.** Смена chunking settings → новый `chunk_set`, переключение после успеха, удаление старого.
  Смена embedding profile → новые embeddings рядом со старыми, переключение KB, удаление старых.
- **Удаление.** Документ исключается из retrieval немедленно (soft delete), производные данные и оригинал удаляются
  purge-job по retention. Удаление в источнике или выход файла за выбранную область → тот же lifecycle с
  `deletion_reason`. Удаление источника → удаление его документов тем же способом.
- **Синхронизация.** Sync job получает изменения через `get_changes` и порождает элементы, которые входят в стадию 1.
  Cursor сохраняется только после обработки страницы изменений.
- **Metadata источника.** `external_id`, путь, ETag/revision, времена создания/изменения в источнике, MIME источника
  сохраняются в `documents`/`document_versions` (`source_metadata`). Внешние ссылки не используются для доступа к
  содержимому (§5: in-app viewer).

### 5. Изоляция от SDK

Бизнес-логика pipeline находится в `application/ingestion` и знает только ports: `DataSourceConnector`,
`StorageProvider`, `ParserProvider`, `ChunkingStrategy`, `EmbeddingProvider`. Google и Microsoft SDK используются только
в `infrastructure/integrations/sources/*`. Коннектор возвращает поток байтов и `RemoteFileMetadata`; специфика (export
Google Docs в DOCX/XLSX/PDF, redirect загрузки Graph) скрыта в адаптере.

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| Отдельный pipeline на каждый источник | Дублирование проверок безопасности и логики, расхождение поведения, тройные тесты |
| Upload без источника (документ без `data_source_id`) | Две ветки кода и правил уникальности; нарушает «один pipeline» |
| Один upload-источник на инсталляцию | Ломает KB-level доступ (см. выше) |
| Новая таблица `document_sources` | `data_sources` из §9 уже решает задачу |
| Перезапись chunks версии «на месте» при reindex | Во время reindex retrieval видит неполный набор; гонки с повторными jobs |

## Consequences

- В `data_sources` появляется тип `upload`; в `document_chunks` — `chunk_set`, в `document_versions` —
  `active_chunk_set`; в `documents` — `deletion_reason`, `source_metadata` (DATABASE_SCHEMA).
- Upload API принимает файл, сохраняет его в quarantine и ставит job; вся обработка — в worker.
- Коннекторы Google Drive и Microsoft 365 (Фазы 9–10) не добавляют собственной логики парсинга, chunking и удаления.

## Security considerations

- Стадии 2–4 выполняются для **всех** источников: внешние файлы так же недоверенны, как загруженные.
- Worker повторно проверяет существование и состояние ресурсов, а не доверяет payload задачи.
- Содержимое документов не пишется в логи и ошибки job.
- Контрольные сценарии **SC-05**, **SC-08**, **SC-09** — [SECURITY_MODEL.md §18](../SECURITY_MODEL.md).

## Implementation notes

- Фаза 5 реализует pipeline только для `upload`, но интерфейсы стадии 1 и 2 сразу рассчитаны на коннекторы.
- Названия `chunk_set`/`active_chunk_set` — **[Proposed]**.

## Related documents

[ARCHITECTURE.md §9.2–9.3, §9.5](../ARCHITECTURE.md) · [DATABASE_SCHEMA.md §5.4–5.9, §7.1](../DATABASE_SCHEMA.md) ·
[API_CONTRACTS.md §2.9, §2.11](../API_CONTRACTS.md) · [SECURITY_MODEL.md §9](../SECURITY_MODEL.md) ·
[DEVELOPMENT_ROADMAP.md](../DEVELOPMENT_ROADMAP.md) · [PRODUCT_SPEC.md](../PRODUCT_SPEC.md)
