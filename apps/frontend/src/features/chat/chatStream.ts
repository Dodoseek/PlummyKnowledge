import { CSRF_HEADER } from '../../shared/api/baseApi';
import { fromHttpStatus, type ApiError } from '../../shared/api/errors';
import { config } from '../../shared/config/env';
import type { Citation } from './chatApi';
import { readSse } from './sse';

/** `POST /chat/stream` body (API_CONTRACTS.md §3.1). Knowledge base IDs are never sent (ADR-005). */
export interface SendBody {
  conversation_id: string | null;
  assistant_id: string | null;
  content: string;
  client_message_id: string;
}

export interface MessageStart {
  conversation_id: string;
  user_message_id: string;
  assistant_message_id: string;
  generation_run_id: string;
  created_at: string;
}

export interface MessageComplete {
  assistant_message_id: string;
  finish_reason: 'stop' | 'length' | 'user_cancelled';
  citation_ids: string[];
  usage: { prompt_tokens: number | null; completion_tokens: number | null };
}

export interface StreamFailure {
  code: string;
  message: string;
  retryable: boolean;
  correlation_id: string | null;
}

export type ChatEvent =
  | { type: 'message_start'; data: MessageStart }
  | { type: 'token'; data: { delta: string } }
  | { type: 'citation'; data: Citation }
  | { type: 'message_complete'; data: MessageComplete }
  | { type: 'error'; data: StreamFailure };

const KNOWN = new Set(['message_start', 'token', 'citation', 'message_complete', 'error']);

/** Refused before the stream started: an ordinary problem+json response (API_CONTRACTS.md §3.1). */
export class ChatRequestError extends Error {
  override name = 'ChatRequestError';
  constructor(
    readonly error: ApiError,
    readonly body: Record<string, unknown>,
  ) {
    super(error.message);
  }
}

async function problemOf(response: Response): Promise<ChatRequestError> {
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    data = undefined;
  }
  const body = typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {};
  return new ChatRequestError(fromHttpStatus(response.status, data), body);
}

/**
 * Sends a message and yields the answer's events. Throws {@link ChatRequestError} when refused before the
 * stream, a `TypeError` when the network fails, an `AbortError` when `signal` aborts (closing the connection
 * makes the server stop the generation, ADR-019 §4). Heartbeats and unknown events are skipped.
 */
export async function* streamChat(
  body: SendBody,
  csrfToken: string | null,
  signal: AbortSignal,
): AsyncGenerator<ChatEvent> {
  const headers: Record<string, string> = {
    Accept: 'text/event-stream, application/problem+json',
    'Content-Type': 'application/json',
  };
  if (csrfToken !== null) headers[CSRF_HEADER] = csrfToken;
  const response = await fetch(`${config.apiBaseUrl}/chat/stream`, {
    method: 'POST',
    credentials: 'include',
    headers,
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) throw await problemOf(response);
  if (response.body === null || !response.headers.get('Content-Type')?.startsWith('text/event-stream')) {
    throw new ChatRequestError({ kind: 'parse', message: 'The server sent an unexpected response.' }, {});
  }
  for await (const event of readSse(response.body)) {
    if (!KNOWN.has(event.event)) continue;
    let data: unknown;
    try {
      data = JSON.parse(event.data);
    } catch {
      continue;
    }
    if (typeof data !== 'object' || data === null) continue;
    yield { type: event.event, data } as ChatEvent;
  }
}
