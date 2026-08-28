"""Cursor-based log reading for the build -> deploy -> reproduce -> read loop.

Poll-on-demand only: an MCP server cannot push to the agent over stdio, so the
agent calls back with the cursor it was last given while the user reproduces an
issue in-game.

The cursor is a byte offset. Files are opened in binary mode and decoded after
slicing, because seeking a text-mode handle to a raw byte count is not valid
per Python's `io` contract -- a multi-byte character straddling the offset
would desynchronise the stream.

The cursor was once the only evidence that the game RESTARTED, which is a
different question from whether the log was written to and the one that
actually matters after a redeploy: a mod assembly is loaded once when the
process starts, so a game left running keeps writing to its log with the
previous build still in memory. It was never good evidence. A cursor is a byte
offset with no timestamp on it, and a loader that truncates its log on startup
and then writes past the old offset before being polled leaves neither a
shorter file to catch nor a banner at any offset the cursor reaches -- the
ordinary case, which reported a working session as stale.

So the loader's own account is read instead, from the HEAD of the file, which
is where it records what it loaded and precisely the region a carried-over
cursor skips. `LogRead.session` carries it. `LogRead.restarted` and
`loader_starts` remain for loaders that record nothing usable, where they can
still show that a new process began -- never that one did not.

WHAT IS READ AND WHAT IS RETURNED ARE NOT THE SAME THING, and the difference
is the point of `_tail`. A read resumed from a cursor covers everything
written since that cursor, which for the workflow this tool documents -- keep
polling from the cursor `deploy_mod` handed back until the loader is seen
starting -- is an entire play session. Real profiles on the machine this was
written on hold logs of 77 KB, 140 KB and 1.1 MB; returning one of those whole
would spend more of the agent's context on one poll than the rest of the
session put together. So the region is scanned in full, for the restart signal
that has to see all of it, and only its tail is returned -- with the count of
what was dropped, because a trimmed read and a quiet one must not look alike.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from modwright.models import LoaderSession, LogRead

#: Bytes to scan back through when no cursor is supplied. Generous enough to
#: hold a few hundred lines of a typical loader log without reading a log that
#: has grown to hundreds of megabytes.
_TAIL_WINDOW = 256 * 1024

#: Bytes read from the START of the log, always, whatever the cursor says.
#: This is where a loader records what it loaded, and it is precisely the
#: region a cursor carried over from a previous session skips past -- the
#: blind spot that had a running mod reported as never loaded. On the log
#: that exposed it the whole startup block ends by byte 2313, so this is
#: generous by two orders of magnitude.
_HEAD_WINDOW = 256 * 1024

#: Hard ceiling on the characters any single read returns, whatever `lines`
#: asks for. `lines` is the caller's budget; this is the one that survives a
#: caller passing a large number, and the one that stops a single pathological
#: line -- a serialized dump, a stack trace written without newlines -- from
#: blowing the budget on its own.
_MAX_CONTENT_CHARS = 32 * 1024


def read_since(
    log_path: Path,
    since_cursor: int | None = None,
    lines: int = 50,
    loader_starts: Callable[[str], int] | None = None,
    read_session: Callable[[str], LoaderSession | None] | None = None,
) -> LogRead:
    """Read new log content, returning it with the cursor to resume from.

    At most `lines` lines come back, from the end, on BOTH paths -- with and
    without a cursor. The cursor path used to ignore `lines` entirely and
    return everything since the cursor, which is unbounded and grows for as
    long as the game is left running.

    `loader_starts` counts loader startups in a slice of log text; the adapter
    owns that knowledge, so it is passed in rather than known here. It runs
    over everything that was read, before the tail is taken, because the
    banner it looks for sits at the top of a freshly truncated log -- exactly
    what a tail drops.

    A read with no cursor never reports `restarted`, because there is no
    earlier point to have restarted since. That is not a gap to paper over:
    establishing the baseline is precisely what the first read is for.
    """
    size = log_path.stat().st_size
    restarted = False
    if since_cursor is not None and since_cursor > size:
        # The file shrank: the game restarted and the loader truncated its
        # log. Resume from the beginning rather than returning junk -- and
        # REPORT it, because a caller asking "is the build I just deployed the
        # one running?" has no other way to know. Swallowing this silently is
        # what left that question to be answered by hand outside the tool.
        since_cursor = 0
        restarted = True

    with log_path.open("rb") as handle:
        head, tail_start, tail = _windows(handle, size)

    session = read_session(_decode(head)) if read_session is not None else None

    # Counted over the bytes at or after the cursor, in both windows. Offsets
    # matter here: a banner sitting BEFORE the cursor belongs to the session
    # that was already running at the last read, and counting it would report
    # a restart on every poll for as long as the game stays up.
    starts = 0
    if loader_starts is not None:
        floor = since_cursor or 0
        starts = sum(
            loader_starts(_decode(chunk[max(0, floor - offset) :]))
            for offset, chunk in _unread(head, tail_start, tail)
        )

    begin = 0 if since_cursor is None else max(0, since_cursor - tail_start)
    text = _decode(tail[begin:])
    if begin == 0 and tail_start > 0:
        # The window almost certainly cut a line in half; drop the partial
        # leader so the caller never sees a fragment.
        text = text.partition("\n")[2]
    content, omitted = _tail(text, lines)

    return LogRead(
        content=content,
        cursor=size,
        path=log_path,
        restarted=restarted,
        loader_starts=starts,
        omitted_lines=omitted,
        session=session,
    )


def _windows(handle, size: int) -> tuple[bytes, int, bytes]:
    """The head and tail of the log, as one read when they would overlap.

    Two bounded reads rather than one open-ended one. The old shape read from
    the cursor all the way to EOF on every poll, purely so the startup banner
    could be counted -- which on a session left running is the whole session,
    2.26 MB of it on the log that prompted this, decoded and thrown away to
    return fifty lines. Nothing needs the middle: a loader truncates its log
    when it starts, so what it recorded about loading sits at the top and what
    just happened sits at the end.

    Returns the head bytes, the offset the tail begins at, and the tail bytes.
    A file small enough that the two windows would meet is read once and
    shared, so no byte is decoded twice and no gap can open between them.
    """
    if size <= _HEAD_WINDOW + _TAIL_WINDOW:
        handle.seek(0)
        whole = handle.read()
        return whole, 0, whole

    handle.seek(0)
    head = handle.read(_HEAD_WINDOW)
    tail_start = size - _TAIL_WINDOW
    handle.seek(tail_start)
    return head, tail_start, handle.read()


def _unread(head: bytes, tail_start: int, tail: bytes) -> list[tuple[int, bytes]]:
    """The windows as (offset, bytes) pairs, without counting anything twice.

    A small file is one buffer handed back under both names; returning it
    under both would double every banner in it and report a restart that
    never happened.
    """
    if tail_start == 0:
        return [(0, head)]
    return [(0, head), (tail_start, tail)]


def _tail(text: str, lines: int) -> tuple[str, int]:
    """The last `lines` lines of `text`, under a character ceiling.

    Returns the text kept and the number of lines dropped, so the caller can
    say so. Two limits rather than one, because they fail differently: a
    caller asking for many lines of an ordinary log is fine, and a single line
    holding a megabyte of serialized state is not.
    """
    all_lines = text.splitlines()
    wanted = all_lines[-lines:] if lines > 0 else []

    kept: list[str] = []
    remaining = _MAX_CONTENT_CHARS
    for line in reversed(wanted):
        remaining -= len(line) + 1
        if remaining < 0:
            break
        kept.append(line)
    kept.reverse()

    if not kept and wanted:
        # One line on its own longer than the whole ceiling. Return its end
        # rather than nothing: the end is where an exception's message is.
        kept = [wanted[-1][-_MAX_CONTENT_CHARS:]]

    return "\n".join(kept), len(all_lines) - len(kept)


def _decode(raw: bytes) -> str:
    # Loader logs are effectively UTF-8, but a torn multi-byte sequence at a
    # window boundary must not blow up a debugging session.
    return raw.decode("utf-8", errors="replace")
