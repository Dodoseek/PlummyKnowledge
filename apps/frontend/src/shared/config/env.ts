/**
 * Frontend runtime configuration. Only public values live here: everything in `import.meta.env` ends up in
 * the bundle that every visitor downloads.
 */

export const DEFAULT_API_BASE_URL = '/api/v1';

export interface AppConfig {
  /** Absolute URL of the API root without a trailing slash, e.g. `https://kb.example.com/api/v1`. */
  apiBaseUrl: string;
  appVersion: string;
}

export class ConfigError extends Error {
  override name = 'ConfigError';
}

/**
 * Resolves the API base URL against the page origin. Relative paths (the default, same-origin API behind
 * Nginx or the Vite proxy) and absolute http(s) URLs are accepted; anything else is rejected.
 */
export function resolveApiBaseUrl(raw: string | undefined, origin: string): string {
  const value = raw?.trim() ? raw.trim() : DEFAULT_API_BASE_URL;
  const isAbsolute = /^https?:\/\//i.test(value);
  if (!isAbsolute && !value.startsWith('/')) {
    throw new ConfigError(`VITE_API_BASE_URL must start with "/" or http(s)://, got "${value}"`);
  }
  if (value.startsWith('//')) {
    throw new ConfigError('VITE_API_BASE_URL must not be protocol-relative');
  }
  const url = new URL(value, origin);
  if (url.username || url.password || url.search || url.hash) {
    throw new ConfigError('VITE_API_BASE_URL must not contain credentials, query or fragment');
  }
  return url.toString().replace(/\/+$/, '');
}

export function loadConfig(env: ImportMetaEnv, origin: string, appVersion: string): AppConfig {
  return {
    apiBaseUrl: resolveApiBaseUrl(env.VITE_API_BASE_URL, origin),
    appVersion,
  };
}

export const config: AppConfig = loadConfig(import.meta.env, window.location.origin, __APP_VERSION__);
