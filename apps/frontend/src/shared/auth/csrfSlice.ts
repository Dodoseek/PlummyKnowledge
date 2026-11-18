import { createSlice, type PayloadAction } from '@reduxjs/toolkit';

/**
 * CSRF token of the current session (ADR-013 §2). Kept only in memory: never in localStorage, sessionStorage
 * or a readable cookie. After a page reload it comes back from `GET /auth/me`.
 *
 * The session token itself is an HttpOnly cookie: JavaScript can neither read nor store it.
 */
export interface CsrfState {
  token: string | null;
}

const initialState: CsrfState = { token: null };

export const csrfSlice = createSlice({
  name: 'csrf',
  initialState,
  reducers: {
    csrfTokenReceived(state, action: PayloadAction<string>) {
      state.token = action.payload;
    },
    csrfTokenCleared(state) {
      state.token = null;
    },
  },
});

export const { csrfTokenReceived, csrfTokenCleared } = csrfSlice.actions;

export function selectCsrfToken(state: unknown): string | null {
  const slice = (state as { csrf?: CsrfState }).csrf;
  return slice?.token ?? null;
}
