"""Append-only audit trail.

These are production order logs. A shared self-service tool that hands them out
over the internet needs to be able to answer "who pulled what, and when" — the
legacy flow could only answer that from one person's WinSCP log on one laptop.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def record(path: Path, event: str, **fields: Any) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **fields,
    }
    line = json.dumps(entry, default=str, ensure_ascii=False)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def tail(path: Path, limit: int = 200, max_bytes: int = 4 * 1024 * 1024) -> list[dict]:
    """Most recent entries first.

    Reads only the last ``max_bytes`` of the file. The audit log is append-only
    and never rotated by the app, so after a few busy months it can be large —
    the Admin page must not pull all of it into memory to show the last 200 rows.
    """
    if not path.exists():
        return []

    with _lock:
        size = path.stat().st_size
        with path.open("rb") as fh:
            start = max(0, size - max_bytes)
            fh.seek(start)
            data = fh.read()

    if start > 0:
        # The seek almost certainly landed mid-line; drop that partial record.
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline != -1 else b""

    out: list[dict] = []
    for line in reversed(data.decode("utf-8", "replace").splitlines()):
        if len(out) >= limit:
            break
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
