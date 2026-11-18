/**
 * Post-login redirect target from `?next=`. Only same-app absolute paths are accepted, so the parameter
 * cannot be used as an open redirect (`//evil.example`, `https://…`, `/\evil.example`, `javascript:`).
 */
export function safeRedirectPath(next: string | null | undefined, fallback = '/'): string {
  if (!next?.startsWith('/') || next.startsWith('//')) {
    return fallback;
  }
  // Backslashes and control characters are normalized by browsers into surprising URLs.
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f\\]/.test(next)) {
    return fallback;
  }
  if (next === '/login' || next.startsWith('/login?') || next.startsWith('/setup')) {
    return fallback;
  }
  return next;
}
