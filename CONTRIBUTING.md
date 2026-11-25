# Участие в разработке

Спасибо за интерес к PlummyKnowledge. Проект — модульный монолит с жёсткими правилами безопасности, поэтому перед
изменениями прочитайте [README.md](README.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) и утверждённые [ADR](docs/adr/README.md). Рабочие правила и все
команды собраны в [CLAUDE.md](CLAUDE.md) — они действуют и для людей.

Об уязвимостях не пишите в issues — см. [SECURITY.md](SECURITY.md).

## Перед началом

- Для ошибки или идеи откройте issue: что происходит, что ожидается, как воспроизвести, версия.
- Крупное изменение (новая функция, смена поведения API) сначала обсудите в issue.
- Изменение framework, БД, очереди, модели доступа, auth/session-стратегии или публичного API требует ADR со
  статусом `Proposed` **до** кода ([docs/adr/](docs/adr/README.md)).
- Не расширяйте MVP: что вне его границ — [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md).

## Среда

```bash
sh scripts/setup-git-hooks.sh        # один раз: hooks (secret scan, lint, VERSION)
docker compose up --build            # весь стек разработки
```

Frontend требует npm 11.21.2 (`npx npm@11.21.2 ci` в `apps/frontend`).

## Требования к изменению

- **Одна задача — один коммит**, с понятным сообщением на английском.
- **Слои:** `domain` и `application` не импортируют FastAPI, Celery, SQLAlchemy и SDK; бизнес-логика — в use cases,
  не в routers и не в Celery tasks; ORM-модели не отдаются из API.
- **Безопасность:** права проверяются в каждом use case; данные KB фильтруются в SQL до retrieval и до передачи
  текста в LLM; исходящие соединения — только через `SafeHttpClient`; секреты, токены, пароли, содержимое документов
  и prompts не логируются.
- **Тесты обязательны:** позитивные, негативные, граничные случаи, ошибки и, если задача касается доступа, проверка
  отсутствия доступа к чужим данным. Integration-тесты — на настоящих PostgreSQL и Redis.
- **Зависимости:** новая или обновлённая зависимость — только с обоснованием в описании.
- **Документация:** обновите затронутые документы в `docs/` в том же изменении.
- **Версия:** `VERSION` повышает pre-commit hook; не редактируйте его вручную и не используйте `--no-verify`.

## Проверки

```bash
docker compose exec backend pytest
docker compose exec backend ruff check src tests alembic
docker compose exec backend ruff format --check src tests alembic
docker compose exec backend mypy
cd apps/frontend && npm test && npm run lint && npm run typecheck && npm run format:check && npm run build
python3 scripts/check_secrets.py
```

E2E (Playwright) — команды в [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) («E2E-стек»). CI запускает всё это на каждый
pull request.

## Лицензия

Отправляя изменение, вы соглашаетесь, что оно распространяется на условиях [Apache License 2.0](LICENSE)
(раздел 5 лицензии).
