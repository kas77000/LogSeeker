"""Command execution against the log host.

Two backends:

* ``SSHBackend``  — paramiko, one connection per job. Connections are *not*
  pooled on purpose: a paramiko ``Transport`` is not safe to share across
  worker threads, and a fresh handshake costs a fraction of a second against a
  grep that runs for minutes.
* ``LocalBackend`` — subprocess, for when the app is deployed on the log host
  itself and no hop is needed.

Both stream stdout to a caller-supplied sink so a multi-gigabyte grep never has
to be held in memory, and both honour a cancel event and a hard byte cap.
"""

from __future__ import annotations

import io
import os
import select
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .config import Settings

Sink = Callable[[bytes], None]


@dataclass
class CommandResult:
    exit_status: int | None
    stderr: str
    bytes_read: int
    duration_s: float
    truncated: bool = False   # byte cap hit
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_status in (0, 1) and not self.timed_out and not self.cancelled
        # grep exit 1 == "no match", which is a legitimate outcome, not an error.


class RemoteError(RuntimeError):
    """Raised when we cannot reach the log host at all."""


class Backend(Protocol):
    def run(
        self,
        command: str,
        *,
        sink: Sink,
        timeout: int,
        max_bytes: int,
        cancel: threading.Event | None = None,
    ) -> CommandResult: ...

    def session(self): ...

    def check(self) -> str: ...


# --------------------------------------------------------------------------- #
# SSH
# --------------------------------------------------------------------------- #

