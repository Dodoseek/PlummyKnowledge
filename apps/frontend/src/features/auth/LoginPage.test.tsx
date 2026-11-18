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
  browserStorageDump,
} from '../../test/utils';

function renderLogin(path = '/login') {
  const store = makeStore();
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <AppProviders store={store}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { router, store };
}

async function fillAndSubmit(email: string, password: string) {
  await userEvent.type(await screen.findByLabelText(/Email/), email);
  await userEvent.type(screen.getByLabelText(/Password/), password);
  await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('LoginPage', () => {
  it('signs in, keeps the CSRF token in memory only and follows a safe next path', async () => {
    let signedIn = false;
    const fetchMock = mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': () => (signedIn ? jsonResponse(SESSION) : problem(401, 'unauthenticated')),
      'POST /auth/login': () => {
        signedIn = true;
        return jsonResponse(SESSION);
      },
      'GET /health/ready': jsonResponse({ status: 'ready' }),
    });
    const { router, store } = renderLogin('/login?next=%2F%3Ftab%3D2');

    await fillAndSubmit('admin@example.com', 'correct horse battery staple');

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/');
    });
    expect(router.state.location.search).toBe('?tab=2');
    const login = fetchMock.mock.calls.map(requestOf).find((request) => request.method === 'POST');
    expect(login).toBeDefined();
    expect(await login!.json()).toEqual({
      email: 'admin@example.com',
      password: 'correct horse battery staple',
    });
    expect(login!.credentials).toBe('include');
    expect(selectCsrfToken(store.getState())).toBe(SESSION.csrf_token);
    // Only Chakra's color-mode preference may be stored; nothing session-related.
    const stored = browserStorageDump();
    expect(stored).not.toContain(SESSION.csrf_token);
    expect(stored).not.toContain('correct horse');
    expect(document.cookie).toBe('');
  });

  it('ignores an open-redirect next parameter', async () => {
    let signedIn = false;
    mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': () => (signedIn ? jsonResponse(SESSION) : problem(401, 'unauthenticated')),
      'POST /auth/login': () => {
        signedIn = true;
        return jsonResponse(SESSION);
      },
      'GET /health/ready': jsonResponse({ status: 'ready' }),
    });
    const { router } = renderLogin('/login?next=%2F%2Fevil.example%2Fphish');
    await fillAndSubmit('admin@example.com', 'correct horse battery staple');
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/');
    });
    expect(router.state.location.search).toBe('');
  });

  it('shows one generic message for wrong credentials and clears the password', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': problem(401, 'unauthenticated'),
      'POST /auth/login': problem(401, 'invalid_credentials', { detail: 'Invalid email or password.' }),
    });
    renderLogin();
    await fillAndSubmit('nobody@example.com', 'wrong password value');
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.');
    expect(screen.getByLabelText(/Password/)).toHaveValue('');
  });

  it('explains rate limiting', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': problem(401, 'unauthenticated'),
      'POST /auth/login': problem(429, 'rate_limited'),
    });
    renderLogin();
    await fillAndSubmit('admin@example.com', 'whatever password');
    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts');
  });

  it('validates required fields without calling the API', async () => {
    const fetchMock = mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': problem(401, 'unauthenticated'),
    });
    renderLogin();
    await userEvent.click(await screen.findByRole('button', { name: 'Sign in' }));
    expect(screen.getByText('Enter your email.')).toBeInTheDocument();
    expect(fetchMock.mock.calls.map(requestOf).some((request) => request.method === 'POST')).toBe(false);
  });

  it('redirects a user who is already signed in', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': jsonResponse(SESSION),
      'GET /health/ready': jsonResponse({ status: 'ready' }),
    });
    const { router } = renderLogin();
    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/');
    });
  });
});
