import { useCallback, useEffect, useRef, useState } from 'react';

import { useAppDispatch, useAppSelector } from '../../app/store/hooks';
import { CONVERSATIONS_TAG, MESSAGES_TAG, baseApi } from '../../shared/api/baseApi';
import { messageFor } from '../../shared/api/messages';
import { selectCsrfToken } from '../../shared/auth/csrfSlice';
import { useStopGenerationMutation, type Citation } from './chatApi';
import { ChatRequestError, streamChat, type MessageStart } from './chatStream';

export type TurnStatus = 'sending' | 'streaming' | 'completed' | 'stopped' | 'failed';

/** The question being answered right now; replaced by the saved messages once they are reloaded. */
export interface Turn {
  clientMessageId: string;
  question: string;
  start: MessageStart | null;
  answer: string;
  citations: Citation[];
  status: TurnStatus;
  /** Shown under the answer (or instead of it, when refused before the stream). */
  error: { message: string; correlationId?: string } | null;
  stopping: boolean;
}

interface SendArgs {
  conversationId: string | null;
  assistantId: string | null;
  question: string;
  /** Called once the backend saved the question, with the (possibly new) conversation. */
  onStarted?: (start: MessageStart) => void;
}

const STREAM_MESSAGES: Record<string, string> = {
  provider_timeout: 'The answer took too long and was stopped. The partial answer is kept.',
  client_disconnected: 'The connection was lost. The partial answer is kept.',
  internal_error: 'The answer could not be completed.',
};

function newId(): string {
  return crypto.randomUUID();
}

/**
 * Sends one message and follows its answer stream (API_CONTRACTS.md §3). Leaving the page aborts the request;
 * the server then stops the generation and keeps the partial answer (ADR-019 §4).
 */
export function useChatTurn() {
  const dispatch = useAppDispatch();
  const csrfToken = useAppSelector(selectCsrfToken);
  const [stopGeneration] = useStopGenerationMutation();
  const [turn, setTurn] = useState<Turn | null>(null);
  const controller = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      controller.current?.abort();
    },
    [],
  );

  const refresh = useCallback(
    (conversationId: string | null) => {
      const tags = [{ type: CONVERSATIONS_TAG, id: 'LIST' } as const];
      dispatch(
        baseApi.util.invalidateTags(
          conversationId === null
            ? tags
            : [
                ...tags,
                { type: CONVERSATIONS_TAG, id: conversationId },
                { type: MESSAGES_TAG, id: conversationId },
              ],
        ),
      );
    },
    [dispatch],
  );

  const update = (change: (current: Turn) => Turn) => {
    setTurn((current) => (current === null ? current : change(current)));
  };

  const send = useCallback(
    async ({ conversationId, assistantId, question, onStarted }: SendArgs) => {
      controller.current?.abort();
      const abort = new AbortController();
      controller.current = abort;
      const clientMessageId = newId();
      setTurn({
        clientMessageId,
        question,
        start: null,
        answer: '',
        citations: [],
        status: 'sending',
        error: null,
        stopping: false,
      });
      let saved: string | null = conversationId;
      try {
        const events = streamChat(
          {
            conversation_id: conversationId,
            assistant_id: conversationId === null ? assistantId : null,
            content: question,
            client_message_id: clientMessageId,
          },
          csrfToken,
          abort.signal,
        );
        for await (const event of events) {
          switch (event.type) {
            case 'message_start':
              saved = event.data.conversation_id;
              update((current) => ({ ...current, start: event.data, status: 'streaming' }));
              onStarted?.(event.data);
              break;
            case 'token':
              update((current) => ({ ...current, answer: current.answer + event.data.delta }));
              break;
            case 'citation':
              update((current) => ({ ...current, citations: [...current.citations, event.data] }));
              break;
            case 'message_complete':
              update((current) => ({
                ...current,
                status: event.data.finish_reason === 'user_cancelled' ? 'stopped' : 'completed',
              }));
              break;
            case 'error':
              update((current) => ({
                ...current,
                status: 'failed',
                error: {
                  message: STREAM_MESSAGES[event.data.code] ?? messageFor({ kind: 'http', ...event.data }),
                  ...(event.data.correlation_id !== null && { correlationId: event.data.correlation_id }),
                },
              }));
              break;
          }
        }
        update((current) =>
          current.status === 'streaming' || current.status === 'sending'
            ? { ...current, status: 'failed', error: { message: STREAM_MESSAGES.client_disconnected ?? '' } }
            : current,
        );
      } catch (error) {
        if (abort.signal.aborted) return; // the page went away or another message replaced this one
        if (error instanceof ChatRequestError) {
          const already = error.error.code === 'message_already_sent';
          update((current) => ({
            ...current,
            status: 'failed',
            error: already
              ? null
              : {
                  message: messageFor(error.error),
                  ...(error.error.correlationId !== undefined && {
                    correlationId: error.error.correlationId,
                  }),
                },
          }));
          if (already) setTurn(null);
        } else {
          update((current) => ({
            ...current,
            status: 'failed',
            error: { message: STREAM_MESSAGES.client_disconnected ?? '' },
          }));
        }
      } finally {
        if (controller.current === abort) controller.current = null;
        refresh(saved);
      }
    },
    [csrfToken, refresh],
  );

  const stop = useCallback(async () => {
    const runId = turn?.start?.generation_run_id;
    if (runId === undefined) return;
    update((current) => ({ ...current, stopping: true }));
    // The stream ends with message_complete (user_cancelled); a failed request just leaves it running.
    await stopGeneration(runId);
  }, [stopGeneration, turn?.start?.generation_run_id]);

  const clear = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
    setTurn(null);
  }, []);

  return { turn, send, stop, clear, busy: turn?.status === 'sending' || turn?.status === 'streaming' };
}
