import { expect, test, type Page } from '@playwright/test';

import { ADMIN_SESSION_FILE, FAKE_MICROSOFT_URL, loadState, type E2eState } from './support/api';
import { openSignedIn } from './support/fixtures';

/**
 * The Microsoft 365 connector end to end (ADR-021) against the fake Entra ID and Graph of compose.e2e.yaml:
 * OAuth in the browser → the actual Graph permissions → site → library → folder → initial sync → search → a
 * file deleted in SharePoint leaves the index after the next sync → disconnect explains how to withdraw the
 * consent. Tokens never reach the browser; a file outside the folder is never indexed.
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
  const response = await fetch(`${FAKE_MICROSOFT_URL}/_control/tokens`);
  return ((await response.json()) as { tokens: string[] }).tokens;
}

test.describe('administrator', () => {
  test.use({ storageState: ADMIN_SESSION_FILE });

  test('connects Microsoft 365, syncs a SharePoint folder and drops a deleted file', async ({ page }) => {
    test.setTimeout(240_000);
    const kb = state.microsoftKnowledgeBase;
    await openSignedIn(page, `/knowledge-bases/${kb.id}`);
    await expect(page.getByRole('heading', { name: kb.name })).toBeVisible();

    await page.getByRole('combobox', { name: 'Provider' }).selectOption('microsoft_365');
    await page.getByRole('textbox', { name: 'Source name' }).fill('Legal SharePoint');
    await page.getByRole('button', { name: 'Add source' }).click();
    await page.getByRole('button', { name: 'Connect Microsoft account' }).click();

    // The fake sign-in page approves at once and sends the browser back to the callback.
    await expect(page).toHaveURL(new RegExp(`/knowledge-bases/${kb.id}$`));
    await expect(page.getByTestId('oauth-notice')).toContainText('“Legal SharePoint” is connected.');
    const scopes = page.getByTestId('granted-scopes');
    await expect(scopes).toContainText('https://graph.microsoft.com/Files.Read.All');
    await expect(scopes).toContainText('https://graph.microsoft.com/Sites.Read.All');
    await expect(page.getByText(/every file and SharePoint site this account can read/)).toBeVisible();
    await expect(page.getByText('Account: E2E Graph Bot <e2e-graph@e2e.example>')).toBeVisible();

    await page.getByRole('button', { name: 'Choose folder' }).click();
    const picker = page.getByTestId('folder-picker');
    await expect(picker.getByRole('button', { name: 'Choose E2E Legal' })).toHaveCount(0); // a site: opened only
    await picker.getByRole('button', { name: 'Site: E2E Legal' }).click();
    await picker.getByRole('button', { name: 'Library: Documents' }).click();
    await picker.getByRole('button', { name: 'Choose Contracts' }).click();
    await expect(page.getByText('Folder: Contracts')).toBeVisible();

    await expect(page.getByTestId('latest-sync-job')).toContainText('succeeded', { timeout: 90_000 });
    await expect(page.getByTestId('latest-sync-job')).toContainText('2 added');
    const documents = page.getByRole('table');
    await expect(documents.getByRole('row', { name: /nda\.md.*indexed/ })).toBeVisible({ timeout: 120_000 });
    await expect(documents.getByRole('row', { name: /terms\.txt.*indexed/ })).toBeVisible({
      timeout: 120_000,
    });
    await expect(page.getByText('outside.txt')).toHaveCount(0);

    await search(page, 'Velmora-2281 confidentiality code');
    await expect(page.getByTestId('search-hit').first()).toContainText('nda.md');
    await search(page, 'Orrinvale-5530');
    await expect(page.getByText('Nothing found.')).toBeVisible();

    const tokens = await issuedTokens();
    expect(tokens.length).toBeGreaterThan(0);
    const listed = await (await page.request.get(`/api/v1/data-sources?knowledge_base_id=${kb.id}`)).text();
    const html = await page.content();
    for (const token of tokens) {
      expect(listed).not.toContain(token);
      expect(html).not.toContain(token);
    }

    // The file is deleted in SharePoint: the delta of the next sync removes the document.
    const removed = await fetch(`${FAKE_MICROSOFT_URL}/_control/remove`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'nda.md' }),
    });
    expect(removed.ok).toBe(true);
    await page.getByRole('button', { name: 'Sync now' }).click();
    await expect(page.getByTestId('latest-sync-job')).toContainText('(incremental)');
    await expect(page.getByTestId('latest-sync-job')).toContainText('1 deleted', { timeout: 90_000 });
    await expect(documents.getByText('nda.md')).toHaveCount(0, { timeout: 30_000 });
    await search(page, 'Velmora-2281 confidentiality code');
    await expect(page.getByText('Nothing found.')).toBeVisible();

    // Disconnecting deletes the token here; Microsoft has no endpoint to revoke it, and the UI says so.
    await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
    await page.getByRole('button', { name: 'Disconnect account' }).click();
    await expect(page.getByTestId('revoke-hint')).toContainText(
      'Microsoft has no way to revoke a single token',
    );
    await expect(page.getByText('Not connected to a Microsoft account.')).toBeVisible();
  });
});
