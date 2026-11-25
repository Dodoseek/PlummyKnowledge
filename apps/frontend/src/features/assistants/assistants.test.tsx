import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { AppProviders } from '../../app/providers/AppProviders';
import { routes } from '../../app/routes';
import { makeStore } from '../../app/store/store';
import {
  ADMIN_SESSION,
  INITIALIZED,
  jsonResponse,
  MEMBER_SESSION,
  mockApi,
  problem,
  requestOf,
} from '../../test/utils';

const MANAGER = {
  ...ADMIN_SESSION,
  permissions: [
    ...ADMIN_SESSION.permissions,
    'assistants.manage',
    'providers.read',
    'knowledge_bases.manage',
  ],
};

const PROVIDER = {
  id: '0190a000-0000-7000-8000-000000000030',
  name: 'Local',
  provider_type: 'ollama',
  base_url: 'http://ollama:11434',
  has_secret: false,
  secret_hint: null,
  enabled: true,
  settings: { timeout_seconds: 60 },
  last_test: null,
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};
const model = (id: string, modelId: string, overrides: Record<string, unknown> = {}) => ({
  id,
  provider_id: PROVIDER.id,
  model_id: modelId,
  display_name: null,
  kind: 'chat',
  context_window: 32000,
  max_output_tokens: 4096,
  enabled: true,
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
  ...overrides,
});
const CHAT = model('0190a000-0000-7000-8000-000000000031', 'qwen3');
const OFF = model('0190a000-0000-7000-8000-000000000032', 'llama-old', { enabled: false });
const EMBEDDING = model('0190a000-0000-7000-8000-000000000033', 'bge-m3', { kind: 'embedding' });

const profile = (id: string, name: string, status = 'active') => ({
  id,
  name,
  provider_id: PROVIDER.id,
  model_id: 'bge-m3',
  dimensions: 1024,
  distance_metric: 'cosine',
  normalize: true,
  max_input_tokens: null,
  status,
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
});
const PROFILE = profile('0190a000-0000-7000-8000-000000000040', 'Main');
const OTHER_PROFILE = profile('0190a000-0000-7000-8000-000000000041', 'Other');

const kb = (id: string, name: string, profileId: string) => ({
  id,
  name,
  description: null,
  embedding_profile_id: profileId,
  indexing: { documents_total: 0, indexed: 0, processing: 0, failed: 0 },
  my_access_level: 'manage',
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
});
const HR = kb('0190a000-0000-7000-8000-000000000050', 'HR', PROFILE.id);
const LEGAL = kb('0190a000-0000-7000-8000-000000000051', 'Legal', OTHER_PROFILE.id);

const ASSISTANT_ID = '0190a000-0000-7000-8000-000000000060';
const assistant = (overrides: Record<string, unknown> = {}) => ({
  id: ASSISTANT_ID,
  name: 'HR helper',
  description: 'Answers about policies',
  instructions: 'Answer briefly.',
  model_config_id: CHAT.id,
  embedding_profile_id: PROFILE.id,
  knowledge_base_ids: [HR.id],
  retrieval: {
    top_k: 8,
    vector_top_k: 40,
    keyword_top_k: 40,
    rrf_k: 60,
    vector_weight: 1,
    keyword_weight: 0.1,
  },
  limits: { max_context_tokens: 8000, max_history_messages: 10, max_output_tokens: 1024 },
  status: 'active',
  availability: { available: true, reason: null },
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
  ...overrides,
});

const page = (items: unknown[]) => jsonResponse({ items, next_cursor: null, has_more: false });

const CHOICES = {
  'GET /providers': page([PROVIDER]),
  [`GET /providers/${PROVIDER.id}/models`]: jsonResponse({ items: [CHAT, OFF, EMBEDDING] }),
  'GET /embedding-profiles': page([PROFILE, OTHER_PROFILE, profile('retired', 'Retired', 'retired')]),
  'GET /knowledge-bases': page([HR, LEGAL]),
};

function renderAt(path: string, session: unknown, handlers: Parameters<typeof mockApi>[0] = {}) {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(session),
    ...handlers,
  });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <AppProviders store={makeStore()}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { fetchMock, router };
}

