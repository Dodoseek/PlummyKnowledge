import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AppProviders } from '../../app/providers/AppProviders';
import { routes } from '../../app/routes';
import { makeStore } from '../../app/store/store';
import { INITIALIZED, jsonResponse, MEMBER_SESSION, mockApi, problem, requestOf } from '../../test/utils';
import { IDS } from '../chat/fixtures';

const VIEW_PATH = `/documents/${IDS.document}/versions/${IDS.version}/chunks/${IDS.chunk}/view`;
const PAGE = `/viewer/documents/${IDS.document}/versions/${IDS.version}?chunk=${IDS.chunk}`;
const HOSTILE = '<script>window.pwned = 1</script> <img src=x onerror=alert(1)> **not bold**';

const view = (overrides: { is_current?: boolean; content?: string } = {}) => ({
  document: { id: IDS.document, filename: 'report.xlsx', mime_type: 'application/vnd.ms-excel' },
  version: { id: IDS.version, version_number: 3, is_current: overrides.is_current ?? true },
  chunk: {
    id: IDS.chunk,
    content: overrides.content ?? 'Revenue grew by 12%.',
    page_start: null,
    page_end: null,
    section_path: [],
    sheet_name: 'Q3',
    row_start: 2,
    row_end: 9,
  },
  context: { before: ['Header row.', 'Notes.'], after: ['Totals.'] },
});

function renderAt(path: string, handlers: Parameters<typeof mockApi>[0]) {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(MEMBER_SESSION),
    ...handlers,
  });
  render(
    <AppProviders store={makeStore()}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />
    </AppProviders>,
  );
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('source viewer', () => {
  it('shows the cited passage between its neighbours', async () => {
    renderAt(PAGE, { [`GET ${VIEW_PATH}`]: jsonResponse(view()) });
    expect(await screen.findByTestId('cited-passage')).toHaveTextContent('Revenue grew by 12%.');
    expect(screen.getByRole('heading', { name: 'report.xlsx' })).toBeInTheDocument();
    expect(screen.getByText('sheet Q3 · rows 2–9')).toBeInTheDocument();
    const passages = screen.getAllByTestId(/passage/).map((element) => element.textContent);
    expect(passages).toEqual(['Header row.', 'Notes.', 'Revenue grew by 12%.', 'Totals.']);
    expect(screen.queryByTestId('outdated-version')).toBeNull();
  });

  it('marks a passage of a version that is no longer current', async () => {
    renderAt(PAGE, { [`GET ${VIEW_PATH}`]: jsonResponse(view({ is_current: false })) });
    expect(await screen.findByTestId('outdated-version')).toHaveTextContent('Not the current version');
  });

  it('renders document text as plain text, never as HTML or Markdown', async () => {
    renderAt(PAGE, { [`GET ${VIEW_PATH}`]: jsonResponse(view({ content: HOSTILE })) });
    const passage = await screen.findByTestId('cited-passage');
    expect(passage.textContent).toBe(HOSTILE);
    expect(passage.querySelector('script, img, strong')).toBeNull();
    expect((window as { pwned?: number }).pwned).toBeUndefined();
  });

  it('says the source is unavailable when access was revoked or the document deleted', async () => {
    renderAt(PAGE, { [`GET ${VIEW_PATH}`]: problem(404, 'not_found') });
    expect(await screen.findByTestId('source-unavailable')).toHaveTextContent(
      'you no longer have access to it',
    );
    expect(screen.queryByTestId('cited-passage')).toBeNull();
  });

  it('shows other errors with the request ID', async () => {
    renderAt(PAGE, { [`GET ${VIEW_PATH}`]: problem(503, 'service_unavailable') });
    expect(await screen.findByText('Cannot open the source')).toBeInTheDocument();
    expect(screen.getByText('req-1')).toBeInTheDocument();
  });

  it('does not request anything for a malformed link', async () => {
    const fetchMock = renderAt(`/viewer/documents/not-an-id/versions/${IDS.version}?chunk=${IDS.chunk}`, {});
    expect(await screen.findByText('The link to the source is incomplete.')).toBeInTheDocument();
    const paths = fetchMock.mock.calls.map((call) => new URL(requestOf(call).url).pathname);
    expect(paths.some((path) => path.includes('/view'))).toBe(false);
  });

  it('rejects a link without the chunk', async () => {
    const fetchMock = renderAt(`/viewer/documents/${IDS.document}/versions/${IDS.version}`, {});
    expect(await screen.findByText('The link to the source is incomplete.')).toBeInTheDocument();
    const paths = fetchMock.mock.calls.map((call) => new URL(requestOf(call).url).pathname);
    expect(paths.some((path) => path.includes('/view'))).toBe(false);
  });
});
