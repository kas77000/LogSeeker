"""Results page — every search this server has run, and the files they produced."""

from datetime import datetime

import streamlit as st

from logseeker import access, ui
from logseeker.config import get_settings
from logseeker.jobs import JobStatus, get_manager

settings = get_settings()
manager = get_manager()
user = access.requester_label(settings)

st.title("Results")
st.caption(
    "Searches are shared across everyone using this app — if a colleague already "
    "pulled the logs you need, take their file instead of running the grep again."
)

controls = st.columns([1, 1, 2], vertical_alignment="center")
with controls[0]:
    scope = st.segmented_control("Show", options=["Everyone", "Mine"], default="Everyone")
with controls[1]:
    only_matches = st.toggle("Only with matches", value=False)

jobs = manager.recent(settings.job_history)
if scope == "Mine":
    jobs = [j for j in jobs if j.request.requested_by == user]
if only_matches:
    jobs = [j for j in jobs if j.matched]

if not jobs:
    st.info("Nothing here yet. Run a search first.", icon=":material/inbox:")
    st.page_link("app_pages/search.py", label="Go to search", icon=":material/search:")
    st.stop()


def _visible_jobs():
    live = manager.recent(settings.job_history)
    if scope == "Mine":
        live = [j for j in live if j.request.requested_by == user]
    if only_matches:
        live = [j for j in live if j.matched]
    return live


def job_table():
    for job in _visible_jobs():
        with st.container(border=True):
            row = st.columns([4, 1, 1], vertical_alignment="center")

            with row[0]:
                st.markdown(f"**{job.request.pattern}**")
                st.caption(
                    f"{'+'.join(job.request.instances)} · "
                    f"{job.request.dates[0]}"
                    + (f"–{job.request.dates[-1]}" if len(job.request.dates) > 1 else "")
                    + f" · requested by {job.request.requested_by} · "
                    + datetime.fromtimestamp(job.created_at).strftime("%d %b %H:%M")
                )
                if job.request.note:
                    st.caption(f":material/label: {job.request.note}")

            with row[1]:
                ui.status_badge(job)
                if job.status is JobStatus.DONE:
                    st.caption(f"{job.lines_written:,} lines")

            with row[2]:
                ui.download_button(job, key=f"dl_{job.id}", use_container_width=True)

            if job.status is JobStatus.FAILED and job.error:
                st.error(job.error, icon=":material/error:")

            for warning in job.warnings:
                st.warning(warning, icon=":material/warning:")

            if job.status is JobStatus.DONE and job.lines_written and job.result_path:
                with st.expander("Preview", icon=":material/description:"):
                    meta = st.columns(4)
                    meta[0].metric("Lines", f"{job.lines_written:,}")
                    meta[1].metric("Size", ui.human_bytes(job.bytes_written))
                    meta[2].metric("Files scanned", job.files_found)
                    meta[3].metric(
                        "Took",
                        "cached" if job.cache_hit else ui.human_duration(job.elapsed),
                    )
                    st.code(ui.preview_text(job.result_path), language="log")


# Only poll while something is actually running. A timer ticking over a settled
# list would re-render every row — and re-read every result file behind the
# download buttons — to show information that has not changed.
if manager.active_count():
    st.fragment(run_every=3.0)(job_table)()
    st.caption(":material/bolt: Refreshing while searches are running.")
else:
    job_table()
    if st.button("Refresh", icon=":material/refresh:"):
        st.rerun()
