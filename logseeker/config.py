"""Configuration.

The app runs on the machine of the one colleague who has access to the log host,
and it connects there as *him*. His login therefore comes from the environment
he launches it in, never from anything a visitor types.

Resolution order for every setting, most specific first:

1. a real environment variable, ``LOGSEEKER_<KEY>``
2. a ``.env`` file next to ``streamlit_app.py``
3. ``.streamlit/secrets.toml``
4. the built-in default

Environment deliberately beats ``secrets.toml``: the launch environment is what
the operator controls at start-up, and a stale secrets file left over from a
demo must not silently override it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = APP_ROOT / "data"
DOTENV_PATH = APP_ROOT / ".env"

# Where OpenSSH — and therefore paramiko's load_system_host_keys() — keeps host
# keys. When ssh_known_hosts is unset this is what actually gets used, so the
# config check has to look here before declaring the host key unpinned.
DEFAULT_KNOWN_HOSTS = Path.home() / ".ssh" / "known_hosts"


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    """Parse a minimal .env file. KEY=value, # comments, optional quotes."""
    values: dict[str, str] = {}
    if not DOTENV_PATH.exists():
        return values
    for raw in DOTENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key.upper()] = value
    return values


def _from_secrets(key: str):
    """Read ``key`` out of st.secrets without exploding outside Streamlit."""
    try:
        import streamlit as st

        # st.secrets raises if no secrets file exists at all.
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return None


def source_of(key: str) -> str:
    """Where a setting's value came from — surfaced on the Admin page."""
    env_key = f"LOGSEEKER_{key.upper()}"
    if env_key in os.environ:
        return "environment"
    if env_key in _dotenv():
        return ".env"
    if _from_secrets(key) is not None:
        return "secrets.toml"
    return "default"


def _get(key: str, default=None):
    env_key = f"LOGSEEKER_{key.upper()}"
    if env_key in os.environ:
        return os.environ[env_key]
    if env_key in _dotenv():
        return _dotenv()[env_key]
    value = _from_secrets(key)
    if value is not None:
        return value
    return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(_get(key, default))
    except (TypeError, ValueError):
        return default


