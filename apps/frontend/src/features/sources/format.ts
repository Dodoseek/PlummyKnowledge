import type { ApiError } from '../../shared/api/errors';
import { messageFor } from '../../shared/api/messages';

/** Connector and source error codes (API_CONTRACTS.md §2.9); also the codes of a failed sync. */
const SOURCE_ERRORS: Record<string, string> = {
  connector_not_configured:
    'Google Drive is not configured on this server. Ask the server operator to add the OAuth client to the ' +
    'instance configuration.',
  source_not_connected: 'Connect the source to a Google account first.',
  connector_auth_revoked: 'Google access was revoked or expired. Connect the source again.',
  connector_access_token_rejected: 'Google rejected the access token. Connect the source again.',
  source_not_active: 'Choose a folder before syncing.',
  sync_in_progress: 'A sync of this source is already queued or running.',
  upload_source_managed_by_kb: 'The upload source belongs to the knowledge base and cannot be changed.',
  connector_unavailable: 'Google Drive is not reachable right now. Try again later.',
  connector_rate_limited: 'Google Drive asked to slow down. Try again in a few minutes.',
  connector_quota_exceeded: 'The Google Drive quota of this project is used up. Try again later.',
  connector_invalid_response: 'Google Drive returned an unexpected answer.',
  connector_permission_denied: 'The connected account cannot read this folder.',
  connector_not_found: 'The folder no longer exists or the account lost access to it.',
  connector_cursor_invalid: 'Google Drive refused the change history; a full check runs instead.',
  connector_error: 'The request to Google Drive failed.',
  egress_denied:
    'The server’s network policy blocks Google. Ask the server operator to allow it in the instance ' +
    'configuration.',
  folder_not_found: 'The folder no longer exists or the account lost access to it.',
  source_reconfigured: 'Cancelled: the folder of the source was changed.',
  attempts_exhausted: 'Temporary errors repeated too often.',
};

/** Why connecting a Google account failed: the `reason` the OAuth callback puts into the redirect. */
const OAUTH_REASONS: Record<string, string> = {
  oauth_state_invalid: 'The sign-in link expired or was already used. Start connecting again.',
  oauth_unavailable: 'The server could not finish connecting. Try again later.',
  oauth_session_mismatch: 'Connecting was started in another session. Start again from this page.',
  oauth_forbidden: 'You no longer have permission to connect this source.',
  oauth_denied: 'Access was not granted in Google.',
  oauth_provider_error: 'Google reported an error. Try again.',
  oauth_code_missing: 'Google did not return an authorization code. Try again.',
  oauth_not_configured: 'Google Drive is not configured on this server.',
  oauth_exchange_failed: 'The authorization code could not be exchanged. Try again.',
  oauth_no_refresh_token:
    'Google did not grant offline access. Remove the app’s access in your Google account and connect again.',
  oauth_scope_missing: 'Read access to Google Drive was not granted. Connect again and allow it.',
};

export function sourceErrorMessage(code: string): string {
  return SOURCE_ERRORS[code] ?? `The sync failed (${code}).`;
}

export function sourceApiMessage(error: ApiError): string {
  // A field error (a bad folder) carries the precise reason; then codes of this feature, then the shared map.
  const field = error.fieldErrors?.[0]?.message;
  if (field !== undefined) return field;
  if (error.code !== undefined && error.code in SOURCE_ERRORS)
    return SOURCE_ERRORS[error.code] ?? error.message;
  return messageFor(error);
}

export function oauthFailureMessage(reason: string | null): string {
  return (reason !== null ? OAUTH_REASONS[reason] : undefined) ?? 'Connecting the Google account failed.';
}

export const INTERVALS: { value: string; label: string }[] = [
  { value: '', label: 'Manual only' },
  { value: '5', label: 'Every 5 minutes' },
  { value: '15', label: 'Every 15 minutes' },
  { value: '60', label: 'Every hour' },
  { value: '360', label: 'Every 6 hours' },
  { value: '1440', label: 'Every day' },
];

export function intervalLabel(minutes: number | null): string {
  if (minutes === null) return 'Manual only';
  return (
    INTERVALS.find((item) => item.value === String(minutes))?.label ?? `Every ${String(minutes)} minutes`
  );
}

/** Only a web address may leave the app: the authorization URL comes from the backend, the check is extra. */
export function isWebUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch {
    return false;
  }
}

export function formatTime(value: string | null): string {
  return value === null ? '—' : new Date(value).toLocaleString();
}

/** Leaving the app for the provider's consent page; an object so tests can replace it. */
export const browser = {
  assign(url: string) {
    window.location.assign(url);
  },
};
