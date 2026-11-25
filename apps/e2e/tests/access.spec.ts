import { expect, test } from '@playwright/test';

import { loadState, MEMBER_SESSION_FILE, type E2eState } from './support/api';
import { ask, chatLog, csrfToken, openSignedIn } from './support/fixtures';

/**
 * SC-01 from the browser (SECURITY_MODEL.md §18.1): a member with access to HR only never sees, opens or sends
 * to the model anything from Finance, whatever the UI or a hand-made request asks for.
 */
// Written by the global setup, which runs after the test files are collected.
let state: E2eState;

test.beforeAll(() => {
  state = loadState();
});

test.use({ storageState: MEMBER_SESSION_FILE });

test('shows only the knowledge bases and assistants the member may use', async ({ page }) => {
  await openSignedIn(page, '/knowledge-bases');
  await expect(page.getByRole('link', { name: state.hrKnowledgeBase.name })).toBeVisible();
  await expect(page.getByText(state.financeKnowledgeBase.name)).toHaveCount(0);

  await page.goto('/assistants');
  await expect(page.getByText(state.hrAssistant.name)).toBeVisible();
  await expect(page.getByText(state.financeAssistant.name)).toHaveCount(0);

  await page.goto('/chat');
  // Wait for the list: the default is the first available assistant.
  await expect(page.getByRole('combobox', { name: 'Assistant' })).toHaveValue(state.hrAssistant.id);
  const options = await page.getByRole('combobox', { name: 'Assistant' }).locator('option').allInnerTexts();
  expect(options).toContain(state.hrAssistant.name);
  expect(options).not.toContain(state.financeAssistant.name);
});

test('never sends a Finance document to the model', async ({ page }) => {
  await openSignedIn(page, '/chat');
  await ask(page, `What is the budget code ${state.financeSecret} of the salary review in March?`);
  const answer = page.getByTestId('answer');
  await expect(answer).toContainText('According to the documents');
  await expect(page.getByTestId('citation-card')).toContainText('handbook.md');
  await expect(page.getByText('salaries.md')).toHaveCount(0);

  // The question itself names the secret; the Finance passage around it must never be in a prompt.
  const prompts = await chatLog();
  // Positive control: the log does hold what the model received (the HR handbook).
  expect(prompts.some((prompt) => prompt.includes('Vacation requests are filed'))).toBe(true);
  for (const prompt of prompts) {
    expect(prompt).not.toContain('internal budget code is');
    expect(prompt).not.toContain('must stay confidential');
  }
});

test('refuses a hand-made request to the Finance assistant', async ({ page }) => {
  await openSignedIn(page, '/chat');
  const response = await page.request.post('/api/v1/chat/stream', {
    headers: { 'X-CSRF-Token': await csrfToken(page), Origin: new URL(page.url()).origin },
    data: {
      conversation_id: null,
      assistant_id: state.financeAssistant.id,
      content: 'What is the salary review budget code?',
      client_message_id: crypto.randomUUID(),
    },
  });
  // An assistant the member may not use does not exist for them (API_CONTRACTS.md §2.14).
  expect(response.status()).toBe(404);
  expect(((await response.json()) as { code: string }).code).toBe('not_found');

  // Knowledge base IDs are not accepted from the client (ADR-005).
  const smuggled = await page.request.post('/api/v1/chat/stream', {
    headers: { 'X-CSRF-Token': await csrfToken(page), Origin: new URL(page.url()).origin },
    data: {
      conversation_id: null,
      assistant_id: state.hrAssistant.id,
      knowledge_base_ids: [state.financeKnowledgeBase.id],
      content: 'Salary review?',
      client_message_id: crypto.randomUUID(),
    },
  });
  expect(smuggled.status()).toBe(422);
});

test('does not open a Finance passage in the source viewer', async ({ page }) => {
  const { documentId, versionId, chunkId } = state.financeChunk;
  await openSignedIn(page, '/');
  await page.goto(`/viewer/documents/${documentId}/versions/${versionId}?chunk=${chunkId}`);
  await expect(page.getByTestId('source-unavailable')).toBeVisible();
  await expect(page.getByText('confidential')).toHaveCount(0);

  const direct = await page.request.get(
    `/api/v1/documents/${documentId}/versions/${versionId}/chunks/${chunkId}/view`,
  );
  expect(direct.status()).toBe(404);
});
