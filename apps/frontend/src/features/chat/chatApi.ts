import { baseApi, CONVERSATIONS_TAG, MESSAGES_TAG } from '../../shared/api/baseApi';
import type { Page } from '../admin/adminApi';

/** DTOs of API_CONTRACTS.md §2.13 and §3.3. */
export interface ConversationAssistant {
  id: string;
  name: string | null;
  /** Whether the current user can send a new message now (ADR-005), evaluated by the backend. */
  available: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  assistant: ConversationAssistant;
  created_at: string;
  updated_at: string;
  last_message_at: string;
}

export interface Citation {
  citation_id: string;
  document_id: string;
  document_version_id: string;
  chunk_id: string;
  filename: string;
  page: number | null;
  section: string | null;
  sheet: string | null;
  row: number | null;
  snippet: string;
  viewer_route: string;
}

export type MessageStatus = 'completed' | 'streaming' | 'stopped' | 'failed';

export interface ChatMessage {
  id: string;
  sequence: number;
  role: 'user' | 'assistant';
  content: string;
  status: MessageStatus;
  error_code: string | null;
  citations: Citation[];
  created_at: string;
  completed_at: string | null;
}

export interface SourceView {
  document: { id: string; filename: string; mime_type: string | null };
  version: { id: string; version_number: number; is_current: boolean };
  chunk: {
    id: string;
    content: string;
    page_start: number | null;
    page_end: number | null;
    section_path: string[];
    sheet_name: string | null;
    row_start: number | null;
    row_end: number | null;
  };
  context: { before: string[]; after: string[] };
}

export interface SourceAddress {
  documentId: string;
  versionId: string;
  chunkId: string;
}

/** The newest messages of a conversation; the chat shows them oldest first. */
export const HISTORY_LIMIT = 100;

export const chatApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    listConversations: build.query<Page<Conversation>, { cursor?: string }>({
      query: ({ cursor }) => ({
        url: '/conversations',
        params: { limit: '50', ...(cursor !== undefined && { cursor }) },
      }),
      providesTags: [{ type: CONVERSATIONS_TAG, id: 'LIST' }],
    }),
    getConversation: build.query<Conversation, string>({
      query: (id) => `/conversations/${encodeURIComponent(id)}`,
      providesTags: (_result, _error, id) => [{ type: CONVERSATIONS_TAG, id }],
    }),
    listMessages: build.query<Page<ChatMessage>, string>({
      query: (id) => ({
        url: `/conversations/${encodeURIComponent(id)}/messages`,
        params: { limit: String(HISTORY_LIMIT) },
      }),
      providesTags: (_result, _error, id) => [{ type: MESSAGES_TAG, id }],
    }),
    renameConversation: build.mutation<Conversation, { id: string; title: string }>({
      query: ({ id, title }) => ({
        url: `/conversations/${encodeURIComponent(id)}`,
        method: 'PATCH',
        body: { title },
      }),
      invalidatesTags: (_result, _error, { id }) => [
        { type: CONVERSATIONS_TAG, id },
        { type: CONVERSATIONS_TAG, id: 'LIST' },
      ],
    }),
    deleteConversation: build.mutation<undefined, string>({
      query: (id) => ({ url: `/conversations/${encodeURIComponent(id)}`, method: 'DELETE' }),
      invalidatesTags: (_result, _error, id) => [
        { type: CONVERSATIONS_TAG, id },
        { type: CONVERSATIONS_TAG, id: 'LIST' },
      ],
    }),
    stopGeneration: build.mutation<undefined, string>({
      query: (runId) => ({ url: `/chat/runs/${encodeURIComponent(runId)}/stop`, method: 'POST' }),
    }),
    viewSource: build.query<SourceView, SourceAddress>({
      query: ({ documentId, versionId, chunkId }) =>
        `/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}` +
        `/chunks/${encodeURIComponent(chunkId)}/view`,
      // Access is checked on every request (it may have been revoked): never serve a cached passage.
      keepUnusedDataFor: 0,
    }),
  }),
});

export const {
  useListConversationsQuery,
  useGetConversationQuery,
  useListMessagesQuery,
  useRenameConversationMutation,
  useDeleteConversationMutation,
  useStopGenerationMutation,
  useViewSourceQuery,
} = chatApi;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The in-app route of a citation, built from its IDs (never a URL taken from the answer text). */
export function viewerPath(citation: Pick<Citation, 'document_id' | 'document_version_id' | 'chunk_id'>) {
  const ids = [citation.document_id, citation.document_version_id, citation.chunk_id];
  if (!ids.every((id) => UUID.test(id))) return null;
  return `/viewer/documents/${ids[0]}/versions/${ids[1]}?chunk=${ids[2]}`;
}

/** Where a citation points inside its document, for display. */
export function citationPlace(citation: Citation): string | null {
  const parts = [
    citation.section,
    citation.page !== null ? `p. ${citation.page}` : null,
    citation.sheet !== null ? `sheet ${citation.sheet}` : null,
    citation.row !== null ? `row ${citation.row}` : null,
  ].filter((part): part is string => part !== null && part !== '');
  return parts.length > 0 ? parts.join(' · ') : null;
}
