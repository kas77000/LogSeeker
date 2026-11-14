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
