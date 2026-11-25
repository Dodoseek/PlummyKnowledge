# Оценка качества retrieval

Фаза 6 roadmap ([DEVELOPMENT_ROADMAP.md](../DEVELOPMENT_ROADMAP.md)): воспроизводимый набор данных, метрики
Recall@K, MRR и nDCG@K и CLI, который прогоняет набор через **обычный** путь продукта — upload, ingestion,
индексацию и hybrid retrieval ([ARCHITECTURE.md §9.4](../ARCHITECTURE.md)).

Метрики генерации (citation precision/coverage, groundedness, корректный отказ) требуют LLM-ответов и
переносятся в harness фазы 8 (chat); в этой фазе неотвечаемые вопросы входят в набор, но не оцениваются.

## Набор `synthetic-v1`

[`apps/backend/evaluation/datasets/synthetic-v1/`](../../apps/backend/evaluation/datasets/synthetic-v1/) —
вымышленная логистическая компания: имена, адреса, телефоны и домены (`*.example`) выдуманы.

| | |
|---|---|
| Документы | 36: Markdown, TXT, CSV; русский и английский |
| Вопросы | 87, из них 8 без ответа в документах (`unanswerable`) |
| Похожие документы (`similar`) | отпуск в России и в Гамбурге, офисы в Москве и Казани, расходы EU и US |
| Противоречия (`conflict`) | командировки 2023 и 2025, roadmap 2025 и 2026: ответ — в новой редакции |
| Другое | `cross_language` (вопрос на английском, ответ в русском тексте), `table` (CSV), `multi` (несколько фактов) |

`manifest.json` перечисляет документы и вопросы. Каждый релевантный факт — пара `{document, evidence}`:
фрагмент текста, который обязан встречаться в документе. При загрузке набор проверяется строго (документы
существуют, evidence найден в своём документе, id уникальны), иначе CLI завершается ошибкой до подключения к
БД. Fingerprint набора — SHA-256 manifest и всех файлов.

PDF и DOCX в набор не входят: их парсинг (Docling) медленный и зависит от модели layout; качество retrieval
измеряется на тексте, а парсеры покрыты своими тестами.

## Метрики

Chunk **покрывает** факт, если он из нужного документа и его текст содержит evidence (без учёта регистра,
пробелов и различия «ё»/«е»). Оценка не зависит от границ chunks и их id, которые меняются при переиндексации.

- **Recall@K** — доля фактов вопроса, покрытых первыми K chunks.
- **MRR** — среднее `1 / rank` первого chunk, покрывающего хотя бы один факт (0, если такого нет в top-K).
- **nDCG@K** — gain chunk = число фактов, которые он покрывает впервые; идеальный список покрывает один новый
  факт на позицию; значение ограничено 1.

Средние считаются по отвечаемым вопросам: всего (`all`) и по каждому тегу. Для измерения ранжирования
бюджет контекста снят (`max_context_tokens = 128000`), `top_k = max(K)`; остальные параметры — по умолчанию
(`vector_top_k = keyword_top_k = 40`, `rrf_k = 60`).

## Запуск

Нужны запущенные `backend`, `worker`, `worker-ingestion`, активный embedding profile с рабочим провайдером и
активный пользователь с `knowledge_bases.manage`. **Индексация вызывает embedding-провайдер** (36 документов и
87 запросов на каждый режим с векторной веткой), то есть расходует токены выбранного провайдера.

```bash
docker compose exec backend python -m plummy.cli eval run \
  --dataset /app/evaluation/datasets/synthetic-v1 \
  --email admin@example.com --embedding-profile <profile-id> \
  --output /tmp/report.json
```

- `--mode hybrid|vector|keyword` (повторяемый; по умолчанию все три), `--k 1,3,5,10`, `--timeout` (ожидание
  индексации, по умолчанию 1800 с); без `--output` отчёт печатается в stdout, прогресс — в stderr.
- Для набора создаётся отдельная KB `Evaluation <набор> <fingerprint> <profile>`; повторный запуск переиспользует
  её и загружает только недостающие документы. Изменённый набор получает новую KB. KB удаляется обычным образом.
- CLI действует от имени указанного пользователя без сессии: роли и права — как у его сессий, все проверки
  доступа выполняют те же use cases. Rate limit загрузки соблюдается (CLI ждёт `Retry-After`).
- Документ, не прошедший индексацию, останавливает прогон: метрики по неполному набору недостоверны.

## Отчёт

JSON: набор (имя, fingerprint, число документов и вопросов), embedding profile, параметры, время, и для каждого
режима — `summary` (Recall/MRR/nDCG по `all` и тегам), средняя длительность запроса и результаты по вопросам
(id, теги, метрики, ранг первого релевантного chunk, имена файлов в выдаче). Текст документов и вопросов в
отчёт **не** попадает.

## Baseline

[baseline-2026-11.md](baseline-2026-11.md) — первый прогон на `bge-m3` в локальном Ollama, вместе с отчётом
[`synthetic-v1-bge-m3-baseline.json`](synthetic-v1-bge-m3-baseline.json). Изменения retrieval (параметры fusion,
FTS, chunking, embedding profile) сравниваются с ним повторным прогоном.

Воспроизведение на машине разработчика (Docker Desktop, модель на хосте; бесплатно, без внешних запросов):

1. `ollama serve` и `ollama pull bge-m3` (≈ 1,2 ГБ).
2. Файл Instance Settings с allowlist и `CONFIG_FILE` для `backend` и `worker` (например, через отдельный
   compose override, не коммитится):

   ```toml
   [[network.allowlist]]
   name           = "local-ollama"
   url            = "http://host.docker.internal:11434"
   purposes       = ["embedding"]
   provider_types = ["ollama"]
   allowed_cidrs  = ["192.168.65.254/32"]   # адрес host.docker.internal в Docker Desktop; проверить в контейнере
   ```

3. Провайдер `ollama` с `base_url` `http://host.docker.internal:11434` и embedding profile `bge-m3`, 1024
   измерения, cosine; connection test профиля должен пройти.
4. `eval run` с id этого профиля (раздел «Запуск»).
