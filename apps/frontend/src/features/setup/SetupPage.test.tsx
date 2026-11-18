import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it } from 'vitest';

import { AppProviders } from '../../app/providers/AppProviders';
import { routes } from '../../app/routes';
import { makeStore } from '../../app/store/store';
import { selectCsrfToken } from '../../shared/auth/csrfSlice';
import {
  INITIALIZED,
  jsonResponse,
  mockApi,
  problem,
  requestOf,
  SESSION,
  UNINITIALIZED,
  browserStorageDump,
} from '../../test/utils';

const PASSWORD = 'correct horse battery staple';

function renderSetup() {
  const store = makeStore();
  const router = createMemoryRouter(routes, { initialEntries: ['/setup'] });
  render(
    <AppProviders store={store}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { router, store };
}

async function fill({ token = 'bootstrap-token', password = PASSWORD, confirm = PASSWORD } = {}) {
  await userEvent.type(await screen.findByLabelText(/Setup token/), token);
  await userEvent.type(screen.getByLabelText(/Administrator email/), 'admin@example.com');
  await userEvent.type(screen.getByLabelText(/^Name/), 'First Admin');
  await userEvent.type(screen.getByLabelText(/^Password/), password);
  await userEvent.type(screen.getByLabelText(/Repeat password/), confirm);
  await userEvent.click(screen.getByRole('button', { name: 'Create administrator' }));
}

describe('SetupPage', () => {
  it('creates the first administrator and opens the app', async () => {
    let initialized = false;
    const fetchMock = mockApi({
      'GET /setup/status': () => jsonResponse(initialized ? INITIALIZED : UNINITIALIZED),
      'POST /setup/initialize': () => {
        initialized = true;
        return jsonResponse(SESSION, 201);
      },
      'GET /auth/me': jsonResponse(SESSION),
      'GET /health/ready': jsonResponse({ status: 'ready' }),
    });
    const { router, store } = renderSetup();
    await fill();

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/');
    });
    const request = fetchMock.mock.calls.map(requestOf).find((r) => r.method === 'POST');
    expect(await request!.json()).toEqual({
      bootstrap_token: 'bootstrap-token',
      admin: { email: 'admin@example.com', display_name: 'First Admin', password: PASSWORD },
    });
    expect(selectCsrfToken(store.getState())).toBe(SESSION.csrf_token);
    expect(browserStorageDump()).not.toContain(SESSION.csrf_token);
  });

  it('checks the password locally before calling the API', async () => {
    const fetchMock = mockApi({ 'GET /setup/status': jsonResponse(UNINITIALIZED) });
    renderSetup();
    await fill({ password: 'short', confirm: 'different' });
    expect(screen.getByText('Use at least 12 characters.')).toBeInTheDocument();
    expect(screen.getByText('Passwords do not match.')).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(requestOf).some((r) => r.method === 'POST')).toBe(false);
  });

  it('explains an invalid or expired token', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(UNINITIALIZED),
      'POST /setup/initialize': problem(403, 'setup_token_invalid'),
    });
    renderSetup();
    await fill();
    expect(await screen.findByRole('alert')).toHaveTextContent('setup init-token --rotate');
  });

  it('shows server-side field errors next to the field', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(UNINITIALIZED),
      'POST /setup/initialize': problem(422, 'validation_error', {
        errors: [
          {
            field: 'body.admin.password',
            code: 'password_matches_email',
            message: 'Password must not be the same as the email.',
          },
        ],
      }),
    });
    renderSetup();
    await fill();
    expect(await screen.findByText('Password must not be the same as the email.')).toBeInTheDocument();
  });

  it('offers sign-in when someone else completed setup first', async () => {
    let completedElsewhere = false;
    mockApi({
      'GET /setup/status': () => jsonResponse(completedElsewhere ? INITIALIZED : UNINITIALIZED),
      'POST /setup/initialize': () => {
        completedElsewhere = true;
        return problem(409, 'setup_already_completed');
      },
      'GET /auth/me': problem(401, 'unauthenticated'),
    });
    const { router } = renderSetup();
    await fill();
    // The status is refetched after 409, and the page moves on to sign in.
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
  });

  it('tells the operator to use the CLI when web setup is disabled', async () => {
    mockApi({ 'GET /setup/status': jsonResponse({ status: 'uninitialized', web_setup_enabled: false }) });
    renderSetup();
    expect(await screen.findByText(/admin create/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create administrator' })).not.toBeInTheDocument();
  });
});
