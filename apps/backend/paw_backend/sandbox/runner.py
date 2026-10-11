"""Starting, using and stopping a sandbox as another Linux user (Decision 0083, 6).

:class:`SandboxRunner` is the seam: ``start`` / ``exec`` / ``stop`` of one
sandbox, as the account of the task's creator. Two implementations send the
``paw-sandbox/v1`` remote command (``wire.py``) to the server's forced-command
wrapper, which re-checks it and runs rootless Podman as that account:

* :class:`SshSandboxRunner`: over the restricted SSH connection of Decision 0029
  (the same key, ``authorized_keys`` line and wrapper as ``SshGitRunner``). The
  backend's Linux user never runs Podman or Docker itself (Decision 0083, 6:
  the Docker group is the host's root).
* :class:`LocalWrapperSandboxRunner`: runs the same wrapper program directly,
  for a backend that already runs as the single Linux user every checkout
  belongs to (what ``SubprocessGitRunner`` is for git); any other account is
  refused. The wrapper's checks are the same either way.

Nothing here logs a path, a command, the spec or any output: a failure is a
fixed reason code (``SandboxUnavailableError``). The output of a command is the
command's (a model chose it) and goes back to the caller as it is.
"""

import asyncio
import logging
import os
import secrets
import shutil
import signal
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from paw_backend.repositories.git import SAFE_PATH
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.ssh import (
    SSH_TRANSPORT_FAILURE_CODE,
    SshGitRunnerPolicy,
    SshKeyDirectory,
    ssh_arguments,
)
from paw_backend.sandbox.spec import (
    MAX_STREAM_BYTES,
    SandboxCommand,
    SandboxExecResult,
    SandboxGoneError,
    SandboxSpec,
    SandboxUnavailableError,
)
from paw_backend.sandbox.wire import (
    exec_command,
    exec_input,
    split_status,
    start_command,
    stop_command,
)

logger = logging.getLogger(__name__)

#: The wrapper's exit status for a refused call (``paw_git_wrapper.REJECTED``).
WRAPPER_REJECTED_CODE = 126
#: How long starting or stopping a sandbox may take (pulling nothing: the image
#: is on the host, ``--pull never``).
CONTROL_TIMEOUT_SECONDS = 120.0
#: The wrapper writes at most this much of each stream, plus its status line.
_PROCESS_OUTPUT_LIMIT = MAX_STREAM_BYTES + 4096


class SandboxRunner(Protocol):
    async def start(
        self, account: LinuxAccount, sandbox_id: uuid.UUID, spec: SandboxSpec
    ) -> None:
        """Start the sandbox; ``SandboxUnavailableError`` when it cannot be."""
        ...

    async def exec(
        self,
        account: LinuxAccount,
        sandbox_id: uuid.UUID,
        command: SandboxCommand,
        *,
        timeout_s: float,
    ) -> SandboxExecResult:
        """Run ``command``; ``SandboxGoneError`` when the sandbox was not
        running (the command did not run), ``SandboxUnavailableError`` when the
        call was refused or did not complete (it may have run), ``TimeoutError``
        when ``timeout_s`` passed (the caller stops the sandbox)."""
        ...

    async def stop(self, account: LinuxAccount, sandbox_id: uuid.UUID) -> None:
        """Stop and remove the sandbox (SIGTERM, five seconds, SIGKILL); a
        sandbox that is gone already is not an error."""
        ...


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool


