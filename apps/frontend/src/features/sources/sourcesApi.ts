import { baseApi, DATA_SOURCES_TAG, DOCUMENTS_TAG, KNOWLEDGE_BASES_TAG } from '../../shared/api/baseApi';
import type { Page } from '../admin/adminApi';

/**
 * DTOs of API_CONTRACTS.md §2.9 and §2.15 (ADR-020, ADR-021). Tokens and secret references are never part of
 * them.
 */
export type SourceType = 'upload' | 'google_drive' | 'microsoft_365';
export type SourceStatus = 'active' | 'paused' | 'error' | 'disconnected';
export type CredentialStatus = 'valid' | 'expired' | 'revoked';
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'dead';

export interface DataSource {
  id: string;
  type: SourceType;
  /** Display only. */
  name: string;
  status: SourceStatus;
  knowledge_base_id: string;
  owner_knowledge_base_id: string | null;
  folder: { id: string; name: string | null } | null;
  sync: {
    interval_minutes: number | null;
    next_sync_at: string | null;
    last_sync_at: string | null;
    last_reconciled_at: string | null;
    last_status: 'succeeded' | 'failed' | 'cancelled' | null;
    last_error: string | null;
  } | null;
  credentials: {
    status: CredentialStatus;
    /** Display only: the label the provider gave for the account. */
    account_label: string | null;
    granted_scopes: string[];
    connected_at: string;
    last_refreshed_at: string | null;
  } | null;
  created_at: string;
  updated_at: string;
}

export interface BrowseEntry {
  id: string;
  /** Display only. */
  name: string;
  /** `site` is opened to list its libraries and never chosen; a `drive` (a library) is chosen as its root. */
  kind: 'folder' | 'shared_drive' | 'site' | 'drive';
}

export interface SyncJob {
  id: string;
  type: 'sync';
  data_source_id: string;
  trigger: 'manual' | 'schedule' | 'reconciliation' | 'initial';
  mode: 'initial' | 'incremental' | 'reconciliation';
  status: JobStatus;
  stats: Partial<Record<'added' | 'updated' | 'renamed' | 'deleted' | 'skipped' | 'failed', number>>;
  attempt: number;
  max_attempts: number;
  error: { code: string } | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface Validation {
  status: 'ok' | 'failed';
  checked_at: string;
  error_code: string | null;
  account_label: string | null;
  folder_accessible: boolean | null;
}

export const SYNC_POLL_INTERVAL_MS = 3000;

export function isActiveJob(status: JobStatus): boolean {
  return status === 'queued' || status === 'running';
}

const sourcePath = (id: string) => `/data-sources/${encodeURIComponent(id)}`;
const listTag = (kbId: string) => ({ type: DATA_SOURCES_TAG, id: kbId }) as const;
/** A sync changes the documents and the counters of the knowledge base. */
const syncedTags = (kbId: string) =>
  [listTag(kbId), { type: DOCUMENTS_TAG, id: kbId }, { type: KNOWLEDGE_BASES_TAG, id: kbId }] as const;

interface SourceRef {
  kbId: string;
  id: string;
}

export const sourcesApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    listSources: build.query<Page<DataSource>, { kbId: string; cursor?: string }>({
      query: ({ kbId, cursor }) => ({
        url: '/data-sources',
        params: { knowledge_base_id: kbId, ...(cursor ? { cursor } : {}) },
      }),
      providesTags: (_result, _error, { kbId }) => [listTag(kbId)],
    }),
    createSource: build.mutation<
      DataSource,
      { kbId: string; name: string; type: Exclude<SourceType, 'upload'> }
    >({
      query: ({ kbId, name, type }) => ({
        url: '/data-sources',
        method: 'POST',
        body: { type, name, knowledge_base_id: kbId },
      }),
      invalidatesTags: (_result, _error, { kbId }) => [listTag(kbId)],
    }),
    updateSource: build.mutation<
      DataSource,
      SourceRef & { name?: string; sync_interval_minutes?: number | null; folder_id?: string | null }
    >({
      query: ({ id, kbId: _kbId, ...body }) => ({ url: sourcePath(id), method: 'PATCH', body }),
      // Choosing a folder queues the initial sync.
      invalidatesTags: (_result, _error, { kbId }) => [listTag(kbId)],
    }),
    deleteSource: build.mutation<{ documents_deleted: number }, SourceRef>({
      query: ({ id }) => ({ url: sourcePath(id), method: 'DELETE' }),
      invalidatesTags: (_result, _error, { kbId }) => [...syncedTags(kbId), KNOWLEDGE_BASES_TAG],
    }),
    startOAuth: build.mutation<{ authorization_url: string }, string>({
      query: (id) => ({ url: `${sourcePath(id)}/oauth/start`, method: 'POST' }),
    }),
    disconnectSource: build.mutation<DataSource, SourceRef>({
      query: ({ id }) => ({ url: `${sourcePath(id)}/disconnect`, method: 'POST' }),
      invalidatesTags: (_result, _error, { kbId }) => [listTag(kbId)],
    }),
    validateSource: build.mutation<Validation, string>({
      query: (id) => ({ url: `${sourcePath(id)}/validate`, method: 'POST' }),
    }),
    // Each listing is fetched again when the picker opens it, and dropped as soon as nothing shows it.
    browseSource: build.query<Page<BrowseEntry>, { id: string; parentId?: string; cursor?: string }>({
      query: ({ id, parentId, cursor }) => ({
        url: `${sourcePath(id)}/browse`,
        params: { ...(parentId ? { parent_id: parentId } : {}), ...(cursor ? { cursor } : {}) },
      }),
      keepUnusedDataFor: 0,
    }),
    syncSource: build.mutation<SyncJob, SourceRef & { mode: 'incremental' | 'reconciliation' }>({
      query: ({ id, mode }) => ({ url: `${sourcePath(id)}/sync`, method: 'POST', body: { mode } }),
    }),
    listSyncJobs: build.query<Page<SyncJob>, string>({
      query: (sourceId) => ({ url: '/jobs', params: { type: 'sync', data_source_id: sourceId, limit: 1 } }),
    }),
  }),
});

export const {
  useListSourcesQuery,
  useCreateSourceMutation,
  useUpdateSourceMutation,
  useDeleteSourceMutation,
  useStartOAuthMutation,
  useDisconnectSourceMutation,
  useValidateSourceMutation,
  useBrowseSourceQuery,
  useSyncSourceMutation,
  useListSyncJobsQuery,
} = sourcesApi;

export { syncedTags };
