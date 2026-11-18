import type { ApiError } from './errors';

/**
 * User-facing messages by backend error `code` (API_CONTRACTS.md §1.5). The UI branches on `code`, never on
 * `detail`; unknown codes fall back to the backend's safe `detail`/`title`.
 */
const MESSAGES: Record<string, string> = {
  invalid_credentials: 'Invalid email or password.',
  rate_limited: 'Too many attempts. Wait a minute and try again.',
  csrf_failed: 'The request was blocked by the security check. Reload the page and try again.',
  service_unavailable: 'The service is temporarily unavailable. Try again later.',
  setup_token_invalid:
    'The setup token is wrong, expired or already used. Ask the server operator for a new one ' +
    '(python -m plummy.cli setup init-token --rotate).',
  setup_already_completed: 'Setup has already been completed. Sign in instead.',
  unauthenticated: 'Your session has ended. Sign in again.',
  session_revoked: 'Your session was ended. Sign in again.',
  permission_denied: 'You do not have permission to do this.',
  not_found: 'It no longer exists or was never there.',
  email_taken: 'A user with this email already exists.',
  group_name_taken: 'A group with this name already exists.',
  last_administrator: 'The instance must keep at least one active administrator.',
  cannot_modify_self: 'You cannot disable or delete your own account.',
  invalid_user_state: 'This action is not possible in the user’s current state.',
  provider_name_taken: 'A provider with this name already exists.',
  provider_in_use: 'The provider is used by an embedding profile and cannot be deleted.',
  model_id_taken: 'This provider already has a model with this ID.',
  embedding_profile_name_taken: 'An embedding profile with this name already exists.',
  reindex_required:
    'The model and vector settings of a profile cannot change. Create a new profile and reindex instead.',
  invalid_state: 'This change is not possible in the current state.',
  endpoint_not_allowed:
    'The server’s network policy does not allow this address. Ask the server operator to add it to the ' +
    'allowlist in the instance configuration.',
  secrets_not_configured:
    'Secret storage is not configured on the server (SECRETS_KEYRING_FILE). Keys cannot be saved yet.',
  secret_unavailable: 'The stored key cannot be decrypted on this server. Enter the key again.',
  activation_token_invalid:
    'This activation link is invalid, expired or already used. Ask your administrator for a new one.',
};

export function messageFor(error: ApiError): string {
  if (error.code !== undefined && error.code in MESSAGES) {
    return MESSAGES[error.code] ?? error.message;
  }
  return error.message;
}

/** Field errors keyed by the last path segment (`body.admin.password` → `password`). */
export function fieldErrorsByName(error: ApiError | undefined): Record<string, string> {
  const result: Record<string, string> = {};
  for (const item of error?.fieldErrors ?? []) {
    const name = item.field.split('.').at(-1);
    if (name !== undefined && !(name in result)) {
      result[name] = item.message;
    }
  }
  return result;
}
