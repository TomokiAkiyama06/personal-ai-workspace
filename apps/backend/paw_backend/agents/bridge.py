"""The PAW tools of a CLI session, as an MCP server (Decision 0083, 6; #208).

A CLI (Claude Code, Codex) runs with its built-in tools off (Decision 0087,
4); the only tools it has are these, one call at a time through the node's
:class:`~paw_backend.agents.toolkit.NodeToolkit` and so the Tool Broker.

The transport is MCP's stdio framing (one JSON-RPC 2.0 message per line) on a
Unix socket in the session's directory (Decision 0083, 6: group
``paw-cli-bridge``, mode 0660). Inside the CLI's container a relay
(``deploy/cli-session/paw_mcp_relay.py``, the MCP server command the CLI
starts) connects to the socket, sends one handshake line with the session's
token, and from then on copies the CLI's stdin and stdout. The bridge:

* checks the token (constant-time) before anything else, and closes the
  connection on a wrong one; serves one authenticated connection at a time;
* answers ``initialize``, ``ping``, ``tools/list`` and ``tools/call``
  (anything else is ``Method not found``; notifications are ignored);
* runs the calls one after another (the toolkit is one attempt's); a call's
  answer is the toolkit's text, ``isError`` for a refused call;
* keeps what ends the attempt: a successful ``submit`` (:attr:`outcome`) or a
  failure of the attempt (:attr:`failure`: ``RuntimeFailure``,
  ``InvalidNodeResultError``, ``NodeStopped``, an unexpected error; a
  cancellation of the call is not one). Either
  sets :attr:`ended`, and every later call is refused. The runtime stops the
  CLI then.

Nothing of a call (its arguments, its result) is logged: only the method and
the outcome's type.
"""

import asyncio
import contextlib
import hmac
import json
import logging
import os
from collections.abc import Mapping
from typing import Any, Protocol

from paw_backend.agents.toolkit import ToolError
from paw_backend.orchestrator.runtime import NodeOutcome

logger = logging.getLogger(__name__)

#: The MCP protocol versions this server speaks (the newest first).
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
#: The longest message line (a ``write_file`` of a large file fits).
MAX_LINE_BYTES = 8 * 1024 * 1024
#: The handshake must come within this time.
HANDSHAKE_SECONDS = 10.0
SERVER_NAME = "paw"

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602

ENDED_TEXT = "Tool error: the task has ended; no further tools run."


class Toolkit(Protocol):
    tools: list[dict[str, Any]]

    async def run(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, NodeOutcome | None]: ...


class McpBridge:
    """See the module. ``token``: the session's bridge token (at least 32
    characters; compared in constant time)."""

    def __init__(self, toolkit: Toolkit, *, token: str) -> None:
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("token")
        if not callable(getattr(toolkit, "run", None)):
            raise TypeError("toolkit")
        self._toolkit = toolkit
        self._token = token.encode()
        self._calls = asyncio.Lock()
        self._connected = False
        self.outcome: NodeOutcome | None = None
        self.failure: BaseException | None = None
        self.ended = asyncio.Event()

    def __repr__(self) -> str:
        return "McpBridge(<session>)"

    async def serve(self, path: str) -> asyncio.AbstractServer:
        """Listen on the Unix socket ``path`` (mode 0660: the session's
        directory decides who can reach it)."""
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        server = await asyncio.start_unix_server(
            self.handle, path=path, limit=MAX_LINE_BYTES
        )
        os.chmod(path, 0o660)
        return server

    # --- one connection -----------------------------------------------------

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            if self._connected or not await self._handshake(reader):
                logger.warning("MCP bridge: a connection was refused")
                return
            self._connected = True
            try:
                await self._serve(reader, writer)
            finally:
                self._connected = False
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _handshake(self, reader: asyncio.StreamReader) -> bool:
        try:
            async with asyncio.timeout(HANDSHAKE_SECONDS):
                line = await reader.readline()
            message = json.loads(line)
        except (TimeoutError, ValueError, asyncio.LimitOverrunError, OSError):
            return False
        if not isinstance(message, dict) or message.get("paw_bridge") != 1:
            return False
        token = message.get("token")
        if not isinstance(token, str):
            return False
        return hmac.compare_digest(token.encode(), self._token)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        sending = asyncio.Lock()
        pending: set[asyncio.Task] = set()

        async def send(message: Mapping[str, Any]) -> None:
            data = json.dumps(message, ensure_ascii=False).encode() + b"\n"
            async with sending:
                writer.write(data)
                await writer.drain()

        async def answer(message: Any) -> None:
            response = await self._respond(message)
            if response is not None:
                await send(response)

        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    await send(_error(None, _PARSE_ERROR, "Line too long"))
                    return
                if not line:
                    return
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    await send(_error(None, _PARSE_ERROR, "Parse error"))
                    continue
                # Each request in its own task (a ``ping`` is answered while a
                # long tool call runs); the calls themselves take turns.
                task = asyncio.create_task(answer(message))
                pending.add(task)
                task.add_done_callback(pending.discard)
        finally:
            for task in list(pending):
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def _respond(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, _INVALID_REQUEST, "Invalid request")
        method = message.get("method")
        if method is None:
            return None  # a response of the client: nothing to do
        request_id = message.get("id")
        if "id" not in message:
            return None  # a notification (``notifications/initialized``, ...)
        if not isinstance(request_id, str | int) or isinstance(request_id, bool):
            return _error(None, _INVALID_REQUEST, "Invalid request id")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, _INVALID_PARAMS, "Invalid params")
        if method == "initialize":
            return _result(request_id, self._initialize(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            return _result(request_id, {"tools": self._tool_list()})
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return _error(request_id, _INVALID_PARAMS, "Invalid params")
            return _result(request_id, await self._call(name, arguments))
        return _error(request_id, _METHOD_NOT_FOUND, "Method not found")

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        wanted = params.get("protocolVersion")
        version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }

    def _tool_list(self) -> list[dict[str, Any]]:
        listed = []
        for tool in self._toolkit.tools:
            function = tool["function"]
            listed.append(
                {
                    "name": function["name"],
                    "description": function["description"],
                    "inputSchema": function["parameters"],
                }
            )
        return listed

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._calls:
            if self.ended.is_set():
                return _text(ENDED_TEXT, error=True)
            try:
                text, outcome = await self._toolkit.run(name, arguments)
            except ToolError as error:
                return _text(f"Tool error: {error}", error=True)
            except asyncio.CancelledError:
                raise  # the connection went away: not the attempt's end
            except Exception as error:
                # The attempt ends (a failure, ``NodeStopped``, an unexpected
                # error): the runtime reads it and stops the CLI.
                self.failure = error
                self.ended.set()
                logger.info(
                    "MCP bridge: the attempt ended in a tool call (%s)",
                    type(error).__name__,
                )
                return _text(ENDED_TEXT, error=True)
            if outcome is not None:
                self.outcome = outcome
                self.ended.set()
            return _text(text, error=False)


def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _text(text: str, *, error: bool) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


__all__ = ["ENDED_TEXT", "MAX_LINE_BYTES", "PROTOCOL_VERSIONS", "McpBridge"]
