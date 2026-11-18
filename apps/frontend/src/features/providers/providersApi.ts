import { baseApi, EMBEDDING_PROFILES_TAG, MODELS_TAG, PROVIDERS_TAG } from '../../shared/api/baseApi';
import type { Page } from '../admin/adminApi';

/** DTOs of API_CONTRACTS.md §2.7–2.8. API keys are write-only: no response ever contains one. */
export type ProviderType = 'openai' | 'anthropic' | 'ollama' | 'openai_compatible';

export const PROVIDER_TYPES: {
  value: ProviderType;
  label: string;
  keyRequired: boolean;
  defaultUrl?: string;
}[] = [
  { value: 'openai', label: 'OpenAI', keyRequired: true, defaultUrl: 'https://api.openai.com/v1' },
  { value: 'anthropic', label: 'Anthropic', keyRequired: true, defaultUrl: 'https://api.anthropic.com' },
  { value: 'ollama', label: 'Ollama', keyRequired: false },
  { value: 'openai_compatible', label: 'OpenAI-compatible', keyRequired: false },
];

export const EMBEDDING_PROVIDER_TYPES: ProviderType[] = ['openai', 'ollama', 'openai_compatible'];

export function providerTypeLabel(type: ProviderType): string {
  return PROVIDER_TYPES.find((item) => item.value === type)?.label ?? type;
}

export interface Provider {
  id: string;
  name: string;
  provider_type: ProviderType;
  base_url: string;
  has_secret: boolean;
  secret_hint: string | null;
  enabled: boolean;
  settings: { timeout_seconds: number };
  last_test: { status: 'ok' | 'failed'; tested_at: string; error_code: string | null } | null;
  created_at: string;
  updated_at: string;
}

export interface CreateProviderBody {
  name: string;
  provider_type: ProviderType;
  base_url?: string;
  api_key?: string;
  settings?: { timeout_seconds?: number };
}

/** `api_key`: omitted → unchanged, string → replaced, `null` → removed. */
export interface UpdateProviderBody {
  id: string;
  name?: string;
  base_url?: string;
  api_key?: string | null;
  enabled?: boolean;
  settings?: { timeout_seconds?: number };
}

export interface TestError {
  code: string;
  message: string;
}

export interface ConnectionTestResult {
  status: 'ok' | 'failed';
  latency_ms: number;
  tested_at: string;
  models: string[];
  error: TestError | null;
}

