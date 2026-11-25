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
  permissions: [...ADMIN_SESSION.permissions, 'knowledge_bases.manage', 'providers.read'],
};

const KB_ID = '0190a000-0000-7000-8000-000000000050';
const KB = {
  id: KB_ID,
  name: 'HR',
  description: 'Policies',
  embedding_profile_id: '0190a000-0000-7000-8000-000000000040',
  indexing: { documents_total: 3, indexed: 1, processing: 1, failed: 1 },
  my_access_level: 'manage',
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};

const PROFILE = {
  id: KB.embedding_profile_id,
  name: 'Small',
  provider_id: '0190a000-0000-7000-8000-000000000030',
  model_id: 'text-embedding-3-small',
  dimensions: 1536,
  distance_metric: 'cosine',
  normalize: false,
  max_input_tokens: null,
  status: 'active',
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};

function version(overrides: Record<string, unknown> = {}) {
  return {
    id: '0190a000-0000-7000-8000-000000000070',
    version_number: 1,
    checksum_sha256: 'ab'.repeat(32),
    size_bytes: 2048,
    mime_type: 'application/pdf',
    page_count: 2,
    status: 'ready',
    error_code: null,
    created_at: '2026-11-18T10:00:00Z',
    processed_at: '2026-11-18T10:01:00Z',
    ...overrides,
  };
}

function doc(id: string, filename: string, status: string, overrides: Record<string, unknown> = {}) {
  const latest = version({
    status: status === 'indexed' ? 'ready' : status,
    error_code: status === 'failed' ? 'provider_auth_failed' : null,
  });
  return {
    id,
    filename,
    mime_type: 'application/pdf',
    data_source: { id: '0190a000-0000-7000-8000-000000000060', type: 'upload' },
    external_id: `upload:${id}`,
    current_version: status === 'indexed' ? latest : null,
    latest_version: latest,
    processing_status: status,
    created_at: '2026-11-18T10:00:00Z',
    updated_at: '2026-11-18T10:00:00Z',
    ...overrides,
  };
}

const INDEXED = doc('0190a000-0000-7000-8000-000000000081', 'Policy.pdf', 'indexed');
const FAILED = doc('0190a000-0000-7000-8000-000000000082', 'Broken.pdf', 'failed');

const page = (items: unknown[]) => jsonResponse({ items, next_cursor: null, has_more: false });

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

