import { baseApi, SESSION_TAG } from '../../shared/api/baseApi';
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

export const { useGetSessionQuery, useLoginMutation, useLogoutMutation } = authApi;
