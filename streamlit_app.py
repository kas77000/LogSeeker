"""LogSeeker — entry point.

Run with:  streamlit run streamlit_app.py

There is no sign-in. The operator launches this app with his own log-host
credentials in the environment (see .env.example); everyone who opens the URL
searches as him.
"""

import streamlit as st

from logseeker import access
from logseeker.config import config_problems, get_settings
from logseeker.jobs import get_manager

st.set_page_config(
    page_title="LogSeeker",
    page_icon=":material/travel_explore:",
    layout="wide",
)

settings = get_settings()

allowed, reason = access.network_allowed(settings)
if not allowed:
    st.title(":material/block: Access denied")
    st.error(reason, icon=":material/lock:")
    st.stop()

# Instantiating the manager here means the worker pool exists before any page
# renders, so a page can always poll it.
manager = get_manager()

pages = [
    st.Page("app_pages/search.py", title="Search", icon=":material/search:", default=True),
    st.Page("app_pages/results.py", title="Results", icon=":material/inbox:"),
]
if settings.show_admin:
    pages.append(st.Page("app_pages/admin.py", title="Admin", icon=":material/tune:"))

page = st.navigation(pages)

with st.sidebar:
    st.markdown("### :material/travel_explore: LogSeeker")
    st.caption("Order log retrieval")

    st.text_input(
        "Your name",
        key="ls_requester_name",
        placeholder="optional",
        help="Not a login — just a label recorded next to your searches so the "
             "team can tell requests apart.",
    )

    st.caption(f"Searches run on the log host as **{settings.ssh_user or '?'}**")
    st.caption(f"`{settings.describe_target()}`")

    active = manager.active_count()
    if active:
        st.caption(f":material/bolt: {active} search(es) running")

problems = config_problems(settings)
if problems:
    st.error(
        "**LogSeeker is not fully configured — searches will fail.**\n\n"
        + "\n".join(f"- {p}" for p in problems)
        + "\n\nSee `.env.example`.",
        icon=":material/error:",
    )

if not settings.allowed_networks:
    st.warning(
        f"This app has no sign-in and no network restriction. Anyone who can reach "
        f"this URL can pull production order logs as **{settings.ssh_user or 'the configured account'}**. "
        "Set `LOGSEEKER_ALLOWED_NETWORKS`, or put it behind a VPN or reverse proxy, "
        "before exposing it beyond your own network.",
        icon=":material/lock_open:",
    )

page.run()
