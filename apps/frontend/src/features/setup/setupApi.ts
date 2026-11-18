import { baseApi, SESSION_TAG, SETUP_STATUS_TAG } from '../../shared/api/baseApi';
import { csrfTokenReceived } from '../../shared/auth/csrfSlice';
import { authApi, type Session } from '../auth/authApi';

/** `GET /setup/status` — API_CONTRACTS.md §2.0. */
export interface SetupStatus {
  status: 'uninitialized' | 'initialized';
  web_setup_enabled: boolean;
}

export interface InitializeSetupRequest {
  bootstrap_token: string;
  admin: { email: string; display_name: string; password: string };
}

export const setupApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    getSetupStatus: build.query<SetupStatus, void>({
      query: () => '/setup/status',
      providesTags: [SETUP_STATUS_TAG],
    }),
    initializeSetup: build.mutation<Session, InitializeSetupRequest>({
      query: (body) => ({ url: '/setup/initialize', method: 'POST', body }),
      // 409 means someone else finished setup: the status must be refetched either way.
      invalidatesTags: (_result, error) =>
        error === undefined || ('status' in error && error.status === 409) ? [SETUP_STATUS_TAG] : [],
      async onQueryStarted(_arg, { dispatch, queryFulfilled }) {
        try {
          const { data } = await queryFulfilled;
          dispatch(csrfTokenReceived(data.csrf_token));
          void dispatch(authApi.util.upsertQueryData('getSession', undefined, data));
          dispatch(baseApi.util.invalidateTags([SESSION_TAG]));
        } catch {
          // The page shows the error.
        }
      },
    }),
  }),
});

export const { useGetSetupStatusQuery, useInitializeSetupMutation } = setupApi;
