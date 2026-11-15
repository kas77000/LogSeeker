# Order-time window search — design

Date: 2026-11-15
Status: approved in conversation, awaiting spec review

## Problem

An order's lines sit in a few minutes of a log that can be 145 GB. Today every
search reads the whole file: a full pass takes many minutes, the progress bar
sits at 0 B until the end (one file is one batch), and the answer was already
complete long before the scan finished.

kdb's `target_state` knows when each order lived. Log lines start with a
`HH:MM:SS,mmm` timestamp and are written in time order. So the search can jump
straight to the order's minutes and read only those.

## Behaviour

With **"Search only the order's active window (from kdb)"** on (the default):

| last `target_state.state` | bytes read |
|---|---|
| not `` `activated `` | first time − pad → last time + pad |
| `` `activated `` | first time − pad → end of file |

`pad` is `order_window_pad_minutes` (default 5). With the toggle off, every file
is scanned whole, as today.

A file falls back to a **whole-file scan** — and the card says why — when:

1. the log is gzipped (a gzip stream cannot be jumped into);
2. kdb returned no timed `target_state` row for that file's trading date, or the
   kdb lookup failed;
3. no timestamped line in the first 64 KB of the file (no session start);
4. no timestamped line could be found where the binary search looked.

And after the windowed pass, if **no line matched at all**, every windowed file
is rescanned whole (the log host's clock and kdb's may disagree), with a warning.

Regex searches and setups without kdb always scan whole files; the toggle is
hidden when kdb is not configured.

## Components

### 1. kdb: first/last time and last state (`kdbworker/lookup.py`, `kdb.py`)

The existing lookup gains a second query in the same connection, run in its own
try so a failure there never loses the trader:

```q
/ RDB — current session, no date column
{[x] 0!select first_time:first time, last_time:last time, last_state:last state
   by id_server from `time xasc
   select id_server,time,state from target_state where id_target=x, not null time}

/ HDB — partitioned: date predicate first
{[ds;x] 0!select first_time:first time, last_time:last time, last_state:last state
   by date,id_server from `date`time xasc
   select date,id_server,time,state from target_state
   where date in ds, id_target=x, not null time}
```

Rows with a null `time` are dropped: they cannot be placed, and as midnight they
would become the first event (same rule as `kdb-queries/scripts/luld_orders`).

The worker returns `time` as milliseconds since midnight. `KdbCheck` gains
`states: list[dict]` — `{date, id_server, first_ms, last_ms, last_state}` — with
RDB rows dated to today's trading date (the date `split_dates` sent to the RDB).

### 2. Window logic (`logseeker/window.py`, new, pure)

- `order_window(states, trade_date, pad_ms) -> Window | None` — merges every
  `id_server` row for that date into one window: earliest first, latest last,
  `open_ended` if any row's last state is `activated`. `None` when there is no row.
- Session start = the timestamp of the **first timestamped line of the file**
  (one 64 KB probe at offset 0), not the `[HH-MM-SS]` in the filename. Log
  lines and kdb `target_state.time` are both HKT; the filename is dated one
  day before the trading date (`log_date_offset_days = -1`) and its stamp may
  be on another clock, so it is not used for timing.
- `session_ms(clock_ms, start_ms) -> ms` — time since session start,
  `(clock − start) mod 24h`, so after-midnight lines sort after 23:59 ones in a
  session that began the evening before. A window start before the session
  start clamps to 0. If the session never crosses midnight this is a no-op.
- `first_timestamp(chunk: bytes) -> (offset_in_chunk, clock_ms) | None` — the
  first line in the chunk, after the first newline, that starts with
  `^\d\d:\d\d:\d\d[,.]\d{3}`. Lines without one (stack traces, continuations)
  are skipped.

### 3. Locating the byte range (`search.py`, `jobs.py`)

For each plain file with a window:

- Binary search over byte offsets. One probe =
  `tail -c +N <file> | head -c 65536` on the job's existing SSH session
  (`tail -c +N` seeks; it does not read the bytes before N).
- Lower bound: the start of the first timestamped line whose session time is ≥
  window start. Upper bound: the start of the first such line > window end, or
  end of file when the window is open-ended or the end is past the last line.
- About 40 probes per bound, each a few KB on the wire.

The grep for that file becomes

```sh
tail -c +START <file> | head -c LENGTH | grep -F -- <id>     # closed window
tail -c +START <file> | grep -F -- <id>                      # open-ended
```

reporting its exit status the same way as today (`LOGSEEKER_GREP_RC=`).
`bytes_to_scan` is the sum of the ranges, so progress and the size guard both
see what is really read.

### 4. Result line prefix

`<file>:` is added only when the search covers **two or more** files. A
one-file search returns lines exactly as in the raw log, whichever path (plain,
gzipped, windowed) produced them.

### 5. The toggle and the card

- `SearchRequest.order_window: bool = True`, part of `fingerprint()` so
  windowed and whole-file results never share a cache entry.
- Search page → Advanced: `st.toggle("Search only the order's active window
  (from kdb)", value=True)`, shown only when kdb is configured.
- The card lists, per file, what was read, e.g. *"07:45:01 → 08:55:12 ·
  1.2 GB of 145 GB · last state `filled`"*, or *"whole file — gzipped"*.

### 6. Cache correctness (fix included)

- A search that fails, is cancelled or times out deletes its result file.
- `store.lookup` serves only results whose sidecar says `"complete": true`;
  a file with no sidecar (left by an older version) is not a cache hit.
- A search that read up to end-of-file of a live order (`open_ended`) is marked
  `"complete": false`: the log is still being written, so a cached copy would
  miss lines written since.

## Settings

| key | default | |
|---|---|---|
| `order_window_pad_minutes` | `5` | padding either side of the kdb window |

Shown on the Admin page with the others.

## Testing

- `window.py`: window merge across `id_server`s; open-ended; midnight wrap;
  clamp at session start; `first_timestamp` skipping partial and untimestamped
  lines; session start read from the file's first line, ignoring the
  filename's `[HH-MM-SS]`.
- Locating: a generated log (local backend) with a known layout — the range
  found starts and ends on the right lines, including windows at the very start
  and end of the file.
- End to end (local backend, kdb faked as in `tests/test_kdb.py`): only the
  window is read; each of the four fallbacks triggers a whole-file scan with
  its reason; zero matches in the window triggers the rescan; toggle off reads
  everything; one-file results have no prefix, two-file results do.
- Cache: failed, cancelled and timed-out searches leave nothing servable; an
  open-ended search is not served from cache; the earlier repro
  (permission denied, then a working account) now runs the grep the second time.

## Out of scope

- Why same-day orders fail (RDB/HDB split by the app server's date) — next.
- Byte-range progress inside one grep — the window makes ranges small enough
  that per-file progress is adequate.
