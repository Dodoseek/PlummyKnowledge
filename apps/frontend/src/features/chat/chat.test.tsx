import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AppProviders } from '../../app/providers/AppProviders';
import { routes } from '../../app/routes';
import { makeStore } from '../../app/store/store';
import {
  browserStorageDump,
  INITIALIZED,
  jsonResponse,
  MEMBER_SESSION,
  mockApi,
  problem,
  requestOf,
  SESSION,
} from '../../test/utils';
import { CITATION } from './fixtures';

const ASSISTANT_ID = '0190a000-0000-7000-8000-000000000201';
const CONVERSATION_ID = '0190a000-0000-7000-8000-000000000301';
const START = {
  conversation_id: CONVERSATION_ID,
  user_message_id: '0190a000-0000-7000-8000-000000000401',
  assistant_message_id: '0190a000-0000-7000-8000-000000000402',
  generation_run_id: '0190a000-0000-7000-8000-000000000501',
  created_at: '2026-11-25T10:00:00Z',
};

const ASSISTANT = {
  id: ASSISTANT_ID,
  name: 'HR helper',
  description: null,
  availability: { available: true, reason: null },
};
const page = (items: unknown[]) => jsonResponse({ items, next_cursor: null, has_more: false });
const conversation = (available = true) => ({
  id: CONVERSATION_ID,
  title: 'Vacation rules',
  assistant: { id: ASSISTANT_ID, name: 'HR helper', available },
  created_at: '2026-11-25T10:00:00Z',
  updated_at: '2026-11-25T10:00:00Z',
  last_message_at: '2026-11-25T10:00:00Z',
});
const message = (id: string, role: 'user' | 'assistant', content: string, extra: object = {}) => ({
  id,
  sequence: role === 'user' ? 1 : 2,
  role,
  content,
  status: 'completed',
  error_code: null,
  citations: [],
  created_at: '2026-11-25T10:00:00Z',
  completed_at: '2026-11-25T10:00:01Z',
  ...extra,
});

const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const COMPLETE = {
  assistant_message_id: START.assistant_message_id,
  finish_reason: 'stop',
  citation_ids: ['c1'],
  usage: { prompt_tokens: 10, completion_tokens: 5 },
};

/** An SSE response; `push` adds frames later, `close` ends the stream. */
function sse(...frames: string[]) {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(created) {
      controller = created;
      for (const text of frames) created.enqueue(encoder.encode(text));
    },
  });
  return {
    response: new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }),
    push: (text: string) => {
      controller.enqueue(encoder.encode(text));
    },
    close: () => {
      controller.close();
    },
  };
}

function finished(...frames: string[]) {
  const stream = sse(...frames);
  stream.close();
  return stream.response;
}

const ANSWER = finished(
  frame('message_start', START),
  frame('token', { delta: 'File the request ' }),
  frame('citation', CITATION),
  frame('token', { delta: 'two weeks ahead [c1].' }),
  frame('message_complete', COMPLETE),
);

function renderAt(
  path: string,
  handlers: Parameters<typeof mockApi>[0] = {},
  session: unknown = MEMBER_SESSION,
) {
  const fetchMock = mockApi({
    'GET /setup/status': jsonResponse(INITIALIZED),
    'GET /auth/me': jsonResponse(session),
    'GET /conversations': page([]),
    'GET /assistants': page([ASSISTANT]),
    [`GET /conversations/${CONVERSATION_ID}`]: jsonResponse(conversation()),
    [`GET /conversations/${CONVERSATION_ID}/messages`]: page([]),
    ...handlers,
  });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(
    <AppProviders store={makeStore()}>
      <RouterProvider router={router} />
    </AppProviders>,
  );
  return { fetchMock, router };
}

function requests(fetchMock: ReturnType<typeof mockApi>, method: string, pathname: string): Request[] {
  return fetchMock.mock.calls
    .map((call) => requestOf(call))
    .filter((request) => request.method === method && new URL(request.url).pathname.endsWith(pathname));
}

