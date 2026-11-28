# Connectors

A knowledge base can take documents from Google Drive or Microsoft 365 (SharePoint and OneDrive) besides uploads.
An administrator connects an account, chooses folders, and the worker keeps the knowledge base in sync: new and
changed files are indexed, deleted ones removed. Access to the documents is granted on the knowledge base, as for
uploads; permissions of the source system are **not** copied.

Both connectors need an OAuth application registered by you. The redirect URI is always:

```text
<PUBLIC_URL>/api/v1/data-sources/oauth/callback
```

## Client secrets

Client IDs go into `deploy/prod/.env`; client secrets are files passed to `backend` and `worker` with an override,
for example `compose.connectors.yaml`:

```yaml
services:
  backend:
    environment:
      GOOGLE_OAUTH_CLIENT_SECRET_FILE: /run/secrets/google_oauth_client_secret
    secrets: [google_oauth_client_secret]
  worker:
    environment:
      GOOGLE_OAUTH_CLIENT_SECRET_FILE: /run/secrets/google_oauth_client_secret
    secrets: [google_oauth_client_secret]

secrets:
  google_oauth_client_secret:
    file: ./deploy/prod/secrets/google_oauth_client_secret
```

For Microsoft 365 use `MICROSOFT_OAUTH_CLIENT_SECRET_FILE` and a `microsoft_oauth_client_secret` file the same
way. The services run as uid `10001`: make the file readable by it (`chown 10001` and `chmod 0400` as root). Start
with `-f compose.prod.yaml -f compose.connectors.yaml`. `GET /api/v1/system/settings` then shows the connector
as configured, with the redirect URI it expects.

## Google Drive

1. In Google Cloud Console create a project, enable the **Google Drive API** and configure the OAuth consent
   screen:
   - **Internal** (Google Workspace): no verification by Google is needed;
   - **External**: the `drive.readonly` scope is restricted. Until the app is verified, only test users can
     connect, and their refresh tokens expire after 7 days.
2. Create an OAuth client of type **Web application** with the redirect URI above.
3. Set `GOOGLE_OAUTH_CLIENT_ID` in `.env`, save the client secret as described above and restart.

## Microsoft 365

1. In the Entra admin center register an application (**single tenant**), platform **Web**, with the redirect URI
   above, and create a client secret.
2. Add Microsoft Graph **delegated** permissions `Files.Read.All`, `Sites.Read.All`, `User.Read` and
   `offline_access`, then **Grant admin consent**.
3. Set `MICROSOFT_OAUTH_TENANT_ID` (the tenant GUID or domain; `common`, `organizations` and `consumers` are
   refused) and `MICROSOFT_OAUTH_CLIENT_ID` in `.env`, save the client secret and restart.

Entra client secrets expire: renew yours before that date, or token refresh stops working.

## Connecting a source

On a knowledge base page, in **External sources**, choose **Connect Google account** or **Connect Microsoft
account** and sign in, then pick folders (or SharePoint sites and libraries). The first sync starts; **Sync now**
runs another one. Progress is visible on the page and on the **Jobs** page of the user menu (type `sync`); the
scheduler syncs every source regularly and reconciles it once a day.

**Use a dedicated service account.** The token can read everything the signed-in account can read, whatever
folders you pick in the app. Share only the needed folders or libraries with that account.

## Disconnecting

**Disconnect** on the source removes the stored token, and for Google also revokes it at Google. Microsoft does
not allow revoking a single token: remove the consent at <https://myapps.microsoft.com> or for the enterprise
application in Entra. If a token is revoked on the provider's side, the source goes to `error`
(`connector_auth_revoked`); connect it again.

No allowlist entries are needed: Google and Microsoft are public services, and their hosts are fixed in the code.
