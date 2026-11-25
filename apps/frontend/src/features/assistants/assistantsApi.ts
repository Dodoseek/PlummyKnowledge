import type { FetchBaseQueryError } from '@reduxjs/toolkit/query';

import {
  ASSISTANTS_TAG,
  baseApi,
  KNOWLEDGE_BASES_TAG,
  MODELS_TAG,
  PROVIDERS_TAG,
} from '../../shared/api/baseApi';
import type { Page } from '../admin/adminApi';
import type { KnowledgeBase } from '../knowledge/knowledgeApi';
import type { ModelConfig, Provider } from '../providers/providersApi';

/** DTOs of API_CONTRACTS.md §2.12 and §4. */
export type AssistantStatus = 'active' | 'disabled';
export type Unavailability = 'disabled' | 'no_knowledge_bases' | 'missing_kb_access';

export interface AssistantRetrieval {
  top_k: number;
  vector_top_k: number;
  keyword_top_k: number;
  rrf_k: number;
  vector_weight: number;
  keyword_weight: number;
}

export interface AssistantLimits {
  max_context_tokens: number;
  max_history_messages: number;
  max_output_tokens: number;
}

export interface Assistant {
  id: string;
  name: string;
  description: string | null;
  /** Only returned to assistants.manage. */
  instructions: string | null;
  model_config_id: string;
  embedding_profile_id: string;
  knowledge_base_ids: string[];
  retrieval: AssistantRetrieval;
  limits: AssistantLimits;
  status: AssistantStatus;
  /** For the current user, evaluated by the backend on every request (ADR-005). */
  availability: { available: boolean; reason: Unavailability | null };
  created_at: string;
  updated_at: string;
}

export interface AssistantBody {
  name?: string;
  description?: string | null;
  instructions?: string;
  model_config_id?: string;
  embedding_profile_id?: string;
  knowledge_base_ids?: string[];
  retrieval?: Partial<AssistantRetrieval>;
  limits?: Partial<AssistantLimits>;
  status?: AssistantStatus;
}

export interface ChatModelOption {
  model: ModelConfig;
  provider: Pick<Provider, 'id' | 'name' | 'enabled'>;
}

/** Enough for one instance's configuration; the lists are read page by page up to this many pages. */
const MAX_PAGES = 10;
const PAGE_LIMIT = '100';

type Fetch = (args: { url: string; params?: Record<string, string> }) => Promise<{
  data?: unknown;
  error?: FetchBaseQueryError;
}>;

/** Every item of a cursor-paginated list; the caller knows the item type. */
async function readAll(
  fetch: Fetch,
  url: string,
): Promise<{ data: unknown[] } | { error: FetchBaseQueryError }> {
  const items: unknown[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < MAX_PAGES; page += 1) {
    const params: Record<string, string> = { limit: PAGE_LIMIT, ...(cursor !== null ? { cursor } : {}) };
    const result = await fetch({ url, params });
    if (result.error !== undefined) return { error: result.error };
    const body = result.data as Page<unknown>;
    items.push(...body.items);
    if (!body.has_more || body.next_cursor === null) break;
    cursor = body.next_cursor;
  }
  return { data: items };
}

const assistantTag = (id: string) => ({ type: ASSISTANTS_TAG, id }) as const;
const assistantPath = (id: string) => `/assistants/${encodeURIComponent(id)}`;

export const assistantsApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    listAssistants: build.query<Page<Assistant>, { cursor?: string }>({
      query: ({ cursor }) => ({ url: '/assistants', params: cursor ? { cursor } : {} }),
      providesTags: [ASSISTANTS_TAG],
    }),
    getAssistant: build.query<Assistant, string>({
      query: (id) => assistantPath(id),
      providesTags: (_result, _error, id) => [assistantTag(id)],
    }),
    createAssistant: build.mutation<Assistant, AssistantBody>({
      query: (body) => ({ url: '/assistants', method: 'POST', body }),
      invalidatesTags: [ASSISTANTS_TAG],
    }),
    updateAssistant: build.mutation<Assistant, { id: string; body: AssistantBody }>({
      query: ({ id, body }) => ({ url: assistantPath(id), method: 'PATCH', body }),
      invalidatesTags: (_result, _error, { id }) => [ASSISTANTS_TAG, assistantTag(id)],
    }),
    deleteAssistant: build.mutation<null, string>({
      query: (id) => ({ url: assistantPath(id), method: 'DELETE' }),
      invalidatesTags: [ASSISTANTS_TAG],
    }),
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    listChatModels: build.query<ChatModelOption[], void>({
      async queryFn(_arg, _api, _extra, fetchWithBQ) {
        const fetch = fetchWithBQ as Fetch;
        const providers = await readAll(fetch, '/providers');
        if ('error' in providers) return { error: providers.error };
        const options: ChatModelOption[] = [];
        for (const provider of providers.data as Provider[]) {
          const models = await fetch({ url: `/providers/${encodeURIComponent(provider.id)}/models` });
          if (models.error !== undefined) return { error: models.error };
          for (const model of (models.data as { items: ModelConfig[] }).items) {
            if (model.kind === 'chat') {
              options.push({
                model,
                provider: { id: provider.id, name: provider.name, enabled: provider.enabled },
              });
            }
          }
        }
        return { data: options };
      },
      providesTags: [PROVIDERS_TAG, MODELS_TAG],
    }),
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    listAllKnowledgeBases: build.query<KnowledgeBase[], void>({
      async queryFn(_arg, _api, _extra, fetchWithBQ) {
        const result = await readAll(fetchWithBQ as Fetch, '/knowledge-bases');
        return 'error' in result ? result : { data: result.data as KnowledgeBase[] };
      },
      providesTags: [KNOWLEDGE_BASES_TAG],
    }),
  }),
});

export const {
  useListAssistantsQuery,
  useGetAssistantQuery,
  useCreateAssistantMutation,
  useUpdateAssistantMutation,
  useDeleteAssistantMutation,
  useListChatModelsQuery,
  useListAllKnowledgeBasesQuery,
} = assistantsApi;

export function modelLabel(option: ChatModelOption): string {
  const name = option.model.display_name ?? option.model.model_id;
  const off = !option.model.enabled || !option.provider.enabled ? ' (disabled)' : '';
  return `${option.provider.name} / ${name}${off}`;
}

export function unavailabilityLabel(reason: Unavailability): string {
  switch (reason) {
    case 'disabled':
      return 'Disabled';
    case 'no_knowledge_bases':
      return 'No knowledge bases';
    case 'missing_kb_access':
      return 'Not available to you: no access to all its knowledge bases';
  }
}
