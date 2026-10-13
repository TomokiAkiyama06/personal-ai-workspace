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
import os
import secrets
import shutil
import signal
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from paw_backend.connections import Secret

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
    directory = Path(tempfile.mkdtemp(prefix=f"paw-cli-{session_id}-", dir=base))
    os.chmod(directory, 0o750)
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


class _Subprocess:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        assert process.stdout is not None
        self.stdout = process.stdout

    async def wait(self) -> int:
        return await self._process.wait()

    async def stop(self) -> None:
        """SIGTERM to the process group, then SIGKILL after the grace."""
        process = self._process
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), STOP_GRACE_SECONDS)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


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
        try:
            process.stdin.write(launch.stdin)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        process.stdin.close()
        return _Subprocess(process)


__all__ = [
    "STOP_GRACE_SECONDS",
    "CliLaunch",
    "CliLauncher",
    "CliProcess",
    "CliSessionSpec",
    "SubprocessLauncher",
    "create_session",
    "remove_session",
]
