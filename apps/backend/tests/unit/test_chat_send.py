"""History for the model and SSE frames of the chat stream (API_CONTRACTS.md §3, ADR-019)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from plummy.api.v1.chat.schemas import event_frame, frame, heartbeat_frame
from plummy.application.chat.send import GenerationFailed, MessageCompleted, TokenDelta, _history
from plummy.domain.chat import Message, MessageRole, MessageStatus, RunFinishReason, strip_citation_markers

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


def message(
    sequence: int, role: MessageRole, content: str, status: MessageStatus = MessageStatus.COMPLETED
) -> Message:
    return Message(
        id=uuid4(),
        conversation_id=uuid4(),
        sequence=sequence,
        role=role,
        content=content,
        status=status,
        client_message_id=uuid4() if role is MessageRole.USER else None,
        created_at=NOW,
    )


USER, ASSISTANT = MessageRole.USER, MessageRole.ASSISTANT


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("28 days [c1].", "28 days."),
        ("A [c1][c3] and B [c12]", "A and B"),
        ("[c1] first", "first"),
        ("No markers [x1] [c] [c12345]", "No markers [x1] [c] [c12345]"),
    ],
)
def test_old_citation_ids_are_removed(text: str, expected: str) -> None:
    assert strip_citation_markers(text) == expected


def test_history_keeps_answered_turns_in_order() -> None:
    messages = [
        message(1, USER, "Q1"),
        message(2, ASSISTANT, "A1 [c1]"),
        message(3, USER, "Q2"),
        message(4, ASSISTANT, "partial", MessageStatus.FAILED),
        message(5, USER, "Q3"),
        message(6, ASSISTANT, "A3", MessageStatus.STOPPED),
        message(7, USER, "Q4"),
        message(8, ASSISTANT, "[c2]"),  # nothing left once the marker is gone
    ]
    turns = [(m.role, m.content) for m in _history(messages, 10)]
    assert turns == [("user", "Q1"), ("assistant", "A1"), ("user", "Q3"), ("assistant", "A3")]


@pytest.mark.parametrize(("limit", "count"), [(0, 0), (1, 0), (2, 2), (3, 2), (4, 4), (100, 4)])
def test_history_is_cut_to_whole_turns(limit: int, count: int) -> None:
    messages = [
        message(1, USER, "Q1"),
        message(2, ASSISTANT, "A1"),
        message(3, USER, "Q2"),
        message(4, ASSISTANT, "A2"),
    ]
    turns = _history(messages, limit)
    assert len(turns) == count
    if turns:
        assert turns[0].role == "user"
        assert turns[-1].content == "A2"


def test_history_starting_with_an_answer_skips_it() -> None:
    messages = [message(4, ASSISTANT, "A2"), message(5, USER, "Q3"), message(6, ASSISTANT, "A3")]
    assert [m.content for m in _history(messages, 10)] == ["Q3", "A3"]


def parse(raw: bytes) -> tuple[str, dict[str, object]]:
    text = raw.decode()
    assert text.endswith("\n\n")
    lines = text[:-2].split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0][7:], json.loads(lines[1][6:])


def test_a_frame_keeps_newlines_inside_the_json() -> None:
    event, data = parse(event_frame(TokenDelta("line 1\n\nevent: fake\ndata: x"), correlation_id=None))
    assert event == "token"
    assert data == {"delta": "line 1\n\nevent: fake\ndata: x"}


def test_complete_and_error_frames() -> None:
    answer = uuid4()
    completed = MessageCompleted(answer, RunFinishReason.USER_CANCELLED, ("c1",), None, 3)
    assert parse(event_frame(completed, correlation_id="r")) == (
        "message_complete",
        {
            "assistant_message_id": str(answer),
            "finish_reason": "user_cancelled",
            "citation_ids": ["c1"],
            "usage": {"prompt_tokens": None, "completion_tokens": 3},
        },
    )
    failed = GenerationFailed("provider_error", "The AI provider request failed.", False)
    assert parse(event_frame(failed, correlation_id="req-1")) == (
        "error",
        {
            "code": "provider_error",
            "message": "The AI provider request failed.",
            "retryable": False,
            "correlation_id": "req-1",
        },
    )
    assert parse(heartbeat_frame(NOW)) == ("heartbeat", {"ts": "2026-11-25T12:00:00Z"})
    assert frame("x", {"a": "отпуск"}) == 'event: x\ndata: {"a":"отпуск"}\n\n'.encode()
