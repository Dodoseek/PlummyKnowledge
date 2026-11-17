# ADR-001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-11-17

## Context

Blueprint v1.0 является базовой спецификацией и roadmap и требует фиксировать архитектурные изменения в ADR (§1).
Значительная часть кода будет создаваться с помощью AI-агентов; риск «архитектурного дрейфа от AI coding» назван в
blueprint явно (§18) и митигируется через CLAUDE.md, ADR и code review.

## Decision

- Значимые архитектурные решения фиксируются в `docs/adr/` по шаблону из [README.md](README.md).
- ADR обязателен **до** любого изменения framework, DB, queue, tenant model, auth/session strategy или публичного API
  (§15).
- Порядок приоритетов источников: явные требования владельца проекта и согласованные решения → Accepted ADR →
  `docs/` → проектный `CLAUDE.md` → глобальные инструкции агента.
- Номера ADR неизменяемы; заменённые решения получают статус `Superseded`.

## Alternatives

- Хранить решения только в спецификациях — теряется история и обоснование, спецификации разрастаются.
- Вики вне репозитория — решения расходятся с кодом и недоступны агентам.

## Consequences

- Любая задача, затрагивающая перечисленные области, начинается с ADR (Proposed) и согласования.
- Спецификации ссылаются на ADR, а не дублируют их обоснование.

## Security considerations

Изменения auth/session strategy и tenant model всегда проходят через ADR — это снижает риск незаметного ослабления
изоляции.

## Implementation notes

Индекс поддерживается в [README.md](README.md). Темы, требующие будущих ADR, перечислены там же.

## Related documents

[PROJECT_BLUEPRINT.md](../PROJECT_BLUEPRINT.md) §1, §15, §18, §22 · [CLAUDE.md](../../CLAUDE.md)
