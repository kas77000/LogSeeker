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
    assert f"tail -c +101 '{BRACKETED}' | head -c 500 | {{ grep -F -- 1370037780;" in closed
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
