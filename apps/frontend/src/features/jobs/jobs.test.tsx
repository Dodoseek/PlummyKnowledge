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

const JOBS_ADMIN = {
  ...ADMIN_SESSION,
  permissions: [...ADMIN_SESSION.permissions, 'jobs.read', 'jobs.manage'],
};
const JOBS_READER = { ...ADMIN_SESSION, permissions: ['chat.use', 'jobs.read'] };

function ingestion(id: string, status: string, error: string | null = null) {
  return {
    id,
    type: 'ingestion',
    kind: 'ingest',
    status,
    stage: 'embed',
    result: null,
    attempt: 1,
    max_attempts: 3,
    error: error === null ? null : { code: error, message: 'Redacted message.' },
    document_id: '0190a000-0000-7000-8000-00000000d0c1',
    document_version_id: null,
    queued_at: '2026-11-26T10:00:00Z',
    started_at: null,
    finished_at: null,
  };
}

const SYNC = {
  id: '0190a000-0000-7000-8000-0000000005c1',
  type: 'sync',
  data_source_id: '0190a000-0000-7000-8000-0000000005d1',
  trigger: 'manual',
  mode: 'incremental',
  status: 'running',
  stats: { added: 2 },
  attempt: 1,
  max_attempts: 5,
  error: null,
  queued_at: '2026-11-26T10:00:00Z',
  started_at: '2026-11-26T10:00:01Z',
  finished_at: null,
};

const page = (items: unknown[], next: string | null = null) =>
  jsonResponse({ items, next_cursor: next, has_more: next !== null });

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
  return fetchMock;
}

function urlsOf(fetchMock: ReturnType<typeof mockApi>, method: string, path: string): URL[] {
  return fetchMock.mock.calls
    .map((call) => requestOf(call))
    .filter((request) => request.method === method && new URL(request.url).pathname.endsWith(path))
    .map((request) => new URL(request.url));
}

describe('jobs page', () => {
  it('is hidden from a member and requests no jobs', async () => {
    const fetchMock = renderAt('/admin/jobs', MEMBER_SESSION);
    expect(await screen.findByText(/do not have permission to view this page/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Jobs' })).not.toBeInTheDocument();
    expect(urlsOf(fetchMock, 'GET', '/jobs')).toHaveLength(0);
  });

  it('lists ingestion jobs with redacted error codes and offers retry and stop by status', async () => {
    renderAt('/admin/jobs', JOBS_ADMIN, {
      'GET /jobs': page([
        ingestion('j-failed', 'failed', 'provider_auth_failed'),
        ingestion('j-queued', 'queued'),
        ingestion('j-cancelled', 'cancelled', 'cancelled_by_admin'),
        ingestion('j-deleted', 'cancelled', 'document_deleted'),
        ingestion('j-done', 'succeeded'),
      ]),
    });
    expect(await screen.findByRole('link', { name: 'Jobs' })).toBeInTheDocument();
    const rows = await screen.findAllByTestId('job-row');
    expect(rows).toHaveLength(5);
    const [failed, queued, cancelled, deleted, done] = rows as [
      HTMLElement,
      HTMLElement,
      HTMLElement,
      HTMLElement,
      HTMLElement,
    ];
    expect(within(failed).getByText('provider_auth_failed')).toBeInTheDocument();
    expect(within(failed).queryByText('Redacted message.')).not.toBeInTheDocument();
    expect(within(failed).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(within(failed).queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument();
    expect(within(queued).getByRole('button', { name: 'Stop' })).toBeInTheDocument();
    expect(within(queued).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(within(cancelled).getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(within(deleted).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(within(done).queryAllByRole('button')).toHaveLength(0);
  });

  it('stops a job only after confirmation and refreshes the list', async () => {
    let listed = 0;
    const fetchMock = renderAt('/admin/jobs', JOBS_ADMIN, {
      'GET /jobs': () => {
        listed += 1;
        return listed === 1
          ? page([ingestion('j-queued', 'queued')])
          : page([ingestion('j-queued', 'cancelled', 'cancelled_by_admin')]);
      },
      'POST /jobs/ingestion/j-queued/cancel': jsonResponse(
        ingestion('j-queued', 'cancelled', 'cancelled_by_admin'),
      ),
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Stop' }));
    expect(urlsOf(fetchMock, 'POST', '/cancel')).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: 'Stop job' }));
    await waitFor(() => {
      expect(urlsOf(fetchMock, 'POST', '/jobs/ingestion/j-queued/cancel')).toHaveLength(1);
    });
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
    const [request] = fetchMock.mock.calls.map((call) => requestOf(call)).filter((r) => r.method === 'POST');
    expect(request?.headers.get('X-CSRF-Token')).toBe('csrf-token-from-server');
  });

  it('shows the conflict when a job finished in the meantime', async () => {
    renderAt('/admin/jobs', JOBS_ADMIN, {
      'GET /jobs': page([ingestion('j-failed', 'failed', 'parse_failed')]),
      'POST /jobs/ingestion/j-failed/retry': problem(409, 'job_not_retryable'),
    });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('switches to sync jobs and filters by status with allowlisted parameters only', async () => {
    const fetchMock = renderAt('/admin/jobs', JOBS_ADMIN, {
      'GET /jobs': (_init, request) => {
        const type = new URL(request.url).searchParams.get('type');
        return type === 'sync' ? page([SYNC]) : page([ingestion('j-queued', 'queued')]);
      },
    });
    const user = userEvent.setup();
    await screen.findByText(/Document 0190a000/);
    await user.click(screen.getByRole('tab', { name: 'Sync' }));
    expect(await screen.findByText(/Source 0190a000 · incremental/)).toBeInTheDocument();
    expect(screen.getByText('added 2')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Status' }), 'running');
    await waitFor(() => {
      const last = urlsOf(fetchMock, 'GET', '/jobs').at(-1);
      expect(last?.searchParams.get('status')).toBe('running');
    });
    for (const url of urlsOf(fetchMock, 'GET', '/jobs')) {
      for (const key of url.searchParams.keys()) {
        expect(['type', 'status', 'cursor', 'limit']).toContain(key);
      }
    }
  });

  it('shows no actions without jobs.manage', async () => {
    renderAt('/admin/jobs', JOBS_READER, { 'GET /jobs': page([ingestion('j-failed', 'failed', 'x')]) });
    await screen.findAllByTestId('job-row');
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: 'Actions' })).not.toBeInTheDocument();
  });
});
