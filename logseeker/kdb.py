"""Does this id_target exist in kdb, and whose is it?

Before (and alongside) grepping gigabytes of log, ask the OMS database the
cheap question: is there a `target` with this id on these dates, and which
trader owns it?

    RDB  today's session, in memory      select trader from target
                                           where id_target=<id>
    HDB  every earlier day, by date      select date, trader from target
                                           where date in <dates>, id_target=<id>

A search's dates are split between the two: today goes to the RDB, anything
earlier to the HDB. An endpoint that is not configured is skipped and said so.

pykx is never imported here. The query runs in a short-lived child process
(kdbworker/lookup.py) so a pykx failure cannot take the app down — see that
file for why. This module only builds the request and reads the answer.

The kdb check never blocks the log search: if kdb is down or the id is not in
`target`, the grep still runs and the card says what kdb said.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime
from pathlib import Path

from .config import Settings
from .search import KDB_ID_MAX, ORDER_ID_RE, kdb_id_ok

WORKER = Path(__file__).resolve().parent / "kdbworker" / "lookup.py"

# pykx's import alone takes a few seconds on a cold start; give it room on top
# of the query timeout before deciding the child is stuck.
STARTUP_ALLOWANCE_S = 60

MAX_ROWS = 100


FOUND = "found"
NOT_FOUND = "not_found"
ERROR = "error"
SKIPPED = "skipped"


@dataclass
class SourceResult:
    source: str                 # "RDB" | "HDB"
    endpoint: str
    rows: list[dict] = field(default_factory=list)   # {"date", "trader"}
    error: str | None = None
    info: str = ""


@dataclass
class KdbCheck:
    status: str                 # FOUND | NOT_FOUND | ERROR | SKIPPED
    message: str = ""
    sources: list[SourceResult] = field(default_factory=list)
    skipped_dates: list[str] = field(default_factory=list)   # YYYYMMDD with no endpoint

    @property
    def traders(self) -> list[str]:
        seen: list[str] = []
        for s in self.sources:
            for row in s.rows:
                t = row.get("trader")
                if t and t not in seen:
                    seen.append(t)
        return seen

    @property
    def rows(self) -> list[dict]:
        return [dict(row, source=s.source) for s in self.sources for row in s.rows]

    @property
    def errors(self) -> list[str]:
        return [f"{s.source} {s.endpoint}: {s.error}" for s in self.sources if s.error]


# --------------------------------------------------------------------------- #

def parse_endpoint(value: str) -> tuple[str, int] | None:
    """'host:port' -> ('host', port). None for blank; ValueError if malformed."""
    value = (value or "").strip()
    if not value:
        return None
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"expected host:port, got {value!r}")
    return host, int(port)


def pykx_installed() -> bool:
    return importlib.util.find_spec("pykx") is not None


def split_dates(dates: list[str], today: date_cls) -> tuple[bool, list[str]]:
    """(does the RDB need asking?, YYYYMMDD dates for the HDB)."""
    stamp = today.strftime("%Y%m%d")
    return stamp in dates, sorted(d for d in dates if d != stamp)


def _iso(yyyymmdd: str) -> str:
    return datetime.strptime(yyyymmdd, "%Y%m%d").date().isoformat()


def _run_worker(settings: Settings, request: dict, runner=subprocess.run) -> dict:
    request = dict(request,
                   username=settings.kdb_user, password=settings.kdb_password,
                   timeout=settings.kdb_timeout)
    env = dict(os.environ, PYKX_UNLICENSED="true")
    proc = runner(
        [sys.executable, str(WORKER)],
        input=json.dumps(request),
        capture_output=True, text=True, env=env,
        timeout=settings.kdb_timeout * 2 + STARTUP_ALLOWANCE_S,
    )
    # pykx may print banners before the answer, so the answer is the last line.
    lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if not lines:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError("the kdb lookup process returned nothing"
                           + (": " + " | ".join(tail) if tail else ""))
    answer = json.loads(lines[-1])
    if answer.get("fatal"):
        raise RuntimeError(answer["fatal"])
    return answer


def _sources(answer: dict) -> list[SourceResult]:
    return [SourceResult(source=s.get("source", "?"), endpoint=s.get("endpoint", "?"),
                         rows=list(s.get("rows") or []), error=s.get("error"),
                         info=s.get("info") or "")
            for s in answer.get("sources") or []]


def lookup_target(settings: Settings, id_target: str, dates: list[str], *,
                  exact: bool = True, today: date_cls | None = None,
                  runner=subprocess.run) -> KdbCheck:
    """Ask kdb whether ``id_target`` exists on ``dates`` (YYYYMMDD)."""
    if not settings.kdb_enabled:
        return KdbCheck(SKIPPED, "kdb is not configured.")
    if not exact or not ORDER_ID_RE.match(id_target or ""):
        return KdbCheck(SKIPPED, "Pattern searches are not looked up in kdb — only an exact id is.")
    # Without this, "I"$ on the server would turn the id into a null and the
    # card would say "not found" — true, but it hides why.
    if not kdb_id_ok(id_target):
        return KdbCheck(SKIPPED, f"`{id_target}` cannot be a kdb id_target "
                                 f"(a whole number up to {KDB_ID_MAX:,}), so it was not looked up.")
    if not pykx_installed():
        return KdbCheck(ERROR, "pykx is not installed — run `pip install -r requirements.txt`.")

    try:
        rdb = parse_endpoint(settings.kdb_rdb)
        hdb = parse_endpoint(settings.kdb_hdb)
    except ValueError as exc:
        return KdbCheck(ERROR, f"kdb endpoint misconfigured: {exc}")

    want_rdb, hdb_dates = split_dates(dates, today or date_cls.today())
    skipped = []
    if want_rdb and rdb is None:
        skipped.append((today or date_cls.today()).strftime("%Y%m%d"))
    if hdb_dates and hdb is None:
        skipped.extend(hdb_dates)

    request = {
        "op": "lookup", "id": id_target, "max_rows": MAX_ROWS,
        "rdb": list(rdb) if want_rdb and rdb else None,
        "hdb": list(hdb) if hdb_dates and hdb else None,
        "hdb_dates": [_iso(d) for d in hdb_dates] if hdb else [],
    }
    if request["rdb"] is None and request["hdb"] is None:
        return KdbCheck(SKIPPED, "No kdb endpoint is configured for those dates.",
                        skipped_dates=skipped)

    try:
        sources = _sources(_run_worker(settings, request, runner))
    except subprocess.TimeoutExpired:
        return KdbCheck(ERROR, "kdb did not answer in time.", skipped_dates=skipped)
    except Exception as exc:  # noqa: BLE001 - shown on the card
        return KdbCheck(ERROR, f"kdb lookup failed: {exc}", skipped_dates=skipped)

    check = KdbCheck(NOT_FOUND, sources=sources, skipped_dates=skipped)
    if check.rows:
        check.status = FOUND
        check.message = "Found in kdb `target`."
    elif check.errors:
        check.status = ERROR
        check.message = "kdb lookup failed: " + "; ".join(check.errors)
    else:
        asked = " and ".join(s.source for s in sources)
        check.message = f"No `target` with id_target `{id_target}` in the {asked} for those dates."
    return check


def ping(settings: Settings, runner=subprocess.run) -> list[SourceResult]:
    """Connect to each configured endpoint and report what answered. Admin page."""
    request = {"op": "ping",
               "rdb": list(parse_endpoint(settings.kdb_rdb) or []) or None,
               "hdb": list(parse_endpoint(settings.kdb_hdb) or []) or None}
    return _sources(_run_worker(settings, request, runner))
