import { baseApi } from '../../shared/api/baseApi';

export type CheckStatus = 'ok' | 'fail';

/** `GET /api/v1/health/ready` — API_CONTRACTS.md §2.1. */
export interface ReadinessResponse {
  status: 'ready' | 'not_ready';
  checks?: Record<string, CheckStatus>;
}

export const healthApi = baseApi.injectEndpoints({
  endpoints: (build) => ({
    // `void` = no query argument (RTK Query convention).
    // eslint-disable-next-line @typescript-eslint/no-invalid-void-type
    getReadiness: build.query<ReadinessResponse, void>({
      // 503 with `{"status": "not_ready"}` is a valid answer ("dependencies down"), not a transport error.
      query: () => ({
        url: '/health/ready',
        validateStatus: (response, body: unknown) =>
          response.status === 200 ||
          (response.status === 503 &&
            typeof body === 'object' &&
            body !== null &&
            (body as { status?: unknown }).status === 'not_ready'),
      }),
    }),
  }),
});

export const { useGetReadinessQuery } = healthApi;
