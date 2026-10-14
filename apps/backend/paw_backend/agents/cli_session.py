"""What every CLI session has: its directory, its bridge and its process
(Decision 0083, 6 and 8; Decision 0087, 4; #208).

* :class:`CliSessionSpec`: the session's directory (created by the backend,
  mode 0750; in production its group is ``paw-cli-bridge``, so only the CLI's
  account can enter it) holding the bridge's socket, the token file of the
  MCP relay, the MCP configuration and the CLI's own settings. It travels to
  the session adapter as ``SessionRequest.attachment``.
* :class:`CliLaunch` / :class:`CliLauncher` / :class:`CliProcess`: how a CLI is
  started and stopped. The credential is handed to the launcher as a
  :class:`~paw_backend.connections.Secret` and the environment variable it
  goes into; the launcher decides how it reaches the process (production: a
  file in the session's directory the unit reads; the tests: the variable).
  Stopping is SIGTERM, five seconds, then SIGKILL (Decision 0083, 8).
* :class:`SubprocessLauncher`: runs the CLI as a child process of the backend
  (development and the tests with the fake CLI). Production starts it in its
  own container through a systemd template unit (a later change); the CLI
  rung stays disabled at assembly until then.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from paw_backend.connections import Secret

logger = logging.getLogger(__name__)

#: SIGTERM, then this long, then SIGKILL (Decision 0083, 8).
STOP_GRACE_SECONDS = 5.0
SOCKET_NAME = "bridge.sock"
TOKEN_NAME = "bridge.token"
MCP_CONFIG_NAME = "mcp.json"
SETTINGS_NAME = "settings.json"
CONFIG_DIR_NAME = "config"


@dataclass(frozen=True, slots=True)
class CliSessionSpec:
    """The files of one session (all inside ``directory``)."""

    session_id: uuid.UUID
    directory: Path
    token: str = field(repr=False)
    relay_command: tuple[str, ...]

    @property
    def socket_path(self) -> Path:
        return self.directory / SOCKET_NAME

    @property
    def token_path(self) -> Path:
        return self.directory / TOKEN_NAME

    @property
    def mcp_config_path(self) -> Path:
        return self.directory / MCP_CONFIG_NAME

    @property
    def settings_path(self) -> Path:
        return self.directory / SETTINGS_NAME

    @property
    def config_dir(self) -> Path:
        return self.directory / CONFIG_DIR_NAME


def create_session(
    base: Path, relay_command: Sequence[str], *, settings: Mapping | None = None
) -> CliSessionSpec:
    """Make a session's directory under ``base`` (0750) with its token file
    (0640), the MCP configuration (the PAW relay as the only server) and the
    CLI's settings, and return its spec."""
    session_id = uuid.uuid4()
    # ``<base>/<uuid>``: the template unit's program finds it by the id alone.
    directory = Path(base) / str(session_id)
    directory.mkdir(mode=0o750)
    # Keep the parent's setgid (production: /run/paw-cli, group
    # paw-cli-bridge): the files below then belong to the CLI's group.
    setgid = Path(base).stat().st_mode & stat.S_ISGID
    os.chmod(directory, 0o750 | setgid)
    spec = CliSessionSpec(
        session_id, directory, secrets.token_urlsafe(32), tuple(relay_command)
    )
    _write(spec.token_path, spec.token + "\n", 0o640)
    config = {
        "mcpServers": {
            "paw": {
                "command": spec.relay_command[0],
                "args": [
                    *spec.relay_command[1:],
                    "--socket",
                    str(spec.socket_path),
                    "--token-file",
                    str(spec.token_path),
                ],
            }
        }
    }
    _write(spec.mcp_config_path, json.dumps(config), 0o640)
    _write(spec.settings_path, json.dumps(dict(settings or {})), 0o640)
    spec.config_dir.mkdir(mode=0o750)
    return spec


def remove_session(spec: CliSessionSpec) -> None:
    """Delete the session's directory (also after a cancellation)."""
    shutil.rmtree(spec.directory, ignore_errors=True)


_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}")


class UnitNotStoppedError(OSError):
    """A session's unit that ``systemctl stop`` could not stop (also while a
    failed or cancelled start was cleaned up): it may still run with the
    credential and has the session's directory mounted, so nobody may delete
    that directory."""


