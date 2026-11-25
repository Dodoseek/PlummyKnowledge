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
  permissions: [...ADMIN_SESSION.permissions, 'knowledge_bases.manage'],
};
/** Manages this knowledge base through its access list only: no directory, no permission to delete. */
const KB_OWNER = { ...MEMBER_SESSION, permissions: ['chat.use'] };

const KB_ID = '0190a000-0000-7000-8000-000000000050';
const KB = {
  id: KB_ID,
  name: 'HR',
  description: 'Policies',
  embedding_profile_id: '0190a000-0000-7000-8000-000000000040',
  indexing: { documents_total: 0, indexed: 0, processing: 0, failed: 0 },
  my_access_level: 'manage',
  created_at: '2026-11-18T10:00:00Z',
  updated_at: '2026-11-18T10:00:00Z',
};
const USER_GRANT = {
  principal_type: 'user',
  principal_id: '0190a000-0000-7000-8000-000000000002',
  name: 'Member',
  email: 'member@example.com',
  access_level: 'manage',
  granted_by: null,
  granted_at: '2026-11-18T10:00:00Z',
};
const GROUP = {
  id: '0190a000-0000-7000-8000-000000000090',
  name: 'Lawyers',
  description: null,
  member_count: 2,
  created_at: '2026-11-18T10:00:00Z',
};

const page = (items: unknown[]) => jsonResponse({ items, next_cursor: null, has_more: false });

function renderAt(path: string, session: unknown, handlers: Parameters<typeof mockApi>[0] = {}) {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(session),
    'GET /documents': page([]),
    [`GET /knowledge-bases/${KB_ID}/access`]: jsonResponse({ items: [USER_GRANT] }),
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

describe('knowledge base settings', () => {
  it('are not shown with read access', async () => {
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, MEMBER_SESSION, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse({ ...KB, my_access_level: 'read' }),
    });
    expect(await screen.findByRole('heading', { name: 'HR' })).toBeInTheDocument();
    expect(await screen.findByText('No documents yet.')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Settings' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Access' })).not.toBeInTheDocument();
    expect(requests(fetchMock, 'GET', '/access')).toHaveLength(0);
  });

  it('renames with only the edited fields and clears an emptied description', async () => {
    const user = userEvent.setup();
    let current: Record<string, unknown> = KB;
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: () => jsonResponse(current),
      [`PATCH /knowledge-bases/${KB_ID}`]: () => {
        current = { ...KB, name: 'People', description: null, updated_at: '2026-11-19T10:00:00Z' };
        return jsonResponse(current);
      },
    });
    const save = await screen.findByRole('button', { name: 'Save' });
    expect(save).toBeDisabled(); // nothing changed yet
    const name = screen.getByRole('textbox', { name: /^Name/ });
    await user.clear(name);
    await user.type(name, ' People ');
    await user.clear(screen.getByRole('textbox', { name: 'Description' }));
    await user.click(save);

    expect(await screen.findByRole('heading', { name: 'People' })).toBeInTheDocument();
    const [request] = requests(fetchMock, 'PATCH', `/knowledge-bases/${KB_ID}`);
    expect(await request?.json()).toEqual({ name: 'People', description: null });
    expect(screen.getByText('Saved.')).toBeInTheDocument();
  });

  it('shows a taken name on the field', async () => {
    const user = userEvent.setup();
    renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      [`PATCH /knowledge-bases/${KB_ID}`]: problem(409, 'knowledge_base_name_taken'),
    });
    await user.type(await screen.findByRole('textbox', { name: /^Name/ }), '2');
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByText('A knowledge base with this name already exists.')).toBeInTheDocument();
  });

  it('reindexes every document and reports the counts', async () => {
    const user = userEvent.setup();
    renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      [`POST /knowledge-bases/${KB_ID}/reindex`]: jsonResponse(
        { jobs_created: 2, job_ids: ['a', 'b'], already_queued: 1, without_version: 0 },
        202,
      ),
    });
    await user.click(await screen.findByRole('button', { name: 'Reindex all documents' }));
    expect(await screen.findByTestId('reindex-result')).toHaveTextContent(
      '2 queued, 1 already in the queue, 0 without a file.',
    );
  });

  it('deletes with knowledge_bases.manage after confirming, then returns to the list', async () => {
    const user = userEvent.setup();
    const { fetchMock, router } = renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      [`DELETE /knowledge-bases/${KB_ID}`]: new Response(null, { status: 204 }),
      'GET /knowledge-bases': page([]),
    });
    await user.click(await screen.findByRole('button', { name: 'Delete knowledge base' }));
    expect(requests(fetchMock, 'DELETE', `/knowledge-bases/${KB_ID}`)).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: 'Delete for good' }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/knowledge-bases');
    });
    expect(requests(fetchMock, 'DELETE', `/knowledge-bases/${KB_ID}`)).toHaveLength(1);
  });

  it('offers no deletion and no directory to a manager through the access list only', async () => {
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, KB_OWNER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
    });
    expect(await screen.findByText('member@example.com')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete knowledge base' })).not.toBeInTheDocument();
    expect(screen.getByText(/needs the users.read or groups.read permission/)).toBeInTheDocument();
    expect(requests(fetchMock, 'GET', '/users')).toHaveLength(0);
    expect(requests(fetchMock, 'GET', '/groups')).toHaveLength(0);
  });
});