class SSHBackend:
    def __init__(self, settings: Settings):
        self.settings = settings

    # -- connection --------------------------------------------------------- #

    def _connect(self):
        try:
            import paramiko
        except ImportError as exc:  # pragma: no cover
            raise RemoteError("paramiko is not installed — run `pip install -r requirements.txt`") from exc

        s = self.settings
        client = paramiko.SSHClient()

        if s.ssh_known_hosts:
            known = Path(s.ssh_known_hosts).expanduser()
            if not known.exists():
                raise RemoteError(f"known_hosts file not found: {known}")
            client.load_host_keys(str(known))
        else:
            try:
                client.load_system_host_keys()
            except Exception:
                pass

        if s.ssh_auto_add_host_key:
            # Convenience for the first run only. Leaving this on in production
            # means the app will happily talk to an impostor host.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        kwargs = {
            "hostname": s.ssh_host,
            "port": s.ssh_port,
            "username": s.ssh_user,
            "timeout": s.ssh_connect_timeout,
            "banner_timeout": s.ssh_connect_timeout,
            "auth_timeout": s.ssh_connect_timeout,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if s.ssh_key_path:
            kwargs["key_filename"] = str(Path(s.ssh_key_path).expanduser())
            if s.ssh_key_passphrase:
                kwargs["passphrase"] = s.ssh_key_passphrase
        if s.ssh_password:
            kwargs["password"] = s.ssh_password

        try:
            client.connect(**kwargs)
        except Exception as exc:
            raise RemoteError(f"SSH connection to {s.describe_target()} failed: {exc}") from exc

        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(30)
        return client

    # -- execution ---------------------------------------------------------- #

    @contextmanager
    def session(self):
        """One connection, many commands.

        A gigabyte-scale search is split into batches so it can report progress.
        Reconnecting for each batch would add an SSH handshake per 512 MB for no
        reason, so the job holds a session open for its whole run.
        """
        client = self._connect()
        try:
            yield _SSHSession(client)
        finally:
            try:
                client.close()
            except Exception:
                pass

    def run(
        self,
        command: str,
        *,
        sink: Sink,
        timeout: int,
        max_bytes: int,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        with self.session() as sess:
            return sess.run(command, sink=sink, timeout=timeout,
                            max_bytes=max_bytes, cancel=cancel)

    def check(self) -> str:
        """Connect, run a trivial command, report what we found. Used by /Admin."""
        out = io.BytesIO()
        result = self.run("uname -a; echo; command -v ksh grep du",
                          sink=out.write, timeout=30, max_bytes=8192)
        if not result.ok:
            raise RemoteError(result.stderr or "connection check failed")
        return out.getvalue().decode("utf-8", "replace").strip()


class _SSHSession:
    """Runs commands over an already-open paramiko client."""

    def __init__(self, client):
        self._client = client

    def run(
        self,
        command: str,
        *,
        sink: Sink,
        timeout: int,
        max_bytes: int,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        client = self._client
        stderr_buf = io.BytesIO()
        total = 0
        truncated = timed_out = cancelled = False

        transport = client.get_transport()
        if transport is None:
            raise RemoteError("SSH transport closed unexpectedly")
        chan = transport.open_session()
        chan.settimeout(1.0)
        chan.exec_command(command)

        try:
            deadline = started + timeout
            while True:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                if time.monotonic() > deadline:
                    timed_out = True
                    break

                # Channel implements fileno(), so select works on it directly.
                try:
                    select.select([chan], [], [], 0.2)
                except Exception:
                    time.sleep(0.05)

                moved = False
                while chan.recv_ready():
                    chunk = chan.recv(65536)
                    if not chunk:
                        break
                    moved = True
                    remaining = max_bytes - total
                    if remaining <= 0:
                        truncated = True
                        break
                    if len(chunk) > remaining:
                        chunk = chunk[:remaining]
                        truncated = True
                    sink(chunk)
                    total += len(chunk)
                    if truncated:
                        break
                if truncated:
                    break

                while chan.recv_stderr_ready():
                    err = chan.recv_stderr(65536)
                    if not err:
                        break
                    moved = True
                    if stderr_buf.tell() < 64 * 1024:
                        stderr_buf.write(err)

                if not moved and chan.exit_status_ready():
                    if not chan.recv_ready() and not chan.recv_stderr_ready():
                        break

            exit_status = chan.exit_status if chan.exit_status_ready() else None
        finally:
            # Close the channel, never the client — the session outlives this
            # command and later batches still need the connection.
            try:
                chan.close()
            except Exception:
                pass

        return CommandResult(
            exit_status=exit_status,
            stderr=stderr_buf.getvalue().decode("utf-8", "replace").strip(),
            bytes_read=total,
            duration_s=time.monotonic() - started,
            truncated=truncated,
            timed_out=timed_out,
            cancelled=cancelled,
        )


# --------------------------------------------------------------------------- #
# Local
# --------------------------------------------------------------------------- #

class LocalBackend:
    """For deployments that sit on the log host itself.

    The commands built by ``search.py`` are POSIX shell — globs, ``||``, pipes.
    On Windows that means we must go through bash rather than cmd.exe, which is
    what makes ``tools/make_demo.py`` usable on a developer laptop.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def session(self):
        """Matches the SSH backend's interface; a subprocess needs no setup."""
        yield self

    @staticmethod
    def _shell() -> list[str] | None:
        """Argv prefix for running a POSIX shell command, or None to use shell=True."""
        if os.name != "nt":
            return None
        import shutil as _shutil

        for candidate in ("bash", "sh"):
            found = _shutil.which(candidate)
            if found:
                return [found, "-c"]
        return None

    def run(
        self,
        command: str,
        *,
        sink: Sink,
        timeout: int,
        max_bytes: int,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        started = time.monotonic()
        total = 0
        truncated = timed_out = cancelled = False

        shell_prefix = self._shell()
        if os.name == "nt" and shell_prefix is None:
            raise RemoteError(
                "The local backend needs a POSIX shell. No bash/sh was found on PATH — "
                "install Git for Windows or run the app from WSL, or use backend = 'ssh'."
            )

        popen_kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if shell_prefix is None:
            proc = subprocess.Popen(command, shell=True, **popen_kwargs)
        else:
            proc = subprocess.Popen([*shell_prefix, command], **popen_kwargs)
        stderr_chunks: list[bytes] = []
        deadline = started + timeout

        def drain_stderr():
            assert proc.stderr is not None
            for line in proc.stderr:
                if sum(len(c) for c in stderr_chunks) < 64 * 1024:
                    stderr_chunks.append(line)

        err_thread = threading.Thread(target=drain_stderr, daemon=True)
        err_thread.start()

        assert proc.stdout is not None
        while True:
            if cancel is not None and cancel.is_set():
                cancelled = True
                proc.kill()
                break
            if time.monotonic() > deadline:
                timed_out = True
                proc.kill()
                break
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            remaining = max_bytes - total
            if remaining <= 0:
                truncated = True
                proc.kill()
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
                truncated = True
            sink(chunk)
            total += len(chunk)
            if truncated:
                proc.kill()
                break

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err_thread.join(timeout=2)

        return CommandResult(
            exit_status=proc.returncode,
            stderr=b"".join(stderr_chunks).decode("utf-8", "replace").strip(),
            bytes_read=total,
            duration_s=time.monotonic() - started,
            truncated=truncated,
            timed_out=timed_out,
            cancelled=cancelled,
        )

    def check(self) -> str:
        out = io.BytesIO()
        self.run("uname -a 2>/dev/null || ver", sink=out.write, timeout=15, max_bytes=8192)
        return out.getvalue().decode("utf-8", "replace").strip() or "local backend reachable"


def get_backend(settings: Settings) -> Backend:
    if settings.backend == "local":
        return LocalBackend(settings)
    return SSHBackend(settings)
