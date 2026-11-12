"""Job queue.

A grep across a day of order-stream logs takes minutes. Three things follow:

1. It cannot run inside a Streamlit rerun — the requester's browser would hang
   and every other widget interaction would be blocked.
2. It must be bounded. Ten people hitting Search at once must not become ten
   concurrent greps on a production host.
3. Two people asking the identical question should share one grep.

So searches run on a small worker pool owned by this module, and the UI polls
the job records. The manager is deliberately a *process-wide* singleton rather
than per-session state: the whole point is that the queue and its results are
shared by everyone using the app.
"""

from __future__ import annotations

import io
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import audit, kdb, store
from .config import Settings, get_settings
from .remote import CommandResult, RemoteError, get_backend
from .search import (
    SearchRequest,
    batch_files,
    build_grep_command,
    build_list_command,
    parse_file_list,
)


class JobStatus(str, Enum):
    QUEUED = "queued"
    CHECKING_KDB = "checking_kdb"
    LISTING = "listing"
    SEARCHING = "searching"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED}


@dataclass
class Job:
    id: str
    request: SearchRequest
    status: JobStatus = JobStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    files_found: int = 0
    files_capped: bool = False
    bytes_to_scan: int = 0      # total size of the logs this search will read
    bytes_scanned: int = 0      # how much of that is done
    bytes_written: int = 0      # size of the result
    lines_written: int = 0
    result_path: Path | None = None
    truncated: bool = False
    cache_hit: bool = False
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    command: str = ""
    kdb: "kdb.KdbCheck | None" = None   # None until the kdb check has run

    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- read helpers ------------------------------------------------------- #

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    @property
    def wait_time(self) -> float:
        return (self.started_at or time.time()) - self.created_at

    @property
    def matched(self) -> bool:
        return self.status is JobStatus.DONE and self.lines_written > 0

    @property
    def scan_fraction(self) -> float:
        """Real progress: bytes of log read, not time elapsed."""
        if self.bytes_to_scan <= 0:
            return 0.0
        return min(1.0, self.bytes_scanned / self.bytes_to_scan)

    @property
    def eta_seconds(self) -> float | None:
        """Estimate from throughput so far. None until there is enough to go on."""
        if self.bytes_scanned <= 0 or self.elapsed < 2:
            return None
        remaining = max(0, self.bytes_to_scan - self.bytes_scanned)
        rate = self.bytes_scanned / self.elapsed
        return remaining / rate if rate > 0 else None

    def cancel(self) -> None:
        self._cancel.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "label": self.request.label(),
                "requested_by": self.request.requested_by,
                "files_found": self.files_found,
                "lines": self.lines_written,
                "bytes": self.bytes_written,
                "elapsed": self.elapsed,
                "error": self.error,
                "cache_hit": self.cache_hit,
                "truncated": self.truncated,
            }


