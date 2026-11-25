import { expect, type Page } from '@playwright/test';

import { FAKE_LLM_URL } from './api';

/** Signs in through the login page, like a user. */
export async function signIn(page: Page, email: string, password: string): Promise<void> {
  await page.goto('/login');
  await page.getByRole('textbox', { name: 'Email', exact: true }).fill(email);
  await page.getByRole('textbox', { name: 'Password', exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByTestId('current-user')).toBeVisible();
}

/** Opens the app with a saved session: the page loads the session (and its CSRF token) from /auth/me. */
export async function openSignedIn(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page.getByTestId('current-user')).toBeVisible();
}

/** Opens a new chat with an assistant and sends a question with Enter. */
export async function ask(page: Page, question: string): Promise<void> {
  const box = page.getByRole('textbox', { name: 'Message' });
  await expect(box).toBeEnabled();
  await box.fill(question);
  await box.press('Enter');
}

/** Everything the fake model has received in chat requests so far. */
export async function chatLog(): Promise<string[]> {
  const response = await fetch(`${FAKE_LLM_URL}/_chat-log`);
  const body = (await response.json()) as { requests: string[] };
  return body.requests;
}

/** The CSRF token of the page's session (kept in memory by the app; read here from /auth/me). */
export async function csrfToken(page: Page): Promise<string> {
  const session = await page.request.get('/api/v1/auth/me');
  return ((await session.json()) as { csrf_token: string }).csrf_token;
}
