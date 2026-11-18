"""Search page — submit requests and watch them run.

Several searches can be in flight at once. Each one this browser session
submitted gets its own status card, so you can fire off a second order id while
the first is still scanning and watch both.
"""

from dataclasses import replace
from datetime import date

import streamlit as st

from logseeker import access, kdb, store, ui
from logseeker.config import get_settings
from logseeker.jobs import JobStatus, get_manager
from logseeker.search import (
    MATCH_FIXED,
    MATCH_REGEX,
    STREAM_NAMES,
    STREAM_ORDER,
    STREAM_QUOTE,
    SearchRequest,
    ValidationError,
    date_range,
    validate,
)

settings = get_settings()
manager = get_manager()
user = access.requester_label(settings)

FORM_WIDTH = 560

st.title("Search order logs")
st.caption(
    f"Greps `{settings.log_glob_template}`"
    + (f" and `{settings.quote_glob_template}`" if settings.kdb_enabled else "")
    + f" under `{settings.log_dir_template}` on {settings.describe_target()}."
)

# --------------------------------------------------------------------------- #
# Request form
# --------------------------------------------------------------------------- #

# The date-mode switch lives outside the form on purpose: widgets inside a form
# do not trigger a rerun until submit, so a selector in there could not change
# which date inputs are shown.
with st.container(width=FORM_WIDTH):
    date_mode = st.segmented_control(
        "Dates",
        options=["Single date", "Date range"],
        default="Single date",
        key="ls_date_mode",
        help="Most searches are for one day. Switch to a range only when you need it — "
             "every extra day is more log to read.",
    )

with st.form("search_form", border=True, width=FORM_WIDTH):
    pattern = st.text_input(
        "Order id",
        placeholder="540000001",
        help="Searched as literal text. Use Advanced → Search type for pattern matching.",
    )

    # With kdb configured the instance can be left empty: kdb's `target`.trader
    # names the log folder, so the job looks it up before grepping.
    auto_ok = settings.kdb_enabled
    instances = st.multiselect(
        "Instance",
        options=settings.instances,
        default=[] if auto_ok else settings.instances[:1],
        placeholder="Automatic — from the kdb trader" if auto_ok else "Choose an instance",
        help=("Leave empty and LogSeeker asks kdb which instance handled this order "
              "(the `trader` column), then searches only that one. Pick one or more "
              "to override.") if auto_ok else None,
    )

    today = date.today()
    if date_mode == "Date range":
        cols = st.columns(2)
        with cols[0]:
            start = st.date_input("From", value=today, max_value=today, format="YYYY-MM-DD")
        with cols[1]:
            end = st.date_input("To", value=today, max_value=today, format="YYYY-MM-DD")
    else:
        start = st.date_input("Date", value=today, max_value=today, format="YYYY-MM-DD")
        end = start

    # quoteStream is greped for the order's symbol inside the order's window,
    # both of which only kdb knows — so it is offered only with kdb.
    if settings.kdb_enabled:
        streams = st.pills(
            "Logs",
            options=[STREAM_ORDER, STREAM_QUOTE],
            format_func=STREAM_NAMES.get,
            selection_mode="multi",
            default=[STREAM_ORDER],
            help=(
                "**orderStream** — lines with this order id.\n\n"
                "**quoteStream** — the order's symbol (kdb `target`.sym), only between "
                f"its first `target_state` time − {settings.order_window_pad_minutes} min "
                f"and its last + {settings.order_window_pad_minutes} min. Each log gets "
                "its own result file."
            ),
        ) or []
    else:
        streams = [STREAM_ORDER]

    note = st.text_input(
        "Reason (optional)",
        placeholder="e.g. INC0123456 — client query on fill price",
        help="Stored in the audit log next to your search.",
    )

    with st.expander("Advanced", icon=":material/tune:"):
        search_type = st.radio(
            "Search type",
            options=["Exact text", "Pattern (regex)"],
            index=0,
            help=(
                "**Exact text** looks for your input literally, as a whole id — "
                "`540000001` matches `orderId=540000001` but not `5400000012` or "
                "`1540000001`, so only lines about that order come back. This is what "
                "you want for an order id.\n\n"
                "**Pattern (regex)** treats your input as a regular expression, so "
                "`5400000(01|02)` finds either order and `orderId=54000.*FILL` finds fills "
                "for a range of ids. Characters like `.` `*` `|` `(` become operators "
                "instead of literal text."
            ),
        )
        ignore_case = st.toggle("Ignore upper/lower case", value=False)
        if settings.kdb_enabled:
            order_window = st.toggle(
                "Search only the order's active window (from kdb)",
                value=True,
                help=(
                    "Reads only the part of each log between the order's first "
                    f"`target_state` time − {settings.order_window_pad_minutes} min and its "
                    f"last + {settings.order_window_pad_minutes} min — or to the end of the "
                    "log while it is still `activated` — instead of the whole file. "
                    "Falls back to the whole file when kdb has no times for it, the "
                    "log is gzipped, or nothing matches inside the window.\n\n"
                    "Off: read every log in full."
                ),
            )
        else:
            order_window = False
        if date_mode == "Date range":
            skip_weekends = st.toggle(
                "Skip weekends", value=True,
                help="There are no trading logs on Saturday or Sunday.",
            )
        else:
            skip_weekends = False

    submitted = st.form_submit_button("Search", type="primary", icon=":material/search:")