def _write_bytes(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _write(path: Path, text: str, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


@dataclass(frozen=True, slots=True)
class CliLaunch:
    """One CLI to start. ``env`` holds no secret; the credential goes into
    ``secret_env`` by the launcher's own means. ``stdin`` is the prompt."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    secret: Secret = field(repr=False)
    secret_env: str
    stdin: bytes = field(repr=False)
    spec: CliSessionSpec


class CliProcess(Protocol):
    stdout: asyncio.StreamReader

    async def wait(self) -> int: ...

    async def stop(self) -> None: ...


class CliLauncher(Protocol):
    async def start(self, launch: CliLaunch) -> CliProcess: ...


async def stop_through_cancellation(
    process: CliProcess,
) -> tuple[OSError | None, bool]:
    """``process.stop()`` run to its end even when the caller is cancelled
    meanwhile: only a stop that ended says whether the CLI (and its
    credential) is gone, so whether its session's files may go. Returns the
    stop's failure (``None``: stopped) and whether a cancellation came
    meanwhile, which the caller raises after acting on the failure."""
    stopping = asyncio.ensure_future(process.stop())
    cancelled = False
    while True:
        try:
            await asyncio.shield(stopping)
        except asyncio.CancelledError:
            if not stopping.done():
                cancelled = True
                continue
            if stopping.cancelled():
                return OSError("the stop was cancelled"), True
        except OSError as failed:
            return failed, cancelled
        return None, cancelled


class _Subprocess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        assert process.stdout is not None
        self.stdout = process.stdout

    async def wait(self) -> int:
        return await self._process.wait()

    async def stop(self) -> None:
        """SIGTERM to the process group, then SIGKILL after the grace; and
        SIGKILL to the group in every case at the end, so no child that
        ignored SIGTERM (it has the credential in its environment) outlives
        the leader."""
        process = self._process
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()), STOP_GRACE_SECONDS
                )
            except TimeoutError:
                pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


class _UnitProcess:
    """A session's unit, as the adapter sees a process."""

    def __init__(self, launcher: "SystemdLauncher", unit: str, stdout) -> None:
        self._launcher = launcher
        self._unit = unit
        self.stdout = stdout

    async def wait(self) -> int:
        """The unit's exit status once it is no longer active."""
        launcher = self._launcher
        for _ in range(int(launcher.settle_seconds / 0.2) + 1):
            state = await launcher.show(self._unit, "ActiveState")
            if state in ("inactive", "failed"):
                break
            await asyncio.sleep(0.2)
        status = await launcher.show(self._unit, "ExecMainStatus")
        try:
            return int(status)
        except ValueError:
            return -1

    async def stop(self) -> None:
        """``systemctl stop``: the unit sends SIGTERM, waits five seconds
        (``TimeoutStopSec``), then SIGKILL, and removes the container. A stop
        that systemd refuses is tried again, then raised (``OSError``): the
        caller must not believe a unit with the credential has ended."""
        await self._launcher.stop_unit(self._unit)


class SystemdLauncher:
    """Production's launcher (Decision 0083, 6): the session's files, then
    ``systemctl start paw-cli-session@<uuid>.service`` (polkit lets the
    backend's user start and stop exactly these units), whose program
    (``deploy/cli-session/paw_cli_session_run.py``, as ``paw-agent-cli``)
    runs the CLI's container and connects its stdout to the session's
    ``stream.sock``.

    The credential is the one line ``NAME=value`` of ``credential.env``
    (mode 0640, owner the backend, group ``paw-cli-bridge`` through the
    session directory's setgid parent: Decision 0088, which approved this in
    place of Decision 0087's 0400 owned by ``paw-agent-cli``; the backend
    cannot hand a file to another account): only the backend's and the
    CLI's accounts read it, and it goes with the session's directory."""

    def __init__(
        self,
        *,
        systemctl: Sequence[str] = ("systemctl",),
        unit_prefix: str = "paw-cli-session@",
        connect_seconds: float = 120.0,
        settle_seconds: float = 30.0,
        systemctl_seconds: float = 30.0,
    ) -> None:
        self._systemctl = tuple(systemctl)
        self._systemctl_seconds = systemctl_seconds
        self._prefix = unit_prefix
        self._connect = connect_seconds
        self.settle_seconds = settle_seconds

    async def systemctl(self, *arguments: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            *self._systemctl,
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        talking = asyncio.ensure_future(process.communicate())
        try:
            finished = await self._finish(process, talking)
        except asyncio.CancelledError:
            # A systemctl already running is let finish (a "start" it was
            # about to send must not come after the caller's "stop"), but
            # only within the bound: a hung one (D-Bus or polkit not
            # answering) is killed, and the cancellation goes on.
            with contextlib.suppress(Exception):
                await self._finish(process, talking)
            raise
        if not finished:
            logger.error("systemctl did not answer in time")
            return -1, ""
        out, _ = talking.result()
        return process.returncode or 0, out.decode("ascii", "replace").strip()

    async def _finish(self, process, talking: asyncio.Future) -> bool:
        """Wait for a systemctl at most ``systemctl_seconds``; past that, kill
        and reap it (``False``)."""
        done, _ = await asyncio.wait({talking}, timeout=self._systemctl_seconds)
        if talking in done:
            return True
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await talking
        return False

    async def stop_unit(self, unit: str, attempts: int = 3) -> None:
        for attempt in range(attempts):
            code, _ = await self.systemctl("stop", unit)
            if code == 0:
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(1.0)
        logger.error("A CLI session's unit could not be stopped")
        raise UnitNotStoppedError("the session's unit could not be stopped")

    async def show(self, unit: str, prop: str) -> str:
        _, out = await self.systemctl("show", "--property", prop, "--value", unit)
        return out

    async def start(self, launch: CliLaunch) -> CliProcess:
        spec = launch.spec
        value = launch.secret.reveal()
        if not _ENV_NAME.fullmatch(launch.secret_env) or any(
            c in value for c in "\n\r\x00"
        ):
            raise ValueError("the credential cannot be written as one line")
        _write(
            spec.directory / "launch.json",
            json.dumps({"argv": list(launch.argv), "env": dict(launch.env)}),
            0o640,
        )
        _write(
            spec.directory / "credential.env", f"{launch.secret_env}={value}\n", 0o640
        )
        _write_bytes(spec.directory / "prompt", launch.stdin, 0o640)
        connected: asyncio.Future = asyncio.get_running_loop().create_future()

        async def accept(reader, writer) -> None:
            if connected.done():
                writer.close()
                return
            connected.set_result(reader)
            await writer.wait_closed()

        server = await asyncio.start_unix_server(
            accept, path=str(spec.directory / "stream.sock"), limit=16 * 1024 * 1024
        )
        os.chmod(spec.directory / "stream.sock", 0o660)
        unit = f"{self._prefix}{spec.session_id}.service"
        process = _UnitProcess(self, unit, None)
        try:
            code, _ = await self.systemctl("start", "--no-block", unit)
            if code != 0:
                raise OSError("the session's unit did not start")
            reader = await asyncio.wait_for(connected, self._connect)
        except BaseException as error:
            # Whatever ended the start (a refusal, the wait, a cancellation
            # that came while systemctl ran): the unit may be queued or
            # running with the credential; stop it before passing on. A stop
            # that fails is raised instead (chained): nobody may take the
            # unit for stopped and delete its directory under it.
            failed, cancelled = await stop_through_cancellation(process)
            if failed is not None:
                raise failed from error
            if cancelled:
                raise asyncio.CancelledError from error
            raise
        finally:
            server.close()  # one connection only; it stays open
        process.stdout = reader
        return process


class SubprocessLauncher:
    """Starts the CLI as a child process (development, the tests). Only
    ``PATH``, the launch's ``env`` and the credential's variable are in its
    environment; its stderr is not read (it may hold anything)."""

    def __init__(self, *, path: str = "/usr/bin:/bin") -> None:
        self._path = path

    async def start(self, launch: CliLaunch) -> CliProcess:
        env = {"PATH": self._path, **launch.env}
        env[launch.secret_env] = launch.secret.reveal()
        process = await asyncio.create_subprocess_exec(
            *launch.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=launch.spec.directory,
            start_new_session=True,
            limit=16 * 1024 * 1024,
        )
        assert process.stdin is not None
        handle = _Subprocess(process)
        try:
            try:
                process.stdin.write(launch.stdin)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()
        except BaseException:
            # Cancelled (or failed) before the caller got the handle: the
            # process must not outlive the attempt with the token in its
            # environment.
            await asyncio.shield(handle.stop())
            raise
        return handle


__all__ = [
    "STOP_GRACE_SECONDS",
    "SystemdLauncher",
    "CliLaunch",
    "CliLauncher",
    "CliProcess",
    "CliSessionSpec",
    "SubprocessLauncher",
    "UnitNotStoppedError",
    "create_session",
    "remove_session",
    "stop_through_cancellation",
]
