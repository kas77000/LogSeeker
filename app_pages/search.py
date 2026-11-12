"""Search page — submit requests and watch them run.

Several searches can be in flight at once. Each one this browser session
submitted gets its own status card, so you can fire off a second order id while
the first is still scanning and watch both.
"""

from datetime import date

import streamlit as st

from logseeker import access, kdb, store, ui
from logseeker.config import get_settings
from logseeker.jobs import JobStatus, get_manager
from logseeker.search import (
    MATCH_FIXED,
    MATCH_REGEX,
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
    f"Greps `{settings.log_glob_template}` under "
    f"`{settings.log_dir_template}` on {settings.describe_target()}."
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
                "**Exact text** looks for your input literally — `540000001` matches only "
                "that string. This is what you want for an order id, and it is faster.\n\n"
                "**Pattern (regex)** treats your input as a regular expression, so "
                "`5400000(01|02)` finds either order and `orderId=54000.*FILL` finds fills "
                "for a range of ids. Characters like `.` `*` `|` `(` become operators "
                "instead of literal text."
            ),
        )
        ignore_case = st.toggle("Ignore upper/lower case", value=False)
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
    )
    try:
        request = validate(request, settings)
    except ValidationError as exc:
        st.error(str(exc), icon=":material/error:")
    else:
        # An automatic request has no instances yet, so it cannot be a cache hit
        # until the job has asked kdb; the toast is for explicit requests only.
        cached = store.lookup(settings, request) if request.instances else None
        job = manager.submit(request)

        # Track every job this session started, newest first, rather than a
        # single "current" one — that is what lets a second search be queued
        # while the first is still running.
        tracked = [j for j in st.session_state.get("ls_my_jobs", []) if j != job.id]
        st.session_state.ls_my_jobs = [job.id] + tracked[:19]

        if cached is not None:
            st.toast("Served from cache — this exact search ran recently.",
                     icon=":material/bolt:")

# --------------------------------------------------------------------------- #
# Status cards for everything this session submitted
# --------------------------------------------------------------------------- #

def render_kdb(j) -> None:
    """What kdb's `target` table said about this id. Never blocks the grep."""
    check = j.kdb
    if check is None:       # kdb not configured, or the check has not run yet
        return

    if check.status == kdb.FOUND:
        traders = ", ".join(f"**{t}**" for t in check.traders) or "no trader recorded"
        st.success(f"id_target exists in kdb — trader: {traders}",
                   icon=":material/database:")
        if len(check.rows) > 1 or any(r.get("date") for r in check.rows):
            with st.expander(f"kdb rows ({len(check.rows)})", icon=":material/table:"):
                st.dataframe(check.rows, hide_index=True, use_container_width=True)
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


def render_job(j, *, live: bool) -> None:
    """One job's status card. ``live`` says whether we are inside the ticking
    fragment, which decides how a Cancel click reruns."""
    with st.container(border=True):
        head = st.columns([3, 1], vertical_alignment="center")
        with head[0]:
            st.markdown(f"**{j.request.label()}**")
            if j.request.note:
                st.caption(j.request.note)
        with head[1]:
            ui.status_badge(j)

        render_kdb(j)
        if j.auto_instances:
            st.caption(f":material/auto_awesome: Instance from kdb: "
                       f"**{', '.join(j.request.instances)}**")

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
        elif j.status is JobStatus.SEARCHING:
            eta = j.eta_seconds
            st.caption(
                f"Scanned {ui.human_bytes(j.bytes_scanned)} of "
                f"{ui.human_bytes(j.bytes_to_scan)} across {j.files_found} log file(s) · "
                f"{j.lines_written:,} matching lines so far · "
                f"{ui.human_duration(j.elapsed)} elapsed"
                + (f" · about {ui.human_duration(eta)} left" if eta and eta > 2 else "")
            )
            # Fraction of log actually read, not time elapsed against the
            # timeout — on a gigabyte scan the latter tells you nothing.
            st.progress(j.scan_fraction)

        for warning in j.warnings:
            st.warning(warning, icon=":material/warning:")

        if j.status is JobStatus.FAILED:
            st.error(j.error, icon=":material/error:")
        elif j.status is JobStatus.CANCELLED:
            st.info(j.error or "Cancelled.", icon=":material/cancel:")
        elif j.status is JobStatus.DONE:
            if j.lines_written == 0:
                st.warning(
                    f"Scanned {ui.human_bytes(j.bytes_scanned)} across {j.files_found} "
                    f"log file(s) in {ui.human_duration(j.elapsed)} — no line matched "
                    f"`{j.request.pattern}`.",
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
                if st.button("Dismiss", icon=":material/close:", key=f"clear_{j.id}"):
                    st.session_state.ls_my_jobs = [
                        i for i in st.session_state.get("ls_my_jobs", []) if i != j.id
                    ]
                    st.session_state.pop("ls_prepared_download", None)
                    st.rerun()

        if j.status is JobStatus.DONE and j.lines_written and j.result_path:
            with st.expander(f"Preview — first lines of {j.result_path.name}",
                             icon=":material/description:"):
                st.code(ui.preview_text(j.result_path), language="log", line_numbers=False)


def my_jobs():
    """Jobs this session submitted that still exist in the manager."""
    out = []
    for job_id in st.session_state.get("ls_my_jobs", []):
        job = manager.get(job_id)
        if job is not None:
            out.append(job)
    return out


def render_all(*, live: bool) -> None:
    jobs = my_jobs()
    if not jobs:
        return
    running = sum(1 for j in jobs if not j.status.is_terminal)
    header = f"Your searches ({len(jobs)})"
    if running:
        header += f" · {running} running"
    st.subheader(header)
    for j in jobs:
        render_job(j, live=live)


@st.fragment(run_every=1.0)
def live_panel() -> None:
    jobs = my_jobs()
    if jobs and all(j.status.is_terminal for j in jobs):
        # Everything settled: hand off to the static renderer and stop ticking.
        # Left running, a finished job would re-render once a second — and every
        # tick would re-read the result file behind its download button.
        st.rerun()
    render_all(live=True)


if any(not j.status.is_terminal for j in my_jobs()):
    live_panel()
else:
    render_all(live=False)

# --------------------------------------------------------------------------- #

others = [j for j in manager.recent(5)
          if j.id not in set(st.session_state.get("ls_my_jobs", []))]
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
