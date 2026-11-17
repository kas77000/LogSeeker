"""quoteStream search: the order's symbol, grepped inside the order's window.

kdb is faked (no server, no pykx). The end-to-end tests run real tail/head/grep
through the local backend on generated logs, like test_order_window_search.
"""

from __future__ import annotations

import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logseeker import kdb, search as srch  # noqa: E402
from logseeker.config import Settings, config_problems  # noqa: E402
from logseeker.jobs import JobManager, JobStatus, trade_date_for  # noqa: E402
from logseeker.search import (  # noqa: E402
    MATCH_REGEX, STREAM_ORDER, STREAM_QUOTE, SearchRequest, ValidationError,
    build_grep_command, build_list_command, symbol_regex, validate)

ORDER_FILE = "APA2_pingOrderStream_20261112[23-20-00].out"
QUOTE_FILE = "quotestream.261112-112000.log"


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


def _req(settings, **kw):
    base = dict(pattern="1370037780", instances=["APIA"], dates=["20261113"])
    base.update(kw)
    return validate(SearchRequest(**base), settings)


# --------------------------------------------------------------------------- #
# Settings and the file list
# --------------------------------------------------------------------------- #

def test_quote_glob_default_and_yymmdd():
    s = Settings()
    assert s.quote_glob_template == "quotestream.{yymmdd}-*.log"
    assert s.log_glob("APIA", "20261116", STREAM_QUOTE) == "quotestream.261116-*.log"
    assert s.log_glob("APIA", "20261116") == "APA*_pingOrderStream_20261116*.out"


def test_quote_listing_uses_the_shifted_date(settings):
    cmd = build_list_command(_req(settings, stream=STREAM_QUOTE), settings)
    assert "/quotestream.261112-*.log " in cmd
    assert "/quotestream.261112-*.log.gz " in cmd
    assert "pingOrderStream" not in cmd


def test_quote_glob_template_must_hold_a_date(settings):
    bad = replace(settings, quote_glob_template="quotestream.log")
    assert any("quote_glob_template" in p for p in config_problems(bad))
    assert not any("quote_glob_template" in p for p in config_problems(settings))


def test_a_quote_file_maps_to_its_trading_date(settings):
    path = f"/l/{QUOTE_FILE}"
    assert trade_date_for(path, ["20261113", "20261114"], settings) == "20261113"
    # the time part of the name must not pass for another date's yymmdd
    tricky = "/l/quotestream.261112-261113.log"
    assert trade_date_for(tricky, ["20261113", "20261114"], settings) == "20261113"


# --------------------------------------------------------------------------- #
# Validation, cache key and the grep
# --------------------------------------------------------------------------- #

def test_quote_needs_kdb_and_an_exact_id(settings):
    with pytest.raises(ValidationError, match="kdb"):
        _req(replace(settings, kdb_rdb="", kdb_hdb=""), stream=STREAM_QUOTE)
    with pytest.raises(ValidationError, match="exact order id"):
        _req(settings, stream=STREAM_QUOTE, match_mode=MATCH_REGEX, pattern="13700.*")
    with pytest.raises(ValidationError, match="kdb id_target"):
        _req(settings, stream=STREAM_QUOTE, pattern="ORD-1")
    with pytest.raises(ValidationError, match="Unknown log"):
        _req(settings, stream="tradeStream")


def test_order_fingerprint_is_unchanged_and_quote_differs(settings):
    order = _req(settings)
    assert order.stream == STREAM_ORDER
    quote = _req(settings, stream=STREAM_QUOTE)
    assert quote.fingerprint() != order.fingerprint()
    assert replace(quote, symbols=["0700.HK"]).fingerprint() != \
        replace(quote, symbols=["0005.HK"]).fingerprint()
    assert "_quote_" in replace(quote, symbols=["0700.HK"]).result_filename()
    assert "_quote_" not in order.result_filename()
    assert "quoteStream" in quote.label()


def test_symbols_are_allowlisted():
    assert srch.symbol_ok("0700.HK") and srch.symbol_ok("BRK/B") and srch.symbol_ok("ES:Z6")
    for bad in ("", "0700.HK; rm -rf /", "a b", "$(x)", "x" * 33, "70'0"):
        assert not srch.symbol_ok(bad), bad


def test_symbol_regex_matches_whole_tokens_only():
    import re
    rx = re.compile(symbol_regex(["0700.HK", "0005.HK"]))
    for hit in ("sym=0700.HK bid=1", "0005.HK", "[0700.HK]", "x,0700.HK,y"):
        assert rx.search(hit), hit
    for miss in ("sym=10700.HK", "0700.HKD", "0700XHK", "A0005.HK"):
        assert not rx.search(miss), miss


