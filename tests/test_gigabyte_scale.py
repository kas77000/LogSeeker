"""Behaviour that only shows up once a single log shard is a gigabyte.

At that size the file *count* stops being a useful guard, an elapsed-time
progress bar stops meaning anything, and a search can plausibly exceed the
timeout — so partial results and honest reporting matter more than raw speed.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from logseeker.config import Settings  # noqa: E402
from logseeker.jobs import JobManager, JobStatus  # noqa: E402
from logseeker.search import (  # noqa: E402
    LogFile,
    SearchRequest,
    batch_files,
    build_grep_command,
    parse_file_list,
    validate,
)

needs_grep = pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep on PATH")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        backend="local",
        instances=["APIA"],
        log_dir_template=(tmp_path / "logs").as_posix() + "/{instance}",
        data_dir=tmp_path / "data",
        workers=1,
        cache_ttl_hours=0,
        command_timeout=60,
    )


def write_logs(settings: Settings, shards: int, lines_each: int) -> None:
    log_dir = Path(settings.log_dir("APIA"))
    log_dir.mkdir(parents=True, exist_ok=True)
    for shard in range(1, shards + 1):
        p = log_dir / f"APA{shard}_pingOrderStream_20260421_{shard}.out"
        with p.open("w", encoding="utf-8", newline="\n") as fh:
            for i in range(lines_each):
                fh.write(f"20260421 09:00:00 orderId=540000001 shard={shard} seq={i}\n")


def run(settings: Settings, **kw):
    req = validate(SearchRequest(
        pattern=kw.pop("pattern", "540000001"),
        instances=["APIA"], dates=["20260421"], **kw,
    ), settings)
    job = JobManager(settings).submit(req)
    deadline = time.time() + 120
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.05)
    return job


# --------------------------------------------------------------------------- #
# Listing now carries sizes
# --------------------------------------------------------------------------- #

def test_parse_du_output_with_tabs(settings):
    files, capped = parse_file_list("1024\t/logs/a.out\n2048\t/logs/b.out\n", settings)
    assert not capped
    assert [f.path for f in files] == ["/logs/a.out", "/logs/b.out"]
    assert [f.size_bytes for f in files] == [1024 * 1024, 2048 * 1024]


def test_parse_du_output_handles_spaces_in_paths(settings):
    files, _ = parse_file_list("1024\t/var/log dir/a b.out\n", settings)
    assert files[0].path == "/var/log dir/a b.out"


def test_parse_du_output_tolerates_space_separated_variants(settings):
    files, _ = parse_file_list("1024   /logs/a.out\n", settings)
    assert files[0].path == "/logs/a.out"
    assert files[0].size_bytes == 1024 * 1024


def test_parse_du_output_skips_junk(settings):
    files, _ = parse_file_list("du: cannot access\n\n1024\t/logs/a.out\n", settings)
    assert [f.path for f in files] == ["/logs/a.out"]


def test_file_count_cap_still_applies(settings):
    tight = Settings(**{**settings.__dict__, "max_files_per_search": 2})
    raw = "".join(f"10\t/logs/f{i}.out\n" for i in range(5))
    files, capped = parse_file_list(raw, tight)
    assert capped and len(files) == 2


# --------------------------------------------------------------------------- #
# Batching
# --------------------------------------------------------------------------- #

def test_gigabyte_files_each_get_their_own_batch(settings):
    gb = 1024 ** 3
    files = [LogFile(f"/logs/f{i}.out", gb) for i in range(3)]
    batches = batch_files(files, settings)
    assert len(batches) == 3, "each 1 GB shard should report progress separately"


def test_small_files_are_grouped_to_limit_round_trips(settings):
    files = [LogFile(f"/logs/f{i}.out", 1024) for i in range(100)]
    batches = batch_files(files, settings)
    assert len(batches) == 4, "100 tiny files should not become 100 SSH commands"
    assert sum(len(b) for b in batches) == 100


def test_every_file_appears_exactly_once(settings):
    files = [LogFile(f"/logs/f{i}.out", 300 * 1024 * 1024) for i in range(7)]
    seen = [f.path for b in batch_files(files, settings) for f in b]
    assert seen == [f.path for f in files]


# --------------------------------------------------------------------------- #
# The line cap must stay exact across batches
# --------------------------------------------------------------------------- #

def test_grep_command_uses_the_remaining_line_budget(settings):
    req = validate(SearchRequest(pattern="x", instances=["APIA"], dates=["20260421"]), settings)
    cmd = build_grep_command(["/logs/a.out"], req, settings, line_budget=37)
    assert "head -n 37" in cmd


@needs_grep
def test_line_cap_is_global_not_per_batch(settings):
    """Three shards, 300 matches each, cap of 100.

    A per-batch cap would return 300. The budget has to shrink as batches
    complete or the documented limit is a lie.
    """
    capped = Settings(**{
        **settings.__dict__,
        "max_result_lines": 100,
        "scan_batch_bytes": 1,      # force one batch per file
    })
    write_logs(capped, shards=3, lines_each=300)
    job = run(capped)

    assert job.status is JobStatus.DONE
    assert job.lines_written == 100, "cap must apply to the whole search"
    assert job.truncated is True
    assert any("100-line cap" in w for w in job.warnings), job.warnings


# --------------------------------------------------------------------------- #
# Pre-flight size guard
# --------------------------------------------------------------------------- #

@needs_grep
def test_oversized_search_is_refused_before_reading_anything(settings):
    write_logs(settings, shards=3, lines_each=2000)
    tiny = Settings(**{**settings.__dict__, "max_scan_bytes": 1000})
    job = run(tiny)

    assert job.status is JobStatus.FAILED
    assert "over the" in job.error and "limit" in job.error
    assert job.bytes_written == 0, "must refuse before reading a byte"


@needs_grep
def test_search_within_the_budget_is_allowed(settings):
    write_logs(settings, shards=2, lines_each=100)
    job = run(settings)
    assert job.status is JobStatus.DONE
    assert job.lines_written == 200


# --------------------------------------------------------------------------- #
# Progress accounting
# --------------------------------------------------------------------------- #

@needs_grep
def test_progress_reaches_100_percent_and_matches_the_estimate(settings):
    write_logs(settings, shards=3, lines_each=500)
    job = run(settings)

    assert job.status is JobStatus.DONE
    assert job.bytes_to_scan > 0
    assert job.bytes_scanned == job.bytes_to_scan
    assert job.scan_fraction == pytest.approx(1.0)


@needs_grep
def test_timeout_keeps_partial_results_and_says_how_far_it_got(settings, monkeypatch):
    """A search that runs out of time keeps what it already found.

    Driven by a backend that times out the second grep rather than by wall
    clock, so the assertion is about the handling and not about how fast this
    machine happens to be.
    """
    from contextlib import contextmanager

    import logseeker.jobs as jobs_mod
    from logseeker.remote import CommandResult, LocalBackend

    write_logs(settings, shards=3, lines_each=200)
    cfg = Settings(**{**settings.__dict__, "scan_batch_bytes": 1})
    real = LocalBackend(cfg)
    state = {"greps": 0}

    class TimingOutBackend:
        @contextmanager
        def session(self):
            with real.session() as inner:
                class Session:
                    def run(_self, command, **kw):
                        if not command.lstrip().startswith("du "):   # a grep batch
                            state["greps"] += 1
                            if state["greps"] > 1:
                                return CommandResult(
                                    exit_status=None, stderr="", bytes_read=0,
                                    duration_s=0.0, timed_out=True,
                                )
                        return inner.run(command, **kw)
                yield Session()

    monkeypatch.setattr(jobs_mod, "get_backend", lambda _s: TimingOutBackend())
    job = run(cfg)

    assert job.status is JobStatus.DONE
    assert job.lines_written == 200, "the shard that completed must be kept"
    assert any("partial" in w for w in job.warnings), job.warnings

    # Progress must reflect only what was actually read: one of three shards.
    assert 0 < job.bytes_scanned < job.bytes_to_scan
    assert job.scan_fraction == pytest.approx(1 / 3, abs=0.05)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
