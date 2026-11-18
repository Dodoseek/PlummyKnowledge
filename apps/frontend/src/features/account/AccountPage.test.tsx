import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { AppProviders } from '../../app/providers/AppProviders';
import { routes } from '../../app/routes';
import { makeStore } from '../../app/store/store';
import { CSRF_HEADER } from '../../shared/api/baseApi';
import { selectCsrfToken } from '../../shared/auth/csrfSlice';
import { INITIALIZED, jsonResponse, mockApi, problem, requestOf, SESSION } from '../../test/utils';

const SESSIONS = {
  items: [
    {
      id: 'current-session',
      created_at: '2026-11-18T10:00:00Z',
      last_seen_at: '2026-11-18T11:00:00Z',
      expires_at: '2026-11-25T10:00:00Z',
      user_agent: 'This laptop',
      ip_address: '192.0.2.1',
      current: true,
    },
    {
      id: 'other-session',
      created_at: '2026-11-17T10:00:00Z',
      last_seen_at: '2026-11-17T11:00:00Z',
      expires_at: '2026-11-24T10:00:00Z',
      user_agent: 'Old phone',
      ip_address: null,
      current: false,
    },
  ],
};

function renderAccount(handlers: Parameters<typeof mockApi>[0]) {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(SESSION),
    'GET /auth/sessions': jsonResponse(SESSIONS),
    ...handlers,
  });
  const store = makeStore();
  const router = createMemoryRouter(routes, { initialEntries: ['/account'] });
  render(
    <AppProviders store={store}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { fetchMock, store, router };
}

async function changePassword(current: string, next: string) {
  await userEvent.type(await screen.findByLabelText(/^Current password/), current);
  await userEvent.type(screen.getByLabelText(/^New password/), next);
  await userEvent.type(screen.getByLabelText(/^Repeat new password/), next);
  await userEvent.click(screen.getByRole('button', { name: 'Change password' }));
}

describe('AccountPage', () => {
  it('changes the password and switches to the new CSRF token', async () => {
    const renewed = { ...SESSION, csrf_token: 'csrf-after-password-change' };
    const { fetchMock, store } = renderAccount({ 'POST /auth/password/change': jsonResponse(renewed) });

    await changePassword('old password value', 'brand new passphrase');

    await waitFor(() => {
      expect(selectCsrfToken(store.getState())).toBe('csrf-after-password-change');
    });
    const request = fetchMock.mock.calls
      .map(requestOf)
      .find((item) => item.url.endsWith('/auth/password/change'));
    expect(await request!.json()).toEqual({
      current_password: 'old password value',
      new_password: 'brand new passphrase',
    });
    expect(request!.headers.get(CSRF_HEADER)).toBe(SESSION.csrf_token);
    expect(await screen.findByText(/Password changed/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Current password/)).toHaveValue('');
  });

  it('shows a wrong current password on its field', async () => {
    renderAccount({
      'POST /auth/password/change': problem(422, 'validation_error', {
        errors: [{ field: 'body.current_password', code: 'invalid_current_password', message: 'Incorrect.' }],
      }),
    });
    await changePassword('wrong one entirely', 'brand new passphrase');
    expect(await screen.findByText('Incorrect.')).toBeInTheDocument();
  });

  it('lists own sessions and ends another one with the CSRF token', async () => {
    const { fetchMock } = renderAccount({
      'DELETE /auth/sessions/other-session': new Response(null, { status: 204 }),
    });
    const rows = await screen.findAllByTestId('session-row');
    expect(rows).toHaveLength(2);
    expect(within(rows[0]!).getByText('This browser')).toBeInTheDocument();

    await userEvent.click(within(rows[1]!).getByRole('button', { name: 'End session' }));

    await waitFor(() => {
      const deletion = fetchMock.mock.calls.map(requestOf).find((item) => item.method === 'DELETE');
      expect(deletion?.headers.get(CSRF_HEADER)).toBe(SESSION.csrf_token);
    });
  });

  it('signing out the current session goes to the login page', async () => {
    let signedOut = false;
    const { router } = renderAccount({
      'DELETE /auth/sessions/current-session': () => {
        signedOut = true;
        return new Response(null, { status: 204 });
      },
      'GET /auth/me': () => (signedOut ? problem(401, 'unauthenticated') : jsonResponse(SESSION)),
    });
    const rows = await screen.findAllByTestId('session-row');
    await userEvent.click(within(rows[0]!).getByRole('button', { name: 'Sign out' }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/login');
    });
  });
});
