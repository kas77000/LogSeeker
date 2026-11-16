"""The cache serves only searches that finished.

The bug this guards: a search that failed (grep: Permission denied) left an
empty result file behind, and the next identical search — even from an
account that could read the log — was served that empty file as
"done, 0 lines" without the log host ever being asked.
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from logseeker import jobs, store  # noqa: E402
from logseeker.config import Settings  # noqa: E402
from logseeker.jobs import JobManager, JobStatus  # noqa: E402
from logseeker.remote import CommandResult  # noqa: E402
from logseeker.search import SearchRequest, validate  # noqa: E402

LOG = "/apps/k/algologs/ap/APIG/logs/APA2_pingOrderStream_20261110[15-15-41].out"


def ok(stderr="LOGSEEKER_GREP_RC=0", **kw):
    return CommandResult(exit_status=0, stderr=stderr, bytes_read=0, duration_s=0.0, **kw)


class FakeHost:
    """A log host whose grep answer is set per test."""

    def __init__(self, grep):
        self.grep = grep          # callable(sink) -> CommandResult

    @contextlib.contextmanager
    def session(self):
        yield self

    def run(self, cmd, *, sink, timeout, max_bytes, cancel=None):
        if cmd.startswith("du "):
            sink(f"1024\t{LOG}\n".encode())
            return ok(stderr="")
        return self.grep(sink)


def readable(sink):
    sink(f"{LOG}:07:50:01,764 orderId=1130005583 NEW\n".encode())
    return ok()


def denied(sink):
    return ok(stderr=f"grep: {LOG}: Permission denied\nLOGSEEKER_GREP_RC=2")


def timed_out(sink):
    sink(b"07:50:01,764 partial\n")
    return ok(timed_out=True)


def cancelled(sink):
    return ok(cancelled=True)


@pytest.fixture
def settings(tmp_path):
    return Settings(backend="ssh", instances=["APIG"], data_dir=tmp_path / "data",
                    workers=1, cache_ttl_hours=12)


def run(settings, monkeypatch, grep):
    monkeypatch.setattr(jobs, "get_backend", lambda s: FakeHost(grep))
    req = validate(SearchRequest(pattern="1130005583", instances=["APIG"],
                                 dates=["20261110"]), settings)
    job = JobManager(settings).submit(req)
    deadline = time.time() + 30
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.02)
    return job, req


def test_a_failed_search_is_not_served_to_the_next_person(settings, monkeypatch):
    first, req = run(settings, monkeypatch, denied)
    assert first.status is JobStatus.FAILED and "Permission denied" in first.error
    assert not store.result_path(settings, req).exists()

    second, _ = run(settings, monkeypatch, readable)
    assert second.status is JobStatus.DONE, second.error
    assert not second.cache_hit
    assert second.lines_written == 1


def test_a_finished_search_is_served_from_cache(settings, monkeypatch):
    run(settings, monkeypatch, readable)
    again, _ = run(settings, monkeypatch, denied)     # host would fail now — cache answers
    assert again.status is JobStatus.DONE and again.cache_hit and again.lines_written == 1


def test_a_timed_out_search_is_kept_for_download_but_not_served(settings, monkeypatch):
    job, req = run(settings, monkeypatch, timed_out)
    assert job.status is JobStatus.DONE and job.lines_written == 1
    assert any("time limit" in w for w in job.warnings)
    assert store.lookup(settings, req) is None

    again, _ = run(settings, monkeypatch, readable)
    assert not again.cache_hit


def test_a_cancelled_search_leaves_nothing_behind(settings, monkeypatch):
    job, req = run(settings, monkeypatch, cancelled)
    assert job.status is JobStatus.CANCELLED
    assert not store.result_path(settings, req).exists()


def test_a_result_without_a_sidecar_is_not_served(settings):
    req = validate(SearchRequest(pattern="1130005583", instances=["APIG"],
                                 dates=["20261110"]), settings)
    path = store.open_for_write(settings, req)
    path.write_text("")                           # what an older version left behind
    assert store.lookup(settings, req) is None


# --------------------------------------------------------------------------- #
# A result is only handed out once it is entirely written
# --------------------------------------------------------------------------- #

def test_the_result_is_written_as_part_and_renamed_to_log_when_finished(settings, monkeypatch):
    seen = {}

    def grep_and_look(sink):
        sink(f"07:50:01,764 orderId=1130005583 NEW\n".encode())
        folder = next(settings.results_dir.iterdir())
        seen["during"] = sorted(p.name for p in folder.iterdir())
        return ok()

    job, req = run(settings, monkeypatch, grep_and_look)
    assert job.status is JobStatus.DONE, job.error

    # While grep ran, only the .part file existed — nothing anyone could take.
    assert len(seen["during"]) == 1 and seen["during"][0].endswith(".log.part")

    assert job.result_path.name.endswith(".log")
    assert job.result_path == store.result_path(settings, req)
    leftovers = [p.name for p in job.result_path.parent.iterdir() if p.name.endswith(".part")]
    assert leftovers == []
    assert job.result_path.read_bytes() == b"07:50:01,764 orderId=1130005583 NEW\n"


def test_an_unfinished_result_is_never_compressed(tmp_path):
    part = tmp_path / "r.log.part"
    part.write_bytes(b"half")
    with pytest.raises(ValueError, match="still being written"):
        store.gzipped(part)


def test_the_gzip_is_named_log_gz_and_leaves_no_temp_file(tmp_path):
    import gzip
    log = tmp_path / "r.log"
    log.write_bytes(b"08:00:00,000 a\n" * 1000)
    gz = store.gzipped(log)
    assert gz.name == "r.log.gz"
    assert gzip.decompress(gz.read_bytes()) == log.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r.log", "r.log.gz"]


def test_the_file_inside_the_gzip_is_named_after_the_log(tmp_path):
    # 7-Zip and WinRAR show the name stored in the gzip header, not the name of
    # the .gz itself. It used to be the temporary "r.log.gz.<id>.part".
    log = tmp_path / "r.log"
    log.write_bytes(b"08:00:00,000 a\n" * 1000)
    assert store.gzip_member_name(store.gzipped(log)) == "r.log"


def test_a_gzip_built_with_the_temp_name_inside_is_rebuilt(tmp_path):
    import gzip
    log = tmp_path / "r.log"
    log.write_bytes(b"08:00:00,000 a\n" * 1000)
    stale = tmp_path / "r.log.gz.137643f5.part"
    with gzip.open(stale, "wb") as fh:                # how it used to be built
        fh.write(log.read_bytes())
    stale.replace(tmp_path / "r.log.gz")
    assert store.gzip_member_name(tmp_path / "r.log.gz") == "r.log.gz.137643f5.part"

    gz = store.gzipped(log)
    assert store.gzip_member_name(gz) == "r.log"
    assert gzip.decompress(gz.read_bytes()) == log.read_bytes()


def test_a_failed_search_leaves_neither_part_nor_log(settings, monkeypatch):
    job, req = run(settings, monkeypatch, denied)
    assert job.status is JobStatus.FAILED
    final = store.result_path(settings, req)
    assert not final.exists()
    assert not final.with_name(final.name + ".part").exists()


def test_finalize_falls_back_to_copy_when_rename_keeps_failing(tmp_path, monkeypatch):
    part = tmp_path / "r.log.part"
    part.write_bytes(b"08:00:00,000 whole answer\n")

    def locked(self, target):
        raise PermissionError("file in use by another process")

    monkeypatch.setattr(Path, "replace", locked)
    final = store.finalize(part, attempts=3, wait_s=0)
    assert final.name == "r.log"
    assert final.read_bytes() == b"08:00:00,000 whole answer\n"
    assert not part.exists()


def test_a_leftover_part_from_a_restart_is_deleted_not_renamed(settings):
    folder = settings.results_dir / "abc"
    folder.mkdir(parents=True)
    (folder / "r.log.part").write_bytes(b"cut off half way")
    (folder / "done.log").write_bytes(b"finished\n")

    JobManager(settings)                              # app start

    assert sorted(p.name for p in folder.iterdir()) == ["done.log"]
