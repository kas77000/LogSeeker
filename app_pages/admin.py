"""Admin page — connection diagnostics, audit trail, cache housekeeping."""

import pandas as pd
import streamlit as st

from logseeker import audit, kdb, store, ui
from logseeker.config import config_problems, get_settings, source_of
from logseeker.jobs import get_manager
from logseeker.remote import RemoteError, get_backend
from logseeker.search import (
    SearchRequest,
    build_grep_command,
    build_list_command,
    parse_file_list,
)

settings = get_settings()
manager = get_manager()

st.title("Admin")

tab_health, tab_audit, tab_storage, tab_config = st.tabs(
    ["Health", "Audit log", "Storage", "Configuration"]
)

# --------------------------------------------------------------------------- #

with tab_health:
    problems = config_problems(settings)
    if problems:
        st.error("\n".join(f"- {p}" for p in problems), icon=":material/error:")
    else:
        st.success("Configuration looks complete.", icon=":material/check_circle:")

    stats = st.columns(3)
    stats[0].metric("Workers", settings.workers)
    stats[1].metric("Running now", manager.active_count())
    stats[2].metric("Jobs in memory", len(manager.recent(settings.job_history)))

    st.subheader("Connection test")
    st.caption(f"Target: `{settings.describe_target()}`")
    # Keys beat passwords in paramiko, so when both are configured the password
    # is only a fallback. Say which one is live — otherwise a key that was never
    # installed looks identical to one that works, the password quietly carrying
    # the app while everyone believes they have moved off it.
    st.caption(f"Signing in with: **{settings.describe_auth()}**")

    if st.button("Test connection", icon=":material/network_check:", type="primary"):
        with st.spinner("Connecting…"):
            try:
                st.code(get_backend(settings).check(), language="text")
                st.success("Reachable.", icon=":material/check_circle:")
            except RemoteError as exc:
                st.error(str(exc), icon=":material/error:")
            except Exception as exc:  # noqa: BLE001
                st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")

    st.subheader("kdb")
    if not settings.kdb_enabled:
        st.caption("Not configured — set `LOGSEEKER_KDB_RDB` and/or `LOGSEEKER_KDB_HDB` "
                   "to check each id_target against the `target` table.")
    else:
        st.caption(
            f"RDB (today): `{settings.kdb_rdb or 'not set'}` · "
            f"HDB (earlier dates): `{settings.kdb_hdb or 'not set'}` · "
            f"user: `{settings.kdb_user or 'none'}`"
        )
        kcols = st.columns([1, 1, 1], vertical_alignment="bottom")
        with kcols[0]:
            k_id = st.text_input("id_target", placeholder="540000001", key="adm_kdb_id")
        with kcols[1]:
            k_date = st.date_input("Date", key="adm_kdb_date", format="YYYY-MM-DD")
        with kcols[2]:
            k_run = st.button("Look up", icon=":material/database:")
        if st.button("Test kdb connection", icon=":material/network_check:"):
            with st.spinner("Connecting to kdb…"):
                try:
                    for s in kdb.ping(settings):
                        if s.error:
                            st.error(f"{s.source} `{s.endpoint}` — {s.error}",
                                     icon=":material/error:")
                        else:
                            st.success(f"{s.source} `{s.endpoint}` — {s.info}",
                                       icon=":material/check_circle:")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")
        if k_run:
            with st.spinner("Querying `target`…"):
                check = kdb.lookup_target(settings, k_id.strip(),
                                          [k_date.strftime("%Y%m%d")])
            {kdb.FOUND: st.success, kdb.NOT_FOUND: st.warning,
             kdb.ERROR: st.error}.get(check.status, st.info)(check.message)
            if check.rows:
                st.dataframe(check.rows, hide_index=True, use_container_width=True)

    st.subheader("Log directory probe")
    st.caption("Lists the log files that exist for one instance and date, without grepping.")
    probe = st.columns([1, 1, 1], vertical_alignment="bottom")
    with probe[0]:
        p_instance = st.selectbox("Instance", settings.instances)
    with probe[1]:
        p_date = st.text_input("Date (YYYYMMDD)", value="", placeholder="20260421")
    with probe[2]:
        run_probe = st.button("Probe", icon=":material/folder_open:")

    if run_probe:
        if not (p_date.isdigit() and len(p_date) == 8):
            st.error("Date must be 8 digits, YYYYMMDD.")
        else:
            import io

            req = SearchRequest(pattern="x", instances=[p_instance], dates=[p_date])
            buf = io.BytesIO()
            with st.spinner("Listing…"):
                try:
                    result = get_backend(settings).run(
                        build_list_command(req, settings),
                        sink=buf.write, timeout=60, max_bytes=1_000_000,
                    )
                    files, _ = parse_file_list(
                        buf.getvalue().decode(errors="replace"), settings
                    )
                    if files:
                        total = sum(f.size_bytes for f in files)
                        st.success(
                            f"{len(files)} file(s) found, {ui.human_bytes(total)} in total.",
                            icon=":material/check_circle:",
                        )
                        st.caption(
                            "This is roughly what a search over this instance and date "
                            "would have to read."
                        )
                        st.dataframe(
                            pd.DataFrame({
                                "file": [f.path for f in files],
                                "size": [ui.human_bytes(f.size_bytes) for f in files],
                            }),
                            use_container_width=True, hide_index=True,
                        )
                    else:
                        st.warning(
                            "No files matched. Either that date has no logs, or the "
                            "`log_dir_template` / `log_glob_template` in your config is wrong.",
                            icon=":material/warning:",
                        )
                    if result.stderr:
                        st.caption("stderr:")
                        st.code(result.stderr, language="text")
                except Exception as exc:  # noqa: BLE001
                    st.error(f"{type(exc).__name__}: {exc}", icon=":material/error:")

