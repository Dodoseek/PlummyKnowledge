import { describe, expect, it } from 'vitest';

import { safeRedirectPath } from './safeRedirect';

describe('safeRedirectPath', () => {
  it.each(['/', '/kb/1', '/search?q=a', '/a#b'])('accepts same-app path %s', (path) => {
    expect(safeRedirectPath(path)).toBe(path);
  });

  it.each([
    null,
    undefined,
    '',
    'kb',
    '//evil.example',
    '/\\evil.example',
    'https://evil.example',
    'javascript:alert(1)',
    '/\u0000x',
    '/login',
    '/login?next=/',
    '/setup',
  ])('rejects %s', (path) => {
    expect(safeRedirectPath(path)).toBe('/');
  });
});