def ui_bytes(n: int) -> str:
    """Byte formatter for warning text (ui.py imports jobs, so not from there)."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}"
        value /= 1024
    return f"{value:,.0f} GB"


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, settings.workers),
            thread_name_prefix="logseeker",
        )
        # Results from previous runs of the app are still on disk. Clear what has
        # expired before serving anyone, so a restart is also a tidy-up.
        self._housekeep()

    def _housekeep(self) -> None:
        """Expire old results and hold the tree to its byte budget."""
        try:
            store.purge_expired(self.settings)
            removed, freed = store.enforce_disk_budget(self.settings)
            if removed:
                audit.record(
                    self.settings.audit_path, "cache_evicted",
                    directories=removed, bytes_freed=freed,
                )
        except OSError:
            pass  # housekeeping must never take a search down with it

    # -- public API --------------------------------------------------------- #

    def submit(self, request: SearchRequest) -> Job:
        """Queue a search — or hand back an equivalent one already in flight."""
        fingerprint = request.fingerprint()
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job.request.fingerprint() == fingerprint and not job.status.is_terminal:
                    return job

            job = Job(id=uuid.uuid4().hex[:10], request=request)
            self._jobs[job.id] = job
            while len(self._jobs) > self.settings.job_history:
                self._jobs.popitem(last=False)

        audit.record(
            self.settings.audit_path,
            "search_submitted",
            job_id=job.id,
            user=request.requested_by,
            pattern=request.pattern,
            instances=request.instances,
            dates=request.dates,
            match_mode=request.match_mode,
            note=request.note,
        )
        self._pool.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self, limit: int = 50) -> list[Job]:
        with self._lock:
            return list(reversed(list(self._jobs.values())))[:limit]

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if not j.status.is_terminal)

    # -- worker ------------------------------------------------------------- #

    def _set(self, job: Job, **fields) -> None:
        with job._lock:
            for key, value in fields.items():
                setattr(job, key, value)

    def _run(self, job: Job) -> None:
        settings = self.settings
        self._set(job, started_at=time.time())

        # The cheap question first: does kdb know this id_target, and whose is
        # it? Informational only — a kdb outage or a miss never stops the grep,
        # because the logs are the authority this app exists to fetch.
        if settings.kdb_enabled:
            self._set(job, status=JobStatus.CHECKING_KDB)
            check = kdb.lookup_target(settings, job.request.pattern, job.request.dates,
                                      exact=job.request.match_mode == "fixed")
            self._set(job, kdb=check)
            audit.record(settings.audit_path, "kdb_checked",
                         job_id=job.id, user=job.request.requested_by,
                         pattern=job.request.pattern, status=check.status,
                         traders=check.traders, message=check.message)

        self._set(job, status=JobStatus.LISTING)

        try:
            cached = store.lookup(settings, job.request)
            if cached is not None:
                self._set(
                    job,
                    status=JobStatus.DONE,
                    finished_at=time.time(),
                    result_path=cached.path,
                    bytes_written=cached.size_bytes,
                    lines_written=cached.lines,
                    cache_hit=True,
                )
                audit.record(settings.audit_path, "search_cache_hit",
                             job_id=job.id, user=job.request.requested_by,
                             pattern=job.request.pattern, lines=job.lines_written)
                return

            backend = get_backend(settings)

            # One connection for the whole job: the listing plus every grep
            # batch. At gigabyte scale a search is several round trips and
            # reconnecting for each would be pure waste.
            with backend.session() as sess:
                # --- step 1: which log files exist, and how big are they? ----
                listing = io.BytesIO()
                list_cmd = build_list_command(job.request, settings)
                self._set(job, command=list_cmd)
                list_result = sess.run(
                    list_cmd,
                    sink=listing.write,
                    timeout=min(120, settings.command_timeout),
                    max_bytes=4 * 1024 * 1024,
                    cancel=job._cancel,
                )
                if job._cancel.is_set():
                    raise _Cancelled()
                if list_result.timed_out:
                    raise RuntimeError("Timed out listing log files on the log host.")
                if list_result.exit_status not in (0, None) and list_result.stderr:
                    raise RuntimeError(f"Could not list log files: {list_result.stderr}")

                files, capped = parse_file_list(
                    listing.getvalue().decode("utf-8", "replace"), settings
                )
                total_bytes = sum(f.size_bytes for f in files)
                self._set(job, files_found=len(files), files_capped=capped,
                          bytes_to_scan=total_bytes)

                if not files:
                    self._set(
                        job,
                        status=JobStatus.FAILED,
                        finished_at=time.time(),
                        error=(
                            "No log files exist for that instance/date combination. "
                            "Check the date is a trading day and the instance is right."
                        ),
                    )
                    audit.record(settings.audit_path, "search_no_files",
                                 job_id=job.id, user=job.request.requested_by,
                                 pattern=job.request.pattern, dates=job.request.dates)
                    return

                # File *count* is a poor guard when a shard is a gigabyte, so
                # refuse on total size — before reading a byte, rather than
                # discovering it by timing out ten minutes later.
                if total_bytes > settings.max_scan_bytes:
                    self._set(
                        job,
                        status=JobStatus.FAILED,
                        finished_at=time.time(),
                        error=(
                            f"That search would scan {ui_bytes(total_bytes)} across "
                            f"{len(files)} files, over the {ui_bytes(settings.max_scan_bytes)} "
                            "limit. Narrow the date range or pick one instance."
                        ),
                    )
                    audit.record(settings.audit_path, "search_too_large",
                                 job_id=job.id, user=job.request.requested_by,
                                 pattern=job.request.pattern,
                                 bytes_to_scan=total_bytes, files=len(files))
                    return

                if capped:
                    job.warnings.append(
                        f"More than {settings.max_files_per_search} log files matched; "
                        f"only the first {len(files)} were scanned. Narrow the date range."
                    )

                # --- step 2: grep, in batches so progress is real ------------
                self._set(job, status=JobStatus.SEARCHING)
                out_path = store.open_for_write(settings, job.request)

                written = {"bytes": 0, "lines": 0}
                result = None
                batches = batch_files(files, settings)
                deadline = time.time() + settings.command_timeout

                with out_path.open("wb") as fh:
                    def sink(chunk: bytes) -> None:
                        fh.write(chunk)
                        written["bytes"] += len(chunk)
                        written["lines"] += chunk.count(b"\n")
                        # Cheap enough to publish on every chunk; the UI polls this.
                        self._set(job,
                                  bytes_written=written["bytes"],
                                  lines_written=written["lines"])

                    for batch in batches:
                        if job._cancel.is_set():
                            raise _Cancelled()

                        remaining_lines = settings.max_result_lines - written["lines"]
                        if remaining_lines <= 0:
                            break
                        remaining_time = deadline - time.time()
                        if remaining_time <= 0:
                            result = CommandResult(
                                exit_status=None, stderr="", bytes_read=0,
                                duration_s=0.0, timed_out=True,
                            )
                            break

                        grep_cmd = build_grep_command(
                            [f.path for f in batch], job.request, settings,
                            line_budget=remaining_lines,
                        )
                        self._set(job, command=grep_cmd)

                        result = sess.run(
                            grep_cmd,
                            sink=sink,
                            timeout=int(remaining_time) + 1,
                            max_bytes=settings.max_result_bytes - written["bytes"],
                            cancel=job._cancel,
                        )

                        if result.cancelled or result.timed_out or result.truncated:
                            # This batch was cut short, so its bytes were NOT all
                            # read — counting them would report progress the
                            # search never made.
                            break

                        self._set(job, bytes_scanned=job.bytes_scanned
                                  + sum(f.size_bytes for f in batch))

                        if result.exit_status not in (0, 1, None):
                            raise RuntimeError(result.stderr or
                                               f"grep exited {result.exit_status}")

            if result is None:
                result = CommandResult(exit_status=0, stderr="", bytes_read=0,
                                       duration_s=0.0)

            if result.cancelled or job._cancel.is_set():
                raise _Cancelled()

            if result.timed_out:
                job.warnings.append(
                    f"Search hit the {settings.command_timeout}s time limit after "
                    f"{ui_bytes(job.bytes_scanned)} of {ui_bytes(job.bytes_to_scan)}; "
                    "the output below is partial."
                )
            if result.truncated:
                self._set(job, truncated=True)
                job.warnings.append(
                    f"Output reached the {ui_bytes(settings.max_result_bytes)} size cap "
                    "and was cut off. Use a more specific pattern or a shorter date range."
                )
            # `head -n` closes the pipe once the cap is reached and grep dies of
            # SIGPIPE, so a line-capped result looks exactly like a complete one.
            # Say so explicitly — silently returning 200,000 of 2,000,000 matches
            # is the kind of thing someone builds a wrong conclusion on.
            if written["lines"] >= settings.max_result_lines:
                self._set(job, truncated=True)
                job.warnings.append(
                    f"Output reached the {settings.max_result_lines:,}-line cap. "
                    "There are almost certainly more matches than you can see here — "
                    "narrow the pattern, the instance, or the date range."
                )

            # grep: 0 = matched, 1 = no match, >1 = real error
            if result.exit_status not in (0, 1, None) and not result.truncated and not result.timed_out:
                raise RuntimeError(result.stderr or f"grep exited {result.exit_status}")

            # Sidecar so a later cache hit never has to re-read the payload to
            # find out how many lines it holds.
            store.write_meta(
                out_path,
                lines=written["lines"],
                bytes_written=written["bytes"],
                truncated=job.truncated,
            )

            self._set(
                job,
                status=JobStatus.DONE,
                finished_at=time.time(),
                result_path=out_path,
                bytes_written=written["bytes"],
                lines_written=written["lines"],
            )
            self._housekeep()
            audit.record(
                settings.audit_path,
                "search_completed",
                job_id=job.id,
                user=job.request.requested_by,
                pattern=job.request.pattern,
                instances=job.request.instances,
                dates=job.request.dates,
                files_scanned=len(files),
                bytes_scanned=job.bytes_scanned,
                lines=written["lines"],
                bytes=written["bytes"],
                duration_s=round(job.elapsed, 1),
                truncated=job.truncated,
            )

        except _Cancelled:
            self._set(job, status=JobStatus.CANCELLED, finished_at=time.time(),
                      error="Cancelled by user.")
            audit.record(settings.audit_path, "search_cancelled",
                         job_id=job.id, user=job.request.requested_by)
        except RemoteError as exc:
            self._set(job, status=JobStatus.FAILED, finished_at=time.time(), error=str(exc))
            audit.record(settings.audit_path, "search_failed", job_id=job.id,
                         user=job.request.requested_by, error=str(exc), kind="remote")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._set(job, status=JobStatus.FAILED, finished_at=time.time(), error=str(exc))
            audit.record(settings.audit_path, "search_failed", job_id=job.id,
                         user=job.request.requested_by, error=str(exc),
                         kind=type(exc).__name__)


class _Cancelled(Exception):
    pass


# --------------------------------------------------------------------------- #

_manager: JobManager | None = None
_manager_lock = threading.Lock()


def get_manager() -> JobManager:
    """Process-wide singleton. Shared across every browser session on purpose."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = JobManager(get_settings())
    return _manager
