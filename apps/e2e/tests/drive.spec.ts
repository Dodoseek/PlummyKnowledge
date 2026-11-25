import { expect, test, type Page } from '@playwright/test';

import {
  ADMIN_SESSION_FILE,
  FAKE_GOOGLE_URL,
  loadState,
  MEMBER_SESSION_FILE,
  type E2eState,
} from './support/api';
import { openSignedIn } from './support/fixtures';

/**
 * The Google Drive connector end to end (ADR-020) against the fake Google of compose.e2e.yaml: OAuth in the
 * browser → the actual granted access → folder picker → initial sync → search → a file deleted in Drive leaves
 * the index after the next sync. Tokens never reach the browser; a file outside the folder is never indexed.
 */

let state: E2eState;

test.beforeAll(() => {
  state = loadState();
});

async function search(page: Page, query: string): Promise<void> {
  await page.getByRole('textbox', { name: 'Query' }).fill(query);
  await page.getByRole('combobox', { name: 'Mode' }).selectOption('keyword');
  await page.getByRole('button', { name: 'Search' }).click();
}

async function issuedTokens(): Promise<string[]> {
  const response = await fetch(`${FAKE_GOOGLE_URL}/_control/tokens`);
  return ((await response.json()) as { tokens: string[] }).tokens;
}

test.describe('administrator', () => {
  test.use({ storageState: ADMIN_SESSION_FILE });

  test('connects Google Drive, syncs a folder and drops a file deleted in Drive', async ({ page }) => {
    test.setTimeout(240_000);
    const kb = state.driveKnowledgeBase;
    await openSignedIn(page, `/knowledge-bases/${kb.id}`);
    await expect(page.getByRole('heading', { name: kb.name })).toBeVisible();

    await page.getByRole('textbox', { name: /New Google Drive source/ }).fill('Sales drive');
    await page.getByRole('button', { name: 'Add source' }).click();
    await page.getByRole('button', { name: 'Connect Google account' }).click();

    // The fake consent page approves at once and Google sends the browser back to the callback.
    await expect(page).toHaveURL(new RegExp(`/knowledge-bases/${kb.id}$`));
    await expect(page.getByTestId('oauth-notice')).toContainText('“Sales drive” is connected.');
    await expect(page.getByTestId('granted-scopes')).toContainText(
      'https://www.googleapis.com/auth/drive.readonly',
    );
    await expect(page.getByText(/Choosing a folder does not limit this access/)).toBeVisible();
    await expect(page.getByText('Account: E2E Drive Bot <e2e-drive@example.com>')).toBeVisible();

    await page.getByRole('button', { name: 'Choose folder' }).click();
    await page.getByTestId('folder-picker').getByRole('button', { name: 'Choose E2E Sales' }).click();
    await expect(page.getByText('Folder: E2E Sales')).toBeVisible();

    // Choosing the folder queued the initial sync; its documents then go through the ingestion pipeline.
    await expect(page.getByTestId('latest-sync-job')).toContainText('succeeded', { timeout: 90_000 });
    await expect(page.getByTestId('latest-sync-job')).toContainText('2 added');
    const documents = page.getByRole('table');
    await expect(documents.getByRole('row', { name: /pricing\.md.*indexed/ })).toBeVisible({
      timeout: 120_000,
    });
    await expect(documents.getByRole('row', { name: /notes\.txt.*indexed/ })).toBeVisible({
      timeout: 120_000,
    });
    await expect(page.getByText('outside.txt')).toHaveCount(0);

    await search(page, 'Quillon-4412 partner discount');
    await expect(page.getByTestId('search-hit').first()).toContainText('pricing.md');
    await search(page, 'Marrowind-0937');
    await expect(page.getByText('Nothing found.')).toBeVisible();

    // No OAuth token the fake issued is in the page or in an API answer the browser gets.
    const tokens = await issuedTokens();
    expect(tokens.length).toBeGreaterThan(0);
    const listed = await (await page.request.get(`/api/v1/data-sources?knowledge_base_id=${kb.id}`)).text();
    const html = await page.content();
    for (const token of tokens) {
      expect(listed).not.toContain(token);
      expect(html).not.toContain(token);
    }

    // The file is deleted in Drive: the next sync removes the document and search no longer finds it.
    const removed = await fetch(`${FAKE_GOOGLE_URL}/_control/remove`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'pricing.md' }),
    });
    expect(removed.ok).toBe(true);
    await page.getByRole('button', { name: 'Sync now' }).click();
    await expect(page.getByTestId('latest-sync-job')).toContainText('(incremental)');
    await expect(page.getByTestId('latest-sync-job')).toContainText('1 deleted', { timeout: 90_000 });
    await expect(documents.getByText('pricing.md')).toHaveCount(0, { timeout: 30_000 });
    await search(page, 'Quillon-4412 partner discount');
    await expect(page.getByText('Nothing found.')).toBeVisible();
  });
});

test.describe('member', () => {
  test.use({ storageState: MEMBER_SESSION_FILE });

  test('cannot see the sources of a knowledge base', async ({ page }) => {
    await openSignedIn(page, '/knowledge-bases');
    const response = await page.request.get(
      `/api/v1/data-sources?knowledge_base_id=${state.driveKnowledgeBase.id}`,
    );
    expect(response.status()).toBe(403);
    const jobs = await page.request.get('/api/v1/jobs?type=sync');
    expect(jobs.status()).toBe(403);
  });
});
