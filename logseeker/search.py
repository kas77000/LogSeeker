"""Search request model, input validation, and remote command construction.

Input validation here is a security control, not a convenience. The legacy chain
passed an order id straight from a .bat variable into `ksh -c "..."` on the log
host; that was tolerable when the only caller was a trusted operator at a
keyboard. The moment the same command line is driven by a web form it becomes
remote code execution, so every value that reaches a shell is either matched
against a strict allowlist pattern or run through shlex.quote.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass, field
from datetime import date as date_cls, timedelta

from .config import Settings

# Deliberately tight. Order ids in these logs are numeric, but ids elsewhere in
# the estate carry separators, so a small punctuation set is allowed.
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
DATE_RE = re.compile(r"^\d{8}$")

# kdb's `target`.id_target is type "i", a 32-bit int.
KDB_ID_MAX = 2**31 - 1


def kdb_id_ok(pattern: str) -> bool:
    """Could this be an id_target in kdb? Digits only, within a q int."""
    return pattern.isdigit() and int(pattern) <= KDB_ID_MAX


MATCH_FIXED = "fixed"
MATCH_REGEX = "regex"

# Which log a search reads. orderStream is greped for the order id; quoteStream
# for the order's symbol (kdb `target`.sym), and only inside the order window.
STREAM_ORDER = "order"
STREAM_QUOTE = "quote"
STREAM_NAMES = {STREAM_ORDER: "orderStream", STREAM_QUOTE: "quoteStream"}

# A symbol comes from kdb and reaches a shell, so it is allowlisted like an id.
SYMBOL_RE = re.compile(r"^[A-Za-z0-9._:/\-]{1,32}$")


def symbol_ok(sym: str) -> bool:
    return bool(SYMBOL_RE.match(sym or ""))


def symbol_regex(symbols: list[str]) -> str:
    """grep -E expression matching any of ``symbols`` as a whole token.

    ``0700.HK`` must not match ``10700.HK`` or ``0700.HKD``, so a symbol may not
    touch a letter or digit on either side. Of SYMBOL_RE's characters only ``.``
    means anything to grep -E.
    """
    alts = "|".join(s.replace(".", r"\.") for s in symbols)
    return f"(^|[^A-Za-z0-9])({alts})([^A-Za-z0-9]|$)"


class ValidationError(ValueError):
    pass


@dataclass
class SearchRequest:
    pattern: str
    instances: list[str]
    dates: list[str]                      # YYYYMMDD
    match_mode: str = MATCH_FIXED
    ignore_case: bool = False
    requested_by: str = "unknown"
    note: str = ""
    # Read only the order's minutes of each log, from kdb's target_state.
    order_window: bool = True
    stream: str = STREAM_ORDER
    # quoteStream only: the symbols to grep, from kdb. Filled in by the job.
    symbols: list[str] = field(default_factory=list)

    @property
    def stream_name(self) -> str:
        return STREAM_NAMES.get(self.stream, self.stream)

    def fingerprint(self) -> str:
        """Identity of the *search*, independent of who asked or when.

        Two people asking the same question get one grep and one cached answer.
        """
        parts = "|".join([
            self.pattern,
            ",".join(sorted(self.instances)),
            ",".join(sorted(self.dates)),
            self.match_mode,
            str(self.ignore_case),
            "window" if self.order_window else "whole",
            # Exact searches match whole ids only. Results cached by the older
            # substring match must not answer the new question.
            "whole-id",
        ])
        # Added only for quoteStream, so every orderStream result cached before
        # streams existed keeps its key.
        if self.stream != STREAM_ORDER:
            parts += f"|{self.stream}|{','.join(sorted(self.symbols))}"
        return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:20]

    def label(self, *, with_stream: bool = True) -> str:
        insts = "+".join(sorted(self.instances)) or "auto"
        if len(self.dates) == 1:
            span = self.dates[0]
        else:
            span = f"{min(self.dates)}..{max(self.dates)}"
        text = f"{self.pattern} · {insts} · {span}"
        if with_stream and self.stream != STREAM_ORDER:
            text += f" · {self.stream_name}"
        return text

    def result_filename(self) -> str:
        """Unique, self-describing filename.

        The legacy flow wrote `<ORDER_ID>.txt` into one shared directory, so two
        people searching the same order for different dates silently clobbered
        each other (../legacy/NOTES.md, point 7). Instance, date span and a hash
        of the full query are all in the name here.
        """
        safe = re.sub(r"[^A-Za-z0-9._\-]", "_", self.pattern)[:40]
        insts = "-".join(sorted(self.instances))
        span = self.dates[0] if len(self.dates) == 1 else f"{min(self.dates)}-{max(self.dates)}"
        kind = "" if self.stream == STREAM_ORDER else f"_{self.stream}"
        return f"{safe}_{insts}_{span}{kind}_{self.fingerprint()[:8]}.log"


def validate(req: SearchRequest, settings: Settings) -> SearchRequest:
    """Raise ValidationError, or return a normalised copy of the request."""
    pattern = req.pattern.strip()
    if not pattern:
        raise ValidationError("Enter an order id or search pattern.")

    if req.match_mode not in {MATCH_FIXED, MATCH_REGEX}:
        raise ValidationError(f"Unknown match mode {req.match_mode!r}.")

    if req.stream not in STREAM_NAMES:
        raise ValidationError(f"Unknown log {req.stream!r}.")
    if req.stream == STREAM_QUOTE:
        # The symbol and the window both come from kdb, for one exact order.
        if not settings.kdb_enabled:
            raise ValidationError("quoteStream needs kdb — it is not configured.")
        if req.match_mode != MATCH_FIXED:
            raise ValidationError("quoteStream is searched for an exact order id only, "
                                  "not a pattern.")
        if not kdb_id_ok(pattern):
            raise ValidationError(
                f"`{pattern}` cannot be a kdb id_target (a whole number up to "
                f"{KDB_ID_MAX:,}), so its symbol cannot be looked up for quoteStream.")
    for sym in req.symbols:
        if not symbol_ok(sym):
            raise ValidationError(f"Invalid symbol: {sym!r}")

    if req.match_mode == MATCH_FIXED:
        if not ORDER_ID_RE.match(pattern):
            raise ValidationError(
                "Order id may only contain letters, digits, dot, underscore, colon or "
                "hyphen (max 64 characters). Switch to regex mode for anything else."
            )
    else:
        if len(pattern) > 200:
            raise ValidationError("Regex pattern is limited to 200 characters.")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValidationError(f"Not a valid regular expression: {exc}") from exc

    instances = [i.strip() for i in req.instances if i.strip()]
    if not instances:
        # No instance picked: the job will take it from kdb's `target`.trader,
        # which names the log folder. That needs kdb and an id kdb can hold.
        if not settings.kdb_enabled:
            raise ValidationError("Select at least one instance.")
        if req.match_mode != MATCH_FIXED:
            raise ValidationError(
                "Pick an instance for a pattern search — the instance can only be "
                "worked out from kdb for an exact order id."
            )
        if not kdb_id_ok(pattern):
            raise ValidationError(
                f"`{pattern}` cannot be a kdb id_target (a whole number up to "
                f"{KDB_ID_MAX:,}), so the instance cannot be looked up. Pick one."
            )
    for inst in instances:
        if not INSTANCE_RE.match(inst):
            raise ValidationError(f"Invalid instance name: {inst!r}")
        if inst not in settings.instances:
            # An instance that is not in config would let a caller point the
            # glob at an arbitrary directory under /apps/k/algologs/ap/.
            raise ValidationError(f"Instance {inst!r} is not in the configured instance list.")

    dates = sorted({d.strip() for d in req.dates if d.strip()})
    if not dates:
        raise ValidationError("Select at least one date.")
    for d in dates:
        if not DATE_RE.match(d):
            raise ValidationError(f"Date must be YYYYMMDD, got {d!r}")
    if len(dates) > settings.max_days_per_search:
        raise ValidationError(
            f"That is {len(dates)} days; the limit is {settings.max_days_per_search}. "
            "Narrow the range — a wide scan ties up the log host for everyone."
        )

    return SearchRequest(
        pattern=pattern,
        instances=instances,
        dates=dates,
        match_mode=req.match_mode,
        ignore_case=req.ignore_case,
        requested_by=req.requested_by,
        note=req.note.strip()[:200],
        order_window=req.order_window,
        stream=req.stream,
        symbols=list(req.symbols),
    )


def date_range(start: date_cls, end: date_cls, *, weekdays_only: bool = True) -> list[str]:
    """Inclusive YYYYMMDD list. Weekends are skipped by default — there are no
    trading logs on a Saturday and scanning for them just wastes time."""
    if end < start:
        start, end = end, start
    out: list[str] = []
    cur = start
    while cur <= end:
        if not weekdays_only or cur.weekday() < 5:
            out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #

_GLOB_META = re.compile(r"([*?\[\]])")


def quote_glob(path: str) -> str:
    """Shell-quote a path while leaving its glob metacharacters live.

    A glob has to reach the shell unquoted to expand, but the surrounding path
    still needs quoting or a directory with a space in it silently becomes two
    arguments. So quote each literal run and pass ``* ? [ ]`` through bare:

        /var/log dir/APA*.out  ->  '/var/log dir/APA'*'.out'

    which is one word to the shell and still expands.
    """
    out = []
    for part in _GLOB_META.split(path):
        if not part:
            continue
        out.append(part if _GLOB_META.fullmatch(part) else shlex.quote(part))
    return "".join(out)


def build_list_command(req: SearchRequest, settings: Settings) -> str:
    """Step 1: resolve the globs to a concrete file list *with sizes*.

    `du -k` gives both existence and size in one round trip, which buys four
    things the legacy script did not have: an accurate "no logs for that date"
    message, a real "you are about to scan 8.4 GB" figure before a long job, a
    denominator for genuine progress reporting, and — most importantly — step 2
    can grep an explicit list of filenames instead of relying on shell globbing.

    `-k` is POSIX and gives 1024-byte units. It reports allocated blocks rather
    than apparent size, which is close enough for an estimate.
    """
    globs = []
    for inst in req.instances:
        directory = settings.log_dir(inst)
        for d in req.dates:
            # The filename carries the session's start date, not the trading
            # date the user picked — see Settings.log_date_offset_days (not
            # applied to same_day_instances, the Asia servers).
            pattern = f"{directory}/{settings.log_glob(inst, settings.file_date(d, inst), req.stream)}"
            globs.append(quote_glob(pattern))
            # After about a week the host gzips each log in place (.out ->
            # .out.gz). Look for both, so older dates are still found.
            if not pattern.endswith(".gz"):
                globs.append(quote_glob(pattern + ".gz"))
    return "du -k " + " ".join(globs) + " 2>/dev/null || true"


def exact_id_regex(order_id: str) -> str:
    """grep -E expression matching ``order_id`` only as a whole id.

    A plain substring match also returns other orders: 1370037780 is inside
    13700377801, 21370037780 and any long counter that happens to contain it.
    So the id must not touch a digit on either side — or, for an id with
    letters in it, a letter or digit. Punctuation around it (``=``, ``:``,
    ``[``, ``,``, space) is fine, as is the start or end of the line.

    ORDER_ID_RE limits ids to ``[A-Za-z0-9._:-]``; of those only ``.`` means
    anything to grep -E, so it is the only one escaped. GNU grep still pulls the
    literal id out of this expression for its fast search, so it costs about
    the same as -F.
    """
    edge = "[^0-9]" if order_id.isdigit() else "[^A-Za-z0-9]"
    literal = order_id.replace(".", r"\.")
    return f"(^|{edge}){literal}({edge}|$)"


def build_probe_command(path: str, offset: int, length: int) -> str:
    """Read ``length`` bytes of ``path`` from byte ``offset`` (0-based).

    `tail -c +N` on a regular file seeks to N rather than reading up to it, so a
    probe costs the same anywhere in a 145 GB log. That is what lets
    window.find_range binary-search a file by its timestamps.
    """
    return f"tail -c +{int(offset) + 1} {shlex.quote(path)} | head -c {int(length)}"


def build_grep_command(
    files: list[str],
    req: SearchRequest,
    settings: Settings,
    *,
    line_budget: int | None = None,
    prefix: bool = True,
    ranges: dict[str, tuple[int, int | None]] | None = None,
) -> str:
    """Step 2: grep an explicit, fully quoted file list.

    ``line_budget`` caps this batch. When a search is split across batches the
    budget is what remains of ``max_result_lines``, so the overall cap stays
    exact instead of being applied per batch.

    ``prefix`` puts ``<file>:`` in front of every line. The job passes it only
    when the search covers two or more files; with one, lines come back exactly
    as they are in the log.

    ``ranges`` maps a plain file to ``(start, end)`` byte offsets — the order's
    window. ``end`` None reads to the end of the file, including anything
    written while the grep runs.
    """
    ranges = ranges or {}
    flags = ["-E"]
    if req.ignore_case:
        flags.append("-i")
    if req.stream == STREAM_QUOTE:
        if not req.symbols or not all(symbol_ok(s) for s in req.symbols):
            raise ValueError("a quoteStream grep needs valid symbols")
        expr = symbol_regex(req.symbols)
    elif req.match_mode == MATCH_FIXED:
        expr = exact_id_regex(req.pattern)
    else:
        expr = req.pattern
    grep = f"grep {' '.join(flags)} -- {shlex.quote(expr)}"
    budget = int(max(1, settings.max_result_lines if line_budget is None else line_budget))

    windowed = [f for f in files if f in ranges]
    plain = [f for f in files if not f.endswith(".gz") and f not in ranges]
    packed = [f for f in files if f.endswith(".gz") and f not in ranges]

    # `| head` makes the pipeline's exit status head's, which is always 0, so a
    # grep that could not read its file looked exactly like "no line matched".
    # Each grep therefore reports its own status on stderr, which head never
    # touches; split_grep_status() reads it back. No `pipefail` needed, which
    # the older shells on these hosts may not have.
    report = f'echo "{GREP_RC_MARKER}$?" >&2'

    def streamed(source: str, f: str) -> str:
        # grep cannot name a pipe, so awk restores the "<file>:" prefix and
        # every result line looks the same whichever path produced it.
        part = f"{source} | {{ {grep}; {report}; }}"
        if prefix:
            part += f" | awk -v f={shlex.quote(f)} '{{print f \":\" $0}}'"
        return part

    parts = []
    if plain:
        # /dev/null as a trailing operand forces grep to prefix every match with
        # its filename even when only one log matched — `grep -H` is not
        # portable to the older greps on these hosts.
        null = " /dev/null" if prefix else ""
        parts.append(f"{{ {grep} {' '.join(shlex.quote(f) for f in plain)}{null}; "
                     f"{report}; }}")
    for f in packed:
        # Older logs are gzipped. Decompress on the host and grep the stream.
        parts.append(streamed(f"gzip -dc {shlex.quote(f)}", f))
    for f in windowed:
        start, end = ranges[f]
        source = f"tail -c +{int(start) + 1} {shlex.quote(f)}"
        if end is not None:
            source += f" | head -c {max(0, int(end) - int(start))}"
        parts.append(streamed(source, f))

    if len(parts) == 1:
        return f"{parts[0]} | head -n {budget}"
    return "{ " + "; ".join(parts) + "; } | head -n " + str(budget)


GREP_RC_MARKER = "LOGSEEKER_GREP_RC="


def split_grep_status(stderr: str) -> tuple[list[int], str]:
    """(each grep's exit status, the host's real stderr with the markers removed)."""
    codes, other = [], []
    for line in (stderr or "").splitlines():
        if line.startswith(GREP_RC_MARKER):
            value = line[len(GREP_RC_MARKER):].strip()
            if value.isdigit():
                codes.append(int(value))
                continue
        if line.strip():
            other.append(line.rstrip())
    return codes, "\n".join(other)


@dataclass
class LogFile:
    path: str
    size_bytes: int


def parse_file_list(raw: str, settings: Settings) -> tuple[list[LogFile], bool]:
    """Parse `du -k` output into a capped file list. Returns (files, was_capped).

    Each line is ``<kilobytes><TAB><path>``. Paths may contain spaces, so split
    once on the tab; fall back to the first whitespace run for `du` variants
    that pad with spaces instead.
    """
    files: list[LogFile] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if "\t" in line:
            size_part, _, path = line.partition("\t")
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            size_part, path = parts
        path = path.strip()
        if not path:
            continue
        try:
            size = int(size_part.strip()) * 1024
        except ValueError:
            continue
        files.append(LogFile(path=path, size_bytes=size))

    files.sort(key=lambda f: f.path)
    if len(files) > settings.max_files_per_search:
        return files[: settings.max_files_per_search], True
    return files, False


def batch_files(files: list[LogFile], settings: Settings) -> list[list[LogFile]]:
    """Group files into grep batches.

    One command per batch is one progress update. Big logs get their own batch
    so a gigabyte-scale scan reports progress as it goes; many small files are
    grouped so a fortnight of little logs does not become hundreds of round
    trips.
    """
    target = max(1, settings.scan_batch_bytes)
    batches: list[list[LogFile]] = []
    current: list[LogFile] = []
    current_bytes = 0
    for f in files:
        current.append(f)
        current_bytes += f.size_bytes
        if current_bytes >= target or len(current) >= 25:
            batches.append(current)
            current, current_bytes = [], 0
    if current:
        batches.append(current)
    return batches