async function ask(question: string) {
  const user = userEvent.setup();
  const box = await screen.findByRole('textbox', { name: 'Message' });
  await waitFor(() => {
    expect(box).toBeEnabled();
  });
  await user.type(box, `${question}{Enter}`);
  return user;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('chat', () => {
  it('streams an answer with a citation and opens the cited source', async () => {
    const { fetchMock, router } = renderAt('/chat', {
      'POST /chat/stream': () => ANSWER,
      [`GET /documents/${CITATION.document_id}/versions/${CITATION.document_version_id}/chunks/${CITATION.chunk_id}/view`]:
        jsonResponse({
          document: { id: CITATION.document_id, filename: 'handbook.pdf', mime_type: 'application/pdf' },
          version: { id: CITATION.document_version_id, version_number: 1, is_current: true },
          chunk: {
            id: CITATION.chunk_id,
            content: 'Vacation requests are filed two weeks ahead.',
            page_start: 4,
            page_end: 4,
            section_path: ['Vacation'],
            sheet_name: null,
            row_start: null,
            row_end: null,
          },
          context: { before: ['Leave policy.'], after: [] },
        }),
    });
    expect(await screen.findByRole('combobox')).toHaveValue(ASSISTANT_ID);
    const user = await ask('How early do I file a vacation?');

    const answer = await screen.findByTestId('answer');
    await waitFor(() => {
      expect(answer).toHaveTextContent('File the request two weeks ahead');
    });
    expect(screen.getByText('How early do I file a vacation?')).toBeInTheDocument();
    expect(screen.getByTestId('citation-card')).toHaveTextContent('handbook.pdf');
    // The new conversation gets its address without reloading the page.
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/chat/${CONVERSATION_ID}`);
    });

    const [sent] = requests(fetchMock, 'POST', '/chat/stream');
    expect(sent?.headers.get('X-CSRF-Token')).toBe(SESSION.csrf_token);
    expect(sent?.credentials).toBe('include');
    const body = (await sent?.json()) as Record<string, unknown>;
    expect(body).toEqual({
      conversation_id: null,
      assistant_id: ASSISTANT_ID,
      content: 'How early do I file a vacation?',
      client_message_id: expect.stringMatching(/^[0-9a-f-]{36}$/) as unknown,
    });
    // Knowledge bases are never chosen by the client (ADR-005).
    expect(JSON.stringify(body)).not.toMatch(/knowledge/);

    await user.click(screen.getByRole('link', { name: 'Source 1: handbook.pdf' }));
    expect(await screen.findByTestId('cited-passage')).toHaveTextContent('Vacation requests are filed');
    expect(screen.getByText('Leave policy.')).toBeInTheDocument();
    // Neither the session nor the conversation ends up in browser storage (only the color mode is there).
    const stored = browserStorageDump();
    for (const value of [SESSION.csrf_token, CONVERSATION_ID, 'vacation', 'two weeks']) {
      expect(stored.toLowerCase()).not.toContain(value.toLowerCase());
    }
  });

  it('stops a running answer and keeps the partial text', async () => {
    const stream = sse(frame('message_start', START), frame('token', { delta: 'Partial' }));
    const { fetchMock } = renderAt(`/chat/${CONVERSATION_ID}`, {
      'POST /chat/stream': () => stream.response,
      [`POST /chat/runs/${START.generation_run_id}/stop`]: () => new Response(null, { status: 202 }),
    });
    const user = await ask('Long question');
    await waitFor(() => {
      expect(screen.getByTestId('answer')).toHaveTextContent('Partial');
    });
    const sendButton = screen.getByRole('button', { name: 'Send' });
    expect(sendButton).toBeDisabled();

    await user.click(screen.getByRole('button', { name: 'Stop' }));
    await waitFor(() => {
      expect(requests(fetchMock, 'POST', `/chat/runs/${START.generation_run_id}/stop`)).toHaveLength(1);
    });
    stream.push(
      frame('message_complete', { ...COMPLETE, finish_reason: 'user_cancelled', citation_ids: [] }),
    );
    stream.close();
    expect(await screen.findByText('Stopped')).toBeInTheDocument();
    expect(screen.getByTestId('answer')).toHaveTextContent('Partial');
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    // The existing conversation is continued, not a new one started.
    const body = (await requests(fetchMock, 'POST', '/chat/stream')[0]?.json()) as Record<string, unknown>;
    expect(body.conversation_id).toBe(CONVERSATION_ID);
    expect(body.assistant_id).toBeNull();
  });

  it('keeps the partial answer and shows the request ID when the stream fails', async () => {
    renderAt(`/chat/${CONVERSATION_ID}`, {
      'POST /chat/stream': () =>
        finished(
          frame('message_start', START),
          frame('token', { delta: 'Half an answer' }),
          frame('error', {
            code: 'provider_error',
            message: 'The AI provider request failed.',
            retryable: true,
            correlation_id: 'req-42',
          }),
        ),
    });
    await ask('Question');
    const error = await screen.findByTestId('turn-error');
    expect(error).toHaveTextContent('The AI provider request failed. Try again later.');
    expect(error).toHaveTextContent('req-42');
    expect(screen.getByTestId('answer')).toHaveTextContent('Half an answer');
    // The composer is usable again.
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
  });

  it('treats a stream that ends without completion as a lost connection', async () => {
    renderAt(`/chat/${CONVERSATION_ID}`, {
      'POST /chat/stream': () => finished(frame('message_start', START), frame('token', { delta: 'Cut' })),
    });
    await ask('Question');
    expect(await screen.findByTestId('turn-error')).toHaveTextContent('The connection was lost');
  });

  it('shows the refusal when the assistant became unavailable before sending', async () => {
    const { fetchMock } = renderAt(`/chat/${CONVERSATION_ID}`, {
      'POST /chat/stream': () => problem(403, 'assistant_unavailable'),
    });
    await ask('Question');
    expect(await screen.findByTestId('turn-error')).toHaveTextContent(
      'you lost access to one of its knowledge bases',
    );
    expect(screen.queryByTestId('answer')).toBeNull();
    // The list and the history are reloaded after every attempt.
    await waitFor(() => {
      expect(requests(fetchMock, 'GET', `/conversations/${CONVERSATION_ID}/messages`).length).toBeGreaterThan(
        1,
      );
    });
  });

  it('reloads the history when the message was already sent', async () => {
    let saved = false;
    renderAt(`/chat/${CONVERSATION_ID}`, {
      'POST /chat/stream': () => {
        saved = true;
        return problem(409, 'message_already_sent', {
          user_message_id: START.user_message_id,
          assistant_message_id: START.assistant_message_id,
        });
      },
      [`GET /conversations/${CONVERSATION_ID}/messages`]: () =>
        page(
          saved
            ? [
                message(START.assistant_message_id, 'assistant', 'Saved answer'),
                message(START.user_message_id, 'user', 'Question'),
              ]
            : [],
        ),
    });
    await ask('Question');
    expect(await screen.findByText('Saved answer')).toBeInTheDocument();
    expect(screen.queryByTestId('turn-error')).toBeNull();
  });

  it('shows the history oldest first with saved citations and statuses', async () => {
    renderAt(`/chat/${CONVERSATION_ID}`, {
      [`GET /conversations/${CONVERSATION_ID}/messages`]: page([
        message('m4', 'assistant', 'Second answer', { status: 'stopped' }),
        message('m3', 'user', 'Second question'),
        message('m2', 'assistant', 'First answer [c1]', { citations: [CITATION] }),
        message('m1', 'user', 'First question'),
      ]),
    });
    const transcript = await screen.findByTestId('transcript');
    await waitFor(() => {
      expect(transcript).toHaveTextContent(/First question.*First answer.*Second question.*Second answer/);
    });
    expect(screen.getByText('Stopped')).toBeInTheDocument();
    expect(within(transcript).getAllByTestId('citation-card')).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Vacation rules' })).toBeInTheDocument();
  });

  it('keeps the history readable but blocks sending when the assistant is unavailable', async () => {
    const { fetchMock } = renderAt(`/chat/${CONVERSATION_ID}`, {
      [`GET /conversations/${CONVERSATION_ID}`]: jsonResponse(conversation(false)),
      [`GET /conversations/${CONVERSATION_ID}/messages`]: page([message('m1', 'user', 'Old question')]),
    });
    expect(await screen.findByTestId('assistant-unavailable')).toBeInTheDocument();
    expect(await screen.findByText('Old question')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(requests(fetchMock, 'POST', '/chat/stream')).toHaveLength(0);
  });

  it('shows that a foreign or deleted conversation does not exist', async () => {
    renderAt(`/chat/${CONVERSATION_ID}`, {
      [`GET /conversations/${CONVERSATION_ID}`]: problem(404, 'not_found'),
      [`GET /conversations/${CONVERSATION_ID}/messages`]: problem(404, 'not_found'),
    });
    expect(await screen.findByText('Cannot open the conversation')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Message' })).toBeNull();
  });

  it('offers no assistant when none is available', async () => {
    renderAt('/chat', {
      'GET /assistants': page([
        { ...ASSISTANT, availability: { available: false, reason: 'missing_kb_access' } },
      ]),
    });
    expect(await screen.findByTestId('no-assistants')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled();
  });

  it('aborts the request when the user leaves the conversation', async () => {
    const stream = sse(frame('message_start', START));
    let signal: AbortSignal | null | undefined;
    const { router } = renderAt(`/chat/${CONVERSATION_ID}`, {
      'POST /chat/stream': (init) => {
        signal = init?.signal;
        return stream.response;
      },
    });
    await ask('Question');
    await screen.findByRole('button', { name: 'Stop' });
    expect(signal?.aborted).toBe(false);
    await router.navigate('/assistants');
    await waitFor(() => {
      expect(signal?.aborted).toBe(true);
    });
  });

  it('sends with Enter but not with Shift+Enter, and not an empty message', async () => {
    const { fetchMock } = renderAt(`/chat/${CONVERSATION_ID}`, { 'POST /chat/stream': () => ANSWER });
    const user = userEvent.setup();
    const box = await screen.findByRole('textbox', { name: 'Message' });
    await user.type(box, '   {Enter}');
    await user.type(box, 'line one{Shift>}{Enter}{/Shift}line two');
    expect(requests(fetchMock, 'POST', '/chat/stream')).toHaveLength(0);
    expect(box).toHaveValue('   line one\nline two');
    await user.type(box, '{Enter}');
    await waitFor(() => {
      expect(requests(fetchMock, 'POST', '/chat/stream')).toHaveLength(1);
    });
    const body = (await requests(fetchMock, 'POST', '/chat/stream')[0]?.json()) as Record<string, unknown>;
    expect(body.content).toBe('line one\nline two');
  });

  it('links the chat from the menu only with chat.use', async () => {
    renderAt('/assistants', {}, { ...MEMBER_SESSION, permissions: [] });
    await screen.findByTestId('current-user');
    expect(screen.queryByRole('link', { name: 'Chat' })).toBeNull();
  });

  it('starts a chat with the assistant chosen on the assistants page', async () => {
    const { router } = renderAt('/assistants');
    const user = userEvent.setup();
    await user.click(await screen.findByRole('link', { name: 'Start chat' }));
    expect(router.state.location.pathname).toBe('/chat');
    expect(router.state.location.search).toBe(`?assistant=${ASSISTANT_ID}`);
    expect(await screen.findByRole('combobox')).toHaveValue(ASSISTANT_ID);
    expect(screen.getByRole('link', { name: 'Chat' })).toBeInTheDocument();
  });
});
