import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { describe, expect, it, vi } from 'vitest';

import { AppLayout } from '../layouts/AppLayout';
import { RouteErrorPage } from '../pages/RouteErrorPage';
import { jsonResponse, mockFetch } from '../test/utils';
import { AppProviders } from './providers/AppProviders';
import { routes } from './routes';
import { makeStore } from './store/store';

function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  return render(
    <AppProviders store={makeStore()}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
}

describe('routes', () => {
  it('renders the home page with system status and app version', async () => {
    mockFetch(jsonResponse({ status: 'ready' }));
    renderAt('/');
    expect(screen.getByRole('heading', { name: 'System status' })).toBeInTheDocument();
    expect(screen.getByTestId('app-version').textContent).toMatch(/^v\d+\.\d+\.\d+/);
    expect(await screen.findByTestId('readiness-status')).toHaveTextContent('Ready');
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
