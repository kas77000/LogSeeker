"""Order-time window search: kdb times in, a byte range of the log out.

kdb is faked (no server, no pykx needed). The end-to-end tests run real
`tail`/`head`/`grep` through the local backend on a generated log.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logseeker import kdb  # noqa: E402
from logseeker.config import Settings  # noqa: E402
from logseeker.kdbworker import lookup as worker  # noqa: E402

TODAY = date(2026, 11, 13)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        backend="local",
        instances=["APIA", "APIE"],
        log_dir_template=(tmp_path / "logs").as_posix() + "/{instance}",
        log_date_offset_days=-1,
        data_dir=tmp_path / "data",
        command_timeout=60,
        kdb_rdb="rdb-host:5012",
        kdb_hdb="hdb-host:5010",
        kdb_timeout=5,
    )


class FakeRunner:
    def __init__(self, answer):
        self.answer = answer

    def __call__(self, argv, *, input, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(self.answer), stderr="")


# --------------------------------------------------------------------------- #
# kdb: first/last time and last state ride along with the trader
# --------------------------------------------------------------------------- #

def test_states_come_back_dated_rdb_rows_as_today(settings, monkeypatch):
    monkeypatch.setattr(kdb, "pykx_installed", lambda: True)
    runner = FakeRunner({"sources": [
        {"source": "HDB", "endpoint": "hdb-host:5010",
         "rows": [{"date": "2026-11-12", "trader": "APIA"}],
         "states": [{"date": "2026-11-12", "id_server": "1", "first_ms": 1000,
                     "last_ms": 2000, "last_state": "filled"}]},
        {"source": "RDB", "endpoint": "rdb-host:5012",
         "rows": [{"date": None, "trader": "APIA"}],
         "states": [{"date": None, "id_server": "2", "first_ms": 3000,
                     "last_ms": 4000, "last_state": "activated"}]},
    ]})
    check = kdb.lookup_target(settings, "540000001", ["20261112", "20261113"],
                              today=TODAY, runner=runner)
    assert check.status == kdb.FOUND
    assert check.rdb_date == "20261113"
    assert [(s["date"], s["last_state"]) for s in check.states] == [
        ("20261112", "filled"), ("20261113", "activated")]


def test_a_failed_state_query_keeps_the_trader(settings, monkeypatch):
    monkeypatch.setattr(kdb, "pykx_installed", lambda: True)
    runner = FakeRunner({"sources": [
        {"source": "HDB", "endpoint": "hdb-host:5010",
         "rows": [{"date": "2026-11-12", "trader": "APIA"}],
         "states": [], "states_error": "QError: target_state"}]})
    check = kdb.lookup_target(settings, "540000001", ["20261112"], today=TODAY, runner=runner)
    assert check.status == kdb.FOUND and check.traders == ["APIA"]
    assert check.states == []
    assert check.sources[0].states_error == "QError: target_state"


def test_worker_turns_q_times_into_milliseconds():
    pd = pytest.importorskip("pandas")
    assert worker._ms(dt.timedelta(hours=8, minutes=27, seconds=31, milliseconds=897)) \
        == ((8 * 60 + 27) * 60 + 31) * 1000 + 897
    assert worker._ms(pd.Timedelta(milliseconds=1500)) == 1500
    assert worker._ms(pd.NaT) is None
    assert worker._ms(None) is None
    assert worker._ms(dt.time(0, 0, 1, 5000)) == 1005


def test_worker_state_rows():
    pd = pytest.importorskip("pandas")

    class Table:
        def pd(self):
            return pd.DataFrame({
                "date": [pd.Timestamp("2026-11-12")],
                "id_server": [7],
                "first_time": [pd.Timedelta(hours=8)],
                "last_time": [pd.Timedelta(hours=9)],
                "last_state": [b"filled"],
            })

    assert worker._state_rows(Table()) == [{
        "date": "2026-11-12", "id_server": "7", "first_ms": 8 * 3600 * 1000,
        "last_ms": 9 * 3600 * 1000, "last_state": "filled"}]


# --------------------------------------------------------------------------- #
# Commands: the probe, a ranged grep, and the file prefix
# --------------------------------------------------------------------------- #

from logseeker.search import (  # noqa: E402
    SearchRequest, build_grep_command, build_probe_command, validate)

BRACKETED = "/apps/k/algologs/ap/APII/logs/APA2_pingOrderStream_20261112[15-20-23].out"


def _req(settings, **kw):
    base = dict(pattern="1370037780", instances=["APIA"], dates=["20261113"])
    base.update(kw)
    return validate(SearchRequest(**base), settings)


def test_order_window_is_part_of_the_cache_key(settings):
    on = _req(settings)
    off = _req(settings, order_window=False)
    assert on.order_window is True and off.order_window is False
    assert on.fingerprint() != off.fingerprint()


def test_probe_command_seeks_and_quotes_the_bracketed_path():
    cmd = build_probe_command(BRACKETED, 1000, 65536)
    assert cmd == f"tail -c +1001 '{BRACKETED}' | head -c 65536"


def test_ranged_grep_reads_only_the_range(settings):
    r = _req(settings)
    closed = build_grep_command([BRACKETED], r, settings, prefix=False,
                                ranges={BRACKETED: (100, 600)})
    assert (f"tail -c +101 '{BRACKETED}' | head -c 500 | "
            f"{{ grep -E -- '(^|[^0-9])1370037780([^0-9]|$)';") in closed
    assert "awk" not in closed and "/dev/null" not in closed

    open_ = build_grep_command([BRACKETED], r, settings, prefix=False,
                               ranges={BRACKETED: (100, None)})
    assert f"tail -c +101 '{BRACKETED}' | {{ grep" in open_ and "head -c 500" not in open_

    prefixed = build_grep_command([BRACKETED], r, settings, prefix=True,
                                  ranges={BRACKETED: (100, None)})
    assert "awk -v f=" in prefixed


def test_one_file_searches_have_no_prefix(settings):
    r = _req(settings)
    assert "/dev/null" not in build_grep_command(["/l/a.out"], r, settings, prefix=False)
    assert "awk" not in build_grep_command(["/l/a.out.gz"], r, settings, prefix=False)
    assert "/dev/null" in build_grep_command(["/l/a.out"], r, settings)   # default keeps it


def test_pad_minutes_setting(monkeypatch):
    from logseeker import config
    monkeypatch.setenv("LOGSEEKER_ORDER_WINDOW_PAD_MINUTES", "7")
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().order_window_pad_minutes == 7
    finally:
        config.get_settings.cache_clear()
    assert Settings().order_window_pad_minutes == 5


# --------------------------------------------------------------------------- #
# End to end: only the order's minutes are read
# --------------------------------------------------------------------------- #

from logseeker import window as win  # noqa: E402
from logseeker.jobs import JobManager, JobStatus  # noqa: E402

TOOLS = all(shutil.which(t) for t in ("grep", "tail", "head", "gzip", "awk"))
needs_tools = pytest.mark.skipif(not TOOLS, reason="needs grep, tail, head, gzip, awk on PATH")
ORDER = b"1370037780"
FILE = "APA2_pingOrderStream_20261112[23-20-00].out"


def hms(h, m, s=0):
    return ((h * 60 + m) * 60 + s) * 1000


def log_bytes(extra, start=hms(23, 20), end=hms(9, 0)):
    """One heartbeat a second across midnight, a stack trace every 500 lines,
    plus `extra` {clock ms: message} lines."""
    out, n, t = [], 0, 0
    span = (end - start) % win.DAY_MS
    while t <= span:
        clock = (start + t) % win.DAY_MS
        ts = f"{clock // 3600000:02d}:{clock // 60000 % 60:02d}:{clock // 1000 % 60:02d},000"
        out.append(ts.encode() + b" INFO [EngineSubscriber] heartbeat %d" % n)
        if clock in extra:
            out.append(ts.encode() + b" INFO [AddOrderTimer] " + extra[clock])
        n += 1
        if n % 500 == 0:
            out += [b"java.lang.IllegalStateException: boom", b"\tat x.Engine.run(Engine.java:42)"]
        t += 1000
    return b"\n".join(out) + b"\n"


ORDER_LINES = {
    hms(3, 0): b"orderId=1370037780 DECOY early",
    hms(8, 27, 31): b"orderId=1370037780 NEW",
    hms(8, 29): b"orderId=1370037780 FILL 100",
    hms(8, 30): b"orderId=1370037780 FILLED",
    hms(8, 55): b"orderId=1370037780 DECOY late",
}


def plant(settings, instance="APIA", name=FILE, extra=ORDER_LINES, gz=False, **kw):
    d = Path(settings.log_dir(instance))
    d.mkdir(parents=True, exist_ok=True)
    data = log_bytes(extra, **kw)
    path = d / (name + (".gz" if gz else ""))
    path.write_bytes(gzip.compress(data) if gz else data)
    return path


def state(first, last, last_state="filled", day="2026-11-13"):
    return {"date": day, "id_server": "1", "first_ms": first, "last_ms": last,
            "last_state": last_state}


def found(*states, traders=("APIA",)):
    return kdb.KdbCheck(kdb.FOUND, "Found.", sources=[kdb.SourceResult(
        "HDB", "h:1", rows=[{"date": "2026-11-13", "trader": t} for t in traders],
        states=list(states))])


def search(settings, monkeypatch, check, *, instances=("APIA",), order_window=True):
    monkeypatch.setattr(kdb, "lookup_target", lambda *a, **k: check)
    req = validate(SearchRequest(pattern="1370037780", instances=list(instances),
                                 dates=["20261113"], order_window=order_window), settings)
    job = JobManager(settings).submit(req)
    deadline = time.time() + 60
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.05)
    assert job.status is JobStatus.DONE, job.error
    return job, job.result_path.read_bytes()


@needs_tools
def test_a_finished_order_reads_only_its_window(settings, monkeypatch):
    path = plant(settings)
    job, body = search(settings, monkeypatch, found(state(hms(8, 27, 31), hms(8, 30))))
    assert b"NEW" in body and b"FILL 100" in body and b"FILLED" in body
    assert b"DECOY" not in body
    assert body.startswith(b"08:27:31,000"), "one file: lines as in the raw log"
    assert job.bytes_to_scan < path.stat().st_size / 4
    assert any("last state `filled`" in n and "08:22:31" in n and "08:35:00" in n
               for n in job.scan_notes), job.scan_notes


@needs_tools
def test_an_active_order_reads_to_the_end_of_the_file(settings, monkeypatch):
    plant(settings)
    job, body = search(settings, monkeypatch,
                       found(state(hms(8, 27, 31), hms(8, 30), "activated")))
    assert b"DECOY late" in body and b"DECOY early" not in body
    assert any("end of file" in n for n in job.scan_notes), job.scan_notes


@needs_tools
def test_toggle_off_reads_the_whole_file(settings, monkeypatch):
    path = plant(settings)
    job, body = search(settings, monkeypatch, found(state(hms(8, 27, 31), hms(8, 30))),
                       order_window=False)
    assert b"DECOY early" in body and b"DECOY late" in body
    assert job.scan_notes == []
    assert job.bytes_to_scan >= path.stat().st_size


@needs_tools
def test_no_state_row_for_the_date_scans_the_whole_file(settings, monkeypatch):
    plant(settings)
    job, body = search(settings, monkeypatch,
                       found(state(hms(8, 27), hms(8, 30), day="2026-11-12")))
    assert b"DECOY early" in body
    assert any("whole file" in n and "target_state" in n for n in job.scan_notes)


@needs_tools
def test_an_empty_window_falls_back_to_the_whole_file(settings, monkeypatch):
    plant(settings)
    job, body = search(settings, monkeypatch, found(state(hms(5, 0), hms(5, 10))))
    assert b"NEW" in body and b"DECOY early" in body
    assert any("whole file" in w.lower() for w in job.warnings), job.warnings


@needs_tools
def test_a_gzipped_log_is_scanned_whole(settings, monkeypatch):
    plant(settings, gz=True)
    job, body = search(settings, monkeypatch, found(state(hms(8, 27, 31), hms(8, 30))))
    assert b"DECOY early" in body
    assert any("gzipped" in n for n in job.scan_notes)


@needs_tools
def test_a_window_after_midnight(settings, monkeypatch):
    plant(settings, extra={hms(23, 30): b"orderId=1370037780 DECOY evening",
                           hms(0, 10): b"orderId=1370037780 NEW night"})
    job, body = search(settings, monkeypatch, found(state(hms(0, 10), hms(0, 11))))
    assert b"NEW night" in body and b"DECOY evening" not in body


@needs_tools
def test_two_files_keep_the_file_prefix(settings, monkeypatch):
    plant(settings, "APIA")
    plant(settings, "APIE")
    job, body = search(settings, monkeypatch, found(state(hms(8, 27, 31), hms(8, 30))),
                       instances=("APIA", "APIE"))
    lines = body.splitlines()
    assert len(lines) == 6 and all(FILE.encode() + b":08:" in ln for ln in lines)


@needs_tools
def test_an_open_ended_result_is_not_served_from_cache(settings, monkeypatch):
    plant(settings)
    check = found(state(hms(8, 27, 31), hms(8, 30), "activated"))
    search(settings, monkeypatch, check)
    again, _ = search(settings, monkeypatch, check)
    assert not again.cache_hit
