import { baseApi, DATA_SOURCES_TAG, DOCUMENTS_TAG, JOBS_TAG } from '../../shared/api/baseApi';
import type { Page } from '../admin/adminApi';
import type { JobStatus, SyncJob } from '../sources/sourcesApi';

/** DTOs of API_CONTRACTS.md §2.15. Errors arrive redacted: a stable code and a short message. */
export type JobType = 'ingestion' | 'sync';

export interface IngestionJob {
  id: string;
  type: 'ingestion';
  kind: string;
  status: JobStatus;
  stage: string;
  result: string | null;
  attempt: number;
  max_attempts: number;
  error: { code: string; message: string | null } | null;
  document_id: string;
  document_version_id: string | null;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export type AnyJob = IngestionJob | SyncJob;

export interface JobListParams {
  type: JobType;
  status?: JobStatus;
  cursor?: string;
  limit?: number;
}

export const CANCELLED_BY_ADMIN = 'cancelled_by_admin';

export function canCancel(job: AnyJob): boolean {
  return job.status === 'queued' || job.status === 'running';
}

/** Mirrors the backend rule; the backend decides and answers `409 job_not_retryable` otherwise. */
export function canRetry(job: AnyJob): boolean {
  if (job.type === 'sync') return false;
  return (
    job.status === 'failed' ||
    job.status === 'dead' ||
    (job.status === 'cancelled' && job.error?.code === CANCELLED_BY_ADMIN)
  );
}

const jobPath = (job: { type: JobType; id: string }) => `/jobs/${job.type}/${encodeURIComponent(job.id)}`;

// A retry or cancel changes the document's or the source's state too.
const CHANGED = [JOBS_TAG, DOCUMENTS_TAG, DATA_SOURCES_TAG] as const;

export const jobsApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    listJobs: build.query<Page<AnyJob>, JobListParams>({
      query: ({ type, status, cursor, limit }) => ({
        url: '/jobs',
        params: {
          ...(type === 'sync' && { type }),
          ...(status !== undefined && { status }),
          ...(cursor !== undefined && { cursor }),
          ...(limit !== undefined && { limit }),
        },
      }),
      providesTags: [JOBS_TAG],
    }),
    retryJob: build.mutation<IngestionJob, { type: JobType; id: string }>({
      query: (job) => ({ url: `${jobPath(job)}/retry`, method: 'POST' }),
      invalidatesTags: CHANGED,
    }),
    cancelJob: build.mutation<AnyJob, { type: JobType; id: string }>({
      query: (job) => ({ url: `${jobPath(job)}/cancel`, method: 'POST' }),
      invalidatesTags: CHANGED,
    }),
  }),
});

export const { useListJobsQuery, useRetryJobMutation, useCancelJobMutation } = jobsApi;
