import type { ApiError } from '../../shared/api/errors';
import { messageFor } from '../../shared/api/messages';
import type { SourceType } from './sourcesApi';

export type ExternalType = Exclude<SourceType, 'upload'>;

/** What the UI calls each provider (ADR-020, ADR-021). Display only. */
export interface Provider {
  type: ExternalType;
  /** The service, e.g. in "not reachable". */
  service: string;
  /** Who grants and revokes the access. */
  company: string;
  account: string;
  /** The top level of the folder picker. */
  root: string;
  /** What the warning about the breadth of the access says the account can read. */
  reach: string;
}

export const PROVIDERS: Record<ExternalType, Provider> = {
  google_drive: {
    type: 'google_drive',
    service: 'Google Drive',
    company: 'Google',
    account: 'Google account',
    root: 'My Drive and shared drives',
    reach: 'everything this account can read in Google Drive',
  },
  microsoft_365: {
    type: 'microsoft_365',
    service: 'Microsoft 365',
    company: 'Microsoft',
    account: 'Microsoft account',
    root: 'OneDrive and SharePoint sites',
    reach: 'every file and SharePoint site this account can read in Microsoft 365',
  },
};

export function providerOf(type: SourceType): Provider {
  return type === 'microsoft_365' ? PROVIDERS.microsoft_365 : PROVIDERS.google_drive;
}

type Message = (provider: Provider) => string;

/** Connector and source error codes (API_CONTRACTS.md §2.9); also the codes of a failed sync. */
const SOURCE_ERRORS: Record<string, Message> = {
  connector_not_configured: (p) =>
    `${p.service} is not configured on this server. Ask the server operator to add the OAuth client to the ` +
    'instance configuration.',
  source_not_connected: (p) => `Connect the source to a ${p.account} first.`,
  connector_auth_revoked: (p) => `${p.company} access was revoked or expired. Connect the source again.`,
  connector_access_token_rejected: (p) => `${p.company} rejected the access token. Connect the source again.`,
  source_not_active: () => 'Choose a folder before syncing.',
  sync_in_progress: () => 'A sync of this source is already queued or running.',
  upload_source_managed_by_kb: () => 'The upload source belongs to the knowledge base and cannot be changed.',
  connector_unavailable: (p) => `${p.service} is not reachable right now. Try again later.`,
  connector_rate_limited: (p) => `${p.service} asked to slow down. Try again in a few minutes.`,
  connector_quota_exceeded: (p) =>
    p.type === 'microsoft_365'
      ? 'A Microsoft 365 quota or storage limit of the tenant is reached. Try again later.'
      : 'The Google Drive quota of this project is used up. Try again later.',
  connector_invalid_response: (p) => `${p.service} returned an unexpected answer.`,
  connector_permission_denied: () => 'The connected account cannot read this folder.',
  connector_not_found: () => 'The folder no longer exists or the account lost access to it.',
  connector_cursor_invalid: (p) => `${p.service} refused the change history; a full check runs instead.`,
  connector_error: (p) => `The request to ${p.service} failed.`,
  egress_denied: (p) =>
    `The server’s network policy blocks ${p.company}. Ask the server operator to allow it in the instance ` +
    'configuration.',
  folder_not_found: () => 'The folder no longer exists or the account lost access to it.',
  source_reconfigured: () => 'Cancelled: the folder of the source was changed.',
  attempts_exhausted: () => 'Temporary errors repeated too often.',
};

/** Why connecting an account failed: the `reason` the OAuth callback puts into the redirect. */
const OAUTH_REASONS: Record<string, Message> = {
  oauth_state_invalid: () => 'The sign-in link expired or was already used. Start connecting again.',
  oauth_unavailable: () => 'The server could not finish connecting. Try again later.',
  oauth_session_mismatch: () => 'Connecting was started in another session. Start again from this page.',
  oauth_forbidden: () => 'You no longer have permission to connect this source.',
  oauth_denied: (p) => `Access was not granted in ${p.company}.`,
  oauth_provider_error: (p) => `${p.company} reported an error. Try again.`,
  oauth_code_missing: (p) => `${p.company} did not return an authorization code. Try again.`,
  oauth_not_configured: (p) => `${p.service} is not configured on this server.`,
  oauth_exchange_failed: () => 'The authorization code could not be exchanged. Try again.',
  oauth_no_refresh_token: (p) =>
    p.type === 'microsoft_365'
      ? 'Microsoft did not grant offline access. Connect again and accept all requested permissions.'
      : 'Google did not grant offline access. Remove the app’s access in your Google account and connect again.',
  oauth_scope_missing: (p) =>
    p.type === 'microsoft_365'
      ? 'Read access to files and sites (Files.Read.All, Sites.Read.All) was not granted. Ask an Entra ' +
        'administrator for consent if needed, then connect again.'
      : 'Read access to Google Drive was not granted. Connect again and allow it.',
};

export function sourceErrorMessage(code: string, provider: Provider = PROVIDERS.google_drive): string {
  return SOURCE_ERRORS[code]?.(provider) ?? `The sync failed (${code}).`;
}

export function sourceApiMessage(error: ApiError, provider: Provider = PROVIDERS.google_drive): string {
  // A field error (a bad folder) carries the precise reason; then codes of this feature, then the shared map.
  const field = error.fieldErrors?.[0]?.message;
  if (field !== undefined) return field;
  const message = error.code !== undefined ? SOURCE_ERRORS[error.code] : undefined;
  return message !== undefined ? message(provider) : messageFor(error);
}

/** For a redirect that names no known source (an invalid `state` names none). */
const ANY_PROVIDER: Provider = {
  type: 'google_drive',
  service: 'the provider',
  company: 'the provider',
  account: 'account',
  root: '',
  reach: '',
};

export function oauthFailureMessage(reason: string | null, provider: Provider | undefined): string {
  const message = reason !== null ? OAUTH_REASONS[reason] : undefined;
  if (message === undefined) return `Connecting the ${provider?.account ?? 'account'} failed.`;
  if (provider === undefined && (reason === 'oauth_no_refresh_token' || reason === 'oauth_scope_missing')) {
    return 'The provider did not grant the access the source needs. Connect again and allow it.';
  }
  const text = message(provider ?? ANY_PROVIDER);
  return text.charAt(0).toUpperCase() + text.slice(1);
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
