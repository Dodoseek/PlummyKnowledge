import { baseApi } from '../../shared/api/baseApi';
import type { Page } from '../admin/adminApi';

/** DTO of API_CONTRACTS.md §2.16. Events never hold secrets, tokens, passwords or document content. */
export type AuditOutcome = 'success' | 'denied' | 'failure';

export interface AuditEvent {
  id: string;
  created_at: string;
  actor_type: string;
  actor_user_id: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  outcome: AuditOutcome;
  correlation_id: string | null;
  ip_address: string | null;
  user_agent: string | null;
  details: Record<string, unknown>;
}

export interface AuditListParams {
  action?: string;
  actor_user_id?: string;
  outcome?: AuditOutcome;
  /** ISO 8601 with a time zone; `from` is inclusive, `to` exclusive. */
  from?: string;
  to?: string;
  cursor?: string;
  limit?: number;
}

/** A `datetime-local` value is local time without a zone; the API wants an instant. */
export function toInstant(local: string): string | undefined {
  if (local === '') return undefined;
  const time = new Date(local);
  return Number.isNaN(time.getTime()) ? undefined : time.toISOString();
}

export const auditApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    // Read only: the log has no routes to change or delete events.
    listAuditEvents: build.query<Page<AuditEvent>, AuditListParams>({
      query: (params) => ({
        url: '/audit/events',
        params: Object.fromEntries(Object.entries(params).filter(([, value]) => value !== undefined)),
      }),
      keepUnusedDataFor: 0,
    }),
  }),
});

export const { useListAuditEventsQuery } = auditApi;
