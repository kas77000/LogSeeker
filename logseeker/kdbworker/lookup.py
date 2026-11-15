"""Look an id_target up in kdb's `target` table — run as its own process.

LogSeeker never imports pykx. It starts this script, writes one JSON request
to stdin, and reads one JSON answer from stdout. Two reasons:

* pykx keeps some state per *process*, and has been seen to fail inside a
  long-running app while succeeding in a fresh one (experiment6/core-vp,
  core/connections.py). A fresh process per lookup sidesteps that entirely.
* Streamlit runs pages and LogSeeker runs searches on worker threads. A pykx
  crash or hang here takes down this process and nothing else.

The request comes over stdin rather than argv so the kdb password never
appears in a process listing.

This file lives in a directory of its own so that running it puts nothing but
itself on sys.path — LogSeeker's modules (config, search, store…) cannot shadow
anything pykx imports.

Request:
    {"op": "lookup" | "ping",
     "rdb": ["host", port] | null, "hdb": ["host", port] | null,
     "username": "", "password": "", "timeout": 30,
     "id": "540000001" (digits; sent to kdb as an int), "hdb_dates": ["2026-11-12", ...], "max_rows": 100}

Answer:
    {"sources": [{"source": "RDB" | "HDB", "endpoint": "host:port",
                  "rows": [{"date": "2026-11-12" | null, "trader": "..."}],
                  "states": [{"date": "2026-11-12" | null, "id_server": "...",
                              "first_ms": 30451897, "last_ms": 34865000,
                              "last_state": "filled"}],
                  "states_error": null | "...",
                  "error": null | "...", "info": "..."}]}
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

TABLE = "target"

# `meta target` gives id_target type "i" (32-bit int), so the id is sent as an
# int atom and compared directly — no cast on the server.
#
# The RDB holds only the current session in memory, so its query has no date
# predicate. The HDB is partitioned by date, so `date in ds` comes first.
RDB_FN = """
{[x;n] n sublist distinct select trader from target where id_target=x }
"""

HDB_FN = """
{[ds;x;n] n sublist distinct select date, trader from target
    where date in ds, id_target=x }
"""

# When did the order live, and is it still live? One row per id_server (and
# date on the HDB): first and last `time`, and the state at the last one.
# Rows with a null time cannot be placed in the day — as midnight they would
# become the first event — so they are dropped, as kdb-queries' luld_orders does.
RDB_STATE_FN = """
{[x] 0!select first_time:first time, last_time:last time, last_state:last state
    by id_server from `time xasc
    select id_server, time, state from target_state where id_target=x, not null time }
"""

HDB_STATE_FN = """
{[ds;x] 0!select first_time:first time, last_time:last time, last_state:last state
    by date, id_server from `date`time xasc
    select date, id_server, time, state from target_state
    where date in ds, id_target=x, not null time }
"""

PING_FN = "{[] (string .z.h; .z.K; string .z.p)}[]"


def _connect(kx, host: str, port: int, req: dict):
    """Open a handle the way experiment6 found works against these servers.

    pykx builds a context interface on connect by resolving `q` remotely; a
    server that keeps its own global `q` breaks that in the constructor. So try
    no_ctx=True first and fall back only on that specific failure.
    """
    kwargs = dict(
        host=host, port=int(port),
        timeout=float(req.get("timeout") or 30),
        connection_timeout=float(req.get("timeout") or 30),
    )
    if req.get("username"):
        kwargs["username"] = req["username"]
    if req.get("password"):
        kwargs["password"] = req["password"]
    try:
        return kx.SyncQConnection(no_ctx=True, **kwargs)
    except (AttributeError, TypeError) as exc:
        if "_context_keys" not in str(exc) and "unexpected keyword" not in str(exc):
            raise
        return kx.SyncQConnection(**kwargs)


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()[:10]
    if hasattr(value, "isoformat"):          # pandas Timestamp
        return value.isoformat()[:10]
    text = str(value)
    return None if text in {"", "nan", "NaT", "None"} else text


def _rows(table) -> list[dict]:
    """A pykx table -> plain dicts. .pd() is what unlicensed pykx offers."""
    frame = table.pd()
    out = []
    for record in frame.to_dict("records"):
        out.append({"date": _text(record.get("date")),
                    "trader": _text(record.get("trader"))})
    return out


def _ms(value) -> int | None:
    """A q time (pandas Timedelta / timedelta / time) as ms since midnight."""
    if value is None:
        return None
    if isinstance(value, dt.time):
        return ((value.hour * 60 + value.minute) * 60 + value.second) * 1000 \
            + value.microsecond // 1000
    if hasattr(value, "total_seconds"):
        try:
            seconds = float(value.total_seconds())
        except (TypeError, ValueError):
            return None
        if seconds != seconds:          # NaT
            return None
        return int(round(seconds * 1000))
    return None


def _state_rows(table) -> list[dict]:
    frame = table.pd()
    out = []
    for record in frame.to_dict("records"):
        out.append({"date": _text(record.get("date")),
                    "id_server": _text(record.get("id_server")),
                    "first_ms": _ms(record.get("first_time")),
                    "last_ms": _ms(record.get("last_time")),
                    "last_state": _text(record.get("last_state"))})
    return out


def _lookup(conn, rows_fn: str, state_fn: str, args: tuple, n) -> dict:
    """Trader rows, then the order's times. The second query has its own try:
    if it fails the trader still comes back, and the search scans whole files."""
    out = {"rows": _rows(conn(rows_fn, *args, n))}
    try:
        out["states"] = _state_rows(conn(state_fn, *args))
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        out["states_error"] = f"{type(exc).__name__}: {exc}"
    return out


def _one(kx, source: str, endpoint, req: dict, run) -> dict:
    host, port = endpoint
    entry = {"source": source, "endpoint": f"{host}:{port}",
             "rows": [], "states": [], "states_error": None, "error": None, "info": ""}
    try:
        with _connect(kx, host, port, req) as conn:
            entry.update(run(conn))
    except Exception as exc:  # noqa: BLE001 - reported to the user verbatim
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


def handle(req: dict) -> dict:
    os.environ.setdefault("PYKX_UNLICENSED", "true")
    import pykx as kx

    op = req.get("op", "lookup")
    n = kx.toq(int(req.get("max_rows") or 100))
    sources = []

    if op == "ping":
        def run(conn):
            host, version, now = conn(PING_FN).py()
            return {"info": f"{_text(host)} · kdb+ {version} · server time {_text(now)}"}
        for name in ("rdb", "hdb"):
            if req.get(name):
                sources.append(_one(kx, name.upper(), req[name], req, run))
        return {"sources": sources}

    ident = kx.toq(int(req["id"]), ktype=kx.IntAtom)

    if req.get("rdb"):
        sources.append(_one(kx, "RDB", req["rdb"], req,
                            lambda conn: _lookup(conn, RDB_FN, RDB_STATE_FN, (ident,), n)))

    dates = [dt.date.fromisoformat(d) for d in req.get("hdb_dates") or []]
    if dates and req.get("hdb"):
        ds = kx.toq(dates, ktype=kx.DateVector)
        sources.append(_one(kx, "HDB", req["hdb"], req,
                            lambda conn: _lookup(conn, HDB_FN, HDB_STATE_FN, (ds, ident), n)))

    return {"sources": sources}


def main() -> int:
    try:
        answer = handle(json.loads(sys.stdin.read()))
    except Exception as exc:  # noqa: BLE001
        answer = {"fatal": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write("\n" + json.dumps(answer) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
