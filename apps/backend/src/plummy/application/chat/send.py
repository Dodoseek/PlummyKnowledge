"""Sending a message and streaming the answer (API_CONTRACTS.md §3, ADR-019).

Two phases. :meth:`SendMessage.prepare` runs before any byte of the response: it authorizes the assistant and
its knowledge bases from the verified session (ADR-005), takes the ``chat`` limit and a generation slot,
saves the question and an empty ``streaming`` answer, and raises ordinary application errors. Then
:meth:`Generation.events` retrieves, streams the model's answer through the citation filter and saves the
outcome; from here on a failure is an event, not an exception.

The client sends no knowledge base IDs: they come from the assistant. The prompt, the retrieved text and
the answer are never logged; ``generation_runs`` keeps IDs, counters and timings only.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

from plummy.application.ai.ports import (
    CancellationToken,
    ChatMessage,
    ChatStreamEvent,
    Finished,
    FinishReason,
    ProviderConnection,
    ProviderError,
    ProviderGateway,
)
from plummy.application.assistants.access import ASSISTANT_NOT_FOUND, availability, require_assistant
from plummy.application.chat.conversations import CONVERSATION_NOT_FOUND
from plummy.application.errors import (
    ApplicationError,
    AssistantUnavailableError,
    ChatModelUnavailableError,
    InvalidInputError,
    MessageAlreadySentError,
    NotFoundError,
    RateLimitedError,
    ServiceUnavailableError,
    UpstreamProviderError,
)
from plummy.application.ports import (
    Clock,
    GenerationControl,
    GenerationControlUnavailableError,
    RateLimiter,
    RateLimiterUnavailableError,
    UniqueViolationError,
    UnitOfWork,
    UnitOfWorkFactory,
)
from plummy.application.providers.use_cases import connection_for
from plummy.application.retrieval.context import CitationFilter, CitationRegistry, build_chat_request
from plummy.application.retrieval.model import (
    MAX_QUERY_LENGTH,
    RetrievalScope,
    RetrievalSettings,
    SearchMode,
)
from plummy.application.retrieval.use_cases import RetrieveChunks, normalize_query, scope_of
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.security import SecurityContext
from plummy.core.ids import new_id
from plummy.domain.assistants import Assistant
from plummy.domain.chat import (
    DEFAULT_TITLE,
    Conversation,
    GenerationRun,
    InvalidChatInputError,
    Message,
    MessageCitation,
    MessageRole,
    MessageStatus,
    RunFinishReason,
    RunStatus,
    normalize_question,
    strip_citation_markers,
    title_from_question,
    viewer_route,
)
from plummy.domain.identity import Permission
from plummy.domain.knowledge import KnowledgeBase

logger = logging.getLogger(__name__)

MESSAGES_PER_MINUTE: Final = 20  # API_CONTRACTS.md §5 ``chat``
MAX_CONCURRENT_GENERATIONS: Final = 2
STOP_POLL_SECONDS: Final = 0.5  # the stop flag is checked at least this often (ADR-019 §3)
SLOT_GRACE_SECONDS: Final = 30  # a slot outlives the hard timeout by this much if its process dies
RETRY_AFTER_SECONDS: Final = 5
CLIENT_DISCONNECTED: Final = "client_disconnected"
ASSISTANT_GONE: Final = "The assistant of this conversation is no longer available."
INTERNAL_ERROR: Final = "internal_error"

# Strong references to the saves started while a stream is being torn down (asyncio keeps weak ones).
_BACKGROUND: set[asyncio.Task[None]] = set()


@dataclass(frozen=True, slots=True)
class SendCommand:
    conversation_id: UUID | None
    assistant_id: UUID | None
    content: str
    client_message_id: UUID


# --- events -----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageStarted:
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    generation_run_id: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TokenDelta:
    delta: str


@dataclass(frozen=True, slots=True)
class CitationIssued:
    citation: MessageCitation


@dataclass(frozen=True, slots=True)
class MessageCompleted:
    assistant_message_id: UUID
    finish_reason: RunFinishReason
    citation_ids: tuple[str, ...]
    prompt_tokens: int | None
    completion_tokens: int | None


@dataclass(frozen=True, slots=True)
class GenerationFailed:
    code: str
    message: str
    retryable: bool


type ChatEvent = MessageStarted | TokenDelta | CitationIssued | MessageCompleted | GenerationFailed


# --- preparation ------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Plan:
    """Everything the stream needs, resolved and authorized before it starts."""

    user_id: UUID
    conversation_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    run: GenerationRun
    created_at: datetime
    question: str
    history: tuple[ChatMessage, ...]
    scope: RetrievalScope | None  # ``None``: an assistant without knowledge bases (preview, ADR-005 §4)
    settings: RetrievalSettings
    connection: ProviderConnection
    model_id: str
    instructions: str
    max_output_tokens: int


def _question(content: str) -> str:
    try:
        return normalize_question(content)
    except InvalidChatInputError as exc:
        raise InvalidInputError(exc.field, exc.code, str(exc)) from None


def _history(messages: Sequence[Message], limit: int) -> tuple[ChatMessage, ...]:
    """Earlier turns as plain text: a question with the answer that followed it (completed or stopped, not
    empty), so roles alternate. Citation markers are removed: their IDs belonged to that generation."""
    turns: list[ChatMessage] = []
    for question, answer in itertools.pairwise(messages):
        if question.role is not MessageRole.USER or answer.role is not MessageRole.ASSISTANT:
            continue
        if answer.status not in (MessageStatus.COMPLETED, MessageStatus.STOPPED):
            continue
        text = strip_citation_markers(answer.content)
        if text:
            turns += [ChatMessage("user", question.content), ChatMessage("assistant", text)]
    keep = limit - limit % 2
    return tuple(turns[len(turns) - keep :]) if keep else ()


def _settings(assistant: Assistant) -> RetrievalSettings:
    retrieval = assistant.retrieval
    return RetrievalSettings(
        top_k=retrieval.top_k,
        vector_top_k=retrieval.vector_top_k,
        keyword_top_k=retrieval.keyword_top_k,
        rrf_k=retrieval.rrf_k,
        vector_weight=retrieval.vector_weight,
        keyword_weight=retrieval.keyword_weight,
        max_context_tokens=assistant.max_context_tokens,
    )


class SendMessage:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        retrieve: RetrieveChunks,
        gateway: ProviderGateway,
        vault: SecretVault,
        limiter: RateLimiter,
        control: GenerationControl,
        clock: Clock,
        *,
        timeout_seconds: int,
    ) -> None:
        self._uow, self._retrieve, self._gateway, self._vault = uow, retrieve, gateway, vault
        self._limiter, self._control, self._clock = limiter, control, clock
        self.timeout_seconds = timeout_seconds

    async def prepare(self, context: SecurityContext, command: SendCommand) -> Generation:
        context.require(Permission.CHAT_USE)
        question = _question(command.content)
        run_id = new_id()
        slot = False
        try:
            async with self._uow() as uow:
                sent = await uow.messages.find_sent(context.user_id, command.client_message_id)
                if sent is not None:
                    raise MessageAlreadySentError(str(sent[0].id), None if sent[1] is None else str(sent[1]))
                conversation, assistant = await self._target(uow, context, command)
                knowledge_bases = await self._knowledge_bases(uow, assistant)
                scope = await scope_of(uow, knowledge_bases) if knowledge_bases else None
                model_id, connection = await self._model(uow, assistant)
                await self._take_limit(context)
                slot = await self._take_slot(context, run_id)
                plan = await self._save_question(
                    uow,
                    context,
                    command,
                    conversation,
                    assistant,
                    question,
                    run_id,
                    scope,
                    (model_id, connection),
                )
                await uow.commit()
        except UniqueViolationError:
            # The same client message ID won a race in another request.
            if slot:
                await self._release(context.user_id, run_id)
            await self._already_sent(context.user_id, command.client_message_id)
            raise
        except BaseException:
            if slot:
                await self._release(context.user_id, run_id)
            raise
        return Generation(plan, self)

    async def _target(
        self, uow: UnitOfWork, context: SecurityContext, command: SendCommand
    ) -> tuple[Conversation | None, Assistant]:
        """ADR-005 on every message. A new conversation with an assistant the caller cannot use: ``404``
        as for a missing one. The caller's own conversation whose assistant is no longer usable:
        ``403 assistant_unavailable`` (the conversation itself is visible to its owner)."""
        if command.conversation_id is None:
            if command.assistant_id is None:
                raise InvalidInputError(
                    "assistant_id", "required", "assistant_id is required to start a conversation."
                )
            assistant, state = await require_assistant(uow, context, command.assistant_id)
            if not state.available:
                raise NotFoundError(ASSISTANT_NOT_FOUND)
            return None, assistant
        conversation = await uow.conversations.get_owned_for_update(command.conversation_id, context.user_id)
        if conversation is None:
            raise NotFoundError(CONVERSATION_NOT_FOUND)
        current = await uow.assistants.get(conversation.assistant_id)
        if current is None or not (await availability(uow, context, [current]))[current.id].available:
            raise AssistantUnavailableError(ASSISTANT_GONE)
        return conversation, current

    async def _knowledge_bases(self, uow: UnitOfWork, assistant: Assistant) -> list[KnowledgeBase]:
        """Already authorized: availability needs ``read`` on every one of them."""
        found = []
        for kb_id in assistant.knowledge_base_ids:
            kb = await uow.knowledge_bases.get(kb_id)
            if kb is None:  # deleted after the check above; the assistant is not usable as configured
                raise AssistantUnavailableError(ASSISTANT_GONE)
            found.append(kb)
        return found

    async def _model(self, uow: UnitOfWork, assistant: Assistant) -> tuple[str, ProviderConnection]:
        model = await uow.models.find(assistant.model_config_id)
        provider = None if model is None else await uow.providers.get(model.provider_id)
        if (
            model is None
            or not model.enabled
            or provider is None
            or provider.deleted_at is not None
            or not provider.enabled
        ):
            raise ChatModelUnavailableError("The chat model of the assistant is disabled or deleted.")
        return model.model_id, await connection_for(uow, self._vault, provider)

    async def _take_limit(self, context: SecurityContext) -> None:
        try:
            status = await self._limiter.hit(
                f"chat:user:{context.user_id.hex}:1m", limit=MESSAGES_PER_MINUTE, window_seconds=60
            )
        except RateLimiterUnavailableError:
            raise ServiceUnavailableError() from None
        if not status.allowed:
            raise RateLimitedError(status.retry_after_seconds)

    async def _take_slot(self, context: SecurityContext, run_id: UUID) -> bool:
        try:
            taken = await self._control.acquire(
                context.user_id,
                run_id,
                limit=MAX_CONCURRENT_GENERATIONS,
                ttl_seconds=self.timeout_seconds + SLOT_GRACE_SECONDS,
            )
        except GenerationControlUnavailableError:
            raise ServiceUnavailableError() from None
        if not taken:
            raise RateLimitedError(RETRY_AFTER_SECONDS)
        return True

    async def _save_question(
        self,
        uow: UnitOfWork,
        context: SecurityContext,
        command: SendCommand,
        conversation: Conversation | None,
        assistant: Assistant,
        question: str,
        run_id: UUID,
        scope: RetrievalScope | None,
        model: tuple[str, ProviderConnection],
    ) -> _Plan:
        now = self._clock.now()
        if conversation is None:
            conversation = Conversation(
                id=new_id(),
                user_id=context.user_id,
                assistant_id=assistant.id,
                title=title_from_question(question),
                created_at=now,
                updated_at=now,
                last_message_at=now,
            )
            await uow.conversations.add(conversation)
            sequence = 1
        else:
            sequence = await uow.messages.next_sequence(conversation.id)
            first = sequence == 1 and conversation.title == DEFAULT_TITLE
            await uow.conversations.touch(
                conversation.id, at=now, title=title_from_question(question) if first else None
            )
        earlier = await uow.messages.recent(
            conversation.id, before_sequence=sequence, limit=assistant.max_history_messages * 2 + 2
        )
        user_message = Message(
            id=new_id(),
            conversation_id=conversation.id,
            sequence=sequence,
            role=MessageRole.USER,
            content=question,
            status=MessageStatus.COMPLETED,
            client_message_id=command.client_message_id,
            created_at=now,
            completed_at=now,
        )
        answer = Message(
            id=new_id(),
            conversation_id=conversation.id,
            sequence=sequence + 1,
            role=MessageRole.ASSISTANT,
            content="",
            status=MessageStatus.STREAMING,
            client_message_id=None,
            created_at=now,
        )
        await uow.messages.add(user_message)
        await uow.messages.add(answer)
        run = GenerationRun(
            id=run_id,
            conversation_id=conversation.id,
            message_id=answer.id,
            assistant_id=assistant.id,
            model_config_id=assistant.model_config_id,
            embedding_config_id=None if scope is None else scope.profile.id,
            status=RunStatus.RUNNING,
            started_at=now,
        )
        await uow.generation_runs.add(run)
        return _Plan(
            user_id=context.user_id,
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_message_id=answer.id,
            run=run,
            created_at=now,
            question=question,
            history=_history(earlier, assistant.max_history_messages),
            scope=scope,
            settings=_settings(assistant),
            connection=model[1],
            model_id=model[0],
            instructions=assistant.instructions,
            max_output_tokens=assistant.max_output_tokens,
        )

    async def _already_sent(self, user_id: UUID, client_message_id: UUID) -> None:
        async with self._uow() as uow:
            sent = await uow.messages.find_sent(user_id, client_message_id)
        if sent is not None:
            raise MessageAlreadySentError(str(sent[0].id), None if sent[1] is None else str(sent[1]))

    async def _release(self, user_id: UUID, run_id: UUID) -> None:
        try:
            await self._control.release(user_id, run_id)
        except GenerationControlUnavailableError:
            logger.warning("generation slot not released; it expires on its own")


# --- the stream -------------------------------------------------------------------------------------------


class _FailureError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code, self.message, self.retryable = code, message, retryable


class _StopRequestedError(Exception):
    pass


_TIMEOUT_MESSAGE: Final = "The answer took too long and was stopped."
_RETRIEVAL_MESSAGE: Final = "The knowledge base could not be searched."
_PROVIDER_MESSAGE: Final = "The AI provider request failed."
_FILTERED_MESSAGE: Final = "The provider declined to generate a response."
_INTERNAL_MESSAGE: Final = "The answer could not be completed."


@dataclass(slots=True)
class _Progress:
    """What has been streamed so far; saved whatever way the stream ends."""

    registry: CitationRegistry = field(default_factory=lambda: CitationRegistry(()))
    checker: CitationFilter | None = None
    parts: list[str] = field(default_factory=list)
    saved: bool = False

    def text(self) -> str:
        return "".join(self.parts)

    def citations(self) -> list[MessageCitation]:
        if self.checker is None:
            return []
        return [_citation(self.registry, key, ordinal) for ordinal, key in enumerate(self.checker.cited, 1)]


def _citation(registry: CitationRegistry, key: str, ordinal: int) -> MessageCitation:
    cited = registry.citation(key)
    return MessageCitation(
        citation_key=key,
        ordinal=ordinal,
        document_id=cited.document_id,
        document_version_id=cited.document_version_id,
        chunk_id=cited.chunk_id,
        filename=cited.filename,
        page=cited.page,
        section=cited.section,
        sheet=cited.sheet,
        row=cited.row,
        snippet=cited.snippet,
        viewer_route=viewer_route(cited.document_id, cited.document_version_id, cited.chunk_id),
    )


def _elapsed_ms(since: float) -> int:
    return round((time.monotonic() - since) * 1000)


class Generation:
    """One prepared answer. :meth:`events` may be consumed once."""

    def __init__(self, plan: _Plan, owner: SendMessage) -> None:
        self._plan = plan
        self._owner = owner
        self.run_id = plan.run.id
        self.conversation_id = plan.conversation_id

    async def events(self) -> AsyncIterator[ChatEvent]:
        plan, run = self._plan, self._plan.run
        started = time.monotonic()
        deadline = started + self._owner.timeout_seconds
        progress = _Progress()
        cancel = CancellationToken()
        stream: _ProviderStream | None = None
        try:
            yield MessageStarted(
                plan.conversation_id,
                plan.user_message_id,
                plan.assistant_message_id,
                run.id,
                plan.created_at,
            )
            await self._retrieve(progress, deadline)
            run.retrieval_ms = _elapsed_ms(started)
            request = build_chat_request(
                model_id=plan.model_id,
                question=plan.question,
                registry=progress.registry,
                history=plan.history,
                assistant_instructions=plan.instructions,
                max_output_tokens=plan.max_output_tokens,
            )
            llm = self._owner._gateway.llm(plan.connection)
            stream = _ProviderStream(
                llm.stream_chat(request, cancel=cancel, timeout=plan.connection.timeout_seconds),
                deadline=deadline,
                stop_requested=self._stop_requested,
            )
            checker = progress.checker or CitationFilter(progress.registry)
            finished: Finished | None = None
            while finished is None:
                event = await stream.next()
                if isinstance(event, Finished):
                    finished = event
                    continue
                piece = checker.feed(event.text)
                if piece.text:
                    if run.time_to_first_token_ms is None:
                        run.time_to_first_token_ms = _elapsed_ms(started)
                    progress.parts.append(piece.text)
                    yield TokenDelta(piece.text)
                for key in piece.new_citations:
                    yield CitationIssued(_citation(progress.registry, key, checker.cited.index(key) + 1))
            await stream.close()
            tail = checker.finish().text
            if tail:
                progress.parts.append(tail)
                yield TokenDelta(tail)
            run.prompt_tokens = finished.usage.input_tokens
            run.completion_tokens = finished.usage.output_tokens
            if finished.reason is FinishReason.CONTENT_FILTER:
                raise _FailureError("provider_content_filtered", _FILTERED_MESSAGE)
            reason = (
                RunFinishReason.LENGTH if finished.reason is FinishReason.LENGTH else RunFinishReason.STOP
            )
            await self._save(progress, started, RunStatus.COMPLETED, reason, None)
            yield self._completed(progress, reason)
        except _StopRequestedError:
            cancel.cancel()
            if stream is not None:
                stream.abandon()
            tail = progress.checker.finish().text if progress.checker else ""
            if tail:
                progress.parts.append(tail)
                yield TokenDelta(tail)
            await self._save(progress, started, RunStatus.STOPPED, RunFinishReason.USER_CANCELLED, None)
            yield self._completed(progress, RunFinishReason.USER_CANCELLED)
        except Exception as exc:  # _FailureError, ProviderError, or a defect: the stream ends with an event
            cancel.cancel()
            if stream is not None:
                stream.abandon()
            failure = _as_failure(exc)
            if progress.checker is not None:
                progress.parts.append(progress.checker.finish().text)
            await self._save(progress, started, RunStatus.FAILED, RunFinishReason.ERROR, failure.code)
            yield GenerationFailed(failure.code, failure.message, failure.retryable)
        finally:
            cancel.cancel()
            if stream is not None:
                stream.abandon()
            if not progress.saved:
                # The client went away (cancellation or the generator closed): keep what was streamed.
                task = asyncio.ensure_future(
                    self._save(
                        progress,
                        started,
                        RunStatus.STOPPED,
                        RunFinishReason.USER_CANCELLED,
                        CLIENT_DISCONNECTED,
                    )
                )
                _BACKGROUND.add(task)
                task.add_done_callback(_BACKGROUND.discard)
                await asyncio.shield(task)

    async def _retrieve(self, progress: _Progress, deadline: float) -> None:
        plan = self._plan
        if plan.scope is not None:
            query = normalize_query(plan.question[:MAX_QUERY_LENGTH])
            try:
                async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                    result = await self._owner._retrieve.search(
                        plan.scope, query, plan.settings, SearchMode.HYBRID
                    )
            except TimeoutError:
                raise _FailureError("provider_timeout", _TIMEOUT_MESSAGE, retryable=True) from None
            except UpstreamProviderError as exc:
                raise _FailureError(
                    exc.code, _RETRIEVAL_MESSAGE, retryable=exc.code == "provider_timeout"
                ) from None
            except ApplicationError as exc:  # the embedding profile or its provider became unavailable
                raise _FailureError(exc.code, _RETRIEVAL_MESSAGE) from None
            progress.registry = CitationRegistry(result.chunks)
        plan.run.retrieved_chunk_ids = tuple(
            progress.registry.hit(key).chunk_id for key in progress.registry.ids
        )
        progress.checker = CitationFilter(progress.registry)

    async def _stop_requested(self) -> bool:
        try:
            return await self._owner._control.stop_requested(self.run_id)
        except GenerationControlUnavailableError:
            logger.warning("stop flag unavailable; the generation goes on")
            return False

    def _completed(self, progress: _Progress, reason: RunFinishReason) -> MessageCompleted:
        run = self._plan.run
        cited = tuple(progress.checker.cited) if progress.checker else ()
        return MessageCompleted(
            self._plan.assistant_message_id, reason, cited, run.prompt_tokens, run.completion_tokens
        )

    async def _save(
        self,
        progress: _Progress,
        started: float,
        status: RunStatus,
        reason: RunFinishReason,
        error_code: str | None,
    ) -> None:
        if progress.saved:
            return
        progress.saved = True
        plan, run = self._plan, self._plan.run
        now = self._owner._clock.now()
        run.status, run.finish_reason, run.error_code = status, reason, error_code
        run.rejected_citation_count = progress.checker.rejected if progress.checker else 0
        run.total_ms, run.finished_at = _elapsed_ms(started), now
        message_status = {
            RunStatus.COMPLETED: MessageStatus.COMPLETED,
            RunStatus.STOPPED: MessageStatus.STOPPED,
        }.get(status, MessageStatus.FAILED)
        try:
            async with self._owner._uow() as uow:
                await uow.messages.finish(
                    plan.assistant_message_id,
                    content=progress.text(),
                    status=message_status,
                    error_code=error_code if message_status is MessageStatus.FAILED else None,
                    at=now,
                    citations=progress.citations(),
                )
                await uow.generation_runs.finish(run)
                await uow.commit()
        finally:
            await self._owner._release(plan.user_id, run.id)
        logger.info(
            "generation finished",
            extra={
                "generation_run_id": str(run.id),
                "status": status.value,
                "finish_reason": reason.value,
                "error_code": error_code,
                "chunks": len(run.retrieved_chunk_ids),
                "citations": len(progress.checker.cited) if progress.checker else 0,
                "rejected_citations": run.rejected_citation_count,
                "duration_ms": run.total_ms,
            },
        )


def _as_failure(exc: BaseException) -> _FailureError:
    if isinstance(exc, _FailureError):
        return exc
    if isinstance(exc, ProviderError):
        logger.warning("generation failed", extra={"provider_error": exc.code.value})
        return _FailureError(exc.code.value, _PROVIDER_MESSAGE, retryable=exc.retryable)
    logger.error("generation failed", extra={"error_type": type(exc).__name__})
    return _FailureError(INTERNAL_ERROR, _INTERNAL_MESSAGE)


class _ProviderStream:
    """Reads the provider's events while watching the hard deadline and the stop flag: the flag is checked
    between events and at least every :data:`STOP_POLL_SECONDS` while waiting for one."""

    def __init__(
        self,
        events: AsyncIterator[ChatStreamEvent],
        *,
        deadline: float,
        stop_requested: Callable[[], Awaitable[bool]],
    ) -> None:
        self._events = events
        self._deadline = deadline
        self._stop_requested = stop_requested
        self._pending: asyncio.Future[ChatStreamEvent] | None = None
        self._checked = time.monotonic()

    async def next(self) -> ChatStreamEvent:
        if time.monotonic() - self._checked >= STOP_POLL_SECONDS:
            await self._check_stop()
        pending = asyncio.ensure_future(anext(self._events))
        self._pending = pending
        while not pending.done():
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise _FailureError("provider_timeout", _TIMEOUT_MESSAGE, retryable=True)
            await asyncio.wait({pending}, timeout=min(STOP_POLL_SECONDS, remaining))
            if not pending.done():
                await self._check_stop()
        self._pending = None
        try:
            return pending.result()
        except StopAsyncIteration:
            raise _FailureError("provider_invalid_response", _PROVIDER_MESSAGE) from None

    async def _check_stop(self) -> None:
        self._checked = time.monotonic()
        if await self._stop_requested():
            raise _StopRequestedError

    async def close(self) -> None:
        closer = getattr(self._events, "aclose", None)
        if closer is not None:
            with suppress(Exception):
                await closer()

    def abandon(self) -> None:
        """Stop reading without waiting: a read in flight is cancelled (the adapter closes its connection)."""
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
        self._pending = None