def test_quote_grep_greps_the_symbol_not_the_id(settings):
    req = replace(_req(settings, stream=STREAM_QUOTE), symbols=["0700.HK"])
    cmd = build_grep_command(["/l/q.log"], req, settings, prefix=False)
    assert "0700\\.HK" in cmd and "1370037780" not in cmd


# --------------------------------------------------------------------------- #
# kdb: the sym rides along with the trader
# --------------------------------------------------------------------------- #

def test_symbols_come_from_the_target_rows():
    check = kdb.KdbCheck(kdb.FOUND, sources=[
        kdb.SourceResult("HDB", "h:1", rows=[{"date": "2026-11-12", "trader": "APIA",
                                              "sym": "0700.HK"}]),
        kdb.SourceResult("RDB", "r:1", rows=[{"date": None, "trader": "APIA", "sym": "0700.HK"},
                                             {"date": None, "trader": "APIA", "sym": None}])])
    assert check.symbols == ["0700.HK"]


# --------------------------------------------------------------------------- #
# End to end: one card, one kdb call, one job per stream
# --------------------------------------------------------------------------- #

TOOLS = all(shutil.which(t) for t in ("grep", "tail", "head", "gzip", "awk"))
needs_tools = pytest.mark.skipif(not TOOLS, reason="needs grep, tail, head, gzip, awk on PATH")


def hms(h, m, s=0):
    return ((h * 60 + m) * 60 + s) * 1000


def clock(ms):
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},000"


def quote_log(extra, start=hms(7, 0), end=hms(10, 0)) -> bytes:
    """A quote line a second for some other symbol, plus `extra` {ms: line}."""
    out = []
    for t in range(start, end + 1, 1000):
        out.append(f"{clock(t)} INFO [QuoteSub] sym=0005.HK bid=60.1 ask=60.2")
        if t in extra:
            out.append(f"{clock(t)} INFO [QuoteSub] {extra[t]}")
    return ("\n".join(out) + "\n").encode()


QUOTES = {
    hms(7, 30): "sym=0700.HK bid=1 DECOY early",
    hms(8, 28): "sym=0700.HK bid=2 IN",
    hms(8, 29): "sym=10700.HK bid=3 OTHER SYMBOL",
    hms(8, 30): "sym=0700.HK bid=4 IN",
    hms(9, 30): "sym=0700.HK bid=5 DECOY late",
}


def plant(settings, name, data, instance="APIA", gz=False):
    import gzip
    d = Path(settings.log_dir(instance))
    d.mkdir(parents=True, exist_ok=True)
    path = d / (name + (".gz" if gz else ""))
    path.write_bytes(gzip.compress(data) if gz else data)
    return path


def plant_both(settings, **kw):
    plant(settings, QUOTE_FILE, quote_log(QUOTES), **kw)
    plant(settings, ORDER_FILE, quote_log({hms(8, 28): "orderId=1370037780 NEW"}), **kw)


def found(*, sym="0700.HK", states=None, day="2026-11-13", trader="APIA"):
    if states is None:
        states = [{"date": day, "id_server": "1", "first_ms": hms(8, 27),
                   "last_ms": hms(8, 31), "last_state": "filled"}]
    return kdb.KdbCheck(kdb.FOUND, "Found.", sources=[kdb.SourceResult(
        "HDB", "h:1", rows=[{"date": day, "trader": trader, "sym": sym}], states=states)])


def run_search(settings, monkeypatch, check, streams=(STREAM_ORDER, STREAM_QUOTE),
               instances=("APIA",)):
    calls = []

    def fake_lookup(*a, **k):
        calls.append(a)
        return check

    monkeypatch.setattr(kdb, "lookup_target", fake_lookup)
    req = validate(SearchRequest(pattern="1370037780", instances=list(instances),
                                 dates=["20261113"]), settings)
    s = JobManager(settings).submit_search(req, list(streams))
    deadline = time.time() + 60
    while not s.is_terminal and time.time() < deadline:
        time.sleep(0.05)
    assert s.is_terminal, s
    return s, calls


def by_stream(s):
    return {j.request.stream: j for j in s.jobs}


