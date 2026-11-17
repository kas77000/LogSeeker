"""Order details: the target row(s) and the workorders of one id_target.

No kdb server: the worker process is replaced by a fake runner, and the
worker's own conversion is tested on fake pykx tables (pandas frames).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
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
    return Settings(backend="local", data_dir=tmp_path / "data",
                    kdb_rdb="rdb-host:5012", kdb_hdb="hdb-host:5010", kdb_timeout=5)


@pytest.fixture(autouse=True)
def _pykx(monkeypatch):
    monkeypatch.setattr(kdb, "pykx_installed", lambda: True)


class FakeRunner:
    def __init__(self, answer=None, raises=None):
        self.answer = answer if answer is not None else {"sources": []}
        self.raises = raises
        self.request = None

    def __call__(self, argv, *, input, **kwargs):
        self.request = json.loads(input)
        if self.raises:
            raise self.raises
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(self.answer), stderr="")


def test_details_request_routes_today_to_rdb_and_the_rest_to_hdb(settings):
    runner = FakeRunner()
    kdb.order_details(settings, "540000001", ["20261112", "20261113"],
                      today=TODAY, runner=runner)
    req = runner.request
    assert req["op"] == "details" and req["id"] == "540000001"
    assert req["rdb"] == ["rdb-host", 5012] and req["hdb"] == ["hdb-host", 5010]
    assert req["hdb_dates"] == ["2026-11-12"]
    assert req["max_rows"] == kdb.DETAILS_MAX_ROWS


def test_details_answer_is_read_per_source(settings):
    runner = FakeRunner({"sources": [
        {"source": "HDB", "endpoint": "hdb-host:5010",
         "target": [{"date": "2026-11-12", "id_target": 540000001, "sym": "0700.HK"}],
         "workorder": [{"id_work": 2, "t_gen": "08:30:00.000"},
                       {"id_work": 1, "t_gen": "08:27:31.897"}],
         "workorder_error": None, "target_error": None, "error": None},
        {"source": "RDB", "endpoint": "rdb-host:5012", "target": [], "workorder": [],
         "workorder_error": "QError: workorder", "error": None},
    ]})
    details = kdb.order_details(settings, "540000001", ["20261112", "20261113"],
                                today=TODAY, runner=runner)
    assert details.error is None
    hdb, rdb = details.sources
    assert hdb.target[0]["sym"] == "0700.HK"
    assert [w["id_work"] for w in hdb.workorder] == [1, 2], "sorted by t_gen"
    assert rdb.workorder_error == "QError: workorder"


def test_details_failures_are_reported_not_raised(settings):
    hung = kdb.order_details(settings, "540000001", ["20261113"], today=TODAY,
                             runner=FakeRunner(raises=subprocess.TimeoutExpired("x", 1)))
    assert "in time" in hung.error

    off = Settings(kdb_rdb="", kdb_hdb="")
    assert "not configured" in kdb.order_details(off, "540000001", ["20261113"]).error
    assert "kdb id_target" in kdb.order_details(settings, "ORD-1", ["20261113"]).error


def test_worker_turns_any_q_value_into_json():
    pd = pytest.importorskip("pandas")
    np = pytest.importorskip("numpy")
    p = worker._plain
    assert p(b"filled") == "filled"
    assert p(pd.Timestamp("2026-11-12")) == "2026-11-12"
    assert p(pd.Timestamp("2026-11-12 08:27:31.897")) == "2026-11-12 08:27:31.897"
    assert p(pd.Timedelta(hours=8, minutes=27, seconds=31, milliseconds=897)) == "08:27:31.897"
    assert p(dt.time(8, 27, 31, 897000)) == "08:27:31.897"
    assert p(pd.NaT) is None and p(float("nan")) is None and p(None) is None
    assert p(np.int32(7)) == 7 and isinstance(p(np.int32(7)), int)
    assert p(np.float64(1.5)) == 1.5
    assert p(np.array([b"a", b"b"])) == "a b"
    json.dumps([p(v) for v in (np.int64(1), b"x", pd.Timedelta(0))])


def test_worker_records_keep_every_column():
    pd = pytest.importorskip("pandas")

    class Table:
        def pd(self):
            return pd.DataFrame({"id_target": [540000001], "sym": [b"0700.HK"],
                                 "t_gen": [pd.Timedelta(hours=8)]})

    assert worker._records(Table()) == [
        {"id_target": 540000001, "sym": "0700.HK", "t_gen": "08:00:00.000"}]


def test_worker_rows_carry_the_sym():
    pd = pytest.importorskip("pandas")

    class Table:
        def pd(self):
            return pd.DataFrame({"trader": [b"APIA"], "sym": [b"0700.HK"]})

    assert worker._rows(Table()) == [{"date": None, "trader": "APIA", "sym": "0700.HK"}]
