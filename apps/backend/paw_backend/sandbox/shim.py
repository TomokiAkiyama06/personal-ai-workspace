"""The backend's end of the sandbox's ``git`` shim (Decision 0083, 2 and 6).

Each sandbox that may run commands has its own Unix socket, in its own directory
``<shim base>/<sandbox id>/`` (``git.sock``, and ``token``: 256 random bits),
which the wrapper mounts read-only at ``/run/paw-shim`` in that container and
nowhere else. The shim inside (``deploy/sandbox/paw-git-shim``) sends one JSON
line per ``git`` call and reads one JSON line back::

    -> {"token": "<hex>", "cwd": "/workspace/src", "argv": ["diff", "--stat"]}
    <- {"exit": 0, "output": "..."}            git ran
    <- {"error": "<code>", "message": "..."}   it did not (a fixed code)

A request is answered only while a command of that sandbox runs
(:meth:`ShimServer.activate`: the pool binds the call's task context then) and
only with the sandbox's token (compared in constant time); one at a time; at
most :data:`MAX_REQUEST_BYTES`. The token is never logged. The socket is not on
TCP, and the directory is removed with the sandbox.

The directory is ``0750`` and the socket and token ``0660`` / ``0640`` with
``group`` (the group the task creators' accounts share with the backend, when
the backend runs as its own user: ``SshSandboxRunner``), else ``0700`` /
``0600`` / ``0600`` (the backend is that user: ``LocalWrapperSandboxRunner``).
The base directory itself should not be listable by those accounts (``0711``):
a sandbox's directory is then reached only by its name.
"""

import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

SOCKET_NAME = "git.sock"
TOKEN_NAME = "token"
MAX_REQUEST_BYTES = 64 * 1024
#: How long a connection may take to send its one request.
REQUEST_TIMEOUT_SECONDS = 10.0

#: Answers one checked request: ``(sandbox cwd, argv) -> answer document``.
ShimHandler = Callable[[str, list[str]], Awaitable[dict[str, object]]]


def _error(code: str, message: str = "") -> dict[str, object]:
    return {"error": code, "message": message}


class ShimServer:
    """The socket of one sandbox's git shim."""

    def __init__(self, directory: str, *, group: int | None = None) -> None:
        if not os.path.isabs(directory):
            raise ValueError("the shim directory must be absolute")
        if group is not None and (
            isinstance(group, bool) or not isinstance(group, int)
        ):
            raise TypeError("group must be a gid")
        self._directory = directory
        self._group = group
        self._token = secrets.token_hex(32)
        self._server: asyncio.AbstractServer | None = None
        self._handler: ShimHandler | None = None
        self._lock = asyncio.Lock()

    @property
    def directory(self) -> str:
        return self._directory

    async def open(self) -> None:
        """Create the directory, the token and the socket."""
        shared = self._group is not None
        os.mkdir(self._directory, 0o700)
        try:
            token_path = os.path.join(self._directory, TOKEN_NAME)
            descriptor = os.open(
                token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "w") as handle:
                handle.write(self._token)
            socket_path = os.path.join(self._directory, SOCKET_NAME)
            self._server = await asyncio.start_unix_server(
                self._serve, path=socket_path, limit=MAX_REQUEST_BYTES
            )
            os.chmod(socket_path, 0o660 if shared else 0o600)
            if shared:
                assert self._group is not None
                for path in (self._directory, token_path, socket_path):
                    os.chown(path, -1, self._group, follow_symlinks=False)
                os.chmod(token_path, 0o640)
                os.chmod(self._directory, 0o750)
        except BaseException:
            await self.close()
            raise

    def activate(self, handler: ShimHandler) -> None:
        """Answer requests with ``handler`` (while a command runs)."""
        self._handler = handler

    def deactivate(self) -> None:
        self._handler = None

    async def close(self) -> None:
        """Stop answering and remove the directory."""
        self._handler = None
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        await asyncio.to_thread(shutil.rmtree, self._directory, True)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            try:
                async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                    line = await reader.readline()
            except (TimeoutError, ValueError, asyncio.LimitOverrunError):
                answer = _error("bad_request")
            else:
                answer = await self._answer(line)
            writer.write(json.dumps(answer, ensure_ascii=True).encode() + b"\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _answer(self, line: bytes) -> dict[str, object]:
        if not line.endswith(b"\n"):
            return _error("bad_request")
        try:
            request = json.loads(line)
        except ValueError:
            return _error("bad_request")
        if not isinstance(request, dict):
            return _error("bad_request")
        token, cwd, argv = request.get("token"), request.get("cwd"), request.get("argv")
        if not isinstance(token, str) or not hmac.compare_digest(
            token.encode("ascii", "replace"), self._token.encode("ascii")
        ):
            return _error("bad_token")
        if (
            not isinstance(cwd, str)
            or not isinstance(argv, list)
            or not all(isinstance(word, str) for word in argv)
        ):
            return _error("bad_request")
        async with self._lock:
            handler = self._handler
            if handler is None:
                return _error("inactive", "git can be used only inside a command")
            try:
                return await handler(cwd, argv)
            except Exception as error:  # never the message: it can hold a path
                logger.error("The git shim failed (%s)", type(error).__name__)
                return _error("failed")
