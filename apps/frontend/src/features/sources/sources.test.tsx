import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

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
import { browser } from './format';
import { SYNC_POLL_INTERVAL_MS } from './sourcesApi';

const MANAGER = {
  ...ADMIN_SESSION,
  permissions: [
    ...ADMIN_SESSION.permissions,
    'knowledge_bases.manage',
    'data_sources.read',
    'data_sources.manage',
    'data_sources.sync',
    'jobs.read',
  ],
};
const READER = { ...ADMIN_SESSION, permissions: [...ADMIN_SESSION.permissions, 'data_sources.read'] };

const KB_ID = '0190a000-0000-7000-8000-000000000050';
const SOURCE_ID = '0190a000-0000-7000-8000-000000000061';
const SCOPE = 'https://www.googleapis.com/auth/drive.readonly';
const KB = {
  id: KB_ID,
  name: 'Sales',
  description: null,
  embedding_profile_id: '0190a000-0000-7000-8000-000000000040',
  indexing: { documents_total: 0, indexed: 0, processing: 0, failed: 0 },
  my_access_level: 'manage',
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};
const UPLOAD = {
  id: '0190a000-0000-7000-8000-000000000060',
  type: 'upload',
  name: 'Uploads',
  status: 'active',
  knowledge_base_id: KB_ID,
  owner_knowledge_base_id: KB_ID,
  folder: null,
  sync: null,
  credentials: null,
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};

function drive(overrides: Record<string, unknown> = {}) {
  return {
    ...UPLOAD,
    id: SOURCE_ID,
    type: 'google_drive',
    name: 'Team drive',
    status: 'active',
    owner_knowledge_base_id: null,
    folder: { id: 'folder-1', name: 'Contracts' },
    sync: {
      interval_minutes: 15,
      next_sync_at: '2026-11-18T10:15:00Z',
      last_sync_at: '2026-11-18T10:00:00Z',
      last_reconciled_at: null,
      last_status: 'succeeded',
      last_error: null,
    },
    credentials: {
      status: 'valid',
      account_label: 'Drive Bot <drive-bot@example.com>',
      granted_scopes: [SCOPE],
      connected_at: '2026-11-18T09:00:00Z',
      last_refreshed_at: null,
    },
    ...overrides,
  };
}

function job(overrides: Record<string, unknown> = {}) {
  return {
    id: '0190a000-0000-7000-8000-000000000090',
    type: 'sync',
    data_source_id: SOURCE_ID,
    trigger: 'initial',
    mode: 'initial',
    status: 'succeeded',
    stats: { added: 3, updated: 0, renamed: 0, deleted: 1, skipped: 0, failed: 0 },
    attempt: 1,
    max_attempts: 5,
    error: null,
    queued_at: '2026-11-18T10:00:00Z',
    started_at: '2026-11-18T10:00:01Z',
    finished_at: '2026-11-18T10:00:09Z',
    ...overrides,
  };
}

const page = (items: unknown[], nextCursor: string | null = null) =>
  jsonResponse({ items, next_cursor: nextCursor, has_more: nextCursor !== null });

type Handlers = Parameters<typeof mockApi>[0];

function renderKb(session: unknown, handlers: Handlers = {}, search = '') {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(session),
    [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
    'GET /documents': page([]),
    'GET /data-sources': page([UPLOAD, drive()]),
    'GET /jobs': page([job()]),
    ...handlers,
  });
  const router = createMemoryRouter(routes, { initialEntries: [`/knowledge-bases/${KB_ID}${search}`] });
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