describe('knowledge bases list', () => {
  it('lists visible knowledge bases; without manage there is no create form', async () => {
    renderAt('/knowledge-bases', MEMBER_SESSION, {
      'GET /knowledge-bases': page([{ ...KB, my_access_level: 'read' }]),
    });
    const link = await screen.findByRole('link', { name: 'HR' });
    expect(link).toHaveAttribute('href', `/knowledge-bases/${KB_ID}`);
    const row = link.closest('tr') as HTMLElement;
    expect(within(row).getByText('1 failed')).toBeInTheDocument();
    expect(within(row).getByText('read')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create knowledge base' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Knowledge bases' })).toBeInTheDocument();
  });

  it('explains an empty list for a user without access', async () => {
    renderAt('/knowledge-bases', MEMBER_SESSION, { 'GET /knowledge-bases': page([]) });
    expect(await screen.findByText(/do not have access to any knowledge base/)).toBeInTheDocument();
  });

  it('creates a knowledge base with an active profile and opens it', async () => {
    const user = userEvent.setup();
    const { fetchMock, router } = renderAt('/knowledge-bases', MANAGER, {
      'GET /knowledge-bases': page([]),
      'GET /embedding-profiles': page([
        { ...PROFILE, id: 'retired', name: 'Old', status: 'retired' },
        PROFILE,
      ]),
      'POST /knowledge-bases': jsonResponse(KB, 201),
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      'GET /documents': page([]),
    });
    await user.type(await screen.findByRole('textbox', { name: /^Name/ }), '  HR ');
    const profiles = screen.getByRole('combobox', { name: /Embedding profile/ });
    expect(within(profiles).getAllByRole('option')).toHaveLength(1); // retired profiles are not offered
    await user.click(screen.getByRole('button', { name: 'Create knowledge base' }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/knowledge-bases/${KB_ID}`);
    });
    const [request] = requests(fetchMock, 'POST', '/knowledge-bases');
    expect(await request?.json()).toEqual({ name: 'HR', embedding_profile_id: PROFILE.id });
  });

  it('shows a taken name and points to profiles when none is active', async () => {
    const user = userEvent.setup();
    renderAt('/knowledge-bases', MANAGER, {
      'GET /knowledge-bases': page([]),
      'GET /embedding-profiles': page([PROFILE]),
      'POST /knowledge-bases': problem(409, 'knowledge_base_name_taken'),
    });
    await user.type(await screen.findByRole('textbox', { name: /^Name/ }), 'HR');
    await user.click(screen.getByRole('button', { name: 'Create knowledge base' }));
    expect(await screen.findByText('A knowledge base with this name already exists.')).toBeInTheDocument();
  });

  it('asks for an active embedding profile first', async () => {
    renderAt('/knowledge-bases', MANAGER, {
      'GET /knowledge-bases': page([]),
      'GET /embedding-profiles': page([{ ...PROFILE, status: 'deprecated' }]),
    });
    expect(await screen.findByRole('link', { name: 'embedding profile' })).toHaveAttribute(
      'href',
      '/admin/embedding-profiles',
    );
  });
});

describe('knowledge base page', () => {
  it('shows documents with readable statuses and errors, filenames as plain text', async () => {
    const hostile = doc(
      '0190a000-0000-7000-8000-000000000083',
      '<img src=x onerror=alert(1)>.pdf',
      'indexed',
    );
    renderAt(`/knowledge-bases/${KB_ID}`, MEMBER_SESSION, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse({ ...KB, my_access_level: 'read' }),
      'GET /documents': page([INDEXED, FAILED, hostile]),
    });
    expect(await screen.findByRole('heading', { name: 'HR' })).toBeInTheDocument();
    const failedRow = (await screen.findByText('Broken.pdf')).closest('tr') as HTMLElement;
    expect(within(failedRow).getByText('failed')).toBeInTheDocument();
    expect(within(failedRow).getByText('The embedding provider rejected the API key.')).toBeInTheDocument();
    expect(screen.getByText('<img src=x onerror=alert(1)>.pdf')).toBeInTheDocument();
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect(screen.getAllByText('2.0 KB')).toHaveLength(3);
    // read access: no upload and no document actions (the API refuses them too)
    expect(screen.queryByRole('button', { name: 'Upload' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reindex' })).not.toBeInTheDocument();
  });

  it('asks for documents of this knowledge base only', async () => {
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      'GET /documents': page([]),
    });
    expect(await screen.findByText('No documents yet.')).toBeInTheDocument();
    const [request] = requests(fetchMock, 'GET', '/documents');
    expect(new URL(request!.url).searchParams.get('knowledge_base_id')).toBe(KB_ID);
  });

  it('uploads each file with the knowledge base field first and reports failures per file', async () => {
    const user = userEvent.setup();
    let uploads = 0;
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      'GET /documents': page([]),
      'POST /documents': () => {
        uploads += 1;
        return uploads === 1
          ? jsonResponse(
              {
                document: { id: INDEXED.id, filename: 'a.md', processing_status: 'pending' },
                job: { id: 'job-1', type: 'ingestion', kind: 'ingest', status: 'queued' },
              },
              202,
            )
          : problem(413, 'payload_too_large');
      },
    });
    const input = await screen.findByLabelText('Files');
    await user.upload(input, [
      new File(['# A'], 'a.md', { type: 'text/markdown' }),
      new File(['x'.repeat(10)], 'big.pdf', { type: 'application/pdf' }),
    ]);
    await user.click(screen.getByRole('button', { name: 'Upload' }));

    const failures = await screen.findByTestId('upload-failures');
    expect(within(failures).getByText('big.pdf')).toBeInTheDocument();
    expect(within(failures).getByText(/larger than the server allows/)).toBeInTheDocument();
    expect(screen.getByText(/1 file queued/)).toBeInTheDocument();

    const sent = requests(fetchMock, 'POST', '/documents');
    expect(sent).toHaveLength(2);
    const form = await sent[0]!.clone().formData();
    expect([...form.keys()]).toEqual(['knowledge_base_id', 'file']);
    expect(form.get('knowledge_base_id')).toBe(KB_ID);
    // jsdom's File loses its name in undici's FormData; the browser check covers the filename.
    expect(await (form.get('file') as Blob).text()).toBe('# A');
    expect(sent[0]!.headers.get('X-CSRF-Token')).toBe('csrf-token-from-server');
  });

  it('deletes after a confirmation and reindexes a failed document', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      'GET /documents': page([FAILED]),
      [`DELETE /documents/${FAILED.id}`]: new Response(null, { status: 204 }),
      [`POST /documents/${FAILED.id}/reindex`]: jsonResponse(
        { job: { id: 'job-2', type: 'ingestion', kind: 'reindex', status: 'queued' }, created: true },
        202,
      ),
    });
    await user.click(await screen.findByRole('button', { name: 'Reindex' }));
    await waitFor(() => {
      expect(requests(fetchMock, 'POST', `/documents/${FAILED.id}/reindex`)).toHaveLength(1);
    });

    await user.click(screen.getByRole('button', { name: 'Delete' }));
    expect(requests(fetchMock, 'DELETE', `/documents/${FAILED.id}`)).toHaveLength(0); // needs a second click
    await user.click(screen.getByRole('button', { name: 'Delete document' }));
    await waitFor(() => {
      expect(requests(fetchMock, 'DELETE', `/documents/${FAILED.id}`)).toHaveLength(1);
    });
  });

  it('shows why an action was refused', async () => {
    const user = userEvent.setup();
    renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      'GET /documents': page([INDEXED]),
      [`POST /documents/${INDEXED.id}/reindex`]: problem(429, 'rate_limited'),
    });
    await user.click(await screen.findByRole('button', { name: 'Reindex' }));
    expect(await screen.findByText(/Too many attempts/)).toBeInTheDocument();
  });

  it('refreshes while documents are being processed, then stops', async () => {
    let calls = 0;
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      'GET /documents': () => {
        calls += 1;
        return page([calls === 1 ? doc(INDEXED.id, 'Policy.pdf', 'processing') : INDEXED]);
      },
    });
    const row = (await screen.findByText('Policy.pdf')).closest('tr') as HTMLElement;
    expect(within(row).getByText('processing')).toBeInTheDocument();
    await waitFor(
      () => {
        expect(
          within(screen.getByText('Policy.pdf').closest('tr') as HTMLElement).getByText('indexed'),
        ).toBeInTheDocument();
      },
      { timeout: 6000 },
    );
    const settled = requests(fetchMock, 'GET', '/documents').length;
    await new Promise((resolve) => setTimeout(resolve, 3500));
    expect(requests(fetchMock, 'GET', '/documents')).toHaveLength(settled); // no polling once indexed
  }, 15_000);

  it('shows a missing or inaccessible knowledge base as not found', async () => {
    renderAt(`/knowledge-bases/${KB_ID}`, MEMBER_SESSION, {
      [`GET /knowledge-bases/${KB_ID}`]: problem(404, 'not_found'),
    });
    expect(await screen.findByText('Knowledge base not available')).toBeInTheDocument();
  });
});
