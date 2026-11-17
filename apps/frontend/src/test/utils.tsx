import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';
import { vi } from 'vitest';

import { AppProviders } from '../app/providers/AppProviders';
import { makeStore } from '../app/store/store';

export function renderWithProviders(ui: ReactElement): RenderResult {
  return render(<AppProviders store={makeStore()}>{ui}</AppProviders>);
}

export function jsonResponse(body: unknown, status = 200, contentType = 'application/json'): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': contentType } });
}

/** Replaces global fetch; each call gets the next response (or throws the next error). */
export function mockFetch(...responses: (Response | Error)[]) {
  const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => {
    const next = responses.length > 1 ? responses.shift() : responses[0];
    if (next === undefined) {
      return Promise.reject(new Error('mockFetch: no response configured'));
    }
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next.clone());
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

export function requestUrl(call: unknown[]): string {
  const [input] = call;
  if (input instanceof Request) return input.url;
  if (input instanceof URL) return input.toString();
  return String(input);
}
