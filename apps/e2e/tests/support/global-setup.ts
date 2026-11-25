import { readFileSync } from 'node:fs';

import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

import {
  ADMIN,
  ADMIN_SESSION_FILE,
  Api,
  FAKE_LLM_URL,
  MEMBER_PASSWORD,
  MEMBER_SESSION_FILE,
  saveState,
} from './api';

/**
 * Prepares the instance through the public API, as an administrator would in the UI:
 * provider (the fake OpenAI-compatible server) → models → embedding profile → two knowledge bases with one
 * Markdown document each → indexing → two assistants → a member with access to HR only.
 * Names carry a run ID, so the setup can run again on the same stack.
 */

const HANDBOOK = `# Employee handbook

## Vacation

Vacation requests are filed two weeks ahead in the HR portal. Your manager approves them within three days.

## Remote work

Remote work is allowed up to three days a week after the probation period.
`;

const FINANCE_SECRET = 'Zephyrine-7781';
const SALARIES = `# Salary review

The salary review happens in March. The internal budget code is ${FINANCE_SECRET} and must stay confidential.
`;

interface Page<T> {
  items: T[];
}
interface Id {
  id: string;
}

async function adminSession(): Promise<Api> {
  const context = await Api.anonymous();
  const status = (await (await context.get('/api/v1/setup/status')).json()) as { status: string };
  if (status.status === 'uninitialized') {
    const file = process.env.E2E_SETUP_TOKEN_FILE ?? '/bootstrap/setup-token';
    const token = readFileSync(file, 'utf8').trim();
    return Api.from(
      context,
      await context.post('/api/v1/setup/initialize', {
        data: {
          bootstrap_token: token,
          admin: { email: ADMIN.email, display_name: ADMIN.displayName, password: ADMIN.password },
        },
      }),
    );
  }
  await context.dispose();
  return Api.login(ADMIN.email, ADMIN.password);
}

async function waitIndexed(api: Api, documentId: string): Promise<void> {
  const deadline = Date.now() + 180_000;
  for (;;) {
    const document = await api.get<{ processing_status: string }>(`/documents/${documentId}`);
    if (document.processing_status === 'indexed') return;
    if (document.processing_status === 'failed') throw new Error(`document ${documentId} failed to index`);
    if (Date.now() > deadline)
      throw new Error(`document ${documentId} is still ${document.processing_status}`);
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

export default async function globalSetup(): Promise<void> {
  const fakeLlm = await fetch(`${FAKE_LLM_URL}/health`);
  if (!fakeLlm.ok) throw new Error('The fake LLM is not reachable from the test runner.');

  const runId = Date.now().toString(36);
  const api = await adminSession();
  try {
    const provider = await api.send<Id>('POST', '/providers', {
      name: `Fake LLM ${runId}`,
      provider_type: 'openai_compatible',
      base_url: `${FAKE_LLM_URL}/v1`,
      settings: { timeout_seconds: 30 },
      enabled: true,
    });
    const test = await api.send<{ status: string }>('POST', `/providers/${provider.id}/test`);
    if (test.status !== 'ok') throw new Error(`connection test failed: ${JSON.stringify(test)}`);
    const chatModel = await api.send<Id>('POST', `/providers/${provider.id}/models`, {
      model_id: 'fake-chat',
      context_window: 32000,
      max_output_tokens: 1024,
    });
    const profile = await api.send<Id>('POST', '/embedding-profiles', {
      name: `Fake embeddings ${runId}`,
      provider_id: provider.id,
      model_id: 'fake-embed',
      dimensions: 64,
      distance_metric: 'cosine',
      normalize: true,
    });

    const hrName = `HR ${runId}`;
    const financeName = `Finance ${runId}`;
    const hr = await api.send<Id>('POST', '/knowledge-bases', {
      name: hrName,
      embedding_profile_id: profile.id,
    });
    const finance = await api.send<Id>('POST', '/knowledge-bases', {
      name: financeName,
      embedding_profile_id: profile.id,
    });
    const handbook = await api.upload(hr.id, 'handbook.md', HANDBOOK);
    const salaries = await api.upload(finance.id, 'salaries.md', SALARIES);
    await waitIndexed(api, handbook.document.id);
    await waitIndexed(api, salaries.document.id);

    const assistant = (name: string, knowledgeBaseId: string) =>
      api.send<Id>('POST', '/assistants', {
        name,
        instructions: 'Answer from the documents only.',
        model_config_id: chatModel.id,
        embedding_profile_id: profile.id,
        knowledge_base_ids: [knowledgeBaseId],
      });
    const hrAssistantName = `HR helper ${runId}`;
    const financeAssistantName = `Finance helper ${runId}`;
    const hrAssistant = await assistant(hrAssistantName, hr.id);
    const financeAssistant = await assistant(financeAssistantName, finance.id);

    const email = `member-${runId}@example.com`;
    const created = await api.send<{ user: Id; activation: { url: string } }>('POST', '/users', {
      email,
      display_name: `Member ${runId}`,
      roles: ['member'],
    });
    const token = new URL(created.activation.url).hash.replace(/^#token=/, '');
    // Activation signs the member in: that session is the one the tests use.
    const anonymous = await Api.anonymous();
    await Api.from(
      anonymous,
      await anonymous.post('/api/v1/auth/activate', { data: { token, password: MEMBER_PASSWORD } }),
    );
    mkdirSync(dirname(MEMBER_SESSION_FILE), { recursive: true });
    await anonymous.storageState({ path: MEMBER_SESSION_FILE });
    await anonymous.dispose();
    await api.context.storageState({ path: ADMIN_SESSION_FILE });
    await api.send('PUT', `/knowledge-bases/${hr.id}/access`, {
      grants: [{ principal_type: 'user', principal_id: created.user.id, access_level: 'read' }],
    });

    const found = await api.send<
      Page<{ chunk_id: string; document_id: string; document_version_id: string }>
    >('POST', `/knowledge-bases/${finance.id}/search`, { query: 'salary review budget', mode: 'keyword' });
    const [chunk] = found.items;
    if (chunk === undefined) throw new Error('the Finance document was not found by keyword search');

    saveState({
      runId,
      member: { email, password: MEMBER_PASSWORD, id: created.user.id },
      hrAssistant: { id: hrAssistant.id, name: hrAssistantName },
      financeAssistant: { id: financeAssistant.id, name: financeAssistantName },
      hrKnowledgeBase: { id: hr.id, name: hrName },
      financeKnowledgeBase: { id: finance.id, name: financeName },
      financeChunk: {
        documentId: chunk.document_id,
        versionId: chunk.document_version_id,
        chunkId: chunk.chunk_id,
      },
      financeSecret: FINANCE_SECRET,
    });
  } finally {
    await api.dispose();
  }
}
