import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { AppProviders } from '../../app/providers/AppProviders';
import { routes } from '../../app/routes';
import { makeStore } from '../../app/store/store';
import { selectCsrfToken } from '../../shared/auth/csrfSlice';
import {
  browserStorageDump,
  INITIALIZED,
  jsonResponse,
  mockApi,
  problem,
  requestOf,
  SESSION,
} from '../../test/utils';

const TOKEN = 'k3Y-activation_token-value-0123456789abcdefABCDEF';
const PASSWORD = 'a long and private passphrase';

function renderAt(path: string) {
  const store = makeStore();
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <AppProviders store={store}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { router, store };
}

async function submit(password: string, confirmation = password) {
  await userEvent.type(await screen.findByLabelText(/^New password/), password);
  await userEvent.type(screen.getByLabelText(/^Repeat password/), confirmation);
  await userEvent.click(screen.getByRole('button', { name: 'Activate and sign in' }));
}

describe('ActivatePage', () => {
  it('takes the token from the fragment, removes it from the URL and signs in', async () => {
    let active = false;
    const fetchMock = mockApi({
      'POST /auth/activate': () => {
        active = true;
        return jsonResponse(SESSION);
      },
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': () => (active ? jsonResponse(SESSION) : problem(401, 'unauthenticated')),
      'GET /health/ready': jsonResponse({ status: 'ready' }),
    });
    const { router, store } = renderAt(`/activate#token=${TOKEN}`);

    await waitFor(() => {
      expect(router.state.location.hash).toBe('');
    });
    await submit(PASSWORD);

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/');
    });
    const request = fetchMock.mock.calls.map(requestOf).find((item) => item.method === 'POST');
    expect(await request!.json()).toEqual({ token: TOKEN, password: PASSWORD });
    expect(request!.credentials).toBe('include');
    expect(selectCsrfToken(store.getState())).toBe(SESSION.csrf_token);
    // Neither the token nor the password is kept anywhere in the browser or the Redux state.
    const dump = browserStorageDump();
    expect(dump).not.toContain(TOKEN);
    expect(JSON.stringify(store.getState())).not.toContain(TOKEN);
    expect(JSON.stringify(store.getState())).not.toContain(PASSWORD);
  });

  it('explains that the full link is needed when there is no token', async () => {
    const fetchMock = mockApi({});
    renderAt('/activate');
    expect(await screen.findByText(/needs the full activation link/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not send mismatching passwords', async () => {
    const fetchMock = mockApi({});
    renderAt(`/activate#token=${TOKEN}`);
    await submit(PASSWORD, `${PASSWORD}!`);
    expect(await screen.findByText('The passwords do not match.')).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows a clear message for an invalid or used link', async () => {
    mockApi({ 'POST /auth/activate': problem(400, 'activation_token_invalid') });
    renderAt(`/activate#token=${TOKEN}`);
    await submit(PASSWORD);
    expect(await screen.findByRole('alert')).toHaveTextContent(/invalid, expired or already used/);
  });

  it('shows the password policy error on the field', async () => {
    mockApi({
      'POST /auth/activate': problem(422, 'validation_error', {
        errors: [{ field: 'body.password', code: 'password_too_short', message: 'Too short.' }],
      }),
    });
    renderAt(`/activate#token=${TOKEN}`);
    await submit('short');
    expect(await screen.findByText('Too short.')).toBeInTheDocument();
  });
});
