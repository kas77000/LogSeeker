"""Small presentation helpers shared by the pages."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from . import store
from .jobs import Job, JobStatus

# Wide layout is right for this app — the results rows and the log preview both
# want room. But uncapped it keeps growing, and on a 34" monitor a status card
# stretches far enough that its badge sits a hand's width from the order id it
# belongs to. Cap the content column instead of dropping back to layout="centered",
# which would squeeze the previews.
CONTENT_MAX_WIDTH = 1200


def apply_content_width() -> None:
    """Cap and centre the main content column. Call once, after set_page_config."""
    st.markdown(
        f"""
        <style>
          [data-testid="stMainBlockContainer"] {{
              max-width: {CONTENT_MAX_WIDTH}px;
              margin-inline: auto;
          }}
        </style>
        """,
        unsafe_allow_html=True,
    )


STATUS_BADGE = {
    JobStatus.QUEUED: ("Queued", "gray", ":material/schedule:"),
    JobStatus.LISTING: ("Finding logs", "blue", ":material/folder_open:"),
    JobStatus.SEARCHING: ("Searching", "blue", ":material/search:"),
    JobStatus.DONE: ("Done", "green", ":material/check_circle:"),
    JobStatus.FAILED: ("Failed", "red", ":material/error:"),
    JobStatus.CANCELLED: ("Cancelled", "orange", ":material/cancel:"),
}


def status_badge(job: Job) -> None:
    label, color, icon = STATUS_BADGE[job.status]
    if job.status is JobStatus.DONE and job.lines_written == 0:
        label, color, icon = "No matches", "orange", ":material/search_off:"
    st.badge(label, icon=icon, color=color)


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def preview_text(path: Path, max_lines: int = 200, max_bytes: int = 400_000) -> str:
    """Head of a result file — never load a 64 MB grep into the browser."""
    if not path.exists():
        return ""
    chunks: list[str] = []
    used = 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i >= max_lines or used >= max_bytes:
                chunks.append("\n… truncated preview — download the file for the full output.\n")
                break
            chunks.append(line)
            used += len(line)
    return "".join(chunks)


def download_button(job: Job, *, key: str, use_container_width: bool = False) -> None:
    """Offer the result file, without re-reading a large one on every rerun.

    ``st.download_button`` needs its payload up front, so a naive
    ``data=path.read_bytes()`` re-reads the whole file on *every* script run —
    once per second inside an auto-refreshing fragment, and once per listed job
    on the results page. At 60 MB a search that is perfectly cheap to produce
    becomes ruinous to display.

    So: small results are offered directly, and anything larger needs one click
    to prepare. Then only that one file is read, and gzip is offered first
    because grep output over these logs compresses about ten-to-one.
    """
    from .config import get_settings

    path = job.result_path
    if path is None or not path.exists() or job.lines_written == 0:
        return

    settings = get_settings()
    size = path.stat().st_size

    if size <= settings.inline_download_bytes:
        st.download_button(
            "Download",
            data=path.read_bytes(),
            file_name=path.name,
            mime="text/plain",
            icon=":material/download:",
            key=key,
            type="primary",
            use_container_width=use_container_width,
        )
        return

    prepared_key = "ls_prepared_download"
    if st.session_state.get(prepared_key) != job.id:
        if st.button(
            f"Prepare download · {human_bytes(size)}",
            icon=":material/archive:",
            key=f"{key}_prep",
            use_container_width=use_container_width,
            help="Large result — click to compress it and get the download links.",
        ):
            st.session_state[prepared_key] = job.id
            st.rerun()
        return

    with st.spinner("Compressing…"):
        gz = store.gzipped(path)
    gz_size = gz.stat().st_size

    st.download_button(
        f"Download .gz · {human_bytes(gz_size)}",
        data=gz.read_bytes(),
        file_name=gz.name,
        mime="application/gzip",
        icon=":material/download:",
        key=f"{key}_gz",
        type="primary",
        use_container_width=use_container_width,
        help=f"Compressed from {human_bytes(size)}.",
    )
    st.download_button(
        f"Download raw .txt · {human_bytes(size)}",
        data=path.read_bytes(),
        file_name=path.name,
        mime="text/plain",
        icon=":material/description:",
        key=f"{key}_raw",
        use_container_width=use_container_width,
    )
