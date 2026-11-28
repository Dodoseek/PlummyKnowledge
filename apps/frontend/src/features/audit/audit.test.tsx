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
import { toInstant } from './auditApi';

const AUDITOR = { ...ADMIN_SESSION, permissions: ['audit.read'] };
const ACTOR = '0190a000-0000-7000-8000-0000000000a1';

function event(id: string, action: string, outcome: string, extra: Record<string, unknown> = {}) {
  return {
    id,
    created_at: '2026-11-26T10:00:00Z',
    actor_type: 'user',
    actor_user_id: ACTOR,
    action,
    target_type: 'user',
    target_id: '0190a000-0000-7000-8000-0000000000b2',
    outcome,
    correlation_id: 'req-1',
    ip_address: '203.0.113.7',
    user_agent: 'test',
    details: {},
    ...extra,
  };
}

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

function auditUrls(fetchMock: ReturnType<typeof mockApi>): URL[] {
  return fetchMock.mock.calls
    .map((call) => requestOf(call))
    .filter((request) => new URL(request.url).pathname.endsWith('/audit/events'))
    .map((request) => new URL(request.url));
}

describe('audit log page', () => {
  it('is hidden from a member and requests no events', async () => {
    const fetchMock = renderAt('/admin/audit', MEMBER_SESSION);
    expect(await screen.findByText(/do not have permission to view this page/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Audit log' })).not.toBeInTheDocument();
    expect(auditUrls(fetchMock)).toHaveLength(0);
  });

  it('lists events for an auditor, read only', async () => {
    renderAt('/admin/audit', AUDITOR, {
      'GET /audit/events': page([
        event('e1', 'auth.login.failed', 'denied', {
          actor_type: 'anonymous',
          actor_user_id: null,
          details: { reason: 'invalid_password' },
        }),
        event('e2', 'user.created', 'success'),
      ]),
    });
    expect(await screen.findByRole('link', { name: 'Audit log' })).toBeInTheDocument();
    const [failed, created] = (await screen.findAllByTestId('audit-row')) as [HTMLElement, HTMLElement];
    expect(within(failed).getByText('auth.login.failed')).toBeInTheDocument();
    expect(within(failed).getByText('denied')).toBeInTheDocument();
    expect(within(failed).getByText('anonymous')).toBeInTheDocument();
    expect(within(failed).getByText('reason=invalid_password')).toBeInTheDocument();
    expect(within(created).getAllByText('user 0190a000')).toHaveLength(2); // actor and target
    expect(within(created).getByText('203.0.113.7')).toBeInTheDocument();
    expect(screen.queryAllByRole('button', { name: /delete|edit/i })).toHaveLength(0);
  });

  it('sends only the allowlisted filters, with instants for the time range', async () => {
    const fetchMock = renderAt('/admin/audit', AUDITOR, { 'GET /audit/events': page([]) });
    const user = userEvent.setup();
    expect(await screen.findByText('No events match.')).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: 'Action' }), 'auth.logout');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Outcome' }), 'denied');
    await user.type(screen.getByRole('textbox', { name: 'Actor user ID' }), 'not-a-uuid');
    await waitFor(() => {
      const last = auditUrls(fetchMock).at(-1);
      expect(last?.searchParams.get('action')).toBe('auth.logout');
      expect(last?.searchParams.get('outcome')).toBe('denied');
    });
    // An incomplete ID is not sent: the backend would refuse it.
    expect(auditUrls(fetchMock).some((url) => url.searchParams.has('actor_user_id'))).toBe(false);
    await user.clear(screen.getByRole('textbox', { name: 'Actor user ID' }));
    await user.type(screen.getByRole('textbox', { name: 'Actor user ID' }), ACTOR);
    await waitFor(() => {
      expect(auditUrls(fetchMock).at(-1)?.searchParams.get('actor_user_id')).toBe(ACTOR);
    });
    for (const url of auditUrls(fetchMock)) {
      for (const key of url.searchParams.keys()) {
        expect(['action', 'actor_user_id', 'outcome', 'from', 'to', 'cursor', 'limit']).toContain(key);
      }
    }
  });

  it('converts a local time to an instant and ignores an empty or invalid one', () => {
    expect(toInstant('')).toBeUndefined();
    expect(toInstant('nonsense')).toBeUndefined();
    expect(toInstant('2026-11-26T10:00')).toBe(new Date('2026-11-26T10:00').toISOString());
  });

  it('shows the error when the backend refuses', async () => {
    renderAt('/admin/audit', AUDITOR, { 'GET /audit/events': problem(403, 'permission_denied') });
    expect(await screen.findByText('Cannot load the audit log')).toBeInTheDocument();
  });
});
