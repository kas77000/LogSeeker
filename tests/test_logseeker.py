"""Tests for the parts that must not be wrong: input validation, command
construction, and an end-to-end search through the local backend.

    python -m pytest tests -q      (or:  python tests/test_logseeker.py)
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logseeker.config import Settings          # noqa: E402
from logseeker.jobs import JobManager, JobStatus  # noqa: E402
from logseeker.search import (                 # noqa: E402
    MATCH_FIXED,
    MATCH_REGEX,
    SearchRequest,
    ValidationError,
    build_grep_command,
    build_list_command,
    date_range,
    validate,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        backend="local",
        instances=["APIA", "APIE"],
        # as_posix(): the commands are POSIX shell, so on Windows the paths must
        # use forward slashes for bash to understand them.
        log_dir_template=(tmp_path / "logs").as_posix() + "/{instance}",
        data_dir=tmp_path / "data",
        workers=2,
        cache_ttl_hours=1,
        command_timeout=30,
    )


def req(**kw) -> SearchRequest:
    base = dict(pattern="540000001", instances=["APIA"], dates=["20260421"])
    base.update(kw)
    return SearchRequest(**base)


# --------------------------------------------------------------------------- #
# Validation — this is the security boundary, so the hostile cases matter most.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [
    "540000001; rm -rf /",
    "$(cat /etc/passwd)",
    "`id`",
    "a && curl evil.example.com",
    "x|y",
    "../../etc/passwd",
    "'",
    "",
    "   ",
    "A" * 65,
])
def test_shell_metacharacters_rejected_in_exact_mode(bad, settings):
    with pytest.raises(ValidationError):
        validate(req(pattern=bad), settings)


def test_valid_order_ids_accepted(settings):
    for good in ["540000001", "ORD-12345", "a.b_c:d", "0"]:
        assert validate(req(pattern=good), settings).pattern == good


def test_unknown_instance_rejected(settings):
    """An arbitrary instance would let a caller aim the glob at any directory."""
    with pytest.raises(ValidationError):
        validate(req(instances=["../../etc"]), settings)
    with pytest.raises(ValidationError):
        validate(req(instances=["NOTCONFIGURED"]), settings)


def test_bad_dates_rejected(settings):
    for bad in ["2026-04-21", "2026042", "abcdefgh", "2026042*"]:
        with pytest.raises(ValidationError):
            validate(req(dates=[bad]), settings)


def test_date_range_limit(settings):
    dates = [f"202604{d:02d}" for d in range(1, 30)]
    with pytest.raises(ValidationError):
        validate(req(dates=dates), settings)


def test_regex_mode_allows_metacharacters_but_quotes_them(settings):
    r = validate(req(pattern=r"orderId=5400000\d+", match_mode=MATCH_REGEX), settings)
    cmd = build_grep_command(["/logs/a.out"], r, settings)
    # The pattern must reach grep intact, but inside single quotes.
    assert "'orderId=5400000\\d+'" in cmd
    assert cmd.count("|") == 1  # only the pipe into head


def test_invalid_regex_rejected(settings):
    with pytest.raises(ValidationError):
        validate(req(pattern="[unclosed", match_mode=MATCH_REGEX), settings)


def test_filenames_are_quoted_in_grep_command(settings):
    r = validate(req(), settings)
    cmd = build_grep_command(["/logs/weird name;rm -rf /.out"], r, settings)
    assert "'/logs/weird name;rm -rf /.out'" in cmd


# --------------------------------------------------------------------------- #
# Command shape
# --------------------------------------------------------------------------- #

def test_list_command_covers_every_instance_and_date(settings):
    r = validate(req(instances=["APIA", "APIE"], dates=["20260421", "20260422"]), settings)
    cmd = build_list_command(r, settings)
    assert cmd.count("_pingOrderStream_") == 4
    assert "APIA" in cmd and "APIE" in cmd


def test_glob_quoting_survives_spaces_in_paths(settings):
    from logseeker.search import quote_glob

    quoted = quote_glob("/var/log dir/APA*_ping_2026*.out")
    assert "'" in quoted                       # the spacey literal got quoted
    assert quoted.count("*") == 2              # both globs stayed live
    assert " " not in quoted.replace("log dir", "logdir")  # no bare space left

    spacey = Settings(
        backend="local",
        instances=["APIA"],
        log_dir_template="/apps/my logs/{instance}",
        data_dir=settings.data_dir,
    )
    cmd = build_list_command(validate(req(), spacey), spacey)
    assert "'/apps/my logs/APIA/APA'" in cmd


def test_grep_command_forces_filename_prefix_and_caps_lines(settings):
    r = validate(req(), settings)
    cmd = build_grep_command(["/logs/a.out"], r, settings)
    assert "/dev/null" in cmd, "needed so single-file greps still show the filename"
    assert f"head -n {settings.max_result_lines}" in cmd


def test_exact_mode_uses_fixed_string_matching(settings):
    assert " -F " in build_grep_command(["/a"], validate(req(), settings), settings)


# --------------------------------------------------------------------------- #
# Request identity
# --------------------------------------------------------------------------- #

def test_fingerprint_ignores_requester(settings):
    a = validate(req(requested_by="alice"), settings)
    b = validate(req(requested_by="bob"), settings)
    assert a.fingerprint() == b.fingerprint()


def test_fingerprint_distinguishes_dates(settings):
    a = validate(req(dates=["20260421"]), settings)
    b = validate(req(dates=["20260422"]), settings)
    assert a.fingerprint() != b.fingerprint()


def test_result_filename_is_collision_free(settings):
    """The legacy flow wrote <ORDER_ID>.txt for every search; these must differ."""
    a = validate(req(dates=["20260421"]), settings)
    b = validate(req(dates=["20260422"]), settings)
    c = validate(req(instances=["APIE"]), settings)
    assert len({a.result_filename(), b.result_filename(), c.result_filename()}) == 3


def test_date_range_skips_weekends():
    # 2026-04-17 is a Friday, 2026-04-20 a Monday.
    out = date_range(date(2026, 4, 17), date(2026, 4, 20))
    assert out == ["20260417", "20260420"]
    assert len(date_range(date(2026, 4, 17), date(2026, 4, 20), weekdays_only=False)) == 4


# --------------------------------------------------------------------------- #
# End to end, through the local backend
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep on PATH")
def test_end_to_end_search(settings, tmp_path):
    log_dir = Path(settings.log_dir("APIA"))
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "APA1_pingOrderStream_20260421_1.out").write_text(
        "line one orderId=540000001 status=ACK\n"
        "line two orderId=540000002 status=ACK\n"
        "line three orderId=540000001 status=FILL\n",
        encoding="utf-8",
    )

    manager = JobManager(settings)
    job = manager.submit(validate(req(), settings))

    deadline = time.time() + 30
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.1)

    assert job.status is JobStatus.DONE, job.error
    assert job.files_found == 1
    assert job.lines_written == 2
    assert job.result_path is not None
    body = job.result_path.read_text(encoding="utf-8")
    assert "540000002" not in body
    assert body.count("540000001") == 2


@pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep on PATH")
def test_missing_logs_fail_clearly(settings):
    manager = JobManager(settings)
    job = manager.submit(validate(req(dates=["20200101"]), settings))
    deadline = time.time() + 30
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.1)
    assert job.status is JobStatus.FAILED
    assert "No log files" in job.error


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
