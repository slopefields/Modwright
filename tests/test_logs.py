"""Cursor-based log reading."""

from __future__ import annotations

import pytest

from modwright.logs import read_since


@pytest.fixture()
def log_file(tmp_path):
    path = tmp_path / "LogOutput.log"

    def _write(text: str):
        path.write_text(text, encoding="utf-8")
        return path

    return _write


def test_first_read_returns_the_tail_not_the_whole_file(log_file):
    path = log_file("".join(f"line {i}\n" for i in range(500)))
    result = read_since(path, lines=5)

    assert result.content.splitlines() == [f"line {i}" for i in range(495, 500)]
    assert result.cursor == path.stat().st_size


def test_cursor_resumes_from_where_the_last_read_stopped(log_file):
    path = log_file("first\n")
    first = read_since(path)

    with path.open("a", encoding="utf-8") as handle:
        handle.write("second\n")

    second = read_since(path, since_cursor=first.cursor)
    assert "second" in second.content
    assert "first" not in second.content


def test_no_new_content_returns_empty(log_file):
    path = log_file("only\n")
    first = read_since(path)
    assert read_since(path, since_cursor=first.cursor).content == ""


def test_truncated_log_restarts_instead_of_returning_garbage(log_file):
    """BepInEx truncates its log when the game restarts, so a stale cursor can
    point past the end of the file."""
    path = log_file("a long previous session\n" * 20)
    stale = read_since(path).cursor

    path.write_text("fresh session\n", encoding="utf-8")
    result = read_since(path, since_cursor=stale)

    assert "fresh session" in result.content
    assert result.cursor == path.stat().st_size


def test_multibyte_characters_survive_a_byte_cursor(log_file):
    """The cursor is a byte offset, so the file must be read as bytes and
    decoded after slicing -- seeking a text handle by byte count is invalid."""
    path = log_file("héllo wörld ✓\n")
    result = read_since(path)
    assert "héllo wörld ✓" in result.content


def test_partial_leading_line_is_dropped_on_windowed_read(log_file):
    """The tail window cuts mid-line; a fragment must not be surfaced."""
    path = log_file("x" * 300_000 + "\ncomplete line\n")
    result = read_since(path, lines=10)
    assert not result.content.startswith("x")