function requests(fetchMock: ReturnType<typeof mockApi>, method: string, pathname: string): Request[] {
  return fetchMock.mock.calls
    .map((call) => requestOf(call))
    .filter((request) => request.method === method && new URL(request.url).pathname.endsWith(pathname));
}

describe('assistants for a member', () => {
  it('lists the available assistants without configuration links', async () => {
    renderAt('/assistants', MEMBER_SESSION, {
      'GET /assistants': page([
        assistant({ instructions: null }),
        // The backend does not send these to a member; the page would not show them anyway.
        assistant({
          id: 'other',
          name: 'Hidden',
          availability: { available: false, reason: 'missing_kb_access' },
        }),
      ]),
    });
    const cards = await screen.findAllByTestId('assistant-card');
    expect(cards).toHaveLength(1);
    expect(within(cards[0]!).getByText('HR helper')).toBeInTheDocument();
    expect(screen.queryByText('Hidden')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Assistants' })).toHaveAttribute('href', '/assistants');
    expect(screen.queryByRole('link', { name: 'Manage assistants' })).not.toBeInTheDocument();
  });

  it('explains an empty list', async () => {
    renderAt('/assistants', MEMBER_SESSION, { 'GET /assistants': page([]) });
    expect(await screen.findByText(/No assistant is available to you/)).toBeInTheDocument();
  });

  it('does not open the admin page without assistants.manage', async () => {
    const { fetchMock } = renderAt('/admin/assistants', MEMBER_SESSION);
    expect(await screen.findByText('You do not have permission to view this page.')).toBeInTheDocument();
    expect(requests(fetchMock, 'GET', '/assistants')).toHaveLength(0);
  });
});

describe('assistants administration', () => {
  it('shows the availability the backend reports for each assistant', async () => {
    renderAt('/admin/assistants', MANAGER, {
      ...CHOICES,
      'GET /assistants': page([
        assistant(),
        assistant({
          id: 'a-2',
          name: 'Off',
          status: 'disabled',
          availability: { available: false, reason: 'disabled' },
        }),
        assistant({
          id: 'a-3',
          name: 'Legal',
          availability: { available: false, reason: 'missing_kb_access' },
        }),
      ]),
    });
    const row = (name: string) => screen.getByRole('link', { name }).closest('tr') as HTMLElement;
    expect(await screen.findByRole('link', { name: 'HR helper' })).toBeInTheDocument();
    expect(within(row('HR helper')).getByText('available')).toBeInTheDocument();
    expect(within(row('Off')).getByText('Disabled')).toBeInTheDocument();
    expect(within(row('Legal')).getByText(/no access to all its knowledge bases/)).toBeInTheDocument();
  });

  it('creates an assistant from enabled chat models and knowledge bases of the chosen profile', async () => {
    const user = userEvent.setup();
    const { fetchMock, router } = renderAt('/admin/assistants', MANAGER, {
      ...CHOICES,
      'GET /assistants': page([]),
      'POST /assistants': jsonResponse(assistant(), 201),
      [`GET /assistants/${ASSISTANT_ID}`]: jsonResponse(assistant()),
    });
    await user.type(await screen.findByRole('textbox', { name: /^Name/ }), ' HR helper ');
    const models = screen.getByRole('combobox', { name: /Chat model/ });
    // Embedding models and disabled models are not offered.
    expect(
      within(models)
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['Local / qwen3']);
    const profiles = screen.getByRole('combobox', { name: /Embedding profile/ });
    expect(within(profiles).getAllByRole('option')).toHaveLength(2);

    const kbs = screen.getByTestId('assistant-knowledge-bases');
    expect(within(kbs).getByRole('checkbox', { name: 'HR' })).toBeInTheDocument();
    expect(within(kbs).queryByRole('checkbox', { name: 'Legal' })).not.toBeInTheDocument();
    await user.click(within(kbs).getByRole('checkbox', { name: 'HR' }));
    await user.type(screen.getByRole('textbox', { name: 'Instructions' }), 'Answer briefly.');
    await user.click(screen.getByRole('button', { name: 'Create assistant' }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/admin/assistants/${ASSISTANT_ID}`);
    });
    const [request] = requests(fetchMock, 'POST', '/assistants');
    // Retrieval and limits are left to the server's defaults on creation.
    expect(await request?.json()).toEqual({
      name: 'HR helper',
      description: null,
      instructions: 'Answer briefly.',
      model_config_id: CHAT.id,
      embedding_profile_id: PROFILE.id,
      knowledge_base_ids: [HR.id],
      status: 'active',
    });
  });

  it('drops knowledge bases of another profile when the profile changes', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderAt('/admin/assistants', MANAGER, {
      ...CHOICES,
      'GET /assistants': page([]),
      'POST /assistants': problem(409, 'assistant_name_taken'),
    });
    await user.type(await screen.findByRole('textbox', { name: /^Name/ }), 'X');
    await user.click(
      within(screen.getByTestId('assistant-knowledge-bases')).getByRole('checkbox', { name: 'HR' }),
    );
    await user.selectOptions(screen.getByRole('combobox', { name: /Embedding profile/ }), OTHER_PROFILE.id);
    expect(screen.getByText(/Knowledge bases of another profile are removed on save/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Create assistant' }));

    expect(await screen.findByText('An assistant with this name already exists.')).toBeInTheDocument();
    const [request] = requests(fetchMock, 'POST', '/assistants');
    const body = (await request?.json()) as { embedding_profile_id: string; knowledge_base_ids: string[] };
    expect(body.embedding_profile_id).toBe(OTHER_PROFILE.id);
    expect(body.knowledge_base_ids).toEqual([]);
  });

  it('edits retrieval and limits and shows the field the backend refused', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderAt(`/admin/assistants/${ASSISTANT_ID}`, MANAGER, {
      ...CHOICES,
      // It keeps a model that is disabled now: still offered, marked as disabled.
      [`GET /assistants/${ASSISTANT_ID}`]: jsonResponse(assistant({ model_config_id: OFF.id })),
      [`PATCH /assistants/${ASSISTANT_ID}`]: problem(422, 'validation_error', {
        errors: [
          {
            field: 'body.limits.max_output_tokens',
            code: 'exceeds_model_limit',
            message: 'The model writes at most 4096 tokens.',
          },
        ],
      }),
    });
    const models = await screen.findByRole('combobox', { name: /Chat model/ });
    expect(models).toHaveValue(OFF.id);
    expect(within(models).getByRole('option', { name: 'Local / llama-old (disabled)' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Instructions' })).toHaveValue('Answer briefly.');

    const tokens = screen.getByRole('spinbutton', { name: 'Answer tokens' });
    await user.clear(tokens);
    await user.type(tokens, '9000');
    const weight = screen.getByRole('spinbutton', { name: 'Weight of keyword search' });
    await user.clear(weight);
    await user.type(weight, '0.25');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText('The model writes at most 4096 tokens.')).toBeInTheDocument();
    const [request] = requests(fetchMock, 'PATCH', `/assistants/${ASSISTANT_ID}`);
    const body = (await request?.json()) as Record<string, Record<string, number>>;
    expect(body.limits).toEqual({
      max_context_tokens: 8000,
      max_history_messages: 10,
      max_output_tokens: 9000,
    });
    expect(body.retrieval?.keyword_weight).toBe(0.25);
    expect(body.retrieval?.top_k).toBe(8);
  });

  it('deletes after confirming and returns to the list', async () => {
    const user = userEvent.setup();
    const { router } = renderAt(`/admin/assistants/${ASSISTANT_ID}`, MANAGER, {
      ...CHOICES,
      [`GET /assistants/${ASSISTANT_ID}`]: jsonResponse(assistant()),
      [`DELETE /assistants/${ASSISTANT_ID}`]: new Response(null, { status: 204 }),
      'GET /assistants': page([]),
    });
    await user.click(await screen.findByRole('button', { name: 'Delete assistant' }));
    await user.click(screen.getByRole('button', { name: 'Delete for good' }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/admin/assistants');
    });
    expect(await screen.findByText('No assistants yet.')).toBeInTheDocument();
  });

  it('reports an assistant that does not exist', async () => {
    renderAt(`/admin/assistants/${ASSISTANT_ID}`, MANAGER, {
      ...CHOICES,
      [`GET /assistants/${ASSISTANT_ID}`]: problem(404, 'not_found'),
    });
    expect(await screen.findByText('Assistant not available')).toBeInTheDocument();
  });
});
