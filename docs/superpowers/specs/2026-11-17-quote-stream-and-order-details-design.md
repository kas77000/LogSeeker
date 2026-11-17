# quoteStream search and Order details — design

Date: 2026-11-17
Status: approved in conversation

## 1. quoteStream

### Behaviour

The search form gains **Logs**: `orderStream` (default) and `quoteStream`, one or
both. quoteStream is offered only when kdb is configured, and only for an exact
order id that kdb can hold — it needs kdb for both the symbol and the window.

One submit is one **card**:

1. kdb is asked once (`target` trader + sym, `target_state` times);
2. the instance is taken from `trader` when none was picked, as today;
3. one job per ticked stream starts, each with its own status, live line
   count, scan notes, warnings, cancel, cache entry and download.

Searching orderStream first and quoteStream later is simply a second card; a
later search with both serves orderStream from cache.

### Finding the file

`quote_glob_template = "quotestream.{yymmdd}-*.log"` under the same
`log_dir_template`. `{yymmdd}` is the file date (trading date shifted by
`log_date_offset_days`) as six digits; `{date}` stays available as YYYYMMDD.
`.log.gz` is listed too. A file belongs to a trading date when its name holds
`.<yymmdd>-` (quote) or `<yyyymmdd>` (order).

### What is grepped, and where

- The symbol(s) of the order: `target.sym`, now returned by the kdb lookup.
  Each must match `^[A-Za-z0-9._:/\-]{1,32}$` (it reaches a shell); several
  are OR-ed. Matched as a whole token: not touching a letter or digit.
- Only inside the order window (first `target_state` time − pad → last + pad,
  or to end of file while `activated`) — the order-window toggle does not
  apply to quoteStream.
- **No whole-file fallback.** A file without a window (gzipped, no timed
  `target_state` row for its date, window not placeable) is not read and the
  card says why. If no file is left, or kdb did not find the order, or it has
  no usable sym, the stream ends as **Skipped** with the reason. The
  "nothing in the window → rescan whole file" step is orderStream only.

### Cache and naming

`SearchRequest.stream` (`order` | `quote`) and `symbols` join the fingerprint
only for quote, so existing orderStream cache entries stay valid. Quote result
files are `<id>_<inst>_<span>_quote_<hash>.log`.

## 2. Order details

A button on the card once kdb said **found**. It opens an `st.dialog` that asks
kdb (worker op `details`, same RDB/HDB split by date as the lookup):

```q
/ RDB                                          / HDB
n sublist select from target    where id_target=x     ... where date in ds, id_target=x
n sublist select from workorder where id_target=x     ... where date in ds, id_target=x
```

`n` = 500. Every column is returned; values become JSON-safe text/numbers in
the worker (times as `HH:MM:SS.mmm`). The dialog shows **target** and
**workorder** (sorted by `t_gen` when present) per source, each query's error
in its own section only. The answer is kept in the session; **Refresh**
re-queries.

## Testing

Glob/date mapping for `{yymmdd}`; symbol validation and whole-token regex;
fingerprints by stream and symbol; a two-stream search end to end on the local
backend with kdb faked (one kdb call, two result files, quote reads only the
window, each skip reason, one stream failing leaves the other alone); the
worker's `details` op and value conversion with fake tables; the card renders
for one and two streams, and the details dialog renders.
