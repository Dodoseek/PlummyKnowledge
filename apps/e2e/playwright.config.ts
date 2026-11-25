import { defineConfig, devices } from '@playwright/test';

/**
 * End-to-end tests against the Compose stack (compose.e2e.yaml). The global setup prepares a fresh instance
 * through the public API; the tests then drive the browser like a user. One worker: the tests share the
 * instance and its per-user rate limits.
 */
export default defineConfig({
  testDir: './tests',
  globalSetup: './tests/support/global-setup.ts',
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:5173',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
