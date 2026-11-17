import type { SerializedError } from '@reduxjs/toolkit';
import type { FetchBaseQueryError } from '@reduxjs/toolkit/query';

/**
 * Normalized API error for the UI. Built from RTK Query errors and the backend error format
 * (RFC 9457 problem+json, API_CONTRACTS.md §1.5 — [Proposed]).
 */
export interface ApiError {
  kind: 'http' | 'network' | 'timeout' | 'parse' | 'unknown';
  /** HTTP status for `kind: 'http'`. */
  status?: number;
  /** Machine-readable backend code, e.g. `not_found`, `validation_error`. */
  code?: string;
  /** Human-readable message that is safe to show. */
  message: string;
  /** Backend correlation ID (`X-Request-ID`) to quote in bug reports. */
  correlationId?: string;
}

interface ProblemDetails {
  title?: unknown;
  detail?: unknown;
  code?: unknown;
  correlation_id?: unknown;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const asString = (value: unknown): string | undefined =>
  typeof value === 'string' && value.trim() !== '' ? value : undefined;

function isFetchBaseQueryError(error: unknown): error is FetchBaseQueryError {
  return isRecord(error) && 'status' in error;
}

function fromHttpStatus(status: number, data: unknown): ApiError {
  const problem: ProblemDetails = isRecord(data) ? data : {};
  const code = asString(problem.code);
  const correlationId = asString(problem.correlation_id);
  const message =
    asString(problem.detail) ??
    asString(problem.title) ??
    (status >= 500 ? 'The server failed to process the request.' : `Request failed with status ${status}.`);
  return {
    kind: 'http',
    status,
    message,
    ...(code !== undefined && { code }),
    ...(correlationId !== undefined && { correlationId }),
  };
}

export function toApiError(error: FetchBaseQueryError | SerializedError | undefined): ApiError {
  if (error === undefined) {
    return { kind: 'unknown', message: 'Unknown error.' };
  }
  if (!isFetchBaseQueryError(error)) {
    return { kind: 'unknown', message: error.message ?? 'Unexpected application error.' };
  }
  switch (error.status) {
    case 'FETCH_ERROR':
      return { kind: 'network', message: 'Cannot reach the server. Check your connection and try again.' };
    case 'TIMEOUT_ERROR':
      return { kind: 'timeout', message: 'The server did not respond in time.' };
    case 'PARSING_ERROR':
      return fromHttpStatus(error.originalStatus, undefined);
    case 'CUSTOM_ERROR':
      return { kind: 'unknown', message: error.error };
    default:
      return fromHttpStatus(error.status, error.data);
  }
}
