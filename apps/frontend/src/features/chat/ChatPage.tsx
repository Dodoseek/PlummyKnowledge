import {
  Alert,
  AlertDescription,
  AlertIcon,
  Badge,
  Box,
  Button,
  Code,
  Flex,
  FormControl,
  FormLabel,
  Heading,
  HStack,
  Link,
  Select,
  Stack,
  Text,
  Textarea,
} from '@chakra-ui/react';
import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react';
import { Link as RouterLink, useNavigate, useParams, useSearchParams } from 'react-router';

import { toApiError } from '../../shared/api/errors';
import { ApiErrorAlert } from '../../shared/components/ApiErrorAlert';
import { PageSpinner } from '../../shared/components/PageSpinner';
import { ConfirmButton } from '../admin/components';
import { useListAssistantsQuery } from '../assistants/assistantsApi';
import { AnswerMarkdown } from './AnswerMarkdown';
import {
  useDeleteConversationMutation,
  useGetConversationQuery,
  useListConversationsQuery,
  useListMessagesQuery,
  type ChatMessage,
  type Citation,
} from './chatApi';
import { CitationCards } from './CitationCards';
import { useChatTurn, type Turn } from './useChatTurn';

/** The backend limit for a message (API_CONTRACTS.md §3.1). */
const MAX_LENGTH = 8000;

const STATUS_NOTES: Partial<Record<ChatMessage['status'], { label: string; color: string }>> = {
  stopped: { label: 'Stopped', color: 'yellow' },
  failed: { label: 'Failed', color: 'red' },
  streaming: { label: 'Being written', color: 'blue' },
};

function UserBubble({ text }: { text: string }) {
  return (
    <Box
      alignSelf="flex-end"
      maxW="80%"
      bg="brand.50"
      _dark={{ bg: 'gray.700' }}
      px={4}
      py={2}
      borderRadius="lg"
      data-testid="question"
    >
      <Text whiteSpace="pre-wrap" wordBreak="break-word">
        {text}
      </Text>
    </Box>
  );
}

function AnswerBlock({
  text,
  citations,
  note,
  children,
}: {
  text: string;
  citations: Citation[];
  note?: { label: string; color: string } | undefined;
  children?: ReactNode;
}) {
  return (
    <Box alignSelf="stretch" data-testid="assistant-message">
      {text !== '' && <AnswerMarkdown text={text} citations={citations} />}
      {note !== undefined && (
        <Badge colorScheme={note.color} mt={1}>
          {note.label}
        </Badge>
      )}
      {children}
      <CitationCards citations={citations} />
    </Box>
  );
}

function PendingTurn({ turn, onStop }: { turn: Turn; onStop: () => void }) {
  const running = turn.status === 'sending' || turn.status === 'streaming';
  const note =
    turn.status === 'stopped'
      ? STATUS_NOTES.stopped
      : turn.status === 'streaming' && turn.answer === ''
        ? { label: 'Searching the knowledge bases…', color: 'gray' }
        : undefined;
  return (
    <>
      <UserBubble text={turn.question} />
      <AnswerBlock text={turn.answer} citations={turn.citations} note={note}>
        {turn.error !== null && (
          <Alert status="error" borderRadius="md" mt={2} data-testid="turn-error">
            <AlertIcon />
            <AlertDescription>
              <Text>{turn.error.message}</Text>
              {turn.error.correlationId !== undefined && (
                <Text fontSize="sm">
                  Request ID: <Code>{turn.error.correlationId}</Code>
                </Text>
              )}
            </AlertDescription>
          </Alert>
        )}
        {running && (
          <Button
            size="sm"
            variant="outline"
            mt={2}
            onClick={onStop}
            isDisabled={turn.start === null || turn.stopping}
            isLoading={turn.stopping}
            loadingText="Stopping"
          >
            Stop
          </Button>
        )}
      </AnswerBlock>
    </>
  );
}

