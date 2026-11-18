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

type Handler = Response | Error | ((init: RequestInit | undefined, request: Request) => Response | Error);

/**
 * Routes fetch calls by "METHOD /path" (path relative to the API base, e.g. "GET /auth/me").
 * Unmatched requests fail the test loudly. Returns the mock to inspect calls.
 */
export function mockApi(handlers: Record<string, Handler>) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const request = input instanceof Request ? input : new Request(String(input), init);
    const path = new URL(request.url).pathname.replace(/^\/api\/v1/, '');
    const key = `${request.method} ${path}`;
    const handler = handlers[key];
    if (handler === undefined) {
      return Promise.reject(new Error(`mockApi: unexpected request ${key}`));
    }
    const result = typeof handler === 'function' ? handler(init, request) : handler;
    return result instanceof Error ? Promise.reject(result) : Promise.resolve(result.clone());
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

export function problem(status: number, code: string, extra: Record<string, unknown> = {}): Response {
  return jsonResponse(
    { type: 'about:blank', title: 'Error', status, code, correlation_id: 'req-1', ...extra },
    status,
    'application/problem+json',
  );
}

export function requestOf(call: unknown[]): Request {
  const [input, init] = call as [RequestInfo | URL, RequestInit | undefined];
  return input instanceof Request ? input : new Request(String(input), init);
}

export const SESSION = {
  user: {
    id: '0190a000-0000-7000-8000-000000000001',
    email: 'admin@example.com',
    display_name: 'First Admin',
  },
  roles: ['admin'],
  permissions: ['chat.use', 'settings.manage'],
  session: { expires_at: '2026-11-24T12:00:00Z', idle_expires_at: '2026-11-18T12:00:00Z' },
  csrf_token: 'csrf-token-from-server',
};

export const INITIALIZED = { status: 'initialized', web_setup_enabled: true };
export const UNINITIALIZED = { status: 'uninitialized', web_setup_enabled: true };

/** Everything in localStorage and sessionStorage, to assert that no session data was persisted. */
export function browserStorageDump(): string {
  const entries: string[] = [];
  for (const storage of [window.localStorage, window.sessionStorage]) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key !== null) entries.push(`${key}=${storage.getItem(key) ?? ''}`);
    }
  }
  return entries.join('\n');
}
