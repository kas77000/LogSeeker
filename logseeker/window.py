"""Which bytes of a log hold an order's minutes.

An order's lines sit in a few minutes of a log that can run to 145 GB. kdb's
`target_state` says when the order lived; every log line starts with an
``HH:MM:SS,mmm`` timestamp and the file is written in time order. So instead of
reading the file from the top, binary-search it by byte offset: read 64 KB
somewhere, look at the first whole line's timestamp, halve, repeat. Each read
seeks straight to its offset, so placing a window costs a few dozen small
reads whatever the size of the file.

Everything here is pure. The caller passes ``probe(offset, length) -> bytes``;
the job implements it with ``tail -c +N | head -c L`` on the log host, the
tests with a slice of a bytes object.

Times are compared as *time since the session started*: a session that opens
at 23:20 HKT runs past midnight, and a 00:10 line comes after a 23:59 one. The
session start is the first timestamped line of the file — the ``[HH-MM-SS]``
in the filename is on a different clock and is never used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator

DAY_MS = 24 * 3600 * 1000
CHUNK = 64 * 1024
# How far past an offset to look for a timestamped line before giving up. A
# stack trace is a few KB; a megabyte with no timestamp means this is not the
# log format we expect, and guessing would risk missing lines.
MAX_LOOK = 1024 * 1024
ACTIVE_STATE = "activated"

_TS = re.compile(rb"(\d\d):(\d\d):(\d\d)[,.](\d{3})")

Probe = Callable[[int, int], bytes]


class WindowError(Exception):
    """The window cannot be placed in this file; the caller scans it whole."""


def clock_ms(line: bytes) -> int | None:
    """Milliseconds since midnight from a line's leading timestamp, or None."""
    m = _TS.match(line)
    if not m:
        return None
    h, mi, s, frac = (int(g) for g in m.groups())
    if h > 23 or mi > 59 or s > 59:
        return None
    return ((h * 60 + mi) * 60 + s) * 1000 + frac


def timed_lines(chunk: bytes, *, at_line_start: bool) -> Iterator[tuple[int, int]]:
    """(offset in chunk, clock ms) of each line that *starts* in the chunk and
    carries a timestamp. Without ``at_line_start`` the chunk may begin mid-line,
    so everything up to the first newline is skipped."""
    if at_line_start:
        pos = 0
    else:
        nl = chunk.find(b"\n")
        if nl < 0:
            return
        pos = nl + 1
    size = len(chunk)
    while pos < size:
        end = chunk.find(b"\n", pos)
        t = clock_ms(chunk[pos:pos + 12])
        if t is not None:
            yield pos, t
        if end < 0:
            return
        pos = end + 1


def session_ms(clock: int, start: int) -> int:
    """Time since the session started, for a clock time on the same day or the next."""
    return (clock - start) % DAY_MS


def fmt_clock(ms: int) -> str:
    ms %= DAY_MS
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}"


@dataclass(frozen=True)
class Window:
    """When the order lived, in kdb clock time (HKT, ms since midnight)."""
    first_ms: int
    last_ms: int
    open_ended: bool        # still `activated`: read to the end of the file
    last_state: str


def order_window(states: list[dict], trade_date: str) -> Window | None:
    """Merge every `target_state` summary row for ``trade_date`` into one window.

    An id can live on more than one ``id_server``; the window covers them all,
    and stays open while any of them is still active.
    """
    rows = [r for r in states
            if r.get("date") == trade_date
            and r.get("first_ms") is not None and r.get("last_ms") is not None]
    if not rows:
        return None
    first = min(int(r["first_ms"]) for r in rows)
    latest = max(rows, key=lambda r: int(r["last_ms"]))
    open_ended = any((r.get("last_state") or "") == ACTIVE_STATE for r in rows)
    return Window(
        first_ms=first,
        last_ms=int(latest["last_ms"]),
        open_ended=open_ended,
        last_state=ACTIVE_STATE if open_ended else (latest.get("last_state") or "unknown"),
    )


def session_start(probe: Probe) -> int:
    """Clock time of the first timestamped line in the file."""
    for _, t in timed_lines(probe(0, CHUNK), at_line_start=True):
        return t
    raise WindowError("no timestamped line at the start of the log")


def _first_at_or_after(probe: Probe, offset: int, start: int) -> tuple[int, int] | None:
    """(byte offset, session ms) of the first timestamped line starting at or
    after ``offset``. None when the file ends first."""
    pos, at_start, looked = offset, offset == 0, 0
    while looked < MAX_LOOK:
        chunk = probe(pos, CHUNK)
        if not chunk:
            return None
        for off, t in timed_lines(chunk, at_line_start=at_start):
            return pos + off, session_ms(t, start)
        if len(chunk) < CHUNK:
            return None
        # Carry on from the last newline, so a line split across reads is
        # seen whole next time.
        nl = chunk.rfind(b"\n")
        pos += nl if nl > 0 else len(chunk)
        at_start = False
        looked += CHUNK
    raise WindowError(f"no timestamped line within 1 MB of byte {offset:,}")


def locate(probe: Probe, size: int, target: int, start: int) -> int | None:
    """Byte offset of the first timestamped line at or after session time
    ``target``; None when every line is earlier."""
    first = _first_at_or_after(probe, 0, start)
    if first is None:
        return None
    if first[1] >= target:
        return first[0]

    # Invariant: the first timed line after `lo` is before target; nothing at
    # or after `hi` is.
    lo, hi = 0, max(size, 1)
    while hi - lo > CHUNK:
        mid = (lo + hi) // 2
        found = _first_at_or_after(probe, mid, start)
        if found is None or found[1] >= target:
            hi = mid
        else:
            lo = mid

    # Finish with a forward read: the answer is within a chunk or two of `lo`.
    pos, at_start, looked = lo, lo == 0, 0
    span = 2 * CHUNK
    while looked < MAX_LOOK:
        chunk = probe(pos, span)
        if not chunk:
            return None
        for off, t in timed_lines(chunk, at_line_start=at_start):
            if session_ms(t, start) >= target:
                return pos + off
        if len(chunk) < span:
            return None
        nl = chunk.rfind(b"\n")
        pos += nl if nl > 0 else len(chunk)
        at_start = False
        looked += span
    raise WindowError(f"no timestamped line within 1 MB of byte {lo:,}")


def find_range(probe: Probe, size: int, window: Window, pad_ms: int) -> tuple[int, int | None]:
    """(start offset, end offset or None for end of file) of the padded window."""
    start = session_start(probe)
    first = session_ms(window.first_ms, start)
    last = session_ms(window.last_ms, start)
    if last < first:
        raise WindowError("kdb's last time is before its first in this session")

    begin = locate(probe, size, max(0, first - pad_ms), start)
    if begin is None:
        raise WindowError("the order's window starts after the last line of the log")
    if window.open_ended:
        return begin, None
    return begin, locate(probe, size, last + pad_ms + 1, start)