@needs_tools
def test_both_streams_one_kdb_call_two_results(settings, monkeypatch):
    plant_both(settings)
    s, calls = run_search(settings, monkeypatch, found())
    assert len(calls) == 1
    jobs = by_stream(s)
    assert set(jobs) == {STREAM_ORDER, STREAM_QUOTE}
    order, quote = jobs[STREAM_ORDER], jobs[STREAM_QUOTE]
    assert order.status is JobStatus.DONE, order.error
    assert quote.status is JobStatus.DONE, quote.error
    assert order.result_path != quote.result_path

    body = quote.result_path.read_bytes()
    assert body.count(b"0700.HK") == 2 and b" IN" in body
    assert b"DECOY" not in body and b"10700.HK" not in body
    assert quote.request.symbols == ["0700.HK"]
    assert "_quote_" in quote.result_path.name
    assert b"orderId=1370037780" in order.result_path.read_bytes()


@needs_tools
def test_automatic_instance_is_worked_out_once_for_both(settings, monkeypatch):
    plant_both(settings)
    s, calls = run_search(settings, monkeypatch, found(), instances=())
    assert len(calls) == 1 and s.auto_instances
    assert all(j.request.instances == ["APIA"] for j in s.jobs)
    assert all(j.status is JobStatus.DONE for j in s.jobs), [j.error for j in s.jobs]


@needs_tools
def test_quote_only(settings, monkeypatch):
    plant_both(settings)
    s, _ = run_search(settings, monkeypatch, found(), streams=(STREAM_QUOTE,))
    assert [j.request.stream for j in s.jobs] == [STREAM_QUOTE]
    assert s.jobs[0].status is JobStatus.DONE


@pytest.mark.parametrize("check, reason", [
    (kdb.KdbCheck(kdb.NOT_FOUND, "No target with id_target 1370037780."), "No target"),
    (kdb.KdbCheck(kdb.ERROR, "kdb lookup failed: refused"), "refused"),
    (found(sym=None), "no symbol"),
    (found(sym="07 00"), "cannot be searched"),
    (found(states=[]), "no timed target_state"),
])
@needs_tools
def test_quote_is_skipped_not_read_whole(settings, monkeypatch, check, reason):
    plant_both(settings)
    s, _ = run_search(settings, monkeypatch, check, instances=("APIA",))
    quote = by_stream(s)[STREAM_QUOTE]
    assert quote.status is JobStatus.SKIPPED
    assert reason in quote.error, quote.error
    assert quote.bytes_scanned == 0 and quote.result_path is None
    # orderStream is unaffected
    assert by_stream(s)[STREAM_ORDER].status is JobStatus.DONE


@needs_tools
def test_a_gzipped_quote_log_is_skipped(settings, monkeypatch):
    plant_both(settings, gz=True)
    s, _ = run_search(settings, monkeypatch, found())
    quote = by_stream(s)[STREAM_QUOTE]
    assert quote.status is JobStatus.SKIPPED
    assert any("gzipped" in n for n in quote.scan_notes), quote.scan_notes


@needs_tools
def test_nothing_in_the_quote_window_is_not_rescanned_whole(settings, monkeypatch):
    plant_both(settings)
    early = [{"date": "2026-11-13", "id_server": "1", "first_ms": hms(7, 50),
              "last_ms": hms(7, 52), "last_state": "filled"}]
    s, _ = run_search(settings, monkeypatch, found(states=early))
    quote = by_stream(s)[STREAM_QUOTE]
    assert quote.status is JobStatus.DONE and quote.lines_written == 0
    assert not any("whole file" in w.lower() for w in quote.warnings), quote.warnings


@needs_tools
def test_one_stream_failing_leaves_the_other(settings, monkeypatch):
    plant(settings, ORDER_FILE, quote_log({hms(8, 28): "orderId=1370037780 NEW"}))
    s, _ = run_search(settings, monkeypatch, found())       # no quote file at all
    jobs = by_stream(s)
    assert jobs[STREAM_QUOTE].status is JobStatus.FAILED
    assert "No log files" in jobs[STREAM_QUOTE].error
    assert jobs[STREAM_ORDER].status is JobStatus.DONE


def test_no_instance_from_kdb_fails_the_card_before_any_job(settings, monkeypatch):
    s, _ = run_search(settings, monkeypatch, kdb.KdbCheck(kdb.NOT_FOUND, "No target."),
                      instances=())
    assert s.jobs == [] and "Pick an instance" in s.error


@needs_tools
def test_order_only_search_still_works_on_the_card(settings, monkeypatch):
    plant_both(settings)
    s, _ = run_search(settings, monkeypatch, found(), streams=(STREAM_ORDER,))
    assert [j.request.stream for j in s.jobs] == [STREAM_ORDER]
    assert s.jobs[0].status is JobStatus.DONE and s.kdb.status == kdb.FOUND
