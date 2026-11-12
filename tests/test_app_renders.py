"""Render the pages headlessly with Streamlit's AppTest and assert no exceptions.

Catches the class of mistake unit tests miss: a wrong widget kwarg, a bad icon
name, a page referencing session state that was never set.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _isolated_demo_config(tmp_path_factory):
    """Run every render against fake logs, never the operator's real setup.

    AppTest runs streamlit_app.py in this process, so without this the pages
    would read the real .env — and its network allowlist, service-account login
    and log host — exactly as a live launch does. Environment variables beat
    .env and secrets.toml, so setting them here pins the configuration.
    """
    import logseeker.config as config_mod
    import logseeker.jobs as jobs_mod
    import tools.make_demo as demo

    root = tmp_path_factory.mktemp("render")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(demo, "DEMO_LOGS", root / "logs")
        demo.make_logs()
        base = (root / "logs" / "apps" / "k" / "algologs" / "ap").as_posix()

        mp.setattr(config_mod, "DOTENV_PATH", root / "no.env")
        for key, value in {
            "BACKEND": "local",
            "LOG_DIR_TEMPLATE": f"{base}/{{instance}}/logs",
            "LOG_GLOB_TEMPLATE": "APA*_pingOrderStream_{date}*.out",
            "INSTANCES": ",".join(demo.INSTANCES),
            "ALLOWED_NETWORKS": "",
            "SHOW_ADMIN": "true",
            "DATA_DIR": str(root / "data"),
            "WORKERS": "2",
            "SSH_HOST": "", "SSH_USER": "", "SSH_PASSWORD": "", "SSH_KEY_PATH": "",
            "KDB_RDB": "", "KDB_HDB": "",
        }.items():
            mp.setenv(f"LOGSEEKER_{key}", value)

        config_mod._dotenv.cache_clear()
        config_mod.get_settings.cache_clear()
        mp.setattr(jobs_mod, "_manager", None)
        yield
        config_mod._dotenv.cache_clear()
        config_mod.get_settings.cache_clear()


def _app(**session) -> AppTest:
    at = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=30)
    for key, value in session.items():
        at.session_state[key] = value
    return at


def _no_exceptions(at: AppTest) -> None:
    assert not at.exception, "\n\n".join(str(e.value) for e in at.exception)


def _order_id_input(at: AppTest):
    """The order id box on the search form.

    text_input[0] is the sidebar "Your name" field, so index by label rather
    than position — otherwise this test silently drives the wrong widget.
    """
    for widget in at.text_input:
        if widget.label == "Order id":
            return widget
    raise AssertionError(f"no 'Order id' input; found {[w.label for w in at.text_input]}")


def test_search_page_renders_with_no_sign_in():
    """There is no login gate — the search page must come up immediately."""
    at = _app().run()
    _no_exceptions(at)
    assert any("Search order logs" in h.value for h in at.title)
    assert _order_id_input(at) is not None


def test_no_sign_in_warning_is_shown_without_a_network_allowlist():
    at = _app().run()
    _no_exceptions(at)
    assert any("no sign-in" in w.value for w in at.warning), \
        "the open-access warning must be visible when allowed_networks is empty"


def test_results_page_renders():
    at = _app()
    at.run()
    _no_exceptions(at)
    at.switch_page("app_pages/results.py").run()
    _no_exceptions(at)
    assert any("Results" in h.value for h in at.title)


def test_admin_page_renders():
    at = _app()
    at.run()
    _no_exceptions(at)
    at.switch_page("app_pages/admin.py").run()
    _no_exceptions(at)
    assert any("Admin" in h.value for h in at.title)


def test_bad_order_id_shows_an_error_not_a_crash():
    at = _app().run()
    _no_exceptions(at)
    _order_id_input(at).set_value("540000001; rm -rf /")
    at.button[0].click().run()
    _no_exceptions(at)
    assert at.error, "expected a validation error to be shown"


def _submit(at, order_id: str):
    _order_id_input(at).set_value(order_id)
    [b for b in at.button if b.label == "Search"][0].click().run()
    return at


def test_submitting_a_valid_search_starts_a_job():
    at = _app(ls_requester_name="tester").run()
    _no_exceptions(at)
    _submit(at, "540000001")
    _no_exceptions(at)

    # AppTest.session_state is not a plain dict — no .get().
    assert "ls_my_jobs" in at.session_state, "expected a tracked job in session state"

    from logseeker.jobs import get_manager

    job = get_manager().get(at.session_state["ls_my_jobs"][0])
    assert job is not None
    assert job.request.pattern == "540000001"
    # The typed name is a label for the audit trail, not an authenticated user.
    assert "tester" in job.request.requested_by


def test_a_second_search_does_not_replace_the_first():
    """Submitting again must queue alongside, not drop the running search."""
    at = _app(ls_requester_name="tester").run()
    _no_exceptions(at)

    _submit(at, "540000001")
    _no_exceptions(at)
    first = at.session_state["ls_my_jobs"][0]

    _submit(at, "540000002")
    _no_exceptions(at)
    tracked = list(at.session_state["ls_my_jobs"])

    assert len(tracked) == 2, "both searches should be tracked"
    assert tracked[0] != first and first in tracked, "newest first, first one kept"

    from logseeker.jobs import get_manager

    manager = get_manager()
    patterns = {manager.get(j).request.pattern for j in tracked}
    assert patterns == {"540000001", "540000002"}


def test_single_date_is_the_default_and_range_adds_a_second_input():
    at = _app().run()
    _no_exceptions(at)

    labels = [d.label for d in at.date_input]
    assert labels == ["Date"], f"single date should be the default, got {labels}"

    at.session_state["ls_date_mode"] = "Date range"
    at.run()
    _no_exceptions(at)
    labels = [d.label for d in at.date_input]
    assert labels == ["From", "To"], labels


@pytest.mark.skipif(
    not (ROOT / "demo_logs").exists(),
    reason="needs demo logs — run `python tools/make_demo.py`",
)
def test_large_result_uses_the_two_step_download():
    """A result over the inline limit must not be attached directly.

    st.download_button holds its payload in memory, so a 60 MB file wired
    straight into the Results page would be re-read for every row on every
    rerun. The large path shows a Prepare button, then gzip + raw.
    """
    import time
    from datetime import date, timedelta

    from logseeker.config import get_settings
    from logseeker.jobs import get_manager
    from logseeker.search import MATCH_REGEX, SearchRequest, date_range, validate

    settings = get_settings()
    manager = get_manager()
    job = manager.submit(validate(SearchRequest(
        pattern="pingOrderStream",
        instances=settings.instances,
        dates=date_range(date.today() - timedelta(days=4), date.today()),
        match_mode=MATCH_REGEX,
        requested_by="large-download-test",
    ), settings))

    deadline = time.time() + 90
    while not job.status.is_terminal and time.time() < deadline:
        time.sleep(0.1)
    assert job.status.name == "DONE", job.error
    assert job.result_path.stat().st_size > settings.inline_download_bytes

    at = _app()
    at.run()
    at.switch_page("app_pages/results.py").run()
    _no_exceptions(at)

    prepare = [b for b in at.button if "Prepare download" in b.label]
    assert prepare, "large result should offer a Prepare button, not a direct download"

    prepare[0].click().run()
    _no_exceptions(at)
    labels = [d.label for d in at.get("download_button")]
    assert any(".gz" in l for l in labels), labels
    assert any("raw .txt" in l for l in labels), labels


@pytest.mark.parametrize("status, expect", [
    ("found", "trader: **jdoe**"),
    ("not_found", "No target"),
    ("error", "kdb lookup failed"),
])
def test_kdb_result_is_shown_on_the_card(status, expect):
    from logseeker import kdb
    from logseeker.jobs import Job, JobStatus, get_manager
    from logseeker.search import SearchRequest

    rows = [{"date": "2026-11-12", "trader": "jdoe"}] if status == "found" else []
    message = {"found": "Found.", "not_found": "No target with that id.",
               "error": "kdb lookup failed: refused"}[status]
    job = Job(id=f"kdb{status}", status=JobStatus.FAILED, error="no logs",
              request=SearchRequest(pattern="540000001", instances=["APIA"],
                                    dates=["20261112"]),
              kdb=kdb.KdbCheck(status, message, sources=[
                  kdb.SourceResult("HDB", "h:1", rows=rows)]))
    manager = get_manager()
    with manager._lock:
        manager._jobs[job.id] = job

    at = _app(ls_my_jobs=[job.id]).run()
    _no_exceptions(at)
    shown = [e.value for e in (*at.success, *at.warning, *at.error)]
    assert any(expect in v for v in shown), shown


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