def _get_bool(key: str, default: bool) -> bool:
    value = _get(key, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _get_list(key: str, default: list[str]) -> list[str]:
    value = _get(key, None)
    if value is None:
        return list(default)
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [part.strip() for part in str(value).split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    # --- where the logs live -------------------------------------------------
    # Kept as templates so a new instance or a renamed directory is a config
    # change, not a code change.
    log_dir_template: str = "/apps/k/algologs/ap/{instance}/logs"
    log_glob_template: str = "APA*_pingOrderStream_{date}*.out"
    instances: list[str] = field(default_factory=lambda: ["APIA", "APIE"])

    # --- how we reach it -----------------------------------------------------
    backend: str = "ssh"  # "ssh" | "local"
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    # repr=False so the password cannot ride along in a traceback or a log line
    # that happens to render the settings object. It is deliberately plain text
    # in .env; that is the one place it should be readable.
    ssh_password: str = field(default="", repr=False)
    ssh_key_path: str = ""
    ssh_key_passphrase: str = field(default="", repr=False)
    ssh_known_hosts: str = ""
    ssh_auto_add_host_key: bool = False
    ssh_connect_timeout: int = 20

    # --- guard rails ---------------------------------------------------------
    command_timeout: int = 900          # seconds before we kill a search
    max_files_per_search: int = 500     # protects against ARG_MAX / runaway globs
    # Order-stream logs run to a gigabyte a shard, so the file *count* is a poor
    # guard — 500 files can be half a terabyte. This is the real ceiling, checked
    # after listing and before a single byte is read.
    max_scan_bytes: int = 200 * 1024 * 1024 * 1024   # 200 GB
    # How much to grep per round trip. Each batch boundary is a progress update.
    scan_batch_bytes: int = 512 * 1024 * 1024        # 512 MB
    max_result_lines: int = 200_000     # enforced server side via `head -n`
    max_result_bytes: int = 64 * 1024 * 1024   # enforced client side while streaming
    max_days_per_search: int = 14       # a date range wider than this is almost always a mistake

    # --- concurrency ---------------------------------------------------------
    workers: int = 3                    # simultaneous greps against the log host
    job_history: int = 200              # jobs kept in memory
    cache_ttl_hours: int = 12           # identical search served from disk within this window

    # --- storage -------------------------------------------------------------
    data_dir: Path = DEFAULT_DATA_DIR
    # Total budget for cached results. Oldest searches are evicted past this,
    # so a busy week of big greps cannot quietly fill the operator's disk.
    max_disk_bytes: int = 5 * 1024 * 1024 * 1024   # 5 GB
    # Results larger than this get a two-step download and a gzip option rather
    # than being pulled into memory on every rerun.
    inline_download_bytes: int = 2 * 1024 * 1024   # 2 MB

    # --- who visitors reach the app as ---------------------------------------
    # There is no sign-in. Everyone who can open the URL searches as `ssh_user`.
    # The only access control is therefore at the network layer.
    allowed_networks: list[str] = field(default_factory=list)  # CIDRs; empty = anyone
    show_admin: bool = True

    @property
    def results_dir(self) -> Path:
        return self.data_dir / "results"

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "audit.jsonl"

    def log_dir(self, instance: str) -> str:
        return self.log_dir_template.format(instance=instance)

    def log_glob(self, instance: str, date: str) -> str:
        return self.log_glob_template.format(instance=instance, date=date)

    def describe_target(self) -> str:
        if self.backend == "local":
            return "local filesystem"
        user = self.ssh_user or "?"
        host = self.ssh_host or "?"
        return f"{user}@{host}:{self.ssh_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(_get("data_dir", str(DEFAULT_DATA_DIR)))
    settings = Settings(
        log_dir_template=_get("log_dir_template", "/apps/k/algologs/ap/{instance}/logs"),
        log_glob_template=_get("log_glob_template", "APA*_pingOrderStream_{date}*.out"),
        instances=_get_list("instances", ["APIA", "APIE"]),
        backend=str(_get("backend", "ssh")).lower(),
        ssh_host=_get("ssh_host", ""),
        ssh_port=_get_int("ssh_port", 22),
        ssh_user=_get("ssh_user", ""),
        ssh_password=_get("ssh_password", ""),
        ssh_key_path=_get("ssh_key_path", ""),
        ssh_key_passphrase=_get("ssh_key_passphrase", ""),
        ssh_known_hosts=_get("ssh_known_hosts", ""),
        ssh_auto_add_host_key=_get_bool("ssh_auto_add_host_key", False),
        ssh_connect_timeout=_get_int("ssh_connect_timeout", 20),
        command_timeout=_get_int("command_timeout", 900),
        max_files_per_search=_get_int("max_files_per_search", 500),
        max_scan_bytes=_get_int("max_scan_bytes", 200 * 1024 * 1024 * 1024),
        scan_batch_bytes=_get_int("scan_batch_bytes", 512 * 1024 * 1024),
        max_result_lines=_get_int("max_result_lines", 200_000),
        max_result_bytes=_get_int("max_result_bytes", 64 * 1024 * 1024),
        max_days_per_search=_get_int("max_days_per_search", 14),
        workers=_get_int("workers", 3),
        job_history=_get_int("job_history", 200),
        cache_ttl_hours=_get_int("cache_ttl_hours", 12),
        data_dir=data_dir,
        max_disk_bytes=_get_int("max_disk_bytes", 5 * 1024 * 1024 * 1024),
        inline_download_bytes=_get_int("inline_download_bytes", 2 * 1024 * 1024),
        allowed_networks=_get_list("allowed_networks", []),
        show_admin=_get_bool("show_admin", True),
    )
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    settings.audit_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


def config_problems(settings: Settings) -> list[str]:
    """Human-readable list of things that will stop a search from working."""
    problems: list[str] = []
    if settings.backend not in {"ssh", "local"}:
        problems.append(f"backend must be 'ssh' or 'local', got {settings.backend!r}")
    if settings.backend == "ssh":
        if not settings.ssh_host:
            problems.append("ssh_host is not set")
        if not settings.ssh_user:
            problems.append("ssh_user is not set")
        if not settings.ssh_password and not settings.ssh_key_path:
            problems.append(
                "neither ssh_password nor ssh_key_path is set — put the log-host "
                "password in LOGSEEKER_SSH_PASSWORD"
            )
        if settings.ssh_key_path and not Path(settings.ssh_key_path).expanduser().exists():
            problems.append(f"ssh_key_path does not exist: {settings.ssh_key_path}")
        # An unset ssh_known_hosts is not a failure: paramiko falls back to the
        # default OpenSSH file, which one `ssh user@host` populates. Only say the
        # host key is unpinned when there is genuinely nothing to pin against.
        if (
            not settings.ssh_known_hosts
            and not settings.ssh_auto_add_host_key
            and not DEFAULT_KNOWN_HOSTS.exists()
        ):
            problems.append(
                f"the log host's key is not pinned: {DEFAULT_KNOWN_HOSTS} does not exist "
                "and ssh_known_hosts is unset, so the connection will be refused. "
                "Run `ssh <user>@<host>` once and accept the fingerprint."
            )
    if not settings.instances:
        problems.append("no instances configured")
    if "{instance}" not in settings.log_dir_template:
        problems.append("log_dir_template does not contain {instance}")
    if "{date}" not in settings.log_glob_template:
        problems.append("log_glob_template does not contain {date}")
    return problems
