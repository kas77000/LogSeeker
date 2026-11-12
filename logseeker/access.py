"""Who the app talks to, and who it talks *as*.

There is no sign-in. The colleague who has access to the log host launches the
app on his machine with his credentials in the environment; everybody else opens
the URL and searches as him. That is the deployment the team asked for, and it
has one consequence worth stating plainly: **whoever can reach the URL can read
production order logs.** The only thing standing between the internet and those
logs is the network, so ``allowed_networks`` exists to narrow that down and the
UI says out loud whose account is being used.

For the audit trail this leaves no authenticated identity, so we record the best
available substitute: the client IP, plus an optional name the requester can
type. That is a label, not a credential — treat it as such.
"""

from __future__ import annotations

import ipaddress

import streamlit as st

from .config import Settings


def _connection_ip() -> str:
    """The address of the socket the browser connected from.

    st.context.ip_address deliberately returns None for 127.0.0.1 and ::1
    (streamlit/runtime/context.py), which is indistinguishable from "unknown"
    and made the fail-closed check below lock the operator out of his own
    machine. The connection's address is still known underneath; read it there.
    It comes from the TCP peer, not a header, so a remote client cannot claim
    to be localhost.
    """
    try:
        from streamlit.runtime.context import _get_client_context

        cc = _get_client_context()
        return (getattr(cc, "remote_ip", None) or "") if cc is not None else ""
    except Exception:
        return ""


def client_ip() -> str:
    """Best-effort client address. Empty string when Streamlit cannot tell us."""
    try:
        ctx = getattr(st, "context", None)
        ip = getattr(ctx, "ip_address", None) if ctx is not None else None
    except Exception:
        ip = None
    return ip or _connection_ip()


def _is_loopback(addr) -> bool:
    mapped = getattr(addr, "ipv4_mapped", None)     # ::ffff:127.0.0.1
    return addr.is_loopback or bool(mapped and mapped.is_loopback)


def network_allowed(settings: Settings) -> tuple[bool, str]:
    """Check the client against ``allowed_networks``. Returns (allowed, reason)."""
    if not settings.allowed_networks:
        return True, ""

    ip = client_ip()
    if not ip:
        # Fail closed: an allowlist was configured, so an unknown client is not
        # something to wave through.
        return False, (
            "An allowed_networks list is configured but the client address could "
            "not be determined, so access was refused."
        )

    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, f"Client address {ip!r} is not a valid IP."

    # The browser on the operator's own machine. Anyone there can already read
    # .env, so refusing them protects nothing. Behind a reverse proxy on this
    # same machine every visitor looks like loopback; set allow_localhost=false.
    if settings.allow_localhost and _is_loopback(addr):
        return True, ""

    for cidr in settings.allowed_networks:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True, ""
        except ValueError:
            continue
    return False, f"{ip} is not in any allowed network."


def requester_label(settings: Settings) -> str:
    """The label recorded against a search in the audit log."""
    name = (st.session_state.get("ls_requester_name") or "").strip()
    ip = client_ip()
    if name and ip:
        return f"{name} ({ip})"
    if name:
        return name
    if ip:
        return ip
    return "unknown"