if submitted:
    request = SearchRequest(
        pattern=pattern,
        instances=instances,
        dates=date_range(start, end, weekdays_only=skip_weekends),
        match_mode=MATCH_REGEX if search_type == "Pattern (regex)" else MATCH_FIXED,
        ignore_case=ignore_case,
        requested_by=user,
        note=note,
        order_window=order_window,
    )
    try:
        if not streams:
            raise ValidationError("Pick at least one log — orderStream or quoteStream.")
        request = validate(request, settings)
        if STREAM_QUOTE in streams:
            # quoteStream has rules of its own (exact id, kdb); say so now
            # rather than as a skipped row a few seconds later.
            validate(replace(request, stream=STREAM_QUOTE), settings)
    except ValidationError as exc:
        st.error(str(exc), icon=":material/error:")
    else:
        # An automatic request has no instances yet, so it cannot be a cache hit
        # until kdb has been asked; the toast is for explicit requests only.
        cached = (store.lookup(settings, request)
                  if request.instances and STREAM_ORDER in streams else None)
        new = manager.submit_search(request, [s for s in (STREAM_ORDER, STREAM_QUOTE)
                                              if s in streams])

        # Track every card this session started, newest first, rather than a
        # single "current" one — that is what lets a second search be queued
        # while the first is still running.
        tracked = [i for i in st.session_state.get("ls_my_searches", []) if i != new.id]
        st.session_state.ls_my_searches = [new.id] + tracked[:19]

        if cached is not None:
            st.toast("orderStream served from cache — this exact search ran recently.",
                     icon=":material/bolt:")

# --------------------------------------------------------------------------- #
# Status cards for everything this session submitted
# --------------------------------------------------------------------------- #

def render_kdb(check) -> None:
    """What kdb's `target` table said about this id. Never blocks the grep."""
    if check is None:       # kdb not configured, or the check has not run yet
        return

    if check.status == kdb.FOUND:
        traders = ", ".join(f"**{t}**" for t in check.traders) or "no trader recorded"
        syms = ", ".join(f"`{s}`" for s in check.symbols)
        st.success(f"id_target exists in kdb — trader: {traders}"
                   + (f" · sym: {syms}" if syms else ""),
                   icon=":material/database:")
    elif check.status == kdb.NOT_FOUND:
        st.warning(
            f"{check.message} The log search still runs — but check the id and date.",
            icon=":material/database:",
        )
    elif check.status == kdb.ERROR:
        st.warning(f"{check.message} The log search is unaffected.",
                   icon=":material/database:")
    else:
        st.caption(f":material/database: {check.message}")

    if check.skipped_dates:
        which = "today (no RDB configured)" if check.skipped_dates == [
            date.today().strftime("%Y%m%d")] else (
            f"{len(check.skipped_dates)} date(s) — no endpoint configured for them")
        st.caption(f"Not checked in kdb: {which}.")


