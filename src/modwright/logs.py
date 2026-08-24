"""Cursor-based log reading for the build -> deploy -> reproduce -> read loop.

Poll-on-demand only: an MCP server cannot push to the agent over stdio, so the
agent calls back with the cursor it was last given while the user reproduces an
issue in-game.

The cursor is a byte offset. Files are opened in binary mode and decoded after
slicing, because seeking a text-mode handle to a raw byte count is not valid
per Python's `io` contract -- a multi-byte character straddling the offset
would desynchronise the stream.
"""

from __future__ import annotations

from pathlib import Path

from modwright.models import LogRead

#: Bytes to scan back through when no cursor is supplied. Generous enough to
#: hold a few hundred lines of a typical loader log without reading a log that
#: has grown to hundreds of megabytes.
_TAIL_WINDOW = 256 * 1024


def read_since(
    log_path: Path, since_cursor: int | None = None, lines: int = 50
) -> LogRead:
    """Read new log content, returning it with the cursor to resume from.

    With no cursor, returns the last `lines` lines rather than the whole file --
    the first call in a session wants recent context, not history.
    """
    size = log_path.stat().st_size

    with log_path.open("rb") as handle:
        if since_cursor is None:
            start = max(0, size - _TAIL_WINDOW)
            handle.seek(start)
            raw = handle.read()
            text = _decode(raw)
            if start > 0:
                # The window almost certainly cut a line in half; drop the
                # partial leader so the caller never sees a fragment.
                text = text.partition("\n")[2]
            content = "\n".join(text.splitlines()[-lines:])
        else:
            if since_cursor > size:
                # The file shrank: the game restarted and the loader truncated
                # its log. Resume from the beginning rather than returning junk.
                since_cursor = 0
            handle.seek(since_cursor)
            content = _decode(handle.read())

    return LogRead(content=content, cursor=size, path=log_path)


def _decode(raw: bytes) -> str:
    # Loader logs are effectively UTF-8, but a torn multi-byte sequence at a
    # window boundary must not blow up a debugging session.
    return raw.decode("utf-8", errors="replace")
