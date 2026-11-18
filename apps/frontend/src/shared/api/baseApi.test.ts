import { describe, expect, it } from 'vitest';

import { makeStore } from '../../app/store/store';
import { authApi } from '../../features/auth/authApi';
import { csrfTokenReceived } from '../auth/csrfSlice';
import { jsonResponse, mockApi, problem, requestOf, SESSION } from '../../test/utils';

describe('baseApi', () => {
  it('adds the CSRF token to mutations only', async () => {
    const fetchMock = mockApi({
      'GET /auth/me': jsonResponse(SESSION),
      'POST /auth/logout': new Response(null, { status: 204 }),
    });
    const store = makeStore();
    store.dispatch(csrfTokenReceived('token-1'));

    await store.dispatch(authApi.endpoints.getSession.initiate());
    await store.dispatch(authApi.endpoints.logout.initiate());

    const [get, post] = fetchMock.mock.calls.map(requestOf);
    expect(get?.headers.get('X-CSRF-Token')).toBeNull();
    expect(post?.headers.get('X-CSRF-Token')).toBe(SESSION.csrf_token);
  });

  it('clears the CSRF token when the session is gone', async () => {
    mockApi({ 'GET /auth/me': problem(401, 'session_revoked') });
    const store = makeStore();
    store.dispatch(csrfTokenReceived('token-1'));
    await store.dispatch(authApi.endpoints.getSession.initiate());
    expect(store.getState().csrf.token).toBeNull();
  });
});
