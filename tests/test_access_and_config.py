"""Tests for the no-sign-in access model and the config resolution order."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from logseeker import config as config_mod  # noqa: E402
from logseeker.access import network_allowed  # noqa: E402
from logseeker.config import Settings  # noqa: E402


def _settings(**kw) -> Settings:
    return Settings(**kw)


# --------------------------------------------------------------------------- #
# Network allowlist — the only access control the app itself provides
# --------------------------------------------------------------------------- #

def test_no_allowlist_lets_everyone_in():
    allowed, reason = network_allowed(_settings(allowed_networks=[]))
    assert allowed and reason == ""


def test_allowlist_fails_closed_when_the_client_ip_is_unknown():
    """Outside a real session there is no client IP.

    An allowlist was configured, so an unidentifiable client must be refused
    rather than waved through — the opposite default would silently disable the
    restriction whenever Streamlit could not report an address.
    """
    allowed, reason = network_allowed(_settings(allowed_networks=["10.0.0.0/8"]))
    assert not allowed
    assert "could not be determined" in reason


def test_allowlist_matches_and_rejects_addresses(monkeypatch):
    import logseeker.access as access

    monkeypatch.setattr(access, "client_ip", lambda: "10.4.5.6")
    assert network_allowed(_settings(allowed_networks=["10.0.0.0/8"]))[0]

    monkeypatch.setattr(access, "client_ip", lambda: "203.0.113.9")
    allowed, reason = network_allowed(_settings(allowed_networks=["10.0.0.0/8"]))
    assert not allowed and "not in any allowed network" in reason


def test_malformed_cidr_entries_do_not_grant_access(monkeypatch):
    import logseeker.access as access

    monkeypatch.setattr(access, "client_ip", lambda: "10.4.5.6")
    allowed, _ = network_allowed(_settings(allowed_networks=["not-a-cidr"]))
    assert not allowed


def test_a_valid_cidr_still_works_alongside_a_broken_one(monkeypatch):
    import logseeker.access as access

    monkeypatch.setattr(access, "client_ip", lambda: "10.4.5.6")
    assert network_allowed(_settings(allowed_networks=["oops", "10.0.0.0/8"]))[0]


# --------------------------------------------------------------------------- #
# Config resolution: environment > .env > secrets.toml > default
# --------------------------------------------------------------------------- #

def test_environment_beats_dotenv(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "DOTENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("LOGSEEKER_SSH_USER=from_dotenv\n", encoding="utf-8")
    config_mod._dotenv.cache_clear()

    monkeypatch.setenv("LOGSEEKER_SSH_USER", "from_env")
    assert config_mod._get("ssh_user") == "from_env"
    assert config_mod.source_of("ssh_user") == "environment"

    monkeypatch.delenv("LOGSEEKER_SSH_USER")
    assert config_mod._get("ssh_user") == "from_dotenv"
    assert config_mod.source_of("ssh_user") == ".env"
    config_mod._dotenv.cache_clear()


def test_dotenv_parsing_handles_comments_quotes_and_blanks(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "DOTENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text(
        "# a comment\n"
        "\n"
        'LOGSEEKER_SSH_HOST="loghost.example.internal"\n'
        "LOGSEEKER_SSH_USER = youruser \n"
        "LOGSEEKER_INSTANCES='APIA,APIE'\n"
        "not a pair\n",
        encoding="utf-8",
    )
    config_mod._dotenv.cache_clear()
    values = config_mod._dotenv()
    assert values["LOGSEEKER_SSH_HOST"] == "loghost.example.internal"
    assert values["LOGSEEKER_SSH_USER"] == "youruser"
    assert values["LOGSEEKER_INSTANCES"] == "APIA,APIE"
    assert "NOT A PAIR" not in values
    config_mod._dotenv.cache_clear()


def test_missing_dotenv_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "DOTENV_PATH", tmp_path / "nope.env")
    config_mod._dotenv.cache_clear()
    assert config_mod._dotenv() == {}
    config_mod._dotenv.cache_clear()


# --------------------------------------------------------------------------- #

def test_config_problems_flags_a_missing_ssh_identity():
    problems = config_problems_for(Settings(backend="ssh"))
    assert any("ssh_host" in p for p in problems)
    assert any("ssh_user" in p for p in problems)
    assert any("ssh_key_path" in p or "ssh_password" in p for p in problems)


def config_problems_for(settings: Settings):
    from logseeker.config import config_problems

    return config_problems(settings)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