async function sourceCard() {
  return within(await screen.findByTestId(`source-${SOURCE_ID}`));
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('external sources of a knowledge base', () => {
  it('is not shown without data_sources.read and asks nothing of the API', async () => {
    const { fetchMock } = renderKb(MEMBER_SESSION);
    expect(await screen.findByRole('heading', { name: 'Sales' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'External sources' })).not.toBeInTheDocument();
    expect(requests(fetchMock, 'GET', '/data-sources')).toHaveLength(0);
  });

  it('lists the sources of this knowledge base with the actual granted access and a warning', async () => {
    const hostile = drive({
      credentials: { ...drive().credentials, account_label: '<img src=x onerror=alert(1)>' },
      sync: { ...drive().sync, last_status: 'failed', last_error: 'connector_quota_exceeded' },
    });
    const { fetchMock } = renderKb(MANAGER, { 'GET /data-sources': page([UPLOAD, hostile]) });
    const card = await sourceCard();
    expect(card.getByText('Team drive')).toBeInTheDocument();
    expect(within(card.getByTestId('granted-scopes')).getByText(SCOPE)).toBeInTheDocument();
    expect(card.getByText(/Choosing a folder does not limit this access/)).toBeInTheDocument();
    expect(card.getByText(/<img src=x onerror=alert\(1\)>/)).toBeInTheDocument();
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect(card.getByText('Folder: Contracts')).toBeInTheDocument();
    expect(card.getByText('Schedule: Every 15 minutes')).toBeInTheDocument();
    expect(card.getByTestId('last-sync-error')).toHaveTextContent('quota of this project is used up');
    expect(await card.findByTestId('latest-sync-job')).toHaveTextContent('3 added, 1 deleted');
    // The upload source belongs to the knowledge base and is not listed as an external source.
    expect(screen.queryByText('Uploads')).not.toBeInTheDocument();
    const [list] = requests(fetchMock, 'GET', '/data-sources');
    expect(new URL(list?.url ?? '').searchParams.get('knowledge_base_id')).toBe(KB_ID);
    const [jobs] = requests(fetchMock, 'GET', '/jobs');
    expect(Object.fromEntries(new URL(jobs?.url ?? '').searchParams)).toEqual({
      type: 'sync',
      data_source_id: SOURCE_ID,
      limit: '1',
    });
    expect(browserStorageDump()).not.toMatch(/drive|googleapis|Team/);
  });

  it('shows no controls with read access to the knowledge base or without manage permissions', async () => {
    renderKb(READER, { [`GET /knowledge-bases/${KB_ID}`]: jsonResponse({ ...KB, my_access_level: 'read' }) });
    const card = await sourceCard();
    for (const name of ['Reconnect', 'Change folder', 'Sync now', 'Check connection', 'Delete source']) {
      expect(card.queryByRole('button', { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: 'Add source' })).not.toBeInTheDocument();
  });

  it('adds a source and starts the OAuth connection in the browser', async () => {
    const user = userEvent.setup();
    const assign = vi.spyOn(browser, 'assign').mockImplementation(() => undefined);
    const fresh = drive({ status: 'paused', folder: null, credentials: null });
    const { fetchMock } = renderKb(MANAGER, {
      'GET /data-sources': page([fresh]),
      'POST /data-sources': jsonResponse(fresh, 201),
      [`POST /data-sources/${SOURCE_ID}/oauth/start`]: jsonResponse({
        authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=s',
      }),
      'GET /jobs': page([]),
    });
    await user.type(await screen.findByRole('textbox', { name: /New Google Drive source/ }), ' Team drive ');
    await user.click(screen.getByRole('button', { name: 'Add source' }));
    await waitFor(() => {
      expect(requests(fetchMock, 'POST', '/data-sources')).toHaveLength(1);
    });
    const [created] = requests(fetchMock, 'POST', '/data-sources');
    expect(await created?.json()).toEqual({
      type: 'google_drive',
      name: 'Team drive',
      knowledge_base_id: KB_ID,
    });

    const card = await sourceCard();
    expect(card.getByText('Not connected to a Google account.')).toBeInTheDocument();
    expect(card.queryByRole('button', { name: 'Sync now' })).not.toBeInTheDocument();
    await user.click(card.getByRole('button', { name: 'Connect Google account' }));
    await waitFor(() => {
      expect(assign).toHaveBeenCalledWith('https://accounts.google.com/o/oauth2/v2/auth?state=s');
    });
  });

  it('never leaves the app for an address that is not a web URL', async () => {
    const user = userEvent.setup();
    const assign = vi.spyOn(browser, 'assign').mockImplementation(() => undefined);
    const { fetchMock } = renderKb(MANAGER, {
      [`POST /data-sources/${SOURCE_ID}/oauth/start`]: jsonResponse({
        authorization_url: 'javascript:alert(1)',
      }),
    });
    await user.click((await sourceCard()).getByRole('button', { name: 'Reconnect' }));
    await waitFor(() => {
      expect(requests(fetchMock, 'POST', '/oauth/start')).toHaveLength(1);
    });
    expect(assign).not.toHaveBeenCalled();
  });

  it('explains the result of the OAuth redirect once and removes it from the address', async () => {
    const { router } = renderKb(
      MANAGER,
      {},
      `?source=${SOURCE_ID}&oauth=failed&reason=oauth_no_refresh_token`,
    );
    expect(await screen.findByTestId('oauth-notice')).toHaveTextContent('did not grant offline access');
    await waitFor(() => {
      expect(router.state.location.search).toBe('');
    });
    expect(screen.getByTestId('oauth-notice')).toBeInTheDocument();
  });

  it('confirms a connected source by name', async () => {
    renderKb(MANAGER, {}, `?source=${SOURCE_ID}&oauth=connected`);
    expect(await screen.findByText(/“Team drive” is connected\./)).toBeInTheDocument();
  });

  it('falls back to a generic message for an unknown OAuth reason', async () => {
    renderKb(MANAGER, {}, '?oauth=failed&reason=%3Cscript%3E');
    expect(await screen.findByTestId('oauth-notice')).toHaveTextContent(
      'Connecting the Google account failed.',
    );
  });

  it('chooses a folder in the picker, through shared drives and pages', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderKb(MANAGER, {
      [`GET /data-sources/${SOURCE_ID}/browse`]: (_init, request) => {
        const params = new URL(request.url).searchParams;
        if (params.get('parent_id') === 'drive-1') {
          return page([{ id: 'folder-2', name: 'Legal', kind: 'folder' }]);
        }
        if (params.get('cursor') === 'next') {
          return page([{ id: 'folder-3', name: 'Archive', kind: 'folder' }]);
        }
        return page([{ id: 'drive-1', name: 'Shared', kind: 'shared_drive' }], 'next');
      },
      [`PATCH /data-sources/${SOURCE_ID}`]: jsonResponse(
        drive({ folder: { id: 'folder-2', name: 'Legal' } }),
      ),
    });
    const card = await sourceCard();
    await user.click(card.getByRole('button', { name: 'Change folder' }));
    const picker = within(await card.findByTestId('folder-picker'));
    await user.click(await picker.findByRole('button', { name: 'More' }));
    expect(await picker.findByText('Archive')).toBeInTheDocument();
    await user.click(picker.getByRole('button', { name: 'Shared drive: Shared' }));
    expect(await picker.findByText('Legal')).toBeInTheDocument();
    expect(picker.queryByText('Archive')).not.toBeInTheDocument();
    await user.click(picker.getByRole('button', { name: 'Choose Legal' }));

    await waitFor(() => {
      expect(requests(fetchMock, 'PATCH', `/data-sources/${SOURCE_ID}`)).toHaveLength(1);
    });
    const [patch] = requests(fetchMock, 'PATCH', `/data-sources/${SOURCE_ID}`);
    expect(await patch?.json()).toEqual({ folder_id: 'folder-2' });
    await waitFor(() => {
      expect(card.queryByTestId('folder-picker')).not.toBeInTheDocument();
    });
  });

  it('shows a folder the provider refuses and a failed listing', async () => {
    const user = userEvent.setup();
    renderKb(MANAGER, {
      [`GET /data-sources/${SOURCE_ID}/browse`]: problem(502, 'connector_unavailable'),
    });
    const card = await sourceCard();
    await user.click(card.getByRole('button', { name: 'Change folder' }));
    expect(
      await card.findByText('Google Drive is not reachable right now. Try again later.'),
    ).toBeInTheDocument();
  });

  it('starts a sync and a full check, and explains a refusal', async () => {
    const user = userEvent.setup();
    let calls = 0;
    const { fetchMock } = renderKb(MANAGER, {
      [`POST /data-sources/${SOURCE_ID}/sync`]: () => {
        calls += 1;
        return calls === 1 ? jsonResponse(job({ status: 'queued' }), 202) : problem(409, 'sync_in_progress');
      },
    });
    const card = await sourceCard();
    await user.click(card.getByRole('button', { name: 'Sync now' }));
    await waitFor(() => {
      expect(requests(fetchMock, 'POST', '/sync')).toHaveLength(1);
    });
    await user.click(await card.findByRole('button', { name: 'Full check' }));
    expect(await card.findByText('A sync of this source is already queued or running.')).toBeInTheDocument();
    const bodies = await Promise.all(requests(fetchMock, 'POST', '/sync').map((request) => request.json()));
    expect(bodies).toEqual([{ mode: 'incremental' }, { mode: 'reconciliation' }]);
  });

  it('checks the connection, changes the schedule, disconnects and deletes', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderKb(MANAGER, {
      [`POST /data-sources/${SOURCE_ID}/validate`]: jsonResponse({
        status: 'failed',
        checked_at: '2026-11-18T10:00:00Z',
        error_code: 'connector_auth_revoked',
        account_label: null,
        folder_accessible: null,
      }),
      [`PATCH /data-sources/${SOURCE_ID}`]: jsonResponse(drive()),
      [`POST /data-sources/${SOURCE_ID}/disconnect`]: jsonResponse(drive({ credentials: null })),
      [`DELETE /data-sources/${SOURCE_ID}`]: jsonResponse({ documents_deleted: 3 }),
    });
    const card = await sourceCard();
    await user.click(card.getByRole('button', { name: 'Check connection' }));
    expect(await card.findByTestId('validation-result')).toHaveTextContent('access was revoked or expired');

    await user.selectOptions(card.getByRole('combobox', { name: 'Sync schedule' }), 'Manual only');
    await waitFor(() => {
      expect(requests(fetchMock, 'PATCH', `/data-sources/${SOURCE_ID}`)).toHaveLength(1);
    });
    const [patch] = requests(fetchMock, 'PATCH', `/data-sources/${SOURCE_ID}`);
    expect(await patch?.json()).toEqual({ sync_interval_minutes: null });

    await user.click(card.getByRole('button', { name: 'Disconnect' }));
    await user.click(card.getByRole('button', { name: 'Disconnect account' }));
    await user.click(card.getByRole('button', { name: 'Delete source' }));
    await user.click(card.getByRole('button', { name: 'Delete source and its documents' }));
    await waitFor(() => {
      expect(requests(fetchMock, 'DELETE', `/data-sources/${SOURCE_ID}`)).toHaveLength(1);
    });
    expect(requests(fetchMock, 'POST', '/disconnect')).toHaveLength(1);
  });

  it('follows a running sync and reloads the documents when it ends', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let polls = 0;
    const { fetchMock } = renderKb(MANAGER, {
      'GET /jobs': () => {
        polls += 1;
        return page([job(polls === 1 ? { status: 'running', stats: {} } : {})]);
      },
    });
    const card = await sourceCard();
    expect(await card.findByTestId('latest-sync-job')).toHaveTextContent('running');
    expect(card.getByRole('button', { name: 'Sync now' })).toBeDisabled();
    const documentsBefore = requests(fetchMock, 'GET', '/documents').length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SYNC_POLL_INTERVAL_MS + 100);
    });
    await waitFor(() => {
      expect(card.getByTestId('latest-sync-job')).toHaveTextContent('succeeded');
    });
    await waitFor(() => {
      expect(requests(fetchMock, 'GET', '/documents').length).toBeGreaterThan(documentsBefore);
    });
  });

  it('shows why the sources cannot be loaded', async () => {
    renderKb(MANAGER, { 'GET /data-sources': problem(503, 'service_unavailable') });
    expect(await screen.findByText('Cannot load sources')).toBeInTheDocument();
  });
});
