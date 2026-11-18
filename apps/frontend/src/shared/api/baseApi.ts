import {
  createApi,
  fetchBaseQuery,
  type BaseQueryFn,
  type FetchArgs,
  type FetchBaseQueryError,
} from '@reduxjs/toolkit/query/react';

import { selectCsrfToken } from '../auth/csrfSlice';
import { config } from '../config/env';

export const REQUEST_TIMEOUT_MS = 15_000;
export const CSRF_HEADER = 'X-CSRF-Token';

/** Cache tags shared by features. */
export const SESSION_TAG = 'Session';
export const SETUP_STATUS_TAG = 'SetupStatus';

const rawBaseQuery = fetchBaseQuery({
  baseUrl: config.apiBaseUrl,
  // The session lives in an HttpOnly cookie set by the backend (ADR-013); the frontend never sees the token.
  credentials: 'include',
  timeout: REQUEST_TIMEOUT_MS,
  prepareHeaders: (headers, { getState, type }) => {
    headers.set('Accept', 'application/json, application/problem+json');
    const csrfToken = selectCsrfToken(getState());
    if (type === 'mutation' && csrfToken !== null) {
      headers.set(CSRF_HEADER, csrfToken);
    }
    return headers;
  },
});

/** Endpoints whose 401 is an expected answer, not "the session ended". */
const AUTH_PROBE_ENDPOINTS = new Set(['getSession', 'login', 'logout', 'initializeSetup']);

/**
 * A 401 from any other request means the session expired or was revoked: refetch `/auth/me`, so the route
 * guard sends the user to the login page.
 */
const baseQueryWithSession: BaseQueryFn<string | FetchArgs, unknown, FetchBaseQueryError> = async (
  args,
  api,
  extraOptions,
) => {
  const result = await rawBaseQuery(args, api, extraOptions);
  if (result.error?.status === 401 && !AUTH_PROBE_ENDPOINTS.has(api.endpoint)) {
    api.dispatch(baseApi.util.invalidateTags([SESSION_TAG]));
  }
  return result;
};

/**
 * Single RTK Query API slice. Features add endpoints with `baseApi.injectEndpoints(...)` instead of creating
 * their own slices, so caching, middleware and error handling stay in one place.
 */
export const baseApi = createApi({
  reducerPath: 'api',
  baseQuery: baseQueryWithSession,
  tagTypes: [SESSION_TAG, SETUP_STATUS_TAG],
  endpoints: () => ({}),
});
