# ADR-007: Network Access and Local LLM Providers

- **Status:** Accepted
- **Date:** 2026-11-17
- **Amended by:** [ADR-012](0012-single-organization-deployment.md) — одна организация на инсталляцию: фильтр
  записей allowlist по организациям удалён, «администратор организации» → Administrator приложения.

## Context

Blueprint требует SSRF controls и запрет доступа к metadata/private endpoints по умолчанию (§8 п.7), но одновременно
— provider independence с облачными **и локальными** LLM, включая Ollama и OpenAI-compatible adapter (§1, §5).
Локальные модели почти всегда работают во внутренней сети или Docker network, то есть именно там, куда SSRF-защита
запрещает ходить. Endpoint провайдера задаёт администратор приложения через web-интерфейс, а значит это пользовательский ввод.

## Decision

**Explicit Network Allowlist** на уровне Instance Settings (ADR-006).

### 1. Правила

1. Все внутренние и потенциально опасные адреса **запрещены по умолчанию**.
2. Владелец инсталляции может явно разрешить конкретные адреса/сервисы в Instance Settings.
3. Administrator приложения **не может** расширить allowlist: API для этого не существует, а Application Settings
   не содержат сетевых параметров.
4. Каждая запись allowlist ограничена **назначением** (`purposes`) и, опционально, типами провайдеров.
5. Запись разрешает ровно `scheme + host + port` (и опциональный префикс пути) и **только адреса из своих
   `allowed_cidrs`** — не всю внутреннюю сеть и не все адреса, в которые может разрешиться hostname.
6. Проверка выполняется backend при **каждом** сетевом подключении, по фактическому IP соединения.
7. Локальные и облачные модели используют одни и те же `LLMProvider` / `EmbeddingProvider`; отдельной архитектуры для
   локальных моделей нет. Разница только в сетевой политике.

### 2. Запись allowlist **[Proposed — формат]**

```toml
[[network.allowlist]]
name           = "local-ollama"
url            = "http://ollama:11434"          # точное совпадение scheme/host/port
purposes       = ["llm", "embedding"]           # llm | embedding | connector (ADR-020) | (позже: webhook)
provider_types = ["ollama"]                     # необязательно
allowed_cidrs  = ["172.30.10.0/24"]             # обязательно, если адрес не публичный
```

Примеры адресов, которые владелец **может** разрешить: `http://ollama:11434`, `http://host.docker.internal:11434`.
Это только примеры — **по умолчанию allowlist пуст**.

### 3. Классы адресов

| Класс | Диапазоны | По умолчанию | Через allowlist |
|---|---|---|---|
| Публичные (global unicast) | Всё, что не попало ниже | Разрешены для `https` | — |
| Loopback | `127.0.0.0/8`, `::1` | Запрещены | Да, точным CIDR (не рекомендуется) |
| Private / ULA | `10/8`, `172.16/12`, `192.168/16`, `fc00::/7` | Запрещены | Да, точным CIDR |
| CGNAT, benchmark, reserved | `100.64/10`, `198.18/15`, `240/4` и др. IANA special-purpose | Запрещены | Да, точным CIDR |
| Link-local, cloud metadata | `169.254.0.0/16`, `fe80::/10`, `fd00:ec2::254`, `100.100.100.200` | Запрещены | **Никогда** |
| Unspecified, multicast, broadcast | `0.0.0.0/8`, `::`, `224/4`, `ff00::/8`, `255.255.255.255` | Запрещены | **Никогда** |
| Собственные сервисы платформы | IP PostgreSQL, Redis, storage из конфигурации | Запрещены | **Никогда** (ошибка конфигурации при пересечении) |

### 4. Алгоритм проверки соединения

1. Строгий разбор URL: только `http`/`https`; запрет userinfo (`user@host`), пустого host, обратных слэшей; host
   нормализуется (lowercase, IDNA, без завершающей точки).
2. IP-литералы допускаются только в каноническом виде (dotted-quad, `[IPv6]`); десятичные, восьмеричные,
   шестнадцатеричные и сокращённые формы (`2130706433`, `0177.0.0.1`, `0x7f.1`, `127.1`) и IPv6 zone ID отклоняются.
3. Поиск записи allowlist по `(scheme, host, port, path prefix)` + `purpose` + тип провайдера.
4. Одно DNS-разрешение средствами приложения. Каждый полученный адрес нормализуется: IPv4-mapped/compatible IPv6,
   NAT64 (`64:ff9b::/96`), 6to4 (`2002::/16`), Teredo (`2001::/32`) разворачиваются во вложенный IPv4, который
   проверяется наравне с исходным.
5. Решение:
   - нет записи → **все** адреса должны быть публичными; иначе отказ;
   - есть запись с `allowed_cidrs` → **все** адреса должны входить в `allowed_cidrs`; иначе отказ;
   - есть запись без `allowed_cidrs` (публичный сервис, например только с ограничением `purposes`) → **все**
     адреса должны быть публичными;
   - запрещённые «никогда» классы отклоняются всегда.
