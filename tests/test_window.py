"""Order-time window: timestamps, session time, and finding byte ranges.

All of this is pure. The log is an in-memory bytes object and the probe is a
slice of it, so every case runs in milliseconds with no shell involved.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logseeker import window as w  # noqa: E402
from logseeker.window import Window, WindowError  # noqa: E402

H = 3600 * 1000
M = 60 * 1000


def ms(hh, mm, ss=0, mmm=0):
    return ((hh * 60 + mm) * 60 + ss) * 1000 + mmm


def stamp(t):
    t %= w.DAY_MS
    return (f"{t // H:02d}:{t // M % 60:02d}:{t // 1000 % 60:02d},{t % 1000:03d}").encode()


def make_log(start=ms(23, 20), end=ms(9, 0), every_s=1, trace_every=500, extra=None):
    """Lines every `every_s` seconds from `start` across midnight to `end`.

    Every `trace_every` lines a three-line stack trace with no timestamp is
    inserted, as Java logs do. `extra` maps a clock time to a message line
    written at that time (used to plant order lines and decoys).
    """
    extra = dict(extra or {})
    out = []
    span = (end - start) % w.DAY_MS
    n = 0
    t = 0
    while t <= span:
        clock = (start + t) % w.DAY_MS
        out.append(stamp(clock) + b" INFO [EngineSubscriber] heartbeat seq=%d" % n)
        if clock in extra:
            out.append(stamp(clock) + b" INFO [AddOrderTimer] " + extra[clock])
        n += 1
        if n % trace_every == 0:
            out += [b"java.lang.IllegalStateException: boom",
                    b"\tat com.example.Engine.run(Engine.java:42)",
                    b"\tat java.lang.Thread.run(Thread.java:750)"]
        t += every_s * 1000
    return b"\n".join(out) + b"\n"


class Probe:
    def __init__(self, data: bytes):
        self.data = data
        self.calls = 0

    def __call__(self, offset: int, length: int) -> bytes:
        self.calls += 1
        return self.data[offset:offset + length]


def line_at(data: bytes, offset: int) -> bytes:
    end = data.find(b"\n", offset)
    return data[offset:end if end >= 0 else len(data)]


# --------------------------------------------------------------------------- #

def test_clock_ms_reads_the_leading_timestamp_only():
    assert w.clock_ms(b"08:27:31,897 INFO x") == ms(8, 27, 31, 897)
    assert w.clock_ms(b"08:27:31.897 INFO x") == ms(8, 27, 31, 897)
    assert w.clock_ms(b"\tat com.example") is None
    assert w.clock_ms(b"x 08:27:31,897") is None
    assert w.clock_ms(b"25:00:00,000 bad hour") is None
    assert w.clock_ms(b"08:27") is None


def test_timed_lines_skips_the_partial_first_line_unless_at_a_line_start():
    chunk = b"7:31,897 tail of a line\n08:00:00,000 a\n\tat trace\n08:00:01,000 b"
    got = list(w.timed_lines(chunk, at_line_start=False))
    assert [t for _, t in got] == [ms(8, 0), ms(8, 0, 1)]
    assert chunk[got[0][0]:].startswith(b"08:00:00,000")
    assert list(w.timed_lines(b"no newline here", at_line_start=False)) == []
    assert [t for _, t in w.timed_lines(b"08:00:00,000 a\n", at_line_start=True)] == [ms(8, 0)]


def test_session_time_wraps_past_midnight():
    start = ms(23, 20)
    assert w.session_ms(ms(23, 20), start) == 0
    assert w.session_ms(ms(0, 10), start) == 50 * M
    assert w.session_ms(ms(8, 27), start) == ms(9, 7)


def test_order_window_merges_servers_and_is_open_while_any_is_activated():
    states = [
        {"date": "20261113", "id_server": "1", "first_ms": ms(8, 27), "last_ms": ms(9, 0),
         "last_state": "filled"},
        {"date": "20261113", "id_server": "2", "first_ms": ms(8, 30), "last_ms": ms(9, 41),
         "last_state": "cancelled"},
        {"date": "20261112", "id_server": "1", "first_ms": ms(1, 0), "last_ms": ms(2, 0),
         "last_state": "activated"},
    ]
    win = w.order_window(states, "20261113")
    assert win == Window(ms(8, 27), ms(9, 41), False, "cancelled")
    assert w.order_window(states, "20261112").open_ended
    assert w.order_window(states, "20261114") is None
    assert w.order_window([{"date": "20261113", "first_ms": None, "last_ms": None,
                            "last_state": "filled"}], "20261113") is None

    states[0]["last_state"] = "activated"
    win = w.order_window(states, "20261113")
    assert win.open_ended and win.last_state == "activated"


def test_session_start_is_the_first_timestamped_line_not_the_filename():
    data = b"\tat banner without time\n23:20:05,000 first\n23:20:06,000 second\n"
    assert w.session_start(Probe(data)) == ms(23, 20, 5)
    with pytest.raises(WindowError, match="start of the log"):
        w.session_start(Probe(b"no time at all\n" * 10))


def test_locate_returns_the_exact_start_of_the_first_line_at_or_after_target():
    data = make_log()
    probe = Probe(data)
    start = w.session_start(probe)
    for clock in (ms(23, 20), ms(23, 59, 59), ms(0, 0), ms(3, 0, 1), ms(8, 27, 31)):
        off = w.locate(probe, len(data), w.session_ms(clock, start), start)
        assert line_at(data, off).startswith(stamp(clock)), (stamp(clock), line_at(data, off))
        assert off == 0 or data[off - 1:off] == b"\n"

    assert w.locate(probe, len(data), w.session_ms(ms(9, 30), start), start) is None


def test_find_range_brackets_the_padded_window_exactly():
    data = make_log(extra={ms(8, 27, 31): b"orderId=1370037780 NEW",
                           ms(9, 41, 5): b"orderId=1370037780 FILLED"},
                    end=ms(10, 0))
    probe = Probe(data)
    s, e = w.find_range(probe, len(data), Window(ms(8, 27, 31), ms(9, 41, 5), False, "filled"),
                        5 * M)
    chunk = data[s:e]
    assert chunk.startswith(stamp(ms(8, 22, 31)))
    assert chunk.rstrip(b"\n").splitlines()[-1].startswith(stamp(ms(9, 46, 5)))
    assert b"orderId=1370037780 NEW" in chunk and b"FILLED" in chunk
    assert len(chunk) < len(data) / 5
    assert probe.calls < 200, probe.calls


def test_open_ended_window_runs_to_end_of_file():
    data = make_log()
    s, e = w.find_range(Probe(data), len(data),
                        Window(ms(8, 27), ms(8, 30), True, "activated"), 5 * M)
    assert e is None
    assert line_at(data, s).startswith(stamp(ms(8, 22)))


def test_window_after_midnight_and_clamped_at_session_start():
    data = make_log(start=ms(23, 20), end=ms(1, 0))
    s, e = w.find_range(Probe(data), len(data), Window(ms(0, 10), ms(0, 12), False, "filled"),
                        5 * M)
    assert line_at(data, s).startswith(stamp(ms(0, 5)))
    assert b"23:30:00" not in data[s:e]

    # starts 2 minutes into the session: start - 5 min clamps to byte 0
    s, _ = w.find_range(Probe(data), len(data), Window(ms(23, 22), ms(23, 23), False, "x"),
                        5 * M)
    assert s == 0


def test_a_window_entirely_after_the_log_cannot_be_placed():
    data = make_log(end=ms(1, 0))
    with pytest.raises(WindowError, match="after the last line"):
        w.find_range(Probe(data), len(data), Window(ms(5, 0), ms(5, 30), False, "filled"), 5 * M)


def test_a_long_stretch_without_timestamps_is_a_window_error():
    data = b"23:20:00,000 start\n" + b"x" * 80 + b"\n"
    data = data + (b"no timestamp " * 10 + b"\n") * 20000 + b"09:00:00,000 end\n"
    with pytest.raises(WindowError, match="no timestamped line"):
        w.find_range(Probe(data), len(data), Window(ms(5, 0), ms(5, 30), False, "x"), 5 * M)


def test_fmt_clock():
    assert w.fmt_clock(ms(8, 22, 31, 900)) == "08:22:31"
    assert w.fmt_clock(-5 * M) == "23:55:00"
