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


# --------------------------------------------------------------------------- #
# Password auth — the deployment this is actually used in.
#
# There are no admin rights on the log host, so a key cannot be installed there.
# The operator's ordinary password goes in .env and the app signs in with it,
# exactly as a person would at a prompt. These tests pin that path down so a
# later change cannot quietly make a key mandatory again.
# --------------------------------------------------------------------------- #

def _password_settings(**kw) -> Settings:
    base = dict(
        backend="ssh",
        ssh_host="loghost.example.internal",
        ssh_user="youruser",
        ssh_password="s3cret",
    )
    base.update(kw)
    return Settings(**base)


def test_a_password_alone_is_a_complete_configuration(monkeypatch, tmp_path):
    """No key, no explicit known_hosts — just host, user and password."""
    known = tmp_path / "known_hosts"
    known.write_text("loghost.example.internal ssh-ed25519 AAAA\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_KNOWN_HOSTS", known)

    assert config_problems_for(_password_settings()) == []


def test_missing_host_key_is_reported_only_when_nothing_can_pin_it(monkeypatch, tmp_path):
    """The old message claimed the connection 'will be refused' whenever
    ssh_known_hosts was unset. That was wrong: paramiko falls back to the
    default OpenSSH file, so an operator who has ever run `ssh host` is fine.
    Warn only when that file genuinely does not exist."""
    monkeypatch.setattr(config_mod, "DEFAULT_KNOWN_HOSTS", tmp_path / "absent")
    problems = config_problems_for(_password_settings())
    assert any("not pinned" in p for p in problems)

    present = tmp_path / "known_hosts"
    present.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_KNOWN_HOSTS", present)
    assert not any("not pinned" in p for p in config_problems_for(_password_settings()))


def test_a_password_is_not_required_to_also_carry_a_key(monkeypatch, tmp_path):
    """A key path that does not exist is still an error; a password-only
    configuration must not trip that check."""
    known = tmp_path / "known_hosts"
    known.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_KNOWN_HOSTS", known)

    assert not any("ssh_key_path" in p for p in config_problems_for(_password_settings()))
    problems = config_problems_for(
        _password_settings(ssh_key_path=str(tmp_path / "nope"))
    )
    assert any("ssh_key_path does not exist" in p for p in problems)


def test_the_password_never_appears_in_the_settings_repr():
    """It is plain text in .env on purpose, but a traceback or a stray log line
    that renders Settings must not carry it along."""
    settings = _password_settings(ssh_key_passphrase="also-secret")
    text = repr(settings)
    assert "s3cret" not in text
    assert "also-secret" not in text
    assert "youruser" in text          # non-secret fields still shown


def test_both_auth_methods_are_supported_and_the_key_is_reported_as_winning(monkeypatch, tmp_path):
    """Two supported routes: password (step 3A) and key (step 3B).

    paramiko tries keys before passwords, so a configuration carrying both uses
    the key and keeps the password as a fallback. That is what makes switching
    safe, and describe_auth() is what the operator checks to confirm the switch
    actually took — a key that was never installed on the host otherwise looks
    identical to one that works.
    """
    key = tmp_path / "logseeker_ed25519"
    key.write_text("not-a-real-key\n", encoding="utf-8")
    known = tmp_path / "known_hosts"
    known.write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "DEFAULT_KNOWN_HOSTS", known)

    password_only = _password_settings()
    assert password_only.describe_auth() == "password"
    assert config_problems_for(password_only) == []

    key_only = _password_settings(ssh_password="", ssh_key_path=str(key))
    assert key_only.describe_auth() == "SSH key"
    assert config_problems_for(key_only) == []

    both = _password_settings(ssh_key_path=str(key))
    assert both.describe_auth() == "SSH key, with the password as fallback"
    assert config_problems_for(both) == []

    neither = _password_settings(ssh_password="")
    assert neither.describe_auth() == "none configured"
    assert any("ssh_password" in p for p in config_problems_for(neither))


def test_describe_auth_never_leaks_the_credential(tmp_path):
    key = tmp_path / "k"
    key.write_text("x", encoding="utf-8")
    for settings in (
        _password_settings(),
        _password_settings(ssh_key_path=str(key)),
    ):
        assert "s3cret" not in settings.describe_auth()


def test_local_backend_reports_auth_as_not_applicable():
    assert "local" in Settings(backend="local").describe_auth()


def test_password_survives_awkward_characters_in_dotenv(monkeypatch, tmp_path):
    """Real passwords contain punctuation. Anything the parser mangles would
    show up as an unexplained 'Authentication failed'."""
    cases = {
        "hash": ("p@ss#1", "p@ss#1"),
        "equals": ("a=b=c", "a=b=c"),
        "dollar": ("p$ss", "p$ss"),
        "backslash": (r"dom\user", r"dom\user"),
        "interior spaces": ("p ss word", "p ss word"),
        "quoted trailing space": ('"abc   "', "abc   "),
    }
    for label, (written, expected) in cases.items():
        env = tmp_path / f"{label}.env"
        env.write_text(f"LOGSEEKER_SSH_PASSWORD={written}\n", encoding="utf-8")
        monkeypatch.setattr(config_mod, "DOTENV_PATH", env)
        config_mod._dotenv.cache_clear()
        assert config_mod._dotenv()["LOGSEEKER_SSH_PASSWORD"] == expected, label
    config_mod._dotenv.cache_clear()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