describe('knowledge base access', () => {
  it('adds a group, changes its level and saves the whole list', async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      'GET /users': page([]),
      'GET /groups': page([GROUP]),
      [`PUT /knowledge-bases/${KB_ID}/access`]: jsonResponse({
        items: [
          USER_GRANT,
          { ...USER_GRANT, principal_type: 'group', principal_id: GROUP.id, name: 'Lawyers' },
        ],
      }),
    });
    expect(await screen.findByText('member@example.com')).toBeInTheDocument();
    const saveAccess = screen.getByRole('button', { name: 'Save access' });
    expect(saveAccess).toBeDisabled();

    await user.selectOptions(screen.getByRole('combobox', { name: 'Principal type' }), 'group');
    await user.type(screen.getByRole('textbox', { name: 'Find a group' }), 'Law');
    await user.click(await screen.findByRole('button', { name: 'Add Lawyers' }));
    expect(screen.getByRole('button', { name: 'Add Lawyers' })).toBeDisabled(); // listed once only
    await user.selectOptions(screen.getByRole('combobox', { name: 'Access of Lawyers' }), 'manage');
    await user.click(saveAccess);

    expect(await screen.findByText('Access saved.')).toBeInTheDocument();
    const [request] = requests(fetchMock, 'PUT', '/access');
    expect(await request?.json()).toEqual({
      grants: [
        { principal_type: 'user', principal_id: USER_GRANT.principal_id, access_level: 'manage' },
        { principal_type: 'group', principal_id: GROUP.id, access_level: 'manage' },
      ],
    });
    const searches = requests(fetchMock, 'GET', '/groups').map((r) => new URL(r.url).searchParams.get('q'));
    expect(searches).toContain('Law');
  });

  it('explains a refused change, such as losing your own manage access, and can discard it', async () => {
    const user = userEvent.setup();
    renderAt(`/knowledge-bases/${KB_ID}`, KB_OWNER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      [`PUT /knowledge-bases/${KB_ID}/access`]: problem(422, 'validation_error', {
        errors: [
          {
            field: 'body.grants',
            code: 'would_lose_manage',
            message: 'You cannot remove your own permission to manage this knowledge base.',
          },
        ],
      }),
    });
    await user.click(await screen.findByRole('button', { name: 'Remove Member' }));
    expect(screen.getByText(/Nobody has access through this list/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save access' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'You cannot remove your own permission to manage this knowledge base.',
    );
    await user.click(screen.getByRole('button', { name: 'Discard changes' }));
    expect(screen.getByText('member@example.com')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows the user search to users.read and leaves disabled users out', async () => {
    const user = userEvent.setup();
    renderAt(`/knowledge-bases/${KB_ID}`, MANAGER, {
      [`GET /knowledge-bases/${KB_ID}`]: jsonResponse(KB),
      [`GET /knowledge-bases/${KB_ID}/access`]: jsonResponse({ items: [] }),
      'GET /users': page([
        {
          id: 'u-1',
          email: 'ann@example.com',
          display_name: 'Ann',
          status: 'active',
          roles: [],
          created_at: '2026-11-18T10:00:00Z',
          last_login_at: null,
        },
        {
          id: 'u-2',
          email: 'bob@example.com',
          display_name: 'Bob',
          status: 'disabled',
          roles: [],
          created_at: '2026-11-18T10:00:00Z',
          last_login_at: null,
        },
      ]),
    });
    const candidates = await screen.findByTestId('principal-candidates');
    await user.type(screen.getByRole('textbox', { name: 'Find a user' }), 'a');
    expect(await within(candidates).findByText('Ann')).toBeInTheDocument();
    expect(within(candidates).queryByText('Bob')).not.toBeInTheDocument();
    await user.click(within(candidates).getByRole('button', { name: 'Add Ann' }));
    const row = screen.getByRole('combobox', { name: 'Access of Ann' });
    expect(row).toHaveValue('read'); // a new entry starts with read access
  });
});
