"""Cursor-based log reading."""

from __future__ import annotations

import pytest

from modwright.logs import _MAX_CONTENT_CHARS, read_since


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


class TestRestartReporting:
    """A truncated log is the loader having started again, and the caller has
    no other way to learn it: nothing in a loader log is stamped with the
    process start time."""

    def test_truncation_is_reported_not_just_absorbed(self, log_file):
        path = log_file("a long previous session\n" * 20)
        stale = read_since(path).cursor

        path.write_text("fresh session\n", encoding="utf-8")
        assert read_since(path, since_cursor=stale).restarted is True

    def test_a_log_that_only_grew_did_not_restart(self, log_file):
        path = log_file("first\n")
        first = read_since(path)

        with path.open("a", encoding="utf-8") as handle:
            handle.write("second\n")

        assert read_since(path, since_cursor=first.cursor).restarted is False

    def test_first_read_has_no_baseline_to_have_restarted_since(self, log_file):
        path = log_file("whatever\n")
        assert read_since(path).restarted is False


class TestTheReadIsBounded:
    """A cursor read used to ignore `lines` and return everything since the
    cursor. The workflow this tool documents -- keep polling from the cursor
    `deploy_mod` handed back until the loader is seen starting -- means that
    region is a whole play session. Real profiles on the development machine
    held 77 KB, 140 KB and 1.1 MB; the last is a quarter of a million tokens
    in a single tool response."""

    def test_a_cursor_read_honours_the_line_budget(self, log_file):
        path = log_file("".join(f"line {i}\n" for i in range(5_000)))

        result = read_since(path, since_cursor=0, lines=10)

        assert result.content.splitlines() == [
            f"line {i}" for i in range(4_990, 5_000)
        ]

    def test_what_was_dropped_is_counted_not_hidden(self, log_file):
        path = log_file("".join(f"line {i}\n" for i in range(5_000)))

        result = read_since(path, since_cursor=0, lines=10)

        assert result.omitted_lines == 4_990

    def test_a_read_that_fits_reports_nothing_omitted(self, log_file):
        path = log_file("one\ntwo\n")
        assert read_since(path, since_cursor=0, lines=50).omitted_lines == 0

    def test_the_cursor_still_resumes_at_the_end_of_the_file(self, log_file):
        """Trimming what is RETURNED must not rewind where the next poll
        starts, or every poll would re-read the same flood forever."""
        path = log_file("".join(f"line {i}\n" for i in range(5_000)))

        result = read_since(path, since_cursor=0, lines=10)

        assert result.cursor == path.stat().st_size
        assert read_since(path, since_cursor=result.cursor).content == ""

    def test_one_enormous_line_cannot_blow_the_budget(self, log_file):
        """A serialized dump or a stack trace written without newlines is one
        line, so the line budget alone does not bound it."""
        path = log_file("x" * (_MAX_CONTENT_CHARS * 3) + "\n")

        result = read_since(path, since_cursor=0, lines=50)

        assert len(result.content) <= _MAX_CONTENT_CHARS

    def test_the_end_of_that_line_is_what_is_kept(self, log_file):
        """Which is where an exception's message is."""
        path = log_file("x" * (_MAX_CONTENT_CHARS * 3) + "NullReferenceException\n")

        result = read_since(path, since_cursor=0, lines=50)

        assert result.content.endswith("NullReferenceException")


class TestTrimmingDoesNotBlindRestartDetection:
    """The startup banner sits at the top of a freshly truncated log, which is
    the first thing a tail drops. Counting it after trimming would turn every
    restart of a long session into a miss -- and that miss reads as "the game
    is still running the old build", which is the answer this whole feature
    exists to get right."""

    def test_a_banner_in_the_dropped_region_is_still_counted(self, log_file):
        path = log_file(
            "Chainloader started\n" + "".join(f"line {i}\n" for i in range(5_000))
        )

        result = read_since(
            path,
            since_cursor=0,
            lines=10,
            loader_starts=lambda text: text.count("Chainloader started"),
        )

        assert "Chainloader started" not in result.content
        assert result.loader_starts == 1

    def test_no_banner_anywhere_counts_nothing(self, log_file):
        path = log_file("".join(f"line {i}\n" for i in range(500)))

        result = read_since(
            path,
            since_cursor=0,
            lines=10,
            loader_starts=lambda text: text.count("Chainloader started"),
        )

        assert result.loader_starts == 0

    def test_no_scanner_is_a_count_of_zero_not_a_crash(self, log_file):
        path = log_file("Chainloader started\n")
        assert read_since(path, since_cursor=0).loader_starts == 0
