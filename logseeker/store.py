"""On-disk result storage, the identical-search cache, and disk housekeeping.

Results land in ``data/results/<fingerprint>/<descriptive name>.txt`` alongside a
small ``.meta.json`` sidecar. The fingerprint directory is what makes the cache
work: if two people ask the same question within the TTL, the second one gets the
first one's file instantly instead of putting another multi-minute grep on the
log host.

Everything here is written for results that may be tens of megabytes:

* the sidecar holds the line count so a cache hit never re-reads the payload;
* gzip is produced once, streamed, and reused;
* the results tree is held to a total byte budget by evicting oldest-first.
"""

from __future__ import annotations

import gzip
import json
import shutil
import time
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


def write_meta(path: Path, *, lines: int, bytes_written: int, truncated: bool) -> None:
    try:
        _meta_path(path).write_text(
            json.dumps({
                "lines": lines,
                "bytes": bytes_written,
                "truncated": truncated,
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
    """Return a fresh cached result for this exact search, or None."""
    if settings.cache_ttl_hours <= 0:
        return None
    path = result_path(settings, req)
    if not path.exists():
        return None
    stat = path.stat()
    if time.time() - stat.st_mtime > settings.cache_ttl_hours * 3600:
        return None

    meta = read_meta(path)
    lines = meta["lines"] if meta and "lines" in meta else count_lines(path)
    return StoredResult(
        path=path, size_bytes=stat.st_size, created_at=stat.st_mtime, lines=lines
    )


def open_for_write(settings: Settings, req: SearchRequest) -> Path:
    path = result_path(settings, req)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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

def gzipped(path: Path) -> Path:
    """Return a gzip of ``path``, building it once and reusing it after.

    Grep output over order logs is highly repetitive and typically compresses
    ten-to-one, which is the difference between a 60 MB browser download and a
    6 MB one. Copied in 1 MB blocks so memory stays flat regardless of size.
    """
    gz = path.with_suffix(path.suffix + ".gz")
    if gz.exists() and gz.stat().st_mtime >= path.stat().st_mtime:
        return gz
    tmp = gz.with_suffix(".gz.part")
    with path.open("rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    tmp.replace(gz)
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
            if p.suffix == ".txt":
                count += 1
            total += p.stat().st_size
    return count, total
