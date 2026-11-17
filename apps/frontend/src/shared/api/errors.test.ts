import { describe, expect, it } from 'vitest';

import { toApiError } from './errors';

describe('toApiError', () => {
  it('maps backend problem+json to message, code and correlation id', () => {
    expect(
      toApiError({
        status: 404,
        data: {
          type: 'about:blank',
          title: 'Not Found',
          status: 404,
          code: 'not_found',
          detail: 'Knowledge base not found.',
          correlation_id: 'req-1',
        },
      }),
    ).toEqual({
      kind: 'http',
      status: 404,
      code: 'not_found',
      message: 'Knowledge base not found.',
      correlationId: 'req-1',
    });
  });

  it('falls back to the problem title when detail is missing', () => {
    expect(
      toApiError({ status: 500, data: { title: 'Internal Server Error', code: 'internal_error' } }),
    ).toMatchObject({ kind: 'http', status: 500, code: 'internal_error', message: 'Internal Server Error' });
  });

  it.each([
    [500, 'The server failed to process the request.'],
    [418, 'Request failed with status 418.'],
  ])('uses a generic message for status %i without a usable body', (status, message) => {
    expect(toApiError({ status, data: 'plain text body' })).toEqual({ kind: 'http', status, message });
  });

  it('ignores non-string and empty fields in the body', () => {
    const error = toApiError({ status: 400, data: { detail: '  ', code: 42, correlation_id: null } });
    expect(error).toEqual({ kind: 'http', status: 400, message: 'Request failed with status 400.' });
  });

  it('maps network failures', () => {
    expect(toApiError({ status: 'FETCH_ERROR', error: 'TypeError: Failed to fetch' })).toMatchObject({
      kind: 'network',
    });
  });

  it('maps timeouts', () => {
    expect(toApiError({ status: 'TIMEOUT_ERROR', error: 'AbortError' })).toMatchObject({ kind: 'timeout' });
  });

  it('maps unparsable responses (e.g. an HTML error page from a proxy) without exposing the body', () => {
    const error = toApiError({
      status: 'PARSING_ERROR',
      originalStatus: 502,
      data: '<html>upstream 10.0.0.5 failed</html>',
      error: 'SyntaxError',
    });
    expect(error).toEqual({
      kind: 'http',
      status: 502,
      message: 'The server failed to process the request.',
    });
    expect(JSON.stringify(error)).not.toContain('10.0.0.5');
  });

  it('maps custom and serialized errors', () => {
    expect(toApiError({ status: 'CUSTOM_ERROR', error: 'Custom failure' })).toEqual({
      kind: 'unknown',
      message: 'Custom failure',
    });
    expect(toApiError({ name: 'Error', message: 'boom' })).toEqual({ kind: 'unknown', message: 'boom' });
    expect(toApiError({})).toEqual({ kind: 'unknown', message: 'Unexpected application error.' });
  });

  it('handles a missing error', () => {
    expect(toApiError(undefined)).toEqual({ kind: 'unknown', message: 'Unknown error.' });
  });
});
