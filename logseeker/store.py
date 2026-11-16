"""On-disk result storage, the identical-search cache, and disk housekeeping.

Results land in ``data/results/<fingerprint>/<descriptive name>.log`` alongside a
small ``.meta.json`` sidecar. The fingerprint directory is what makes the cache
work: if two people ask the same question within the TTL, the second one gets the
first one's file instantly instead of putting another multi-minute grep on the
log host.

Everything here is written for results that may be tens of megabytes:

* a search writes to ``<name>.log.part`` and the file is renamed to
  ``<name>.log`` only once grep has finished, so nothing half-written is ever
  offered for download or compressed;
* the sidecar holds the line count so a cache hit never re-reads the payload;
* gzip is produced once, streamed, and reused;
* the results tree is held to a total byte budget by evicting oldest-first.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .search import SearchRequest


@dataclass
class StoredResult:
    path: Path
    size_bytes: int
    created_at: float
    lines: int

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.created_at)


def _meta_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".meta.json")


def result_path(settings: Settings, req: SearchRequest) -> Path:
    return settings.results_dir / req.fingerprint() / req.result_filename()


def write_meta(path: Path, *, lines: int, bytes_written: int, truncated: bool,
               complete: bool = True) -> None:
    """``complete`` False keeps the file for its own job's download but never
    serves it as a cache hit: a search that timed out, or one that read to the
    end of a log that is still being written, is not the full answer."""
    try:
        _meta_path(path).write_text(
            json.dumps({
                "lines": lines,
                "bytes": bytes_written,
                "truncated": truncated,
                "complete": complete,
                "written_at": time.time(),
            }),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_meta(path: Path) -> dict | None:
    meta = _meta_path(path)
    if not meta.exists():
        return None
    try:
        return json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def count_lines(path: Path) -> int:
    """Streaming line count — never loads the file into memory."""
    if not path.exists():
        return 0
    total = 0
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            total += block.count(b"\n")
    return total


def lookup(settings: Settings, req: SearchRequest) -> StoredResult | None:
    """Return a fresh cached result for this exact search, or None.

    Only a result whose sidecar says it is complete is served. A result file
    exists from the moment a search starts, so its existence proves nothing: a
    search that failed or was interrupted must never answer the next person.
    """
    if settings.cache_ttl_hours <= 0:
        return None
    path = result_path(settings, req)
    if not path.exists():
        return None
    stat = path.stat()
    if time.time() - stat.st_mtime > settings.cache_ttl_hours * 3600:
        return None

    meta = read_meta(path)
    if not meta or meta.get("complete") is not True:
        return None
    lines = meta["lines"] if "lines" in meta else count_lines(path)
    return StoredResult(
        path=path, size_bytes=stat.st_size, created_at=stat.st_mtime, lines=lines
    )


PART = ".part"


def is_partial(path: Path) -> bool:
    return path.name.endswith(PART)


def _final(path: Path) -> Path:
    return path.with_name(path.name[: -len(PART)]) if is_partial(path) else path


def open_for_write(settings: Settings, req: SearchRequest) -> Path:
    """The ``.part`` file a search writes into; :func:`finalize` publishes it."""
    path = result_path(settings, req)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A previous run's result must not stand in for what this run writes.
    for old in (path, _meta_path(path), path.with_name(path.name + ".gz")):
        old.unlink(missing_ok=True)
    return path.with_name(path.name + PART)


def finalize(part: Path, *, attempts: int = 5, wait_s: float = 0.2) -> Path:
    """Flush the finished ``.part`` file to disk and give it its real name.
    Only a finalized file is downloaded, compressed or cached.

    The search has finished, so the content is whole and only the name is
    left. On Windows a rename can fail for a moment while a virus scanner or
    indexer holds the file open, so it is retried, then done as a copy and
    delete. Raises OSError only if none of that works: a finished search must
    never be left behind as ``.part``.
    """
    final = _final(part)
    with part.open("rb+") as fh:
        os.fsync(fh.fileno())
    for attempt in range(attempts):
        try:
            part.replace(final)
            return final
        except OSError:
            if attempt < attempts - 1:
                time.sleep(wait_s)
    shutil.copyfile(part, final)
    with final.open("rb+") as fh:
        os.fsync(fh.fileno())
    try:
        part.unlink()
    except OSError:
        pass  # the .log is complete; startup clean-up removes the stray .part
    return final


def remove_leftover_parts(settings: Settings) -> int:
    """Delete every ``.part`` under the results tree. Call only when no search
    is running (at startup): any ``.part`` then comes from a search the app was
    stopped in the middle of, so it holds part of an answer and must never be
    passed off as a result."""
    if not settings.results_dir.exists():
        return 0
    removed = 0
    for p in settings.results_dir.rglob(f"*{PART}"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def discard(path: Path | None) -> None:
    """Remove a result that did not finish, with its sidecar and gzip."""
    if path is None:
        return
    final = _final(path)
    for p in (path, final, _meta_path(final), final.with_name(final.name + ".gz")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def stored(path: Path) -> StoredResult | None:
    if not path.exists():
        return None
    stat = path.stat()
    meta = read_meta(path)
    return StoredResult(
        path=path,
        size_bytes=stat.st_size,
        created_at=stat.st_mtime,
        lines=meta["lines"] if meta and "lines" in meta else 0,
    )


# --------------------------------------------------------------------------- #
# Compression
# --------------------------------------------------------------------------- #

_gzip_lock = threading.Lock()


def gzip_member_name(gz: Path) -> str | None:
    """The file name stored in a gzip's header, which is what 7-Zip, WinRAR and
    ``gunzip -N`` call the file inside. None if there is none or it is unreadable."""
    try:
        with gz.open("rb") as fh:
            head = fh.read(10)
            if len(head) < 10 or head[:2] != b"\x1f\x8b" or not head[3] & 0x08:
                return None
            if head[3] & 0x04:                        # optional extra field first
                fh.seek(int.from_bytes(fh.read(2), "little"), os.SEEK_CUR)
            name = bytearray()
            while (byte := fh.read(1)) not in (b"", b"\x00") and len(name) < 1024:
                name += byte
            return name.decode("latin-1")
    except OSError:
        return None


def gzipped(path: Path) -> Path:
    """Return a gzip of ``path``, building it once and reusing it after.

    Grep output over order logs is highly repetitive and typically compresses
    ten-to-one, which is the difference between a 60 MB browser download and a
    6 MB one. Copied in 1 MB blocks so memory stays flat regardless of size.

    Only a finished result is compressed. The gzip is built under its own
    temporary name and renamed into place when complete, one build at a time,
    so two people pressing "Prepare download" together never get a gzip that
    the other is still writing.
    """
    if is_partial(path):
        raise ValueError(f"{path.name} is still being written")
    gz = path.with_name(path.name + ".gz")
    with _gzip_lock:
        if (gz.exists() and gz.stat().st_mtime >= path.stat().st_mtime
                and gzip_member_name(gz) == path.name):
            return gz
        tmp = gz.with_name(f"{gz.name}.{uuid.uuid4().hex[:8]}{PART}")
        try:
            # gzip records a file name in its header, and unzip tools show it as
            # the file inside. Left to itself it records the name it is writing
            # to - the temporary one - so name the log explicitly.
            with path.open("rb") as src, tmp.open("wb") as raw, gzip.GzipFile(
                    filename=path.name, mode="wb", compresslevel=6, fileobj=raw) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            with tmp.open("rb+") as fh:
                os.fsync(fh.fileno())
            tmp.replace(gz)
        finally:
            tmp.unlink(missing_ok=True)
    return gz


# --------------------------------------------------------------------------- #
# Housekeeping
# --------------------------------------------------------------------------- #

def _dir_stats(child: Path) -> tuple[int, float]:
    """(total bytes, newest mtime) for one result directory."""
    total = 0
    newest = 0.0
    for p in child.rglob("*"):
        if p.is_file():
            stat = p.stat()
            total += stat.st_size
            newest = max(newest, stat.st_mtime)
    return total, newest


def purge_expired(settings: Settings) -> int:
    """Delete result directories past the TTL. Returns how many were removed."""
    if not settings.results_dir.exists() or settings.cache_ttl_hours <= 0:
        return 0
    cutoff = time.time() - settings.cache_ttl_hours * 3600
    removed = 0
    for child in settings.results_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            _, newest = _dir_stats(child)
            if newest and newest < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def enforce_disk_budget(settings: Settings) -> tuple[int, int]:
    """Evict oldest results until the tree fits ``max_disk_bytes``.

    Returns (directories removed, bytes freed). Without this, a week of broad
    searches over big logs would fill the operator's disk silently — the TTL
    alone does not bound size, only age.
    """
    if not settings.results_dir.exists() or settings.max_disk_bytes <= 0:
        return 0, 0

    entries = []
    total = 0
    for child in settings.results_dir.iterdir():
        if not child.is_dir():
            continue
        try:
            size, newest = _dir_stats(child)
        except OSError:
            continue
        entries.append((newest, size, child))
        total += size

    if total <= settings.max_disk_bytes:
        return 0, 0

    entries.sort()  # oldest first
    removed = freed = 0
    for _, size, child in entries:
        if total <= settings.max_disk_bytes:
            break
        shutil.rmtree(child, ignore_errors=True)
        total -= size
        freed += size
        removed += 1
    return removed, freed


def disk_usage(settings: Settings) -> tuple[int, int]:
    """(number of result files, total bytes) including sidecars and gzips."""
    if not settings.results_dir.exists():
        return 0, 0
    count = total = 0
    for p in settings.results_dir.rglob("*"):
        if p.is_file():
            if p.suffix in (".log", ".txt"):
                count += 1
            total += p.stat().st_size
    return count, total
