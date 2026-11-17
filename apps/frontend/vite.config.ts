import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

const root = dirname(fileURLToPath(import.meta.url));

/**
 * The version comes from the repository VERSION file (ADR-009): `apps/frontend/../../VERSION` in the
 * repository and `/VERSION` inside the Docker image (the app root is `/app`).
 */
function readAppVersion(): string {
  try {
    const value = readFileSync(resolve(root, '../../VERSION'), 'utf8').trim();
    return /^\d+\.\d+\.\d+(-dev\.\d+)?$/.test(value) ? value : '0.0.0+unknown';
  } catch {
    return '0.0.0+unknown';
  }
}

// Dev-server-only setting (never exposed to the browser bundle): where `/api` requests are proxied.
const apiProxyTarget = process.env.API_PROXY_TARGET ?? 'http://localhost:8000';

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(readAppVersion()),
  },
  server: {
    port: 5173,
    strictPort: true,
    // Same-origin API in development, like Nginx in production: no CORS needed.
    proxy: {
      '/api': { target: apiProxyTarget, changeOrigin: false, xfwd: true },
    },
  },
  build: {
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
    unstubGlobals: true,
    css: false,
  },
});