function History({ conversationId, turn }: { conversationId: string; turn: Turn | null }) {
  const messages = useListMessagesQuery(conversationId);
  if (messages.isLoading) return <PageSpinner />;
  if (messages.isError || messages.data === undefined) {
    return <ApiErrorAlert error={toApiError(messages.error)} title="Cannot load messages" />;
  }
  // The page holds the newest messages first; the turn being answered is shown by the caller.
  const hidden = new Set(
    turn?.start !== null && turn?.start !== undefined
      ? [turn.start.user_message_id, turn.start.assistant_message_id]
      : [],
  );
  const ordered = [...messages.data.items].reverse().filter((message) => !hidden.has(message.id));
  return (
    <>
      {messages.data.next_cursor !== null && (
        <Text fontSize="sm" color="gray.500" textAlign="center">
          Only the latest messages are shown.
        </Text>
      )}
      {ordered.map((message) =>
        message.role === 'user' ? (
          <UserBubble key={message.id} text={message.content} />
        ) : (
          <AnswerBlock
            key={message.id}
            text={message.content}
            citations={message.citations}
            note={STATUS_NOTES[message.status]}
          />
        ),
      )}
    </>
  );
}

function Composer({
  disabled,
  busy,
  onSend,
}: {
  disabled: boolean;
  busy: boolean;
  onSend: (text: string) => void;
}) {
  const [text, setText] = useState('');
  const trimmed = text.trim();
  const canSend = !disabled && !busy && trimmed !== '' && text.length <= MAX_LENGTH;
  const submit = () => {
    if (!canSend) return;
    onSend(trimmed);
    setText('');
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter starts a new line; Enter while composing (IME) does neither.
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };
  return (
    <Stack spacing={2}>
      <Textarea
        aria-label="Message"
        placeholder="Ask a question about your documents"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
        }}
        onKeyDown={onKeyDown}
        isDisabled={disabled}
        rows={3}
        resize="vertical"
      />
      <HStack justify="space-between">
        <Text fontSize="xs" color={text.length > MAX_LENGTH ? 'red.500' : 'gray.500'}>
          {text.length > MAX_LENGTH
            ? `The message is too long (${text.length} of ${MAX_LENGTH} characters).`
            : 'Enter to send, Shift+Enter for a new line.'}
        </Text>
        <Button colorScheme="brand" onClick={submit} isDisabled={!canSend}>
          Send
        </Button>
      </HStack>
    </Stack>
  );
}

function ConversationList({ activeId }: { activeId: string | undefined }) {
  const navigate = useNavigate();
  const list = useListConversationsQuery({});
  const [remove, removal] = useDeleteConversationMutation();
  const active = list.data?.items.find((conversation) => conversation.id === activeId);

  return (
    <Stack spacing={3} as="aside" aria-label="Conversations" minW={0}>
      <Button as={RouterLink} to="/chat" size="sm" variant="outline">
        New chat
      </Button>
      {list.isLoading && <PageSpinner />}
      {list.isError && <ApiErrorAlert error={toApiError(list.error)} title="Cannot load conversations" />}
      {list.data?.items.length === 0 && (
        <Text fontSize="sm" color="gray.500">
          No conversations yet.
        </Text>
      )}
      <Stack spacing={1}>
        {list.data?.items.map((conversation) => (
          <Link
            key={conversation.id}
            as={RouterLink}
            to={`/chat/${conversation.id}`}
            px={2}
            py={1}
            borderRadius="md"
            fontSize="sm"
            noOfLines={1}
            {...(conversation.id === activeId && { bg: 'gray.100', _dark: { bg: 'gray.700' } })}
            aria-current={conversation.id === activeId ? 'page' : undefined}
            data-testid="conversation-link"
          >
            {conversation.title}
          </Link>
        ))}
      </Stack>
      {active !== undefined && (
        <ConfirmButton
          label="Delete conversation"
          confirmLabel="Delete for good"
          isLoading={removal.isLoading}
          onConfirm={() => {
            void remove(active.id)
              .unwrap()
              .then(() => navigate('/chat', { replace: true }));
          }}
        />
      )}
      {removal.isError && <ApiErrorAlert error={toApiError(removal.error)} title="Cannot delete" />}
    </Stack>
  );
}

function NewChatHeader({
  assistantId,
  onChange,
}: {
  assistantId: string | null;
  onChange: (id: string) => void;
}) {
  const list = useListAssistantsQuery({});
  if (list.isLoading) return <PageSpinner />;
  if (list.isError || list.data === undefined) {
    return <ApiErrorAlert error={toApiError(list.error)} title="Cannot load assistants" />;
  }
  const available = list.data.items.filter((assistant) => assistant.availability.available);
  if (available.length === 0) {
    return (
      <Text color="gray.500" data-testid="no-assistants">
        No assistant is available to you. An assistant appears once you have access to all of its knowledge
        bases.
      </Text>
    );
  }
  return (
    <FormControl maxW="sm">
      <FormLabel fontSize="sm">Assistant</FormLabel>
      <Select
        size="sm"
        value={assistantId ?? ''}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        {assistantId === null && <option value="">Choose an assistant</option>}
        {available.map((assistant) => (
          <option key={assistant.id} value={assistant.id}>
            {assistant.name}
          </option>
        ))}
      </Select>
    </FormControl>
  );
}

