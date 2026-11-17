import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { jsonResponse, mockFetch, renderWithProviders, requestUrl } from '../../test/utils';
import { SystemStatusCard } from './SystemStatusCard';

describe('SystemStatusCard', () => {
  it('requests readiness from the same-origin API and shows "Ready"', async () => {
    const fetchMock = mockFetch(jsonResponse({ status: 'ready' }));

    renderWithProviders(<SystemStatusCard />);

    expect(screen.getByRole('status')).toHaveTextContent('Checking backend');
    expect(await screen.findByTestId('readiness-status')).toHaveTextContent('Ready');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(requestUrl(fetchMock.mock.calls[0]!)).toBe(`${window.location.origin}/api/v1/health/ready`);
  });

  it('treats 503 not_ready as data and lists failing dependencies', async () => {
    mockFetch(
      jsonResponse({ status: 'not_ready', checks: { database: 'ok', migrations: 'fail', redis: 'ok' } }, 503),
    );

    renderWithProviders(<SystemStatusCard />);

    expect(await screen.findByTestId('readiness-status')).toHaveTextContent('Not ready');
    const checks = screen.getByRole('list', { name: 'Dependency checks' });
    const items = within(checks).getAllByRole('listitem');
    expect(items.map((item) => item.textContent)).toEqual([
      'PostgreSQLOK',
      'Database migrationsFailed',
      'RedisOK',
    ]);
  });

  it('shows a network error when the backend is unreachable', async () => {
    mockFetch(new TypeError('Failed to fetch'));

    renderWithProviders(<SystemStatusCard />);

    expect(await screen.findByText('Backend is unavailable')).toBeInTheDocument();
    expect(screen.getByText(/Cannot reach the server/)).toBeInTheDocument();
  });

  it('shows the backend error message and request id for unexpected errors', async () => {
    mockFetch(
      jsonResponse(
        { title: 'Internal Server Error', status: 500, code: 'internal_error', correlation_id: 'abc123' },
        500,
        'application/problem+json',
      ),
    );

    renderWithProviders(<SystemStatusCard />);

    expect(await screen.findByText('Internal Server Error')).toBeInTheDocument();
    expect(screen.getByText('abc123')).toBeInTheDocument();
  });

  it('does not treat a 503 without the readiness body as data', async () => {
    mockFetch(new Response('<html>Service Unavailable</html>', { status: 503 }));

    renderWithProviders(<SystemStatusCard />);

    expect(await screen.findByText('Backend is unavailable')).toBeInTheDocument();
    expect(screen.queryByTestId('readiness-status')).not.toBeInTheDocument();
  });

  it('refreshes on demand', async () => {
    const fetchMock = mockFetch(
      jsonResponse({ status: 'not_ready', checks: { redis: 'fail' } }, 503),
      jsonResponse({ status: 'ready' }),
    );
    renderWithProviders(<SystemStatusCard />);
    expect(await screen.findByTestId('readiness-status')).toHaveTextContent('Not ready');

    await userEvent.click(screen.getByRole('button', { name: 'Refresh' }));

    expect(await screen.findByText('Ready')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