export interface ModelConfig {
  id: string;
  provider_id: string;
  model_id: string;
  display_name: string | null;
  kind: string;
  context_window: number | null;
  max_output_tokens: number | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export type DistanceMetric = 'cosine' | 'inner_product' | 'l2';
export type ProfileStatus = 'active' | 'deprecated' | 'retired';

export interface EmbeddingProfile {
  id: string;
  name: string;
  provider_id: string;
  model_id: string;
  dimensions: number;
  distance_metric: DistanceMetric;
  normalize: boolean;
  max_input_tokens: number | null;
  status: ProfileStatus;
  created_at: string;
  updated_at: string;
}

export interface ProfileTestResult {
  status: 'ok' | 'failed';
  latency_ms: number;
  tested_at: string;
  expected_dimensions: number;
  actual_dimensions: number | null;
  error: TestError | null;
}

const providerTag = (id: string) => ({ type: PROVIDERS_TAG, id }) as const;
const modelsTag = (providerId: string) => ({ type: MODELS_TAG, id: providerId }) as const;
const path = (id: string) => `/providers/${encodeURIComponent(id)}`;

export const providersApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    listProviders: build.query<Page<Provider>, { cursor?: string }>({
      query: ({ cursor }) => ({ url: '/providers', params: cursor ? { cursor } : {} }),
      providesTags: [PROVIDERS_TAG],
    }),
    getProvider: build.query<Provider, string>({
      query: (id) => path(id),
      providesTags: (_result, _error, id) => [providerTag(id)],
    }),
    createProvider: build.mutation<Provider, CreateProviderBody>({
      query: (body) => ({ url: '/providers', method: 'POST', body }),
      invalidatesTags: [PROVIDERS_TAG],
    }),
    updateProvider: build.mutation<Provider, UpdateProviderBody>({
      query: ({ id, ...body }) => ({ url: path(id), method: 'PATCH', body }),
      invalidatesTags: (_result, _error, { id }) => [PROVIDERS_TAG, providerTag(id)],
    }),
    deleteProvider: build.mutation<null, string>({
      query: (id) => ({ url: path(id), method: 'DELETE' }),
      invalidatesTags: [PROVIDERS_TAG],
    }),
    testProvider: build.mutation<ConnectionTestResult, string>({
      query: (id) => ({ url: `${path(id)}/test`, method: 'POST' }),
      invalidatesTags: (_result, _error, id) => [PROVIDERS_TAG, providerTag(id)],
    }),
    listModels: build.query<{ items: ModelConfig[] }, string>({
      query: (providerId) => `${path(providerId)}/models`,
      providesTags: (_result, _error, providerId) => [modelsTag(providerId)],
    }),
    createModel: build.mutation<
      ModelConfig,
      { providerId: string; model_id: string; display_name?: string | null; context_window?: number | null }
    >({
      query: ({ providerId, ...body }) => ({ url: `${path(providerId)}/models`, method: 'POST', body }),
      invalidatesTags: (_result, _error, { providerId }) => [modelsTag(providerId)],
    }),
    updateModel: build.mutation<
      ModelConfig,
      { providerId: string; id: string; display_name?: string | null; enabled?: boolean }
    >({
      query: ({ providerId, id, ...body }) => ({
        url: `${path(providerId)}/models/${encodeURIComponent(id)}`,
        method: 'PATCH',
        body,
      }),
      invalidatesTags: (_result, _error, { providerId }) => [modelsTag(providerId)],
    }),
    deleteModel: build.mutation<null, { providerId: string; id: string }>({
      query: ({ providerId, id }) => ({
        url: `${path(providerId)}/models/${encodeURIComponent(id)}`,
        method: 'DELETE',
      }),
      invalidatesTags: (_result, _error, { providerId }) => [modelsTag(providerId)],
    }),
    listEmbeddingProfiles: build.query<Page<EmbeddingProfile>, { cursor?: string }>({
      query: ({ cursor }) => ({ url: '/embedding-profiles', params: cursor ? { cursor } : {} }),
      providesTags: [EMBEDDING_PROFILES_TAG],
    }),
    createEmbeddingProfile: build.mutation<
      EmbeddingProfile,
      {
        name: string;
        provider_id: string;
        model_id: string;
        dimensions: number;
        distance_metric: DistanceMetric;
        normalize: boolean;
      }
    >({
      query: (body) => ({ url: '/embedding-profiles', method: 'POST', body }),
      invalidatesTags: [EMBEDDING_PROFILES_TAG],
    }),
    updateEmbeddingProfile: build.mutation<
      EmbeddingProfile,
      { id: string; name?: string; status?: ProfileStatus }
    >({
      query: ({ id, ...body }) => ({
        url: `/embedding-profiles/${encodeURIComponent(id)}`,
        method: 'PATCH',
        body,
      }),
      invalidatesTags: [EMBEDDING_PROFILES_TAG],
    }),
    testEmbeddingProfile: build.mutation<ProfileTestResult, string>({
      query: (id) => ({ url: `/embedding-profiles/${encodeURIComponent(id)}/test`, method: 'POST' }),
    }),
  }),
});

export const {
  useListProvidersQuery,
  useGetProviderQuery,
  useCreateProviderMutation,
  useUpdateProviderMutation,
  useDeleteProviderMutation,
  useTestProviderMutation,
  useListModelsQuery,
  useCreateModelMutation,
  useUpdateModelMutation,
  useDeleteModelMutation,
  useListEmbeddingProfilesQuery,
  useCreateEmbeddingProfileMutation,
  useUpdateEmbeddingProfileMutation,
  useTestEmbeddingProfileMutation,
} = providersApi;
