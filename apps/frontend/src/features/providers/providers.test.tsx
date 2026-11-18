import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { AppProviders } from '../../app/providers/AppProviders';
import { routes } from '../../app/routes';
import { makeStore } from '../../app/store/store';
import {
  ADMIN_SESSION,
  browserStorageDump,
  INITIALIZED,
  jsonResponse,
  MEMBER_SESSION,
  mockApi,
  problem,
  requestOf,
} from '../../test/utils';

const PROVIDER_ADMIN = {
  ...ADMIN_SESSION,
  permissions: [...ADMIN_SESSION.permissions, 'providers.read', 'providers.manage'],
};
const PROVIDER_READER = { ...MEMBER_SESSION, permissions: ['chat.use', 'providers.read'] };

const API_KEY = 'sk-test-key-typed-in-the-browser-1234'; // fake test value; secret-scan: allow

const PROVIDER = {
  id: '0190a000-0000-7000-8000-000000000030',
  name: 'OpenAI',
  provider_type: 'openai',
  base_url: 'https://api.openai.com/v1',
  has_secret: true,
  secret_hint: '••••1234',
  enabled: true,
  settings: { timeout_seconds: 60 },
  last_test: null,
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};

const OLLAMA = {
  ...PROVIDER,
  id: '0190a000-0000-7000-8000-000000000031',
  name: 'Local',
  provider_type: 'ollama',
  base_url: 'http://ollama:11434',
  has_secret: false,
  secret_hint: null,
};

const PROFILE = {
  id: '0190a000-0000-7000-8000-000000000040',
  name: 'Local 768',
  provider_id: OLLAMA.id,
  model_id: 'nomic-embed-text',
  dimensions: 768,
  distance_metric: 'cosine',
  normalize: false,
  max_input_tokens: null,
  status: 'active',
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};

const page = (items: unknown[]) => jsonResponse({ items, next_cursor: null, has_more: false });

function renderAt(path: string, session: unknown, handlers: Parameters<typeof mockApi>[0] = {}) {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(session),
    ...handlers,
  });
  const store = makeStore();
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <AppProviders store={store}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { fetchMock, router, store };
}

function bodiesOf(fetchMock: ReturnType<typeof mockApi>, method: string, suffix: string): Promise<unknown>[] {
  return fetchMock.mock.calls
    .map((call) => requestOf(call))
    .filter((request) => request.method === method && new URL(request.url).pathname.endsWith(suffix))
    .map((request) => request.json() as Promise<unknown>);
}