# --------------------------------------------------------------------------- #

with tab_audit:
    st.caption(f"`{settings.audit_path}`")
    limit = st.slider("Entries", 25, 1000, 200, step=25)
    entries = audit.tail(settings.audit_path, limit=limit)

    if not entries:
        st.info("No audit entries yet.", icon=":material/history:")
    else:
        df = pd.DataFrame(entries)
        events = sorted(df["event"].dropna().unique().tolist())
        chosen = st.multiselect("Event type", events, default=events)
        df = df[df["event"].isin(chosen)]

        st.dataframe(df, use_container_width=True, hide_index=True)
        st.download_button(
            "Download audit log",
            data=settings.audit_path.read_bytes(),
            file_name="logseeker-audit.jsonl",
            mime="application/x-ndjson",
            icon=":material/download:",
        )

# --------------------------------------------------------------------------- #

with tab_storage:
    count, total = store.disk_usage(settings)
    cols = st.columns(3)
    cols[0].metric("Result files", f"{count:,}")
    cols[1].metric("Disk used", ui.human_bytes(total))
    cols[2].metric("Cache TTL", f"{settings.cache_ttl_hours}h")

    st.caption(f"`{settings.results_dir}`")

    if st.button("Purge expired results", icon=":material/delete_sweep:"):
        removed = store.purge_expired(settings)
        st.success(f"Removed {removed} expired result folder(s).",
                   icon=":material/check_circle:")

    st.caption(
        "Results older than the TTL are only removed when this button is pressed or "
        "when a scheduled job calls `store.purge_expired`. Nothing is deleted "
        "automatically on a timer."
    )

# --------------------------------------------------------------------------- #

with tab_config:
    st.caption("Effective settings. Secrets are masked.")
    masked = {
        "backend": settings.backend,
        "ssh_host": settings.ssh_host,
        "ssh_port": settings.ssh_port,
        "ssh_user": settings.ssh_user,
        "ssh_key_path": settings.ssh_key_path,
        "ssh_password": "••••" if settings.ssh_password else "",
        "ssh_known_hosts": settings.ssh_known_hosts,
        "ssh_auto_add_host_key": settings.ssh_auto_add_host_key,
        "kdb_rdb": settings.kdb_rdb,
        "kdb_hdb": settings.kdb_hdb,
        "kdb_user": settings.kdb_user,
        "kdb_password": "••••" if settings.kdb_password else "",
        "kdb_timeout": settings.kdb_timeout,
        "log_dir_template": settings.log_dir_template,
        "log_glob_template": settings.log_glob_template,
        "instances": ", ".join(settings.instances),
        "workers": settings.workers,
        "command_timeout": settings.command_timeout,
        "max_files_per_search": settings.max_files_per_search,
        "max_scan_bytes": ui.human_bytes(settings.max_scan_bytes),
        "scan_batch_bytes": ui.human_bytes(settings.scan_batch_bytes),
        "max_result_lines": settings.max_result_lines,
        "max_result_bytes": settings.max_result_bytes,
        "max_days_per_search": settings.max_days_per_search,
        "cache_ttl_hours": settings.cache_ttl_hours,
        "data_dir": str(settings.data_dir),
        "allowed_networks": ", ".join(settings.allowed_networks) or "(anyone can reach this app)",
        "show_admin": settings.show_admin,
    }
    st.dataframe(
        pd.DataFrame({
            "setting": list(masked),
            "value": [str(v) for v in masked.values()],
            "source": [source_of(k) for k in masked],
        }),
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "`source` shows which layer won: environment > .env > secrets.toml > default."
    )

    st.subheader("Generated commands")
    st.caption("Exactly what gets sent to the log host for a sample request.")
    sample = SearchRequest(
        pattern="540000001",
        instances=settings.instances[:1] or ["APIA"],
        dates=["20260421"],
    )
    st.code(build_list_command(sample, settings), language="bash")
    st.code(
        build_grep_command(
            [f"{settings.log_dir(sample.instances[0])}/APA1_pingOrderStream_20260421_1.out"],
            sample,
            settings,
        ),
        language="bash",
    )
