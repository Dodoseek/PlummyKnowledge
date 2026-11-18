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
