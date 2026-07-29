"""Behaviour that only matters once the logs get big.

These lock in the guarantees the README makes: constant memory, caps that
announce themselves rather than silently truncating, and a results tree that
cannot grow without bound.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from logseeker import store  # noqa: E402
from logseeker.config import Settings  # noqa: E402
from logseeker.jobs import JobManager, JobStatus  # noqa: E402
from logseeker.search import SearchRequest, validate  # noqa: E402

needs_grep = pytest.mark.skipif(shutil.which("grep") is None, reason="needs grep on PATH")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        backend="local",
        instances=["APIA"],
        log_dir_template=(tmp_path / "logs").as_posix() + "/{instance}",
        data_dir=tmp_path / "data",
        workers=1,
        cache_ttl_hours=1,
        command_timeout=60,
    )


def write_log(settings: Settings, lines: int, *, date: str = "20260421") -> Path:
    log_dir = Path(settings.log_dir("APIA"))
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"APA1_pingOrderStream_{date}_1.out"
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for i in range(lines):
            fh.write(f"{date} 09:00:00 orderId=540000001 seq={i} status=ACK\n")
    return path


def run(settings: Settings, **kw):
    req = validate(SearchRequest(
        pattern=kw.pop("pattern", "540000001"),
        instances=["APIA"],
        dates=[kw.pop("date", "20260421")],
        **kw,
    ), settings)
    manager = JobManager(settings)
    job = manager.submit(req)
    deadline = time.time() + 90
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.05)
    return job


# --------------------------------------------------------------------------- #
# Caps must announce themselves
# --------------------------------------------------------------------------- #

@needs_grep
def test_line_cap_warns_instead_of_silently_truncating(settings, tmp_path):
    """`head -n` kills grep via SIGPIPE, so a capped result looks complete.

    Returning 500 of 5000 matches with no warning is how someone concludes an
    order stopped trading at 09:03.
    """
    capped = Settings(**{**settings.__dict__, "max_result_lines": 500})
    write_log(capped, 5000)
    job = run(capped)

    assert job.status is JobStatus.DONE
    assert job.lines_written == 500
    assert job.truncated is True
    assert any("500-line cap" in w for w in job.warnings), job.warnings


@needs_grep
def test_byte_cap_warns_and_stops_writing(settings):
    small = Settings(**{**settings.__dict__, "max_result_bytes": 20_000})
    write_log(small, 5000)
    job = run(small)

    assert job.status is JobStatus.DONE
    assert job.truncated is True
    assert job.bytes_written <= 20_000
    assert job.result_path.stat().st_size <= 20_000
    assert any("size cap" in w for w in job.warnings), job.warnings


@needs_grep
def test_uncapped_result_carries_no_truncation_warning(settings):
    write_log(settings, 100)
    job = run(settings)
    assert job.lines_written == 100
    assert job.truncated is False
    assert job.warnings == []


# --------------------------------------------------------------------------- #
# Cache metadata: a hit must not re-read the payload
# --------------------------------------------------------------------------- #

@needs_grep
def test_cache_hit_reads_line_count_from_the_sidecar(settings, monkeypatch):
    write_log(settings, 2000)
    first = run(settings)
    assert first.lines_written == 2000
    assert not first.cache_hit

    meta = store.read_meta(first.result_path)
    assert meta and meta["lines"] == 2000

    def explode(_path):
        raise AssertionError("cache hit must not re-read the result file")

    monkeypatch.setattr(store, "count_lines", explode)
    second = run(settings)
    assert second.cache_hit is True
    assert second.lines_written == 2000


# --------------------------------------------------------------------------- #
# Compression
# --------------------------------------------------------------------------- #

@needs_grep
def test_gzip_is_built_once_and_reused(settings):
    write_log(settings, 5000)
    job = run(settings)

    gz = store.gzipped(job.result_path)
    assert gz.exists()
    assert gz.stat().st_size < job.result_path.stat().st_size

    stamp = gz.stat().st_mtime_ns
    assert store.gzipped(job.result_path).stat().st_mtime_ns == stamp, \
        "second call must reuse the existing archive"


# --------------------------------------------------------------------------- #
# Disk budget
# --------------------------------------------------------------------------- #

def test_disk_budget_evicts_oldest_first(settings):
    results = settings.results_dir
    for name, age in [("old", 10_000), ("mid", 5_000), ("new", 0)]:
        d = results / name
        d.mkdir(parents=True, exist_ok=True)
        f = d / "r.txt"
        f.write_bytes(b"x" * 1000)
        stamp = time.time() - age
        import os
        os.utime(f, (stamp, stamp))

    tight = Settings(**{**settings.__dict__, "max_disk_bytes": 2500})
    removed, freed = store.enforce_disk_budget(tight)

    assert removed == 1 and freed == 1000
    assert not (results / "old").exists()
    assert (results / "mid").exists() and (results / "new").exists()


def test_disk_budget_is_a_noop_when_under_budget(settings):
    d = settings.results_dir / "a"
    d.mkdir(parents=True, exist_ok=True)
    (d / "r.txt").write_bytes(b"x" * 100)
    assert store.enforce_disk_budget(settings) == (0, 0)
    assert d.exists()


# --------------------------------------------------------------------------- #
# Audit log must not be read whole
# --------------------------------------------------------------------------- #

def test_audit_tail_reads_only_the_end_of_a_large_file(settings):
    from logseeker import audit

    path = settings.audit_path
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(20_000):
        audit.record(path, "search_completed", job_id=str(i), filler="y" * 200)

    assert path.stat().st_size > 4 * 1024 * 1024

    entries = audit.tail(path, limit=5, max_bytes=64 * 1024)
    assert len(entries) == 5
    # Newest first, and every record parsed cleanly despite the mid-line seek.
    assert entries[0]["job_id"] == "19999"
    assert all("event" in e for e in entries)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