def render_stream(j, *, live: bool) -> None:
    """One log of a card: its status, line count, notes and download."""
    with st.container(border=True):
        head = st.columns([3, 1], vertical_alignment="center")
        with head[0]:
            st.markdown(f"**{j.request.stream_name}**"
                        + (" · " + ", ".join(f"`{s}`" for s in j.request.symbols)
                           if j.request.symbols else ""))
        with head[1]:
            ui.status_badge(j)

        for note in j.scan_notes:
            st.caption(f":material/schedule: {note}")

        if j.status is JobStatus.QUEUED:
            st.caption(
                f"Waiting for a free worker "
                f"({manager.settings.workers} run at a time) · "
                f"{ui.human_duration(j.wait_time)}"
            )
        elif j.status is JobStatus.CHECKING_KDB:
            st.caption("Asking kdb whether this id_target exists…")
        elif j.status is JobStatus.LISTING:
            st.caption("Working out which log files exist for those dates…")
        elif j.status is JobStatus.LOCATING:
            st.caption("Finding the order's minutes in the log…")
        elif j.status is JobStatus.SEARCHING:
            eta = j.eta_seconds
            st.caption(
                f"Scanned {ui.human_bytes(j.bytes_scanned)} of "
                f"{ui.human_bytes(j.bytes_to_scan)} across {j.files_found} log file(s) · "
                f"{j.lines_written:,} matching lines so far · "
                f"{ui.human_duration(j.elapsed)} elapsed"
                + (f" · about {ui.human_duration(eta)} left" if eta and eta > 2 else "")
            )

        for warning in j.warnings:
            st.warning(warning, icon=":material/warning:")

        if j.status is JobStatus.FAILED:
            st.error(j.error, icon=":material/error:")
        elif j.status is JobStatus.SKIPPED:
            st.info(j.error, icon=":material/block:")
        elif j.status is JobStatus.CANCELLED:
            st.info(j.error or "Cancelled.", icon=":material/cancel:")
        elif j.status is JobStatus.DONE:
            what = (f"`{', '.join(j.request.symbols)}`" if j.request.symbols
                    else f"`{j.request.pattern}`")
            if j.lines_written == 0:
                st.warning(
                    f"Scanned {ui.human_bytes(j.bytes_scanned)} across {j.files_found} "
                    f"log file(s) in {ui.human_duration(j.elapsed)} — no line matched "
                    f"{what}.",
                    icon=":material/search_off:",
                )
            else:
                source = "from cache" if j.cache_hit else (
                    f"from {ui.human_bytes(j.bytes_scanned)} of logs "
                    f"({j.files_found} file(s)) in {ui.human_duration(j.elapsed)}"
                )
                st.success(
                    f"{j.lines_written:,} matching lines "
                    f"({ui.human_bytes(j.bytes_written)}) {source}.",
                    icon=":material/check_circle:",
                )

        with st.container(horizontal=True):
            if not j.status.is_terminal:
                if st.button("Cancel", icon=":material/stop_circle:", key=f"cancel_{j.id}"):
                    j.cancel()
                    st.rerun(scope="fragment" if live else "app")
            else:
                ui.download_button(j, key=f"dl_live_{j.id}")

        if j.status is JobStatus.DONE and j.lines_written and j.result_path:
            with st.expander(f"Preview — first lines of {j.result_path.name}",
                             icon=":material/description:"):
                st.code(ui.preview_text(j.result_path), language="log", line_numbers=False)


def render_search(s, *, live: bool) -> None:
    """One card: the kdb check once, then a row per log. ``live`` says whether
    we are inside the ticking fragment, which decides how a click reruns."""
    with st.container(border=True):
        head = st.columns([3, 1], vertical_alignment="center")
        with head[0]:
            st.markdown(f"**{s.request.label(with_stream=False)}**")
            if s.request.note:
                st.caption(s.request.note)
        with head[1]:
            ui.search_badge(s)

        render_kdb(s.kdb)
        if s.kdb is not None and s.kdb.status == kdb.FOUND:
            if st.button("Order details", icon=":material/receipt_long:",
                         key=f"details_{s.id}",
                         help="The order's `target` row and its `workorder`s, from kdb."):
                # Opened from the full script, not from inside the ticking
                # fragment, so the dialog survives the fragment's reruns.
                st.session_state.ls_details_open = s.id
                st.rerun(scope="app")
        if s.auto_instances:
            st.caption(f":material/auto_awesome: Instance from kdb: "
                       f"**{', '.join(s.request.instances)}**")
        for warning in s.warnings:
            st.warning(warning, icon=":material/warning:")

        if s.status is JobStatus.QUEUED:
            st.caption(f"Waiting for a free worker ({manager.settings.workers} run at a time)…")
        elif s.status is JobStatus.CHECKING_KDB:
            st.caption("Asking kdb about this id_target…")
        elif s.status is JobStatus.FAILED:
            st.error(s.error, icon=":material/error:")

        for j in s.jobs:
            render_stream(j, live=live)

        if s.is_terminal:
            if st.button("Dismiss", icon=":material/close:", key=f"clear_{s.id}"):
                st.session_state.ls_my_searches = [
                    i for i in st.session_state.get("ls_my_searches", []) if i != s.id
                ]
                st.session_state.pop("ls_prepared_download", None)
                st.rerun()


