"""The kdb id_target check: routing, failure handling, and the job integration.

No kdb server is needed. The child process is replaced by a fake runner for
the routing tests; one test runs the real worker (with the real pykx, if it is
installed) against a port nothing listens on, to prove a dead endpoint comes
back as a reported error rather than a crash or a hang.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logseeker import kdb                       # noqa: E402
from logseeker.config import Settings, config_problems  # noqa: E402
from logseeker.jobs import JobManager, JobStatus  # noqa: E402
from logseeker.search import SearchRequest, validate  # noqa: E402

TODAY = date(2026, 11, 13)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        backend="local",
        instances=["APIA"],
        log_dir_template=(tmp_path / "logs").as_posix() + "/{instance}",
        data_dir=tmp_path / "data",
        command_timeout=30,
        kdb_rdb="rdb-host:5012",
        kdb_hdb="hdb-host:5010",
        kdb_user="svc",
        kdb_password="s3cret",
        kdb_timeout=5,
    )


class FakeRunner:
    """Stands in for subprocess.run: records the request, returns an answer."""

    def __init__(self, answer=None, *, stdout=None, raises=None):
        self.answer = answer if answer is not None else {"sources": []}
        self.stdout = stdout
        self.raises = raises
        self.request = None
        self.argv = None

    def __call__(self, argv, *, input, **kwargs):
        self.argv = argv
        self.request = json.loads(input)
        if self.raises:
            raise self.raises
        out = self.stdout if self.stdout is not None else json.dumps(self.answer)
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


# --------------------------------------------------------------------------- #

def test_parse_endpoint():
    assert kdb.parse_endpoint("oms-hdb-host:17072") == ("oms-hdb-host", 17072)
    assert kdb.parse_endpoint("  ") is None
    for bad in ("host", "host:", ":5010", "host:abc", "host:70000"):
        with pytest.raises(ValueError):
            kdb.parse_endpoint(bad)


def test_today_goes_to_the_rdb_and_earlier_dates_to_the_hdb():
    assert kdb.split_dates(["20261112", "20261113"], TODAY) == (True, ["20261112"])
    assert kdb.split_dates(["20261110"], TODAY) == (False, ["20261110"])
    assert kdb.split_dates(["20261113"], TODAY) == (True, [])


def test_rdb_request_has_no_dates_and_hdb_request_does(settings):
    runner = FakeRunner()
    kdb.lookup_target(settings, "540000001", ["20261112", "20261113"],
                      today=TODAY, runner=runner)
    req = runner.request
    assert req["rdb"] == ["rdb-host", 5012]
    assert req["hdb"] == ["hdb-host", 5010]
    assert req["hdb_dates"] == ["2026-11-12"]          # today is NOT sent to the HDB
    assert req["id"] == "540000001"


def test_credentials_go_over_stdin_not_argv(settings):
    runner = FakeRunner()
    kdb.lookup_target(settings, "540000001", ["20261113"], today=TODAY, runner=runner)
    assert runner.request["password"] == "s3cret"
    assert "s3cret" not in " ".join(runner.argv)


def test_found_collects_traders(settings):
    runner = FakeRunner({"sources": [
        {"source": "HDB", "endpoint": "hdb-host:5010",
         "rows": [{"date": "2026-11-12", "trader": "jdoe"},
                  {"date": "2026-11-12", "trader": "jdoe"}]},
        {"source": "RDB", "endpoint": "rdb-host:5012",
         "rows": [{"date": None, "trader": "asmith"}]},
    ]})
    check = kdb.lookup_target(settings, "540000001", ["20261112", "20261113"],
                              today=TODAY, runner=runner)
    assert check.status == kdb.FOUND
    assert check.traders == ["jdoe", "asmith"]
    assert {r["source"] for r in check.rows} == {"HDB", "RDB"}


def test_not_found_is_distinct_from_an_error(settings):
    runner = FakeRunner({"sources": [{"source": "RDB", "endpoint": "rdb-host:5012",
                                      "rows": [], "error": None}]})
    check = kdb.lookup_target(settings, "540000001", ["20261113"], today=TODAY, runner=runner)
    assert check.status == kdb.NOT_FOUND
    assert "540000001" in check.message

    runner = FakeRunner({"sources": [{"source": "RDB", "endpoint": "rdb-host:5012",
                                      "rows": [], "error": "ConnectionRefusedError"}]})
    check = kdb.lookup_target(settings, "540000001", ["20261113"], today=TODAY, runner=runner)
    assert check.status == kdb.ERROR
    assert "ConnectionRefusedError" in check.message


def test_a_banner_before_the_answer_is_ignored(settings):
    answer = {"sources": [{"source": "RDB", "endpoint": "x:1",
                           "rows": [{"date": None, "trader": "jdoe"}]}]}
    runner = FakeRunner(stdout="PyKX banner text\n\n" + json.dumps(answer) + "\n")
    check = kdb.lookup_target(settings, "540000001", ["20261113"], today=TODAY, runner=runner)
    assert check.traders == ["jdoe"]


def test_a_hung_worker_is_reported_not_raised(settings):
    runner = FakeRunner(raises=subprocess.TimeoutExpired(cmd="x", timeout=1))
    check = kdb.lookup_target(settings, "540000001", ["20261113"], today=TODAY, runner=runner)
    assert check.status == kdb.ERROR
    assert "in time" in check.message


def test_missing_endpoint_skips_its_dates_and_says_so(settings):
    only_hdb = replace(settings, kdb_rdb="")
    runner = FakeRunner()
    check = kdb.lookup_target(only_hdb, "540000001", ["20261112", "20261113"],
                              today=TODAY, runner=runner)
    assert runner.request["rdb"] is None
    assert check.skipped_dates == ["20261113"]

    runner = FakeRunner()
    check = kdb.lookup_target(only_hdb, "540000001", ["20261113"], today=TODAY, runner=runner)
    assert runner.request is None, "nothing to ask, so no process should start"
    assert check.status == kdb.SKIPPED


def test_regex_searches_and_odd_ids_are_not_sent_to_kdb(settings):
    runner = FakeRunner()
    assert kdb.lookup_target(settings, "5400.01", ["20261113"], exact=False,
                             today=TODAY, runner=runner).status == kdb.SKIPPED
    assert kdb.lookup_target(settings, "54(01|02)", ["20261113"],
                             today=TODAY, runner=runner).status == kdb.SKIPPED
    assert runner.request is None


def test_ids_that_cannot_be_a_kdb_int_are_not_sent(settings):
    """id_target is type "i": digits only, at most 2**31-1."""
    runner = FakeRunner()
    for bad in ("ABC123", "54-0001", "2147483648", "99999999999"):
        check = kdb.lookup_target(settings, bad, ["20261113"], today=TODAY, runner=runner)
        assert check.status == kdb.SKIPPED, bad
        assert "2,147,483,647" in check.message
    assert runner.request is None

    kdb.lookup_target(settings, "2147483647", ["20261113"], today=TODAY, runner=runner)
    assert runner.request["id"] == "2147483647"


def test_disabled_when_no_endpoint(settings):
    off = replace(settings, kdb_rdb="", kdb_hdb="")
    assert not off.kdb_enabled
    assert kdb.lookup_target(off, "540000001", ["20261113"]).status == kdb.SKIPPED


def test_malformed_endpoint_is_a_config_problem(settings):
    problems = config_problems(replace(settings, kdb_hdb="no-port-here"))
    assert any("kdb_hdb" in p for p in problems)


def test_password_is_not_in_the_settings_repr(settings):
    assert "s3cret" not in repr(settings)


# --------------------------------------------------------------------------- #
# The real worker process
# --------------------------------------------------------------------------- #

def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not kdb.pykx_installed(), reason="needs pykx")
def test_real_worker_reports_a_dead_endpoint_as_an_error(settings):
    port = _closed_port()
    dead = replace(settings, kdb_rdb=f"127.0.0.1:{port}", kdb_hdb="", kdb_timeout=5)
    started = time.time()
    check = kdb.lookup_target(dead, "540000001", ["20261113"], today=TODAY)
    assert check.status == kdb.ERROR, check
    assert f"127.0.0.1:{port}" in check.message
    assert time.time() - started < 60


# --------------------------------------------------------------------------- #
# Wired into a search
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep on PATH")
def test_kdb_result_rides_on_the_job_and_the_grep_still_runs(settings, monkeypatch):
    log_dir = Path(settings.log_dir("APIA"))
    log_dir.mkdir(parents=True)
    (log_dir / "APA1_pingOrderStream_20260421_1.out").write_text(
        "orderId=540000001 status=ACK\n", encoding="utf-8")

    seen = {}

    def fake_lookup(s, ident, dates, *, exact=True, **kw):
        seen.update(ident=ident, dates=dates, exact=exact)
        return kdb.KdbCheck(kdb.NOT_FOUND, "No target.")

    monkeypatch.setattr(kdb, "lookup_target", fake_lookup)
    job = JobManager(settings).submit(validate(
        SearchRequest(pattern="540000001", instances=["APIA"], dates=["20260421"]),
        settings))
    deadline = time.time() + 30
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.1)

    assert seen == {"ident": "540000001", "dates": ["20260421"], "exact": True}
    assert job.kdb.status == kdb.NOT_FOUND
    assert job.status is JobStatus.DONE, job.error      # a kdb miss does not stop the grep
    assert job.lines_written == 1
