# ADR-021: Microsoft 365 Connector

- **Status:** Accepted
- **Date:** 2026-11-26 (утверждено владельцем проекта вместе с планом Фазы 10: delegated permissions)

## Context

Фаза 10 ([DEVELOPMENT_ROADMAP.md](../DEVELOPMENT_ROADMAP.md)) подключает второй внешний источник — SharePoint
document libraries и OneDrive для работы и учёбы через Microsoft Graph. Каркас уже есть
([ADR-020](0020-google-drive-connector.md)): источник принадлежит одной KB, OAuth Authorization Code + PKCE с
одноразовым `state` в Redis, refresh token в vault, `sync_jobs` и очередь `connectors`, sync engine с checkpoint,
правилами области папки и reconciliation, typed errors коннектора, redirects загрузки через `SafeHttpClient`.

Открытыми оставались: модель разрешений Graph (delegated или app-only `Sites.Selected`), tenant, scopes, отзыв
токена (у Microsoft identity platform нет endpoint отзыва отдельного токена), структура picker'а (site → library →
папка), курсор Delta Query (он ведётся на весь drive, а не на учётную запись) и то, что из этого требует обобщить
порты, написанные под Google.

## Decision

### 1. Разрешения: delegated, один tenant

- Поток — Authorization Code + PKCE (S256) от имени пользователя, как в ADR-020 §1: тот же `state` в Redis (TTL 10
  минут, `GETDEL`), та же сверка сессии, пользователя и прав в callback, тот же redirect URI
  `PUBLIC_URL/api/v1/data-sources/oauth/callback` и фиксированный путь возврата в UI. OAuth client — confidential
  (*Web*, client secret).
- Authority — **один tenant организации**: `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize` и
  `/token`. `common`, `organizations` и `consumers` запрещены конфигурацией ([ADR-012](0012-single-organization-deployment.md):
  инсталляция принадлежит одной организации). Личные учётные записи Microsoft (OneDrive personal) не
  поддерживаются.
- Запрашиваются scopes `offline_access`, `https://graph.microsoft.com/Files.Read.All`,
  `https://graph.microsoft.com/Sites.Read.All`, `https://graph.microsoft.com/User.Read`; `prompt=select_account`
  (выбрать служебную учётную запись, а не текущую). `Sites.Read.All` требует согласия администратора tenant
  (выдаётся заранее в Entra admin center, DEPLOYMENT §7.5).
- Фактически выданные scopes (поле `scope` ответа token endpoint, сравнение без префикса
  `https://graph.microsoft.com/` и без учёта регистра) сохраняются в `granted_scopes` как есть. Нет `Files.Read.All`
  или `Sites.Read.All` — отказ `oauth_scope_missing`; нет `refresh_token` (не выдан `offline_access`) —
  `oauth_no_refresh_token`; `consent_required`/`access_denied` — `oauth_denied`.
- UI показывает `granted_scopes`, учётную запись и предупреждение: **выбор папки не ограничивает токен** —
  `Files.Read.All` и `Sites.Read.All` читают всё, что доступно учётной записи во всех сайтах и OneDrive tenant.
  Рекомендация: отдельная служебная учётная запись с доступом только к нужным библиотекам.
- Имя учётной записи — `GET /me?$select=displayName,userPrincipalName`; `openid`/`profile` и ID token не нужны.

### 2. Конфигурация (Instance Settings)

- `MICROSOFT_OAUTH_TENANT_ID` (GUID или проверенный домен tenant), `MICROSOFT_OAUTH_CLIENT_ID`,
  `MICROSOFT_OAUTH_CLIENT_SECRET` (или `MICROSOFT_OAUTH_CLIENT_SECRET_FILE`). Без всех трёх `POST /data-sources` с
  `type = microsoft_365` → `409 connector_not_configured`; `GET /system/settings` сообщает
  `connectors.microsoft_365.configured` и `redirect_uri` (без значений). Сертификат вместо client secret — позже,
  отдельным решением.
- Адреса Microsoft фиксированы в коде (глобальное облако; национальные облака вне scope). Секция
  `[connectors.microsoft_365]` в `CONFIG_FILE` переопределяет `auth_url`, `token_url`, `api_url`,
  `download_hosts` **только при `APP_ENV != production`** — для E2E, по правилам ADR-020 §2.

### 3. Отзыв

У Microsoft identity platform нет endpoint отзыва отдельного refresh token (RFC 7009), а `revokeSignInSessions`
завершает **все** сессии пользователя — это вне полномочий приложения. Поэтому `disconnect` и `DELETE` удаляют секрет
и credentials локально, отменяют задания и пишут audit; `capabilities.revoke = false`. UI и DEPLOYMENT объясняют,
как отозвать согласие у провайдера (My Apps пользователя или удаление согласия корпоративного приложения в Entra).
Refresh token Microsoft живёт до 90 дней без использования и при каждом обновлении заменяется новым — новый
сохраняется в vault (как ротация в ADR-020, 9.3). `invalid_grant` и `interaction_required` при обновлении →
credentials `revoked`, источник `error`, `connector_auth_revoked`.