/**
 * The chat (API_CONTRACTS.md §3). Every message is authorized by the backend again (ADR-005); the page only
 * hides what cannot work. The answer streams over `fetch`; leaving the page or the conversation aborts it.
 */
export function ChatPage() {
  const { conversationId } = useParams();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { turn, send, stop, clear, busy } = useChatTurn();
  const assistants = useListAssistantsQuery({}, { skip: conversationId !== undefined });
  const conversation = useGetConversationQuery(conversationId ?? '', { skip: conversationId === undefined });
  const [chosen, setChosen] = useState<string | null>(searchParams.get('assistant'));
  const bottom = useRef<HTMLDivElement>(null);

  // A new chat defaults to the requested or the first available assistant.
  const available = assistants.data?.items.filter((assistant) => assistant.availability.available) ?? [];
  const assistantId = available.find((assistant) => assistant.id === chosen)?.id ?? available[0]?.id ?? null;

  // Another conversation (or a new chat) was opened: the turn of the previous one is no longer shown.
  const turnConversation = turn === null ? undefined : (turn.start?.conversation_id ?? null);
  useEffect(() => {
    if (turnConversation === undefined) return;
    if (turnConversation === null ? conversationId !== undefined : turnConversation !== conversationId) {
      clear();
    }
    // Only a change of the route matters here, not the turn starting.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [turn?.answer, turn?.status]);

  const onSend = (question: string) => {
    void send({
      conversationId: conversationId ?? null,
      assistantId,
      question,
      onStarted: (start) => {
        if (conversationId === undefined) {
          void navigate(`/chat/${start.conversation_id}`, { replace: true });
        }
      },
    });
  };

  let header;
  let unavailable = false;
  if (conversationId === undefined) {
    header = <NewChatHeader assistantId={assistantId} onChange={setChosen} />;
    unavailable = assistantId === null;
  } else if (conversation.isError) {
    return (
      <Flex gap={6} direction={{ base: 'column', md: 'row' }}>
        <Box w={{ base: 'full', md: '240px' }} flexShrink={0}>
          <ConversationList activeId={undefined} />
        </Box>
        <Box flex={1}>
          <ApiErrorAlert error={toApiError(conversation.error)} title="Cannot open the conversation" />
        </Box>
      </Flex>
    );
  } else if (conversation.data !== undefined) {
    const { assistant } = conversation.data;
    unavailable = !assistant.available;
    header = (
      <Stack spacing={1}>
        <Heading size="md" wordBreak="break-word">
          {conversation.data.title}
        </Heading>
        <Text fontSize="sm" color="gray.500">
          {assistant.name ?? 'Deleted assistant'}
        </Text>
      </Stack>
    );
  }

  const showTurn =
    turn !== null &&
    (conversationId === undefined || turn.start === null || turn.start.conversation_id === conversationId);

  return (
    <Flex gap={6} direction={{ base: 'column', md: 'row' }} align="stretch">
      <Box w={{ base: 'full', md: '240px' }} flexShrink={0}>
        <ConversationList activeId={conversationId} />
      </Box>
      <Stack spacing={4} flex={1} minW={0}>
        {header}
        <Stack spacing={4} data-testid="transcript">
          {conversationId !== undefined && (
            <History conversationId={conversationId} turn={showTurn ? turn : null} />
          )}
          {showTurn && (
            <PendingTurn
              turn={turn}
              onStop={() => {
                void stop();
              }}
            />
          )}
          <div ref={bottom} />
        </Stack>
        {conversationId !== undefined && unavailable && (
          <Alert status="warning" borderRadius="md" data-testid="assistant-unavailable">
            <AlertIcon />
            <AlertDescription>
              The assistant is not available to you any more (it was disabled or you lost access to one of its
              knowledge bases). The history stays readable; new messages cannot be sent.
            </AlertDescription>
          </Alert>
        )}
        <Composer disabled={unavailable} busy={busy} onSend={onSend} />
      </Stack>
    </Flex>
  );
}
