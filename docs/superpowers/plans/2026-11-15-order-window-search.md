# Order-Time Window Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read only the bytes of a log that cover an order's life (from kdb `target_state`), instead of the whole file, with a whole-file fallback whenever that cannot be done safely.

**Architecture:** kdb's existing lookup also returns first/last `target_state.time` and the last state. A new pure module (`logseeker/window.py`) turns that into a session-relative window and binary-searches the log's byte offsets through a `probe(offset, length) -> bytes` callback. The job runs probes over its existing session, then greps `tail -c +START | head -c LEN`. The cache only serves results marked complete.

**Tech Stack:** Python 3.11+, Streamlit, paramiko, pykx (worker only), pytest. Remote commands are POSIX shell (`tail`, `head`, `grep`, `gzip`, `awk`).

Spec: `docs/superpowers/specs/2026-11-15-order-window-search-design.md`

## Global Constraints

- Toggle label: "Search only the order's active window (from kdb)", default **on**, shown only when kdb is configured.
- Padding setting: `order_window_pad_minutes`, default `5`.
- Active state value: `` `activated `` (string `"activated"`).
- Log timestamp: `^\d\d:\d\d:\d\d[,.]\d{3}` at the start of a line; log and kdb are both HKT.
- Session start = first timestamped line of the file — never the filename's `[HH-MM-SS]`.
- Whole-file fallback for: gzipped log; no timed `target_state` row for the date / kdb not FOUND; no timestamp at file start; no timestamp where the search looked; **and** a rescan of windowed files when the windowed pass matched 0 lines.
- `<file>:` prefix only when the search covers 2+ files.
- Cache: only `"complete": true` sidecars are served; failed/cancelled searches delete their file; timed-out and open-ended (`activated`) results are `complete: false`.
- Test runs on Windows need Git's tools first on PATH: `$env:PATH="C:\Program Files\Git\usr\bin;$env:PATH"`. Baseline: 8 failures in `tests/test_kdb.py`, all "pykx is not installed"; they are not ours to fix here.

---

## File structure

| file | change |
|---|---|
| `logseeker/window.py` | **new** — timestamps, session time, window merge, byte-range search. Pure, no I/O. |
| `logseeker/kdbworker/lookup.py` | second query per endpoint: first/last time, last state |
| `logseeker/kdb.py` | `SourceResult.states`, `KdbCheck.states` (dated), `rdb_date` |
| `logseeker/search.py` | `SearchRequest.order_window`; `build_probe_command`; ranges + `prefix` in `build_grep_command` |
| `logseeker/store.py` | `complete` flag, `discard()` |
| `logseeker/jobs.py` | `LOCATING` status, `ScanItem`, `_plan_ranges`, `_scan`, rescan, cleanup |
| `logseeker/config.py` | `order_window_pad_minutes` |
| `logseeker/ui.py`, `app_pages/search.py`, `app_pages/admin.py` | badge, toggle, card notes, setting row |
| `README.md`, `.env.example`, `.streamlit/secrets.toml.example` | document |
| `tests/test_window.py`, `tests/test_order_window_search.py`, `tests/test_cache_integrity.py` | **new** |

---

### Task 1: `window.py` — pure window logic

**Files:** Create `logseeker/window.py`; Test `tests/test_window.py`

**Interfaces — Produces:**
- `DAY_MS: int`, `CHUNK: int = 65536`, `ACTIVE_STATE = "activated"`
- `class WindowError(Exception)`
- `clock_ms(line: bytes) -> int | None`
- `timed_lines(chunk: bytes, *, at_line_start: bool) -> Iterator[tuple[int, int]]` — (offset in chunk, clock ms)
- `session_ms(clock: int, start: int) -> int`
- `@dataclass(frozen=True) Window(first_ms: int, last_ms: int, open_ended: bool, last_state: str)`
- `order_window(states: list[dict], trade_date: str) -> Window | None` — rows `{date: "YYYYMMDD", first_ms, last_ms, last_state}`
- `Probe = Callable[[int, int], bytes]`
- `session_start(probe) -> int`
- `locate(probe, size: int, target: int, start: int) -> int | None`
- `find_range(probe, size: int, window: Window, pad_ms: int) -> tuple[int, int | None]` — `(start_offset, end_offset or None = EOF)`
- `fmt_clock(ms: int) -> str` — `"HH:MM:SS"`

- [ ] **Step 1: tests** — `tests/test_window.py`: build an in-memory log (session 23:20:00 → 09:00, one line per second, a 3-line untimestamped stack trace every 500 lines) and a `probe` over its bytes that counts calls. Cover: `clock_ms` valid/invalid; `timed_lines` skips the partial first line unless `at_line_start`; `session_ms` wrap (`00:10` after `23:20` → 50 min); `order_window` merges two servers, open-ended if any `activated`, `None` for another date or null times; `locate` returns the exact start of the first line ≥ target, `None` past the end, `0` before the start; `find_range` closed window brackets exactly the padded lines, open-ended returns `None` end; after-midnight window; `WindowError` for a file with no timestamp at the start; probe count < 200 on a 2 MB file.
- [ ] **Step 2:** `pytest tests/test_window.py -q` → fails (module missing).
- [ ] **Step 3:** implement `window.py` (code in the repo commit; algorithm: `session_start` reads the first `CHUNK`; `_first_at_or_after(probe, offset, start)` reads chunks from `offset` until a timestamped line starts, up to 1 MB, `None` at EOF; `locate` bisects `[0, size]` with invariant "first timed line after `lo` < target", stops at `hi - lo <= CHUNK`, then scans 128 KB chunks forward; `find_range` converts kdb clock times to session ms, clamps the start at 0, `end = locate(last + pad + 1)`).
- [ ] **Step 4:** tests pass.
- [ ] **Step 5:** commit `Add order-window byte-range search over log timestamps`.

### Task 2: kdb returns the order's first/last time and last state

**Files:** Modify `logseeker/kdbworker/lookup.py`, `logseeker/kdb.py`; Test `tests/test_order_window_search.py` (kdb section)

**Interfaces — Produces:**
- worker answer source gains `"states": [{"date": iso|None, "id_server": str, "first_ms": int|None, "last_ms": int|None, "last_state": str|None}]`, `"states_error": str|None`
- `SourceResult.states: list[dict]`, `SourceResult.states_error: str | None`
- `KdbCheck.rdb_date: str` (YYYYMMDD sent to the RDB, `""` if none) and property `KdbCheck.states -> list[dict]` with `date` as `YYYYMMDD`

q, per endpoint, in its own `try` so a failure keeps the trader:

```q
/ RDB
{[x] 0!select first_time:first time, last_time:last time, last_state:last state
    by id_server from `time xasc
    select id_server, time, state from target_state where id_target=x, not null time }
/ HDB
{[ds;x] 0!select first_time:first time, last_time:last time, last_state:last state
    by date, id_server from `date`time xasc
    select date, id_server, time, state from target_state
    where date in ds, id_target=x, not null time }
```

- [ ] **Step 1: tests** — with `FakeRunner` and `monkeypatch.setattr(kdb, "pykx_installed", lambda: True)`: HDB state row dated `2026-11-12` comes back as `20261112`; RDB state row (date `None`) comes back dated today; `states_error` does not change FOUND. Worker helpers: `_ms` on `timedelta`, `pd.Timedelta`, `pd.NaT`, `None`; `_state_rows` on an object whose `.pd()` returns a DataFrame.
- [ ] **Step 2:** run → fail. **Step 3:** implement. **Step 4:** pass. **Step 5:** commit `kdb lookup also returns the order's first/last target_state time`.

### Task 3: request flag, probe command, ranged and prefix-free grep, setting

**Files:** Modify `logseeker/search.py`, `logseeker/config.py`; Test `tests/test_order_window_search.py` (command section)

**Interfaces — Produces:**
- `SearchRequest.order_window: bool = True` (in `fingerprint()`; carried by `validate`)
- `build_probe_command(path: str, offset: int, length: int) -> str` → `tail -c +{offset+1} '<path>' | head -c {length}`
- `build_grep_command(files, req, settings, *, line_budget=None, prefix=True, ranges: dict[str, tuple[int, int | None]] | None = None)`; a ranged file is `tail -c +{start+1} f [| head -c {end-start}] | { grep …; report; }` (+ awk prefix when `prefix`)
- `Settings.order_window_pad_minutes: int = 5`

- [ ] **Step 1: tests** — fingerprint differs on `order_window`; probe command quotes the bracketed path; ranged closed/open commands; `prefix=False` drops `/dev/null` and awk; the setting loads from `LOGSEEKER_ORDER_WINDOW_PAD_MINUTES`.
- [ ] **Steps 2–5:** fail → implement → pass → commit `Grep a byte range of a log; drop the file prefix for one-file searches`.

### Task 4: cache only serves complete results

**Files:** Modify `logseeker/store.py`, `logseeker/jobs.py`; Test `tests/test_cache_integrity.py`

**Interfaces — Produces:** `store.write_meta(path, *, lines, bytes_written, truncated, complete=True)`; `store.discard(path) -> None`; `store.lookup` returns `None` unless the sidecar has `"complete": true`.

- [ ] **Step 1: tests** — the session's repro (fake backend: permission denied, then readable) → 2nd job is not a cache hit and has 1 line; a timed-out search is not served; a result file with no sidecar is not served; a cancelled search leaves no file.
- [ ] **Steps 2–5:** fail → implement (`out_path = None` before `try`; every failure/cancel handler calls `store.discard(out_path)`; `complete = not timed_out and not open_ended`) → pass → commit `Never serve a failed, cancelled or timed-out search from the cache`.

### Task 5: wire the window into the job

**Files:** Modify `logseeker/jobs.py`; Test `tests/test_order_window_search.py` (end-to-end, local backend, `kdb.lookup_target` faked)

**Interfaces — Consumes:** Tasks 1–4. **Produces:** `JobStatus.LOCATING`; `Job.scan_notes: list[str]`; `ScanItem(path, size_bytes, start=0, end=None, windowed=False)`; `trade_date_for(path, dates, settings) -> str | None`.

Flow after listing: `_plan_ranges` (status `LOCATING`) → size guard on the planned bytes → `_scan(items, prefix=len(files) > 1)` → if nothing matched and something was windowed and the pass was not cut short: warn, rescan the windowed files whole (size guard again).

- [ ] **Step 1: tests** (log file `APA2_pingOrderStream_20261112[23-20-00].out`, `log_date_offset_days=-1`, date `20261113`, ~2 MB, order lines 08:27–08:30, decoy match at 03:00 and 08:55):
  - closed window: decoys absent, lines start with the timestamp, `bytes_to_scan` < 25 % of the file, note mentions `` last state `filled` ``
  - `activated`: 08:55 decoy present, 03:00 absent, note says `end of file`
  - toggle off: both decoys present, no notes
  - no state row for the date: whole file, note says so
  - window with no match (kdb says 05:00): rescan finds all lines, warning present
  - gzipped log: whole file, note says gzipped
  - after midnight: order at 00:10, decoy at 23:30 absent
  - two files: prefix kept
- [ ] **Steps 2–5:** fail → implement → pass (plus full suite at baseline) → commit `Search only the order's minutes of the log, from kdb target_state`.

### Task 6: UI, Admin, docs

**Files:** Modify `app_pages/search.py`, `logseeker/ui.py`, `app_pages/admin.py`, `README.md`, `.env.example`, `.streamlit/secrets.toml.example`; Test `tests/test_app_renders.py` (existing render tests must still pass)

- [ ] Toggle in Advanced (kdb configured only) → `SearchRequest(order_window=…)`.
- [ ] Card: `LOCATING` caption "Finding the order's minutes in the log…"; one caption per `scan_notes` entry; badge `("Finding window", "blue", ":material/schedule:")`.
- [ ] Admin: `order_window_pad_minutes` row.
- [ ] Docs: README section "Order-time window", setting in both examples.
- [ ] Full suite at baseline; commit `Order-window toggle, card notes and docs`.

---

## Self-review

- Spec coverage: §1 → T2; §2 → T1; §3 → T1+T5; §4 → T3+T5; §5 → T3+T5+T6; §6 → T4; settings → T3+T6; testing → each task. No gaps.
- Names used across tasks: `order_window`, `find_range`, `WindowError`, `ScanItem`, `scan_notes`, `LOCATING`, `states`, `rdb_date`, `complete` — consistent.