async def _read(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    kept = 0
    truncated = False
    while chunk := await stream.read(65_536):
        room = limit - kept
        if room > 0:
            chunks.append(chunk[:room])
            kept += min(len(chunk), room)
        if len(chunk) > room:
            truncated = True  # read on, so that the writer never blocks
    return b"".join(chunks), truncated


async def run_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    stdin: bytes,
    timeout_s: float,
    limit: int = _PROCESS_OUTPUT_LIMIT,
) -> ProcessResult:
    """Run ``argv`` to its end with ``stdin``, keeping at most ``limit`` bytes
    of each stream. Its whole process group is killed on a timeout
    (``TimeoutError``) or a cancellation, as ``repositories.git.run_subprocess``
    does. A missing executable is ``SandboxUnavailableError``."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=dict(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        raise SandboxUnavailableError("not_installed") from None
    assert process.stdin is not None
    assert process.stdout is not None and process.stderr is not None
    readers = [
        asyncio.ensure_future(_read(process.stdout, limit)),
        asyncio.ensure_future(_read(process.stderr, limit)),
    ]

    async def feed() -> None:
        assert process.stdin is not None
        try:
            if stdin:
                process.stdin.write(stdin)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    writer = asyncio.ensure_future(feed())
    try:
        async with asyncio.timeout(timeout_s):
            await writer
            (out, out_cut), (err, err_cut) = await asyncio.gather(*readers)
            await process.wait()
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for task in (writer, *readers):
            task.cancel()
        await asyncio.gather(writer, *readers, return_exceptions=True)
        await process.wait()
    return ProcessResult(process.returncode or 0, out, err, out_cut or err_cut)


class _WrapperRunner:
    """What both runners share: the remote commands and how their answers read.
    A subclass says how one remote command reaches the wrapper (``_call``)."""

    async def _call(
        self,
        account: LinuxAccount,
        remote: str,
        *,
        stdin: bytes,
        timeout_s: float,
    ) -> ProcessResult:
        raise NotImplementedError

    async def start(
        self, account: LinuxAccount, sandbox_id: uuid.UUID, spec: SandboxSpec
    ) -> None:
        result = await self._call(
            account,
            start_command(sandbox_id, spec),
            stdin=b"",
            timeout_s=CONTROL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.warning("A sandbox could not be started (%s)", result.returncode)
            raise SandboxUnavailableError(
                "refused"
                if result.returncode == WRAPPER_REJECTED_CODE
                else "start_failed"
            )

    async def exec(
        self,
        account: LinuxAccount,
        sandbox_id: uuid.UUID,
        command: SandboxCommand,
        *,
        timeout_s: float,
    ) -> SandboxExecResult:
        nonce = secrets.token_hex(16)
        result = await self._call(
            account,
            exec_command(sandbox_id, nonce, command),
            stdin=exec_input(command),
            timeout_s=timeout_s,
        )
        stderr, status = split_status(result.stderr, nonce)
        if status is None or result.returncode != 0:
            if result.returncode == WRAPPER_REJECTED_CODE:
                raise SandboxUnavailableError("refused")
            # The call did not complete (the wrapper or the connection failed):
            # the command may have run, so it is never run again for this call.
            raise SandboxUnavailableError("exec_failed")
        if status.exit_code is None:
            # The container was not running: the command did not run at all.
            raise SandboxGoneError()
        return SandboxExecResult(
            status.exit_code,
            result.stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
            status.truncated or result.truncated,
        )

    async def stop(self, account: LinuxAccount, sandbox_id: uuid.UUID) -> None:
        result = await self._call(
            account,
            stop_command(sandbox_id),
            stdin=b"",
            timeout_s=CONTROL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.warning("A sandbox could not be stopped (%s)", result.returncode)
            raise SandboxUnavailableError("stop_failed")


class SshSandboxRunner(_WrapperRunner):
    """The ``paw-sandbox/v1`` commands over Decision 0029's SSH connection."""

    def __init__(
        self,
        keys: SshKeyDirectory,
        *,
        policy: SshGitRunnerPolicy | None = None,
        ssh_executable: str | None = None,
        ssh_config_path: str = "/dev/null",
        path: str = SAFE_PATH,
    ) -> None:
        if not hasattr(keys, "key_path_of"):
            raise TypeError("keys must be an SshKeyDirectory")
        if policy is not None and not isinstance(policy, SshGitRunnerPolicy):
            raise TypeError("policy must be an SshGitRunnerPolicy")
        self._keys = keys
        self._policy = policy or SshGitRunnerPolicy()
        self._executable = ssh_executable
        self._config = ssh_config_path
        self._path = path

    async def _call(
        self,
        account: LinuxAccount,
        remote: str,
        *,
        stdin: bytes,
        timeout_s: float,
    ) -> ProcessResult:
        try:
            identity = await self._keys.key_path_of(account)
        except OSError:
            raise SandboxUnavailableError("ssh_key_unavailable") from None
        if self._executable is None:
            self._executable = shutil.which("ssh", path=self._path)
            if self._executable is None:
                raise SandboxUnavailableError("not_installed")
        argv = ssh_arguments(
            self._executable,
            identity=identity,
            config_path=self._config,
            policy=self._policy,
            account=account,
            remote_command=remote,
        )
        result = await run_process(
            argv, env={"PATH": self._path}, stdin=stdin, timeout_s=timeout_s
        )
        if result.returncode == SSH_TRANSPORT_FAILURE_CODE:
            raise SandboxUnavailableError("ssh_unavailable")
        return result


class LocalWrapperSandboxRunner(_WrapperRunner):
    """The ``paw-sandbox/v1`` commands to the wrapper program run directly, as
    this process's own Linux user (a single-user deployment, and the tests).

    ``wrapper`` is the installed wrapper (``/usr/local/lib/paw/paw-git-wrapper``)
    and ``options`` its ``command=`` options. The wrapper gets the remote
    command in ``SSH_ORIGINAL_COMMAND``, exactly as ``sshd`` would hand it over,
    and a fixed environment; any account but this process's is refused
    (``identity_mismatch``), as ``SubprocessGitRunner`` refuses one."""

    def __init__(
        self,
        wrapper: str,
        *,
        options: Sequence[str] = (),
        python: str = sys.executable,
        path: str = SAFE_PATH,
    ) -> None:
        if not isinstance(wrapper, str) or not os.path.isabs(wrapper):
            raise ValueError("wrapper must be an absolute path")
        if isinstance(options, str) or not all(isinstance(o, str) for o in options):
            raise TypeError("options must be a sequence of strings")
        self._argv = [python, "-I", wrapper, *options]
        self._path = path

    async def _call(
        self,
        account: LinuxAccount,
        remote: str,
        *,
        stdin: bytes,
        timeout_s: float,
    ) -> ProcessResult:
        if account.uid != os.geteuid():
            raise SandboxUnavailableError("identity_mismatch")
        environment = {
            "PATH": self._path,
            "HOME": account.home,
            "SSH_ORIGINAL_COMMAND": remote,
        }
        return await run_process(
            self._argv, env=environment, stdin=stdin, timeout_s=timeout_s
        )
