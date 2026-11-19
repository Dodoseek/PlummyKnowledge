"""Knowledge base and document rules (domain/knowledge.py, domain/documents.py)."""

from __future__ import annotations

import pytest

from plummy.domain.documents import MAX_FILENAME_LENGTH, JobStatus, display_filename
from plummy.domain.knowledge import (
    InvalidKnowledgeInputError,
    KbAccessLevel,
    normalize_description,
    normalize_kb_name,
    strongest,
    upload_source_name,
)

READ, MANAGE = KbAccessLevel.READ, KbAccessLevel.MANAGE


def test_access_levels_are_ordered() -> None:
    assert MANAGE.allows(READ)
    assert MANAGE.allows(MANAGE)
    assert READ.allows(READ)
    assert not READ.allows(MANAGE)
    assert strongest([READ, MANAGE, READ]) is MANAGE
    assert strongest([READ]) is READ
    assert strongest([]) is None


@pytest.mark.parametrize(
    ("raw", "name"), [("  HR  ", "HR"), ("Human\t\nResources", "Human Resources"), ("x" * 200, "x" * 200)]
)
def test_kb_names_are_normalized(raw: str, name: str) -> None:
    assert normalize_kb_name(raw) == name


@pytest.mark.parametrize("raw", ["", "   ", "x" * 201, "a\x00b", "a\u200bb"])
def test_bad_kb_names(raw: str) -> None:
    with pytest.raises(InvalidKnowledgeInputError) as exc_info:
        normalize_kb_name(raw)
    assert (exc_info.value.field, exc_info.value.code) == ("name", "invalid_name")


def test_descriptions() -> None:
    assert normalize_description(None) is None
    assert normalize_description("   ") is None
    assert normalize_description(" line one\n\tline two ") == "line one\n\tline two"
    assert normalize_description("d" * 2000) == "d" * 2000
    for bad in ("d" * 2001, "bell\x07"):
        with pytest.raises(InvalidKnowledgeInputError):
            normalize_description(bad)


def test_upload_source_name_fits_the_column() -> None:
    assert upload_source_name("HR") == "Uploads: HR"
    assert len(upload_source_name("x" * 200)) == 200


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\Q3 plan.docx", "Q3 plan.docx"),
        ("evil\x00.pdf", "evil.pdf"),
        ("line\nbreak.txt", "linebreak.txt"),
        ("..", "document"),
        ("", "document"),
        (None, "document"),
        ("/", "document"),
        (".hidden", "hidden"),
    ],
)
def test_display_filename_is_a_safe_label(raw: str | None, shown: str) -> None:
    assert display_filename(raw) == shown


def test_long_filenames_keep_their_extension() -> None:
    shown = display_filename("a" * 400 + ".xlsx")
    assert len(shown) == MAX_FILENAME_LENGTH
    assert shown.endswith(".xlsx")
    assert len(display_filename("b" * 400)) == MAX_FILENAME_LENGTH


def test_finished_job_statuses() -> None:
    assert {status for status in JobStatus if status.finished} == {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.DEAD,
    }
