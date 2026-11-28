# PlummyKnowledge

[English version](README.md) — основной README с диаграммами и подробным описанием.

Open-source **self-hosted** платформа корпоративной базы знаний с AI-ассистентом. PlummyKnowledge подключает
источники документов, индексирует их, даёт сотрудникам безопасный поиск и отвечает на вопросы со **ссылками на
исходные фрагменты, которые проверяет backend**.

- **Данные остаются у вас:** документы, индексы, диалоги и настройки хранятся только в вашей инсталляции.
- **Любые модели:** облачные и локальные LLM (OpenAI, Anthropic, Ollama, OpenAI-compatible), отдельная модель для
  embeddings.
- **Проверенные цитаты:** модель может сослаться только на фрагменты, которые выдал ей backend; цитата открывает
  фрагмент во встроенном просмотрщике.
- **Минимум привилегий:** доступ проверяется до поиска, поэтому недоступный пользователю текст не попадает в модель.

> **Статус: 0.5.0** — первый релиз с production-профилем ([CHANGELOG](CHANGELOG.md)). Идёт подготовка 1.0.0 —
> релиза со стабильным публичным контрактом ([ADR-025](docs/adr/0025-semver-and-contract-stability.md)).

## Демо

Локальное демо без API-ключей: стек со встроенной фейковой моделью и базой знаний из примеров документов.
Команды и ограничения — в [README.md](README.md#try-the-demo).

## Быстрый старт (разработка)

Нужен Docker с Docker Compose v2.

```bash
git clone https://github.com/Dodoseek/PlummyKnowledge.git plummy-knowledge && cd plummy-knowledge
docker compose up --build
docker compose run --rm migrate cat /var/lib/plummy/bootstrap/setup-token   # токен первоначальной настройки
```

Откройте <http://localhost:5173>: мастер `/setup` создаст первого администратора. Дальше — **AI providers**
(провайдер и модель), **Embedding profiles**, **Knowledge bases** (загрузка документов и доступ), **Manage
assistants**, **Users** и **Chat**. Пошагово — в [README.md](README.md#first-steps).

## Production

```bash
sudo sh deploy/prod/init-secrets.sh
cp deploy/prod/prod.env.example deploy/prod/.env
docker compose -f compose.prod.yaml --env-file deploy/prod/.env --profile s3 up -d --build --wait
```

Сразу сохраните `deploy/prod/secrets/keyring.json` в защищённое место. Установка, обновление, backup и restore —
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Документация

Спецификации и ADR — на русском: [продукт](docs/PRODUCT_SPEC.md), [архитектура](docs/ARCHITECTURE.md),
[схема БД](docs/DATABASE_SCHEMA.md), [API](docs/API_CONTRACTS.md), [безопасность](docs/SECURITY_MODEL.md),
[развёртывание](docs/DEPLOYMENT.md), [roadmap](docs/DEVELOPMENT_ROADMAP.md), [ADR](docs/adr/README.md).

Участие — [CONTRIBUTING.md](CONTRIBUTING.md), уязвимости — [SECURITY.md](SECURITY.md). Лицензия —
[Apache License 2.0](LICENSE), см. [NOTICE](NOTICE).
