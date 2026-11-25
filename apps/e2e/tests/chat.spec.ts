import { expect, test } from '@playwright/test';

import { ADMIN_SESSION_FILE, loadState, MEMBER_SESSION_FILE, type E2eState } from './support/api';
import { ask, openSignedIn, signIn } from './support/fixtures';

/**
 * The core vertical slice (CLAUDE.md §22): login → question → streamed answer → verified citation → source.
 */

// Written by the global setup, which runs after the test files are collected.
let state: E2eState;

test.beforeAll(() => {
  state = loadState();
});

test.describe('signed out', () => {
  test('signs in, gets an answer with a verified citation and opens the cited source', async ({ page }) => {
    await signIn(page, state.member.email, state.member.password);
    await page.getByRole('link', { name: 'Chat', exact: true }).click();
    await expect(page.getByRole('combobox', { name: 'Assistant' })).toHaveValue(state.hrAssistant.id);
    await ask(page, 'How early must vacation requests be filed?');

    const answer = page.getByTestId('answer');
    await expect(answer).toContainText('Vacation requests are filed two weeks ahead');
    // The conversation got its own address while the answer streamed.
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
    const card = page.getByTestId('citation-card');
    await expect(card).toHaveCount(1);
    await expect(card).toContainText('handbook.md');
    await expect(card).toContainText('Vacation');

    // The history is saved: after a reload the question and the answer come back from the API.
    await page.reload();
    await expect(page.getByTestId('question')).toContainText('How early must vacation requests be filed?');
    await expect(page.getByTestId('answer')).toContainText('two weeks ahead');

    await page
      .getByTestId('answer')
      .getByRole('link', { name: /^Source 1: handbook\.md$/ })
      .click();
    await expect(page).toHaveURL(/\/viewer\/documents\/.+\/versions\/.+\?chunk=/);
    await expect(page.getByTestId('cited-passage')).toContainText(
      'Vacation requests are filed two weeks ahead',
    );
    await expect(page.getByRole('heading', { name: 'handbook.md' })).toBeVisible();
  });
});

test.describe('member', () => {
  test.use({ storageState: MEMBER_SESSION_FILE });

  test('drops a citation the model invented', async ({ page }) => {
    await openSignedIn(page, '/chat');
    await ask(page, 'Where is the vacation policy? Please invent a second source.');
    const answer = page.getByTestId('answer');
    await expect(answer).toContainText('two weeks ahead');
    await expect(page.getByTestId('citation-card')).toHaveCount(1);
    // [c42] was never issued: the backend removed it from the stream and from the saved text.
    await expect(answer).not.toContainText('c42');
    await page.reload();
    await expect(page.getByTestId('answer')).toContainText('two weeks ahead');
    await expect(page.getByTestId('answer')).not.toContainText('c42');
  });

  test('stops an answer and keeps the partial text', async ({ page }) => {
    await openSignedIn(page, '/chat');
    await ask(page, 'Explain the remote work policy slowly, please.');
    const stop = page.getByRole('button', { name: 'Stop' });
    await expect(page.getByTestId('answer')).toContainText('According to the documents');
    await stop.click();
    await expect(page.getByText('Stopped')).toBeVisible();
    await expect(stop).toBeHidden();
    expect(await page.getByTestId('answer').innerText()).not.toContain('filler59');

    // The stopped answer is saved as such.
    await page.reload();
    await expect(page.getByText('Stopped')).toBeVisible();
    await expect(page.getByTestId('answer')).toContainText('According to the documents');
  });

  test('keeps conversations private to their owner', async ({ page, browser }) => {
    await openSignedIn(page, '/chat');
    await ask(page, 'How many remote days are allowed?');
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
    await expect(page.getByTestId('answer')).toContainText('According to the documents');
    const url = new URL(page.url()).pathname;

    // Another user, even the administrator, cannot open it: the same "does not exist" as a random ID.
    const admin = await browser.newContext({ storageState: ADMIN_SESSION_FILE });
    const other = await admin.newPage();
    await openSignedIn(other, url);
    await expect(other.getByText('Cannot open the conversation')).toBeVisible();
    await expect(other.getByTestId('answer')).toHaveCount(0);
    await admin.close();
  });
});