### 4. HTTP без Microsoft SDK

Адаптер `MicrosoftGraphConnector` и `MicrosoftOAuthClient` используют `SafeHttpClient` напрямую, без `msal` и
`msgraph-sdk` (как ADR-020 §4): новых зависимостей нет. Методы Graph v1.0: `GET /me`, `GET /me/drive`,
`GET /sites?search=`, `GET /sites/{id}/drives`, `GET /drives/{d}`, `GET /drives/{d}/items/{i}`,
`GET /drives/{d}/items/{i}/children`, `GET /drives/{d}/items/{i}/content`, `GET /drives/{d}/root/delta`.

### 5. Сеть

`EgressPurpose.CONNECTOR`; хосты коннектора — `login.microsoftonline.com`, `graph.microsoft.com` и для redirect
загрузки `*.sharepoint.com`. `GET …/content` отвечает `302` на предварительно подписанный URL SharePoint:
переход проверяется по ADR-007 §4 п.7, `Authorization` на другой хост не передаётся (URL сам несёт временную
авторизацию и в логи не пишется). Ссылки `@odata.nextLink`/`@odata.deltaLink` не используются как адреса: из них
берётся только параметр `token` (или `$skiptoken`), запрос строится заново от фиксированного `api_url`; ссылка на
другой хост или путь — `connector_invalid_response`.

### 6. Источник и выбор папки

- Как ADR-020 §6: `POST /data-sources` с `type = microsoft_365`, один источник — одна KB, те же права и статусы.
- Источник синхронизирует **одну папку одного drive** (document library сайта или OneDrive пользователя). `config`:
  `{drive_id, folder_id, folder_name, site_id?, site_name?, drive_name}`; корень библиотеки выбирается как папка.
- `browse` — уровни: без `parent_id` — OneDrive учётной записи (`/me/drive`) и сайты (`/sites?search=*`, первые
  100); `parent_id` сайта — его библиотеки; `parent_id` библиотеки или папки — вложенные папки. ID записей picker'а
  непрозрачны для клиента: `site:{site_id}`, `drive:{drive_id}`, `item:{drive_id}:{item_id}`. `kind` расширяется
  значениями `site` (только для перехода, выбрать нельзя), `drive` (библиотека, выбирается как папка) и `folder`.
- `PATCH {"folder_id"}` принимает ID записи `drive:` или `item:`; сервер проверяет у Graph, что это папка или корень
  drive (`folder` или `root` facet, не `deleted`), и сохраняет `drive_id`, `folder_id` (ID элемента) и имена.

### 7. Синхронизация

Тот же sync engine (ADR-020 §8: задания, checkpoint, правила, reconciliation, расписание, лимиты), с отличиями:

- **Курсор** — Delta Query на весь drive: `GET /drives/{d}/root/delta`. Delta от вложенной папки Graph поддерживает
  только для OneDrive personal, поэтому изменения фильтруются по области папки, как в ADR-020 (цепочка
  `parentReference.id` до выбранной папки, кэш на задание). В `sync_cursor` хранится только значение `token`
  (не URL), ≤ 2048 символов. Начальный курсор — `delta?token=latest` (без перечисления), берётся **до** обхода.
- **Инкрементальная:** страницы delta по `token`; после каждой обработанной страницы сохраняется `token` следующей
  (checkpoint), конец — `token` из `@odata.deltaLink`. `410 Gone` (`resyncRequired`) → `ConnectorCursorInvalid` →
  reconciliation.
- **Удаление:** элемент с facet `deleted` → документ удаляется (`remote_deleted`); у удалённых элементов нет
  родителя, поэтому они сопоставляются по `external_id` (ID элемента). Удалённая или перенесённая папка с
  документами → reconciliation (как в ADR-020).
- **Ревизия:** `file.hashes.quickXorHash` (SharePoint и OneDrive для работы), иначе `sha256Hash`/`sha1Hash`, иначе
  `cTag` (меняется только при изменении содержимого); `eTag` (меняется и при переименовании) для этого не
  используется. Переименование и перенос — обновление метаданных без переиндексации.
- **Форматы:** файлы скачиваются как есть (`…/content`), тип определяется по содержимому, как у upload; PPTX,
  OneNote (`package`) и прочие неподдерживаемые — `stats.skipped`. Офисные файлы не конвертируются.
- **Ограничения скорости:** `429` и `503` с `Retry-After` → `ConnectorRateLimited`; `507`/квоты tenant →
  `ConnectorQuotaExceeded`; `403 accessDenied` → `ConnectorPermissionDenied`; `404 itemNotFound` →
  `ConnectorNotFound`; `401` на свежий токен — `ConnectorAccessTokenRejected`.

