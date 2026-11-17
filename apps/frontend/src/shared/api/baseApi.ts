import { createApi, fetchBaseQuery } from '@reduxjs/toolkit/query/react';

import { config } from '../config/env';

export const REQUEST_TIMEOUT_MS = 15_000;

/**
 * Single RTK Query API slice. Features add endpoints with `baseApi.injectEndpoints(...)` instead of creating
 * their own slices, so caching, middleware and error handling stay in one place.
 *
 * Authentication (Phase 3) will use HttpOnly cookies set by the backend; the frontend never stores tokens.
 */
export const baseApi = createApi({
  reducerPath: 'api',
  baseQuery: fetchBaseQuery({
    baseUrl: config.apiBaseUrl,
    credentials: 'same-origin',
    timeout: REQUEST_TIMEOUT_MS,
    prepareHeaders: (headers) => {
      headers.set('Accept', 'application/json, application/problem+json');
      return headers;
    },
  }),
  endpoints: () => ({}),
});
