# ADR-009: Automatic Versioning

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Владелец проекта требует, чтобы версия автоматически повышалась при каждом коммите. Blueprint предписывает SemVer,
CHANGELOG и GitHub Release (§13 Фаза 13) и закрепляет номера релизов как продуктовые вехи: 0.1.0 — MVP, 0.2.0 —
XLSX/CSV и RAG, 0.3.0 — Google Drive, 0.4.0 — Microsoft 365, 0.5.0 — hardening, 1.0.0 — production readiness (§19).
Схема «feat → minor» сдвинула бы номера относительно roadmap, поэтому номер релиза не должен зависеть от числа
коммитов.

## Decision

1. Единственный источник версии — файл `VERSION` в корне репозитория.
2. Формат — подмножество SemVer: `X.Y.Z` (релиз) или `X.Y.Z-dev.N` (сборка разработки на пути к релизу `X.Y.Z`).
   По SemVer `X.Y.Z-dev.N` < `X.Y.Z`.
3. **Каждый коммит** повышает версию через git-hook `pre-commit`:
   - `X.Y.Z-dev.N` → `X.Y.Z-dev.(N+1)`;
   - после релиза `X.Y.Z` → `X.(Y+1).0-dev.1`.
4. **Релиз** — явное действие владельца: `scripts/version.py release` превращает `X.Y.Z-dev.N` в `X.Y.Z`, создаёт
   коммит `chore(release): vX.Y.Z` и аннотированный тег `vX.Y.Z`. Публикация — `git push origin HEAD vX.Y.Z`.
5. **Другая цель** (например, patch-релиз или 1.0.0): `scripts/version.py set <version>`; версия должна быть больше
   текущей, следующий коммит её сохраняет.
6. **Слияния:** `VERSION` меняют обе ветки, поэтому в `.gitattributes` подключён merge-driver: результат =
   следующая версия после большей из двух.
7. Версия **никогда не уменьшается**: hook и команды отклоняют или исправляют попытки понизить её.
8. Реализация — `scripts/version.py` (только стандартная библиотека Python), hooks — `.githooks/`, установка —
   `scripts/setup-git-hooks.sh` (один раз на клон).

Начальная версия — `0.1.0-dev.N`: проект движется к релизу 0.1.0 (MVP).

## Alternatives

| Вариант | Почему не выбран |
|---|---|
| Conventional Commits (feat → minor, fix → patch) | Номера быстро расходятся с вехами roadmap (0.1.0 наступил бы после первого `feat`) |
| Patch на каждый коммит (0.0.1, 0.0.2, …) | Patch теряет смысл «исправления»; номера релизов задаются вручную |
| Версия, вычисляемая из истории git (`git describe`) без файла | Нет конфликтов и hooks, но версия не видна в содержимом коммита; не требуется владельцем |
| Повышение версии в CI после push | Повышает версию на push, а не на коммит; требует отдельных коммитов от бота |

## Consequences

- Каждый клон должен один раз выполнить `sh scripts/setup-git-hooks.sh`. Без этого коммиты не повышают версию.
- Коммиты, созданные без hooks (`git commit --no-verify`, web-интерфейс GitHub, `git rebase`/`cherry-pick` без
  конфликтов в `VERSION`), версию не повышают. Для контроля предусмотрена команда `scripts/version.py check`; её
  подключение в CI — задача Фазы 1 (GitHub Actions).
- `git commit --amend` с изменениями повышает версию ещё раз (номер пропускается); монотонность сохраняется.
- Если коммит прерван после hook (например, пустое сообщение), повышенный `VERSION` остаётся в индексе — следующий
  коммит использует его без пропуска номера.
- Пустой коммит без изменений версию не трогает (git всё равно его отклоняет).
- Однократный пропуск: `PLUMMY_SKIP_VERSION_BUMP=1 git commit …`.

## Security considerations

Hooks выполняют только локальный скрипт из репозитория, без сети и сторонних зависимостей. Hooks активируются явно
командой установки (`core.hooksPath`), а не автоматически при клонировании.

## Implementation notes

- После `git commit <paths>` git использует временный индекс; hook `post-commit` (`version.py sync-index`)
  возвращает `VERSION` в основной индекс.
- Backend (`pyproject.toml`), frontend (`package.json`) и Docker-образы в Фазе 1 должны читать версию из `VERSION`,
  а не дублировать её.
- CHANGELOG ведётся к релизам (§13 Фаза 13), а не к каждому коммиту.

## Related documents

[DEVELOPMENT_ROADMAP.md §3](../DEVELOPMENT_ROADMAP.md) · [CLAUDE.md](../../CLAUDE.md) ·
[PROJECT_BLUEPRINT.md](../PROJECT_BLUEPRINT.md) §13, §19