def my_searches():
    """Cards this session submitted that still exist in the manager."""
    out = []
    for search_id in st.session_state.get("ls_my_searches", []):
        s = manager.get_search(search_id)
        if s is not None:
            out.append(s)
    return out


def render_all(*, live: bool) -> None:
    searches = my_searches()
    if not searches:
        return
    running = sum(1 for s in searches if not s.is_terminal)
    header = f"Your searches ({len(searches)})"
    if running:
        header += f" · {running} running"
    st.subheader(header)
    for s in searches:
        render_search(s, live=live)


@st.fragment(run_every=1.0)
def live_panel() -> None:
    searches = my_searches()
    if searches and all(s.is_terminal for s in searches):
        # Everything settled: hand off to the static renderer and stop ticking.
        # Left running, a finished job would re-render once a second — and every
        # tick would re-read the result file behind its download button.
        st.rerun()
    render_all(live=True)


if any(not s.is_terminal for s in my_searches()):
    live_panel()
else:
    render_all(live=False)


# --------------------------------------------------------------------------- #
# Order details
# --------------------------------------------------------------------------- #

def _close_details() -> None:
    st.session_state.pop("ls_details_open", None)


def _table(title: str, rows: list[dict], error: str | None, empty: str, *,
           key: str) -> None:
    """One table in a section that can be folded away; open to start with."""
    label = f"**{title}**" + (f" · {len(rows)} row(s)" if rows else "")
    with st.expander(label, expanded=True, key=key):
        if error:
            st.warning(error, icon=":material/warning:")
        elif not rows:
            st.caption(empty)
        elif len(rows) == 1:
            # One row of many columns reads better down the page than across it.
            st.dataframe([{"column": k, "value": "" if v is None else str(v)}
                          for k, v in rows[0].items()],
                         hide_index=True, use_container_width=True)
        else:
            st.dataframe(rows, hide_index=True, use_container_width=True)


@st.dialog("Order details", width="large", on_dismiss=_close_details)
def order_details_dialog(search) -> None:
    req = search.request
    key = f"ls_details_{req.pattern}_{','.join(req.dates)}"
    head = st.columns([4, 1], vertical_alignment="center")
    with head[0]:
        st.markdown(f"id_target **{req.pattern}** · {', '.join(req.dates)}")
    with head[1]:
        if st.button("Refresh", icon=":material/refresh:", key="details_refresh"):
            st.session_state.pop(key, None)

    if key not in st.session_state:
        with st.spinner("Asking kdb…"):
            st.session_state[key] = kdb.order_details(settings, req.pattern, req.dates)
    details = st.session_state[key]

    if details.error:
        st.error(details.error, icon=":material/error:")
    for src in details.sources:
        st.subheader(f"{src.source} · `{src.endpoint}`", divider="gray")
        if src.error:
            st.error(src.error, icon=":material/error:")
            continue
        _table("target", src.target, src.target_error, "No target row.",
               key=f"details_target_{src.source}")
        _table("workorder", src.workorder, src.workorder_error, "No workorder for this order.",
               key=f"details_workorder_{src.source}")
    if details.skipped_dates:
        st.caption(f"Not asked: {', '.join(details.skipped_dates)} — no kdb endpoint "
                   "configured for those dates.")


details_for = st.session_state.get("ls_details_open")
if details_for:
    shown = manager.get_search(details_for)
    if shown is None:
        _close_details()
    else:
        order_details_dialog(shown)

# --------------------------------------------------------------------------- #

mine = {j.id for s in my_searches() for j in s.jobs}
others = [j for j in manager.recent(5) if j.id not in mine]
if others:
    st.subheader("Recent searches by others")
    for j in others:
        row = st.columns([4, 1, 1], vertical_alignment="center")
        with row[0]:
            st.markdown(f"`{j.request.label()}`")
            st.caption(f"{j.request.requested_by} · {ui.human_duration(j.elapsed)}")
        with row[1]:
            ui.status_badge(j)
        with row[2]:
            ui.download_button(j, key=f"dl_recent_{j.id}", use_container_width=True)
    st.page_link("app_pages/results.py", label="All results", icon=":material/inbox:")
