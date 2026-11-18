import { baseApi, MY_SESSIONS_TAG, SESSION_TAG } from '../../shared/api/baseApi';
import { csrfTokenCleared, csrfTokenReceived } from '../../shared/auth/csrfSlice';

/** `Session` DTO — API_CONTRACTS.md §2.2. */
export interface Session {
  user: { id: string; email: string; display_name: string };
  roles: string[];
  permissions: string[];
  session: { expires_at: string; idle_expires_at: string };
  csrf_token: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

/** One of the user's own sessions (`GET /auth/sessions`). */
export interface SessionItem {
  id: string;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  user_agent: string | null;
  ip_address: string | null;
  current: boolean;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export interface ActivateRequest {
  token: string;
  password: string;
}

interface SessionLifecycle {
  dispatch: (action: unknown) => unknown;
  queryFulfilled: Promise<{ data: Session }>;
}

/** A mutation that opened a new session: keep its CSRF token in memory and refresh the cached session. */
async function storeNewSession(_arg: unknown, { dispatch, queryFulfilled }: SessionLifecycle) {
  try {
    const { data } = await queryFulfilled;
    dispatch(csrfTokenReceived(data.csrf_token));
    dispatch(authApi.util.upsertQueryData('getSession', undefined, data));
  } catch {
    // The page shows the error; the session state is unchanged.
  }
}

export const authApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    getSession: build.query<Session, void>({
      query: () => '/auth/me',
      providesTags: [SESSION_TAG],
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          dispatch(csrfTokenReceived(data.csrf_token));
        } catch {
          dispatch(csrfTokenCleared());
        }
      },
    }),
    login: build.mutation<Session, LoginRequest>({
      query: (body) => ({ url: '/auth/login', method: 'POST', body }),
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          dispatch(csrfTokenReceived(data.csrf_token));
          void dispatch(authApi.util.upsertQueryData('getSession', undefined, data));
        } catch {
          // The page shows the error; the session state is unchanged.
        }
      },
    }),
    changePassword: build.mutation<Session, ChangePasswordRequest>({
      query: (body) => ({ url: '/auth/password/change', method: 'POST', body }),
      // Every session is revoked and this browser gets a new cookie and CSRF token.
      invalidatesTags: [MY_SESSIONS_TAG],
      onQueryStarted: storeNewSession,
    }),
    activate: build.mutation<Session, ActivateRequest>({
      query: (body) => ({ url: '/auth/activate', method: 'POST', body }),
      onQueryStarted: storeNewSession,
    }),
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    listMySessions: build.query<{ items: SessionItem[] }, void>({
      query: () => '/auth/sessions',
      providesTags: [MY_SESSIONS_TAG],
    }),
    revokeMySession: build.mutation<null, string>({
      query: (id) => ({ url: `/auth/sessions/${encodeURIComponent(id)}`, method: 'DELETE' }),
      invalidatesTags: [MY_SESSIONS_TAG],
    }),
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    logout: build.mutation<null, void>({
      query: () => ({ url: '/auth/logout', method: 'POST' }),
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        try {
          await queryFulfilled;
        } finally {
          // Forget everything cached for this user, whatever the server answered.
          dispatch(csrfTokenCleared());
          dispatch(baseApi.util.resetApiState());
        }
      },
    }),
  }),
});

export const {
  useGetSessionQuery,
  useLoginMutation,
  useLogoutMutation,
  useChangePasswordMutation,
  useActivateMutation,
  useListMySessionsQuery,
  useRevokeMySessionMutation,
} = authApi;
