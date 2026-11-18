import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { AppLayout } from '../layouts/AppLayout';
import { RouteErrorPage } from '../pages/RouteErrorPage';
import {
  INITIALIZED,
  jsonResponse,
  mockApi,
  problem,
  requestOf,
  SESSION,
  UNINITIALIZED,
  browserStorageDump,
} from '../test/utils';
import { AppProviders } from './providers/AppProviders';
import { routes } from './routes';
import { makeStore } from './store/store';

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <AppProviders store={makeStore()}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return router;
}

describe('routes', () => {
  it('renders the home page with system status for a signed-in user', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': jsonResponse(SESSION),
      'GET /health/ready': jsonResponse({ status: 'ready' }),
    });
    renderAt('/');
    expect(await screen.findByRole('heading', { name: 'System status' })).toBeInTheDocument();
    expect(screen.getByTestId('app-version').textContent).toMatch(/^v\d+\.\d+\.\d+/);
    expect(await screen.findByTestId('readiness-status')).toHaveTextContent('Ready');
    expect(screen.getByTestId('current-user')).toHaveTextContent('First Admin');
  });

  it('sends an anonymous user to the login page and keeps the target', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': problem(401, 'unauthenticated'),
    });
    const router = renderAt('/?tab=1');
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/?tab=1')}`);
    expect(screen.queryByRole('heading', { name: 'System status' })).not.toBeInTheDocument();
  });

  it('sends everyone to setup while the instance is not initialized', async () => {
    const fetchMock = mockApi({ 'GET /setup/status': jsonResponse(UNINITIALIZED) });
    const router = renderAt('/');
    expect(await screen.findByRole('heading', { name: 'Initial setup' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/setup');
    // No session probe before setup.
    expect(fetchMock.mock.calls.map((call) => new URL(requestOf(call).url).pathname)).not.toContain(
      '/api/v1/auth/me',
    );
  });

  it('redirects from setup to login once the instance is initialized', async () => {
    mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': problem(401, 'unauthenticated'),
    });
    const router = renderAt('/setup');
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
  });

  it('shows a retryable error when the backend is unreachable', async () => {
    mockApi({ 'GET /setup/status': new TypeError('Failed to fetch') });
    renderAt('/');
    expect(await screen.findByText('Cannot load the application state')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('signs out with the CSRF token and forgets the session', async () => {
    let signedIn = true;
    const fetchMock = mockApi({
      'GET /setup/status': jsonResponse(INITIALIZED),
      'GET /auth/me': () => (signedIn ? jsonResponse(SESSION) : problem(401, 'unauthenticated')),
      'GET /health/ready': jsonResponse({ status: 'ready' }),
      'POST /auth/logout': () => {
        signedIn = false;
        return new Response(null, { status: 204 });
      },
    });
    const router = renderAt('/');
    await userEvent.click(await screen.findByRole('button', { name: 'Sign out' }));

    await waitFor(() => {
      expect(router.state.location.pathname).toBe('/login');
    });
    const logout = fetchMock.mock.calls.map(requestOf).find((request) => request.method === 'POST');
    expect(logout?.headers.get('X-CSRF-Token')).toBe(SESSION.csrf_token);
    expect(screen.queryByTestId('current-user')).not.toBeInTheDocument();
    expect(browserStorageDump()).not.toContain(SESSION.csrf_token);
  });

  it('renders the not-found page for unknown routes', () => {
    renderAt('/does/not/exist');
    expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to home page' })).toHaveAttribute('href', '/');
  });

  it('renders a generic error page without leaking error details', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const Boom = () => {
      throw new Error('secret internal detail');
    };
    const router = createMemoryRouter(
      [
        {
          path: '/',
          element: <AppLayout />,
          errorElement: <RouteErrorPage />,
          children: [{ index: true, element: <Boom /> }],
        },
      ],
      { initialEntries: ['/'] },
    );
    render(
      <AppProviders store={makeStore()}>
        <RouterProvider router={router} />
      </AppProviders>,
    );
    expect(screen.getByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument();
    expect(screen.queryByText(/secret internal detail/)).not.toBeInTheDocument();
    expect(consoleError).toHaveBeenCalled();
  });
});
