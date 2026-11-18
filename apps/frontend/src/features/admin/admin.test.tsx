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

const ROLES = {
  items: [
    { key: 'admin', name: 'Administrator', description: 'Everything', is_system: true, permissions: [] },
    { key: 'member', name: 'Member', description: 'Chat', is_system: true, permissions: ['chat.use'] },
    { key: 'auditor', name: 'Auditor', description: 'Audit', is_system: true, permissions: [] },
  ],
};

const USER = {
  id: '0190a000-0000-7000-8000-000000000010',
  email: 'new@example.com',
  display_name: 'New User',
  status: 'invited',
  roles: ['member'],
  created_at: '2026-11-18T10:00:00Z',
  last_login_at: null,
  groups: [],
};

const GROUP = {
  id: '0190a000-0000-7000-8000-000000000020',
  name: 'Engineering',
  description: null,
  member_count: 0,
  created_at: '2026-11-18T10:00:00Z',
  members: [],
  roles: ['member'],
};

const ACTIVATION_TOKEN = 'one-time-activation-token-value';

function renderAt(path: string, session: unknown, handlers: Parameters<typeof mockApi>[0] = {}) {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(session),
    'GET /roles': jsonResponse(ROLES),
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

const page = (items: unknown[], next: string | null = null) =>
  jsonResponse({ items, next_cursor: next, has_more: next !== null });

describe('administration pages', () => {
  it('hides admin links and pages from a member without requesting admin data', async () => {
    const { fetchMock } = renderAt('/admin/users', MEMBER_SESSION);
    expect(await screen.findByText(/do not have permission to view this page/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Users' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Groups' })).not.toBeInTheDocument();
    const paths = fetchMock.mock.calls.map((call) => new URL(requestOf(call).url).pathname);
    expect(paths.some((path) => path.endsWith('/users'))).toBe(false);
  });

  it('lists users and pages with the cursor, sending only allowlisted parameters', async () => {
    const { fetchMock } = renderAt('/admin/users', ADMIN_SESSION, {
      'GET /users': (_init, request) =>
        new URL(request.url).searchParams.has('cursor')
          ? page([{ ...USER, id: 'second', email: 'second@example.com', display_name: 'Second' }])
          : page([USER], 'cursor-1'),
    });
    expect(await screen.findByRole('link', { name: 'New User' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Users' })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    expect(await screen.findByRole('link', { name: 'Second' })).toBeInTheDocument();
    const urls = fetchMock.mock.calls
      .map(requestOf)
      .filter((request) => new URL(request.url).pathname.endsWith('/users'))
      .map((request) => new URL(request.url).searchParams);
    expect([...urls[0]!.keys()]).toEqual(['limit']);
    expect(urls.at(-1)!.get('cursor')).toBe('cursor-1');
  });

  it('invites a user and shows the activation link once, without storing it', async () => {
    const { fetchMock } = renderAt('/admin/users', ADMIN_SESSION, {
      'GET /users': page([]),
      'POST /users': jsonResponse(
        {
          user: USER,
          activation: {
            url: `https://kb.example.test/activate#token=${ACTIVATION_TOKEN}`,
            expires_at: '2026-11-21T10:00:00Z',
          },
        },
        201,
      ),
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Invite user' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText(/^Email/), 'new@example.com');
    await userEvent.type(within(dialog).getByLabelText(/^Name/), 'New User');
    await userEvent.click(await within(dialog).findByRole('checkbox', { name: /Auditor/ }));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create and get link' }));

    const notice = await screen.findByTestId('activation-link');
    expect(notice).toHaveTextContent(`https://kb.example.test/activate#token=${ACTIVATION_TOKEN}`);
    const request = fetchMock.mock.calls.map(requestOf).find((item) => item.method === 'POST');
    expect(await request!.json()).toEqual({
      email: 'new@example.com',
      display_name: 'New User',
      roles: ['member', 'auditor'],
    });
    expect(browserStorageDump()).not.toContain(ACTIVATION_TOKEN);
  });

  it('shows the email conflict from the server', async () => {
    renderAt('/admin/users', ADMIN_SESSION, {
      'GET /users': page([]),
      'POST /users': problem(409, 'email_taken'),
    });
    await userEvent.click(await screen.findByRole('button', { name: 'Invite user' }));
    const dialog = await screen.findByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText(/^Email/), 'taken@example.com');
    await userEvent.type(within(dialog).getByLabelText(/^Name/), 'Taken');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create and get link' }));
    expect(await within(dialog).findByText('A user with this email already exists.')).toBeInTheDocument();
  });

  it('explains the last-administrator rule on the user page', async () => {
    renderAt(`/admin/users/${USER.id}`, ADMIN_SESSION, {
      [`GET /users/${USER.id}`]: jsonResponse({ ...USER, status: 'active', roles: ['admin'] }),
      [`PUT /users/${USER.id}/roles`]: problem(409, 'last_administrator'),
    });
    await userEvent.click(await screen.findByRole('checkbox', { name: /Administrator/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save roles' }));
    expect(await screen.findByText(/at least one active administrator/)).toBeInTheDocument();
  });

  it('does not offer the admin role for groups and saves group roles', async () => {
    const { fetchMock } = renderAt(`/admin/groups/${GROUP.id}`, ADMIN_SESSION, {
      [`GET /groups/${GROUP.id}`]: jsonResponse(GROUP),
      [`PUT /groups/${GROUP.id}/roles`]: jsonResponse({ ...GROUP, roles: ['member', 'auditor'] }),
      'GET /users': page([]),
    });
    expect(await screen.findByRole('checkbox', { name: /Auditor/ })).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /Administrator/ })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('checkbox', { name: /Auditor/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Save roles' }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.map(requestOf).some((item) => item.method === 'PUT')).toBe(true);
    });
    const request = fetchMock.mock.calls.map(requestOf).find((item) => item.method === 'PUT');
    expect(await request!.json()).toEqual({ roles: ['member', 'auditor'] });
  });
});
