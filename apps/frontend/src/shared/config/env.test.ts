import { describe, expect, it } from 'vitest';

import { ConfigError, DEFAULT_API_BASE_URL, loadConfig, resolveApiBaseUrl } from './env';

const ORIGIN = 'https://kb.example.com';

describe('resolveApiBaseUrl', () => {
  it('defaults to the same-origin /api/v1', () => {
    expect(DEFAULT_API_BASE_URL).toBe('/api/v1');
    expect(resolveApiBaseUrl(undefined, ORIGIN)).toBe('https://kb.example.com/api/v1');
    expect(resolveApiBaseUrl('   ', ORIGIN)).toBe('https://kb.example.com/api/v1');
  });

  it('accepts relative paths and strips trailing slashes', () => {
    expect(resolveApiBaseUrl('/custom/api/', ORIGIN)).toBe('https://kb.example.com/custom/api');
  });

  it('accepts absolute http(s) URLs', () => {
    expect(resolveApiBaseUrl('https://api.example.com/api/v1/', ORIGIN)).toBe(
      'https://api.example.com/api/v1',
    );
    expect(resolveApiBaseUrl('HTTP://localhost:8000/api/v1', ORIGIN)).toBe('http://localhost:8000/api/v1');
  });

  it.each([
    'api/v1',
    'javascript:alert(1)',
    'ftp://example.com/api',
    '//evil.example/api',
    'https://user:pw@example.com/api',
    '/api/v1?token=x',
    '/api/v1#frag',
  ])('rejects %s', (value) => {
    expect(() => resolveApiBaseUrl(value, ORIGIN)).toThrow(ConfigError);
  });
});

describe('loadConfig', () => {
  it('builds the app configuration', () => {
    expect(loadConfig({ VITE_API_BASE_URL: '/api/v1' } as ImportMetaEnv, ORIGIN, '0.1.0-dev.3')).toEqual({
      apiBaseUrl: 'https://kb.example.com/api/v1',
      appVersion: '0.1.0-dev.3',
    });
  });
});