6. Подключение выполняется **к проверенному IP** (pinning); `Host` и TLS SNI — исходное имя. Повторного DNS-разрешения
   между проверкой и подключением нет.
7. Redirects: для AI-провайдеров не выполняются. Для коннекторов (скачивание через Microsoft Graph и Google Drive
   использует redirect на хосты загрузки) каждый переход проходит шаги 1–6 заново и ограничен числом (≤ 3) и
   списком допустимых хостов назначения коннектора.
8. Переменные окружения `HTTP(S)_PROXY` игнорируются, если прокси явно не настроен в Instance Settings (иначе проверка
   обходится через прокси).
9. Отказ → типизированная ошибка `egress_denied`, событие `network.egress.denied` в audit (без тела запроса).

### 5. Что настраивает владелец инсталляции для локального провайдера

1. Разместить сервис модели в отдельной Docker network с фиксированной подсетью (или на хосте) и не публиковать его
   порт наружу; у Ollama нет встроенной аутентификации — изоляция обеспечивается сетью.
2. Подключить к этой сети только `api` и `worker` (не БД/Redis-сеть).
3. Добавить запись allowlist: точный URL, `purposes`, `provider_types`, `allowed_cidrs` этой подсети.
4. Для `host.docker.internal` на Linux — сопоставление host-gateway в Compose и CIDR шлюза Docker bridge.
5. Перезапустить API и worker (Instance Settings читаются при старте).
6. Administrator приложения создаёт провайдера `ollama` / `openai_compatible` с тем же `base_url` и выполняет
   connection test.

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| Полный запрет внутренних адресов | Делает локальные модели невозможными — противоречит provider independence (§1) |
| Allowlist по hostname без CIDR | Разрешённое имя может разрешиться в любой адрес (DNS rebinding/spoofing) |
| Allowlist в Application Settings (через web-интерфейс) | Компрометация web-учётной записи Administrator открывает доступ во внутреннюю сеть инсталляции |
| Только сетевые политики Docker/firewall | Полезный второй рубеж, но не проверяет purpose и тип провайдера и зависит от окружения |
| Отдельная архитектура для локальных моделей | Дублирование адаптеров и тестов; противоречит единым интерфейсам (§22 п.5) |

## Consequences

- Все исходящие соединения (AI-провайдеры, connection tests, коннекторы, OAuth token exchange) идут через единый
  `SafeHttpClient`; прямые HTTP-вызовы SDK без него запрещены. Для SDK, которые создают свой HTTP-клиент, адаптер
  передаёт им защищённый транспорт.
- Изменение allowlist требует доступа к серверу и перезапуска.
- `base_url` провайдера валидируется при создании/изменении (быстрая обратная связь) **и** при каждом подключении
  (DNS мог измениться).

## Security considerations

- Защита покрывает: SSRF, DNS rebinding, DNS spoofing (pinning + CIDR + обязательная проверка TLS-сертификата для
  `https`), redirects, IPv4/IPv6-обходы, loopback, private, link-local, metadata, альтернативные записи IP, повторное
  DNS-разрешение.
- **Второй рубеж (рекомендация для deployment):** сегментация Docker networks (`internal: true` для сети данных),
  блокировка `169.254.169.254` и внутренних диапазонов для контейнеров worker/api на уровне хоста (например, цепочка
  `DOCKER-USER`) или egress-прокси; в облаке — IMDSv2 с hop limit 1. См. [DEPLOYMENT.md](../DEPLOYMENT.md).
- Контрольные сценарии **SC-02**, **SC-06**, **SC-07** — [SECURITY_MODEL.md §18](../SECURITY_MODEL.md).

## Implementation notes

- `SafeHttpClient` появляется в Фазе 4 вместе с первым адаптером провайдера — до поддержки Ollama.
- Фаза 9: назначение `connector` и `RedirectPolicy` (§4 п.7): переходы только для `GET`/`HEAD`, только `https`, хост из
  списка коннектора (точное имя или `*.suffix`), ≤ 3; каждый переход заново проходит разбор URL, allowlist, DNS и
  pinning; `Authorization`/`Cookie` на другой хост не передаются; отказ — `redirect_not_allowed` /
  `too_many_redirects` в audit ([ADR-020](0020-google-drive-connector.md) §5).
- Набор специальных диапазонов берётся из реестров IANA special-purpose address и проверяется тестами на каждый класс.

## Related documents

[ADR-006](0006-initial-setup-and-system-configuration.md) · [SECURITY_MODEL.md §8](../SECURITY_MODEL.md) ·
[ARCHITECTURE.md §8.7, §10](../ARCHITECTURE.md) · [API_CONTRACTS.md §2.7, §2.17](../API_CONTRACTS.md) ·
[DEPLOYMENT.md](../DEPLOYMENT.md) · [PRODUCT_SPEC.md](../PRODUCT_SPEC.md)
