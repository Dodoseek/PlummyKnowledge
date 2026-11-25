import { baseApi, DOCUMENTS_TAG, KNOWLEDGE_BASES_TAG } from '../../shared/api/baseApi';
import type { Page } from '../admin/adminApi';

/** DTOs of API_CONTRACTS.md §2.10–2.11 and §4. */
export type AccessLevel = 'read' | 'manage';

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string | null;
  embedding_profile_id: string;
  indexing: { documents_total: number; indexed: number; processing: number; failed: number };
  my_access_level: AccessLevel;
  created_at: string;
  updated_at: string;
}

export type DocumentStatus = 'pending' | 'processing' | 'indexed' | 'failed' | 'skipped';
export type VersionStatus = 'pending' | 'processing' | 'ready' | 'failed' | 'outdated' | 'deleted';

export interface DocumentVersion {
  id: string;
  version_number: number;
  checksum_sha256: string;
  size_bytes: number;
  mime_type: string;
  page_count: number | null;
  status: VersionStatus;
  error_code: string | null;
  created_at: string;
  processed_at: string | null;
}

export interface KbDocument {
  id: string;
  /** Display only: shown as text, never used to build a URL or a path. */
  filename: string;
  mime_type: string | null;
  data_source: { id: string; type: string };
  external_id: string;
  current_version: DocumentVersion | null;
  latest_version: DocumentVersion | null;
  processing_status: DocumentStatus;
  created_at: string;
  updated_at: string;
}

export interface JobSummary {
  id: string;
  type: 'ingestion';
  kind: string;
  status: string;
}

export interface UploadResult {
  document: { id: string; filename: string; processing_status: DocumentStatus };
  job: JobSummary;
}

export type SearchMode = 'hybrid' | 'vector' | 'keyword';

export interface SearchHit {
  chunk_id: string;
  document_id: string;
  document_version_id: string;
  /** Display only. */
  filename: string;
  chunk_index: number;
  /** Untrusted document text: rendered as plain text only. */
  content: string;
  token_count: number;
  page_start: number | null;
  page_end: number | null;
  section_path: string[];
  sheet_name: string | null;
  row_start: number | null;
  row_end: number | null;
  score: number;
  vector_rank: number | null;
  keyword_rank: number | null;
}

export interface SearchResult {
  mode: SearchMode;
  items: SearchHit[];
  total_tokens: number;
  keyword_query_empty: boolean;
}

export const UPLOAD_TIMEOUT_MS = 10 * 60_000;

/** Documents still moving through the pipeline: the list polls while there are any. */
export function isInProgress(status: DocumentStatus): boolean {
  return status === 'pending' || status === 'processing';
}

const kbTag = (id: string) => ({ type: KNOWLEDGE_BASES_TAG, id }) as const;
const documentsTag = (kbId: string) => ({ type: DOCUMENTS_TAG, id: kbId }) as const;
const documentPath = (id: string) => `/documents/${encodeURIComponent(id)}`;

export const knowledgeApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    listKnowledgeBases: build.query<Page<KnowledgeBase>, { cursor?: string }>({
      query: ({ cursor }) => ({ url: '/knowledge-bases', params: cursor ? { cursor } : {} }),
      providesTags: [KNOWLEDGE_BASES_TAG],
    }),
    getKnowledgeBase: build.query<KnowledgeBase, string>({
      query: (id) => `/knowledge-bases/${encodeURIComponent(id)}`,
      providesTags: (_result, _error, id) => [kbTag(id)],
    }),
    createKnowledgeBase: build.mutation<
      KnowledgeBase,
      { name: string; description?: string; embedding_profile_id: string }
    >({
      query: (body) => ({ url: '/knowledge-bases', method: 'POST', body }),
      invalidatesTags: [KNOWLEDGE_BASES_TAG],
    }),
    listDocuments: build.query<Page<KbDocument>, { kbId: string; cursor?: string }>({
      query: ({ kbId, cursor }) => ({
        url: '/documents',
        params: { knowledge_base_id: kbId, ...(cursor ? { cursor } : {}) },
      }),
      providesTags: (_result, _error, { kbId }) => [documentsTag(kbId)],
    }),
    uploadDocument: build.mutation<UploadResult, { kbId: string; file: File }>({
      query: ({ kbId, file }) => {
        // The backend checks access with the fields before it reads the file: knowledge_base_id goes first.
        const form = new FormData();
        form.append('knowledge_base_id', kbId);
        form.append('file', file);
        return { url: '/documents', method: 'POST', body: form, timeout: UPLOAD_TIMEOUT_MS };
      },
      invalidatesTags: (_result, _error, { kbId }) => [documentsTag(kbId), kbTag(kbId), KNOWLEDGE_BASES_TAG],
    }),
    deleteDocument: build.mutation<null, { kbId: string; id: string }>({
      query: ({ id }) => ({ url: documentPath(id), method: 'DELETE' }),
      invalidatesTags: (_result, _error, { kbId }) => [documentsTag(kbId), kbTag(kbId), KNOWLEDGE_BASES_TAG],
    }),
    // A mutation: every search is a fresh POST (rate limited per user), nothing is cached between queries.
    searchKnowledgeBase: build.mutation<SearchResult, { kbId: string; query: string; mode: SearchMode }>({
      query: ({ kbId, query, mode }) => ({
        url: `/knowledge-bases/${encodeURIComponent(kbId)}/search`,
        method: 'POST',
        body: { query, mode },
      }),
    }),
    reindexDocument: build.mutation<{ job: JobSummary; created: boolean }, { kbId: string; id: string }>({
      query: ({ id }) => ({ url: `${documentPath(id)}/reindex`, method: 'POST' }),
      invalidatesTags: (_result, _error, { kbId }) => [documentsTag(kbId), kbTag(kbId)],
    }),
  }),
});

export const {
  useListKnowledgeBasesQuery,
  useGetKnowledgeBaseQuery,
  useCreateKnowledgeBaseMutation,
  useListDocumentsQuery,
  useUploadDocumentMutation,
  useDeleteDocumentMutation,
  useReindexDocumentMutation,
  useSearchKnowledgeBaseMutation,
} = knowledgeApi;