### 8. Обобщение портов

Порты ADR-020 остаются, но методы курсора и изменений получают **место синхронизации** источника —
`SourceLocation(drive_id: str | None, folder_id: str)`: Google его игнорирует (Changes API ведётся на учётную
запись), Graph берёт `drive_id`. `BrowseKind` получает `site` и `drive`. `ConnectorCapabilities.revoke` уже есть:
при `false` disconnect не обращается к провайдеру. Код Google не меняет поведение; тесты Фазы 9 подтверждают это.

### 9. Тестирование без живого Microsoft

- Unit и integration: фейковый Entra и Graph на loopback за `SafeHttpClient` (как `tests/fake_google.py`),
  настоящие PostgreSQL и Redis.
- E2E: stdlib-сервер `deploy/e2e/fake_microsoft.py` с адресами из `[connectors.microsoft_365]` и записью allowlist
  `connector`; сценарий как `drive.spec.ts`.
- Опциональный ручной live-тест (`-m live`) с настоящим tenant; в CI не запускается.

## Alternatives

| Вариант | Почему отклонён |
|---|---|
| App-only `Sites.Selected` (client credentials, доступ к сайтам выдаёт администратор) | Самый узкий доступ, но без подключения пользователем, без OneDrive пользователей, с отдельным вызовом Graph администратора на каждый сайт; другой сценарий UI и credentials. Остаётся кандидатом для отдельного ADR, если организации понадобится изоляция на уровне OAuth |
| Delegated `Files.Read.All` без `Sites.Read.All` | Не перечисляет сайты для picker'а и не всегда видит библиотеки сайтов |
| Authority `common`/`organizations` | Подключение учётных записей чужих tenant'ов противоречит ADR-012 |
| `msal` / Microsoft Graph SDK | Свой HTTP-транспорт в обход `SafeHttpClient`, лишние зависимости ради 9 методов |
| Хранить `@odata.deltaLink` целиком | URL от провайдера стал бы адресом исходящего запроса; хранится только `token` |
| `revokeSignInSessions` при disconnect | Завершает все сессии пользователя во всех приложениях — непропорционально |
| Delta от выбранной папки | Для SharePoint и OneDrive для работы Graph поддерживает delta только от корня drive |

## Consequences

- Миграция не нужна: тип `microsoft_365`, `data_source_credentials`, `sync_jobs`, `sync_cursor` (≤ 2048) уже есть.
- Порты коннектора получают `SourceLocation`, `BrowseKind` — `site` и `drive`; API browse и PATCH принимают
  непрозрачные ID picker'а.
- Новые Instance Settings `MICROSOFT_OAUTH_*`; секция `[connectors.microsoft_365]` для тестовых сред.
- Disconnect источника Microsoft не отзывает токен у провайдера — это видно в UI и описано в DEPLOYMENT.

## Security considerations

- Как ADR-020: одноразовый `state` с привязкой к сессии, PKCE, точный redirect URI, нет open redirect;
  refresh token зашифрован и write-only, access token не хранится; внешние документы — недоверенный ввод (SC-08);
  удаление в источнике отражается в индексе.
- Широта delegated-разрешений показывается честно (фактические scopes и предупреждение); рекомендация — служебная
  учётная запись.
- Только tenant организации; национальные облака и личные учётные записи не поддерживаются.
- Адреса следующих страниц не берутся из ответа провайдера; предварительно подписанный URL загрузки не логируется и
  не получает `Authorization`.
- Так как отзыв у провайдера невозможен, после disconnect UI предлагает отозвать согласие в Microsoft.
- В логах и audit нет токенов, кодов, `code_verifier`, delta token, содержимого файлов и тел ответов Graph.

## Implementation notes

Шаги 10.0–10.4 roadmap: 10.0 ADR и спецификации; 10.1 Instance Settings, OAuth Entra, обобщение портов и API;
10.2 адаптер Graph (picker, листинг, загрузка); 10.3 синхронизация через Delta Query; 10.4 UI, E2E и security gate.

## Related documents

[ADR-020](0020-google-drive-connector.md) · [ADR-006](0006-initial-setup-and-system-configuration.md) ·
[ADR-007](0007-network-access-and-local-llm.md) · [ADR-008](0008-unified-document-ingestion.md) ·
[ADR-012](0012-single-organization-deployment.md) · [ADR-016](0016-secret-encryption.md) ·
[API_CONTRACTS.md §2.9](../API_CONTRACTS.md) · [ARCHITECTURE.md §8.6, §9.5](../ARCHITECTURE.md) ·
[SECURITY_MODEL.md §15](../SECURITY_MODEL.md) · [DEPLOYMENT.md §7.5](../DEPLOYMENT.md)
