import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { request, type APIRequestContext, type APIResponse } from '@playwright/test';

export const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173';
export const FAKE_LLM_URL = process.env.E2E_FAKE_LLM_URL ?? 'http://fake-llm:8080';

/**
 * Test accounts of the e2e instance only. The administrator is created by the first run on a fresh stack
 * (`docker compose -p plummy-e2e`); members get a new address on every run.
 */
export const ADMIN = {
  email: 'e2e-admin@example.com',
  password: 'E2e-admin-pass-0001',
  displayName: 'E2E Admin',
};
export const MEMBER_PASSWORD = 'E2e-member-pass-0001';

/** What the global setup created, shared with the tests through a file. */
export interface E2eState {
  runId: string;
  member: { email: string; password: string; id: string };
  hrAssistant: { id: string; name: string };
  financeAssistant: { id: string; name: string };
  hrKnowledgeBase: { id: string; name: string };
  financeKnowledgeBase: { id: string; name: string };
  financeChunk: { documentId: string; versionId: string; chunkId: string };
  /** Words that exist only in the Finance document: they must never reach the model for the member. */
  financeSecret: string;
}

const STATE_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../.state');
const STATE_FILE = resolve(STATE_DIR, 'e2e.json');

/**
 * Browser sessions (cookies) saved by the global setup. Signing in once per role keeps the tests under the login
 * rate limit; one test still signs in through the login page.
 */
export const MEMBER_SESSION_FILE = resolve(STATE_DIR, 'member-session.json');
export const ADMIN_SESSION_FILE = resolve(STATE_DIR, 'admin-session.json');

export function saveState(state: E2eState): void {
  mkdirSync(dirname(STATE_FILE), { recursive: true });
  writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

export function loadState(): E2eState {
  return JSON.parse(readFileSync(STATE_FILE, 'utf8')) as E2eState;
}

/** A signed-in API client: cookie in the context, CSRF token and trusted Origin on every request. */
export class Api {
  private constructor(
    readonly context: APIRequestContext,
    private csrf: string,
  ) {}

  static async anonymous(): Promise<APIRequestContext> {
    // The backend checks Origin on state-changing requests (ADR-013): the browser would send the UI's origin.
    return request.newContext({ baseURL: BASE_URL, extraHTTPHeaders: { Origin: BASE_URL } });
  }

  static async from(context: APIRequestContext, session: APIResponse): Promise<Api> {
    await expectOk(session);
    const body = (await session.json()) as { csrf_token: string };
    return new Api(context, body.csrf_token);
  }

  static async login(email: string, password: string): Promise<Api> {
    const context = await Api.anonymous();
    return Api.from(context, await context.post('/api/v1/auth/login', { data: { email, password } }));
  }

  private headers() {
    return { 'X-CSRF-Token': this.csrf };
  }

  async get<T>(path: string): Promise<T> {
    const response = await this.context.get(`/api/v1${path}`);
    await expectOk(response);
    return (await response.json()) as T;
  }

  async send<T>(method: 'POST' | 'PUT' | 'PATCH' | 'DELETE', path: string, data?: unknown): Promise<T> {
    const response = await this.context.fetch(`/api/v1${path}`, { method, data, headers: this.headers() });
    await expectOk(response);
    return (response.status() === 204 ? undefined : await response.json()) as T;
  }

  async upload(
    knowledgeBaseId: string,
    filename: string,
    content: string,
  ): Promise<{ document: { id: string } }> {
    const response = await this.context.post('/api/v1/documents', {
      headers: this.headers(),
      // knowledge_base_id goes before the file: access is checked before the first byte is stored.
      multipart: {
        knowledge_base_id: knowledgeBaseId,
        file: { name: filename, mimeType: 'text/markdown', buffer: Buffer.from(content) },
      },
    });
    await expectOk(response);
    return (await response.json()) as { document: { id: string } };
  }

  async dispose(): Promise<void> {
    await this.context.dispose();
  }
}

export async function expectOk(response: APIResponse): Promise<void> {
  if (!response.ok()) {
    // The problem body has a code and a safe message only (no secrets): useful in the test output.
    throw new Error(`${response.url()} → ${response.status()} ${await response.text()}`);
  }
}