describe('AI providers pages', () => {
  it('is hidden from users without providers.read and requests nothing', async () => {
    const { fetchMock } = renderAt('/admin/providers', MEMBER_SESSION);
    expect(await screen.findByText(/do not have permission to view this page/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'AI providers' })).not.toBeInTheDocument();
    const paths = fetchMock.mock.calls.map((call) => new URL(requestOf(call).url).pathname);
    expect(paths.some((path) => path.includes('/providers'))).toBe(false);
  });

  it('lists providers with a masked key hint; readers get no create form', async () => {
    renderAt('/admin/providers', PROVIDER_READER, { 'GET /providers': page([PROVIDER, OLLAMA]) });
    const row = (await screen.findByRole('link', { name: 'OpenAI' })).closest('tr');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText('••••1234')).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText('not tested')).toBeInTheDocument();
    expect(screen.getByText('No key')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'AI providers' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add provider' })).not.toBeInTheDocument();
  });

  it('sends the key once on create and keeps it out of the store and browser storage', async () => {
    const user = userEvent.setup();
    const { fetchMock, router, store } = renderAt('/admin/providers', PROVIDER_ADMIN, {
      'GET /providers': page([]),
      'POST /providers': jsonResponse(PROVIDER, 201),
      [`GET /providers/${PROVIDER.id}`]: jsonResponse(PROVIDER),
      [`GET /providers/${PROVIDER.id}/models`]: jsonResponse({ items: [] }),
    });
    await user.type(await screen.findByRole('textbox', { name: /^Name/ }), 'OpenAI');
    await user.type(screen.getByLabelText(/API key/), API_KEY);
    await user.click(screen.getByRole('button', { name: 'Add provider' }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/admin/providers/${PROVIDER.id}`);
    });
    const [body] = await Promise.all(bodiesOf(fetchMock, 'POST', '/providers'));
    expect(body).toEqual({ name: 'OpenAI', provider_type: 'openai', api_key: API_KEY });
    expect(await screen.findByRole('heading', { name: 'OpenAI' })).toBeInTheDocument();
    expect(JSON.stringify(store.getState())).not.toContain(API_KEY);
    expect(browserStorageDump()).not.toContain(API_KEY);
    expect(document.body.innerHTML).not.toContain(API_KEY);
  });

  it('shows a network policy refusal under the base URL field', async () => {
    const user = userEvent.setup();
    const { store } = renderAt('/admin/providers', PROVIDER_ADMIN, {
      'GET /providers': page([]),
      'POST /providers': problem(422, 'endpoint_not_allowed', {
        errors: [
          {
            field: 'body.base_url',
            code: 'address_never_allowed',
            message: 'Endpoint is not allowed by instance network policy.',
          },
        ],
      }),
    });
    await user.type(await screen.findByRole('textbox', { name: /^Name/ }), 'Metadata');
    await user.type(screen.getByRole('textbox', { name: /^Base URL/ }), 'https://169.254.169.254');
    await user.type(screen.getByLabelText(/API key/), API_KEY);
    await user.click(screen.getByRole('button', { name: 'Add provider' }));
    expect(
      await screen.findByText('Endpoint is not allowed by instance network policy.'),
    ).toBeInTheDocument();
    expect(screen.getByText(/allowlist in the instance configuration/)).toBeInTheDocument();
    // the key stays in the form for another try, but not in the store
    expect(screen.getByLabelText(/API key/)).toHaveValue(API_KEY);
    expect(JSON.stringify(store.getState())).not.toContain(API_KEY);
  });

  it('tests the connection and shows only the typed error', async () => {
    const user = userEvent.setup();
    renderAt(`/admin/providers/${PROVIDER.id}`, PROVIDER_ADMIN, {
      [`GET /providers/${PROVIDER.id}`]: jsonResponse(PROVIDER),
      [`GET /providers/${PROVIDER.id}/models`]: jsonResponse({ items: [] }),
      'GET /providers': page([PROVIDER]),
      [`POST /providers/${PROVIDER.id}/test`]: jsonResponse({
        status: 'failed',
        latency_ms: 120,
        tested_at: '2026-11-18T10:05:00Z',
        models: [],
        error: { code: 'provider_auth_failed', message: 'The provider rejected the credentials.' },
      }),
    });
    await user.click(await screen.findByRole('button', { name: 'Test connection' }));
    const outcome = await screen.findByTestId('test-outcome');
    expect(within(outcome).getByText('Connection failed')).toBeInTheDocument();
    expect(within(outcome).getByText(/The provider rejected the credentials/)).toBeInTheDocument();
    expect(within(outcome).getByText('(provider_auth_failed)')).toBeInTheDocument();
  });

  it('replaces a key with a PATCH that carries only the key, then clears the field', async () => {
    const user = userEvent.setup();
    const { fetchMock, store } = renderAt(`/admin/providers/${PROVIDER.id}`, PROVIDER_ADMIN, {
      [`GET /providers/${PROVIDER.id}`]: jsonResponse(PROVIDER),
      [`GET /providers/${PROVIDER.id}/models`]: jsonResponse({ items: [] }),
      'GET /providers': page([PROVIDER]),
      [`PATCH /providers/${PROVIDER.id}`]: jsonResponse({ ...PROVIDER, secret_hint: '••••5678' }),
    });
    await user.type(await screen.findByLabelText('New API key'), API_KEY);
    await user.click(screen.getByRole('button', { name: 'Replace key' }));
    await waitFor(() => {
      expect(screen.getByLabelText('New API key')).toHaveValue('');
    });
    const [body] = await Promise.all(bodiesOf(fetchMock, 'PATCH', `/providers/${PROVIDER.id}`));
    expect(body).toEqual({ api_key: API_KEY });
    expect(JSON.stringify(store.getState())).not.toContain(API_KEY);
    // a cloud provider's key can be replaced but not removed
    expect(screen.queryByRole('button', { name: 'Remove key' })).not.toBeInTheDocument();
  });

  it('removes the key of a local provider with api_key null', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderAt(`/admin/providers/${OLLAMA.id}`, PROVIDER_ADMIN, {
      [`GET /providers/${OLLAMA.id}`]: jsonResponse({ ...OLLAMA, has_secret: true }),
      [`GET /providers/${OLLAMA.id}/models`]: jsonResponse({ items: [] }),
      'GET /providers': page([OLLAMA]),
      [`PATCH /providers/${OLLAMA.id}`]: jsonResponse(OLLAMA),
    });
    await user.click(await screen.findByRole('button', { name: 'Remove key' }));
    await user.click(screen.getByRole('button', { name: 'Remove the key' }));
    await waitFor(async () => {
      const [body] = await Promise.all(bodiesOf(fetchMock, 'PATCH', `/providers/${OLLAMA.id}`));
      expect(body).toEqual({ api_key: null });
    });
  });

  it('explains why a provider in use cannot be deleted', async () => {
    const user = userEvent.setup();
    renderAt(`/admin/providers/${OLLAMA.id}`, PROVIDER_ADMIN, {
      [`GET /providers/${OLLAMA.id}`]: jsonResponse(OLLAMA),
      [`GET /providers/${OLLAMA.id}/models`]: jsonResponse({ items: [] }),
      'GET /providers': page([OLLAMA]),
      [`DELETE /providers/${OLLAMA.id}`]: problem(409, 'provider_in_use'),
    });
    await user.click(await screen.findByRole('button', { name: 'Delete provider' }));
    await user.click(screen.getAllByRole('button', { name: 'Delete provider' })[0]!);
    expect(await screen.findByText(/used by an embedding profile/)).toBeInTheDocument();
  });

  it('adds a chat model', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderAt(`/admin/providers/${PROVIDER.id}`, PROVIDER_ADMIN, {
      [`GET /providers/${PROVIDER.id}`]: jsonResponse(PROVIDER),
      [`GET /providers/${PROVIDER.id}/models`]: jsonResponse({ items: [] }),
      'GET /providers': page([PROVIDER]),
      [`POST /providers/${PROVIDER.id}/models`]: problem(409, 'model_id_taken'),
    });
    await user.type(await screen.findByRole('combobox', { name: 'Model ID' }), 'gpt-x');
    await user.click(screen.getByRole('button', { name: 'Add model' }));
    expect(await screen.findByText('This provider already has a model with this ID.')).toBeInTheDocument();
    const [body] = await Promise.all(bodiesOf(fetchMock, 'POST', '/models'));
    expect(body).toEqual({ model_id: 'gpt-x', display_name: null });
  });
});

describe('embedding profiles page', () => {
  it('creates a profile on an embedding-capable provider and tests its dimension', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderAt('/admin/embedding-profiles', PROVIDER_ADMIN, {
      'GET /embedding-profiles': page([PROFILE]),
      'GET /providers': page([{ ...PROVIDER, provider_type: 'anthropic', name: 'Claude' }, OLLAMA]),
      'POST /embedding-profiles': jsonResponse(PROFILE, 201),
      [`POST /embedding-profiles/${PROFILE.id}/test`]: jsonResponse({
        status: 'failed',
        latency_ms: 30,
        tested_at: '2026-11-18T10:05:00Z',
        expected_dimensions: 768,
        actual_dimensions: 1024,
        error: { code: 'dimension_mismatch', message: 'The model returned vectors of another size.' },
      }),
    });
    const providerSelect = await screen.findByRole('combobox', { name: /^Provider/ });
    // Anthropic has no embeddings API: not offered
    expect(within(providerSelect).queryByRole('option', { name: 'Claude' })).not.toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: /^Name/ }), 'Local 768');
    await user.type(screen.getByRole('textbox', { name: /^Model ID/ }), 'nomic-embed-text');
    await user.type(screen.getByRole('spinbutton', { name: /^Dimensions/ }), '768');
    await user.click(screen.getByRole('button', { name: 'Add profile' }));
    await waitFor(async () => {
      const [body] = await Promise.all(bodiesOf(fetchMock, 'POST', '/embedding-profiles'));
      expect(body).toEqual({
        name: 'Local 768',
        provider_id: OLLAMA.id,
        model_id: 'nomic-embed-text',
        dimensions: 768,
        distance_metric: 'cosine',
        normalize: false,
      });
    });

    await user.click(screen.getByRole('button', { name: 'Test' }));
    const outcome = await screen.findByTestId('test-outcome');
    expect(within(outcome).getByText('Expected 768, got 1024.')).toBeInTheDocument();
    expect(within(outcome).getByText('(dimension_mismatch)')).toBeInTheDocument();
  });
});
