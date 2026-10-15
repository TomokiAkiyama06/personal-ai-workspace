"""Talking to ``codex app-server`` over stdio (Decision 0089, 1 to 3; #208).

``codex app-server`` (the stdio transport) reads and writes one JSON message
per line: JSON-RPC without the ``"jsonrpc"`` field (``app-server-protocol/src/
rpc.rs`` of ``rust-v0.162.1``). A session is:

1. ``initialize`` (``capabilities.experimentalApi``: ``thread/start``'s
   ``environments`` is experimental), then the ``initialized`` notification;
2. ``thread/start`` (ephemeral, approval ``never``, sandbox ``read-only``,
   ``environments: []`` so no shell, ``apply_patch`` or ``view_image`` tool is
   offered, the PAW system prompt as ``baseInstructions``);
3. ``turn/start`` with the node's task; then notifications until
   ``turn/completed``.

:class:`CodexStream` reads the server's lines and says what the session needs
(the text of the conversation is not kept, logged or returned):

* the ``initialize`` answer's ``userAgent`` carries the CLI's version; a
  version that is not in the verified list is refused (``version``: Decision
  0089, 2. The list starts empty: no version has passed the smoke test yet);
* ``thread/tokenUsage/updated``: the growth of ``total`` since the last one is
  one :class:`Turn` (a repeated notification, sent again with a rate-limit
  update, adds nothing; a ``total`` that shrinks is refused, ``usage``);
  ``inputTokens`` and ``outputTokens`` only (the cached and reasoning counts are
  parts of them: Decision 0089, 3);
* ``item/started`` / ``item/completed``: the item's ``type`` against the closed
  table of Decision 0089, 2 (a non-tool item passes; a PAW MCP call passes; an
  item that can execute, or a type not in the table, is refused, ``item``);
* ``error`` that will not be retried, and ``turn/completed``'s status and
  error: their ``codexErrorInfo`` (``unauthorized`` is an expired credential,
  ``usageLimitExceeded`` / ``rateLimitExceeded`` a rate limit);
* a request from the server (an approval, an elicitation) is refused
  (``request``): with approval ``never`` and these tools none should come.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from paw_backend.agents.claude_stream import PAW_SERVER

#: The CLI versions whose tool list passed the smoke test (Decision 0089, 2:
#: the request's ``tools`` were exactly the PAW tools). Empty until then: no
#: session is started with any version.
VERIFIED_VERSIONS: Final[frozenset[str]] = frozenset()

#: Item types that execute nothing (Decision 0089, 2): ``userMessage`` comes
#: with every ``turn/start``, ``contextCompaction`` with an automatic
#: compaction.
PASSING_ITEMS: Final = frozenset(
    {"userMessage", "agentMessage", "reasoning", "contextCompaction"}
)
MCP_ITEM: Final = "mcpToolCall"
#: ``codexErrorInfo`` values (``v2/shared.rs``'s ``CodexErrorInfo``).
CREDENTIAL_ERRORS: Final = frozenset({"unauthorized"})
RATE_ERRORS: Final = frozenset({"usageLimitExceeded", "rateLimitExceeded"})
MAX_LINE_BYTES: Final = 16 * 1024 * 1024

INITIALIZE_ID: Final = 1
THREAD_START_ID: Final = 2
TURN_START_ID: Final = 3

_VERSION = re.compile(r"[^/\s]+/([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*)")


class StreamRefused(Exception):
    """The stream shows a session this backend does not run. ``reason`` is a
    fixed code (``version``, ``usage``, ``item``, ``request``, ``protocol``,
    ``malformed``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Turn:
    """What the model calls since the last report used."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Ready:
    """``thread/start`` answered: the ``turn/start`` can be sent."""

    thread_id: str


@dataclass(frozen=True, slots=True)
class Initialized:
    """``initialize`` answered with a verified version."""

    version: str


@dataclass(frozen=True, slots=True)
class Ended:
    """``turn/completed``: its status and the error's ``codexErrorInfo`` (or
    the last unretried ``error``'s) when it did not complete."""

    status: str
    error: str | None


def request(request_id: int, method: str, params: Mapping[str, Any]) -> bytes:
    return (
        json.dumps({"id": request_id, "method": method, "params": params}).encode(
            "utf-8"
        )
        + b"\n"
    )


def notification(method: str) -> bytes:
    return json.dumps({"method": method}).encode("utf-8") + b"\n"


def initialize(client_version: str) -> bytes:
    return request(
        INITIALIZE_ID,
        "initialize",
        {
            "clientInfo": {"name": "paw", "title": None, "version": client_version},
            "capabilities": {"experimentalApi": True},
        },
    )


def thread_start(model: str, system_prompt: str) -> bytes:
    return request(
        THREAD_START_ID,
        "thread/start",
        {
            "model": model,
            "approvalPolicy": "never",
            "sandbox": "read-only",
            "ephemeral": True,
            "environments": [],
            "baseInstructions": system_prompt,
        },
    )


def turn_start(thread_id: str, task: str) -> bytes:
    return request(
        TURN_START_ID,
        "turn/start",
        {"threadId": thread_id, "input": [{"type": "text", "text": task}]},
    )


def version_of(user_agent: object) -> str | None:
    if not isinstance(user_agent, str):
        return None
    match = _VERSION.match(user_agent)
    return match.group(1) if match else None


class CodexStream:
    """Reads the server's lines of one session (see the module)."""

    def __init__(
        self,
        expected_tools: Iterable[str],
        verified_versions: frozenset[str] = VERIFIED_VERSIONS,
    ) -> None:
        self._tools = frozenset(expected_tools)
        self._verified = frozenset(verified_versions)
        self._input = 0
        self._output = 0
        self._error: str | None = None
        self.thread_id: str | None = None
        self.initialized = False
        self.reported = False
        # The order of the reports and the PAW tool calls (``call_counted``).
        self._seen = 0
        self._last_call: int | None = None
        self._call_ids: set[object] = set()
        self._last_report = 0

    @property
    def call_counted(self) -> bool:
        """A report (a growth of the total) came after the last PAW tool call
        began: the model call that made it is counted."""
        return self._last_call is not None and self._last_report > self._last_call

    @property
    def calls_seen(self) -> int:
        """The PAW tool calls the stream showed (each once)."""
        return len(self._call_ids)

    @property
    def complete(self) -> bool:
        """Every model call is reported as far as the stream can tell: there
        was a report, and one after the last PAW tool call began (the model
        always answers a tool's result with another call)."""
        return self.reported and (self._last_call is None or self.call_counted)

    @property
    def totals(self) -> tuple[int, int]:
        """The thread's tokens as far as the reports went."""
        return self._input, self._output

    def feed(self, line: bytes | str) -> Turn | Ready | Initialized | Ended | None:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        if not line.strip():
            return None
        try:
            message = json.loads(line)
        except ValueError:
            raise StreamRefused("malformed") from None
        if not isinstance(message, dict):
            raise StreamRefused("malformed")
        method = message.get("method")
        if "id" in message and isinstance(method, str):
            raise StreamRefused("request")  # the server asks us something
        if "id" in message:
            return self._answer(message)
        if not isinstance(method, str):
            raise StreamRefused("malformed")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "thread/tokenUsage/updated":
            return self._usage(params)
        if method in ("item/started", "item/completed"):
            self._item(params.get("item"))
            return None
        if method == "error":
            if params.get("willRetry") is not True:
                self._error = _error_info(params.get("error")) or "unknown"
            return None
        if method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict):
                raise StreamRefused("malformed")
            status = turn.get("status")
            status = status if isinstance(status, str) else "unknown"
            error = _error_info(turn.get("error")) or self._error
            if status != "completed" and error is None:
                error = "unknown"
            return Ended(status, error)
        return None

    def _answer(self, message: Mapping[str, Any]) -> Ready | Initialized:
        if "error" in message:
            raise StreamRefused("protocol")
        result = message.get("result")
        if not isinstance(result, dict):
            raise StreamRefused("malformed")
        ident = message.get("id")
        if ident == INITIALIZE_ID:
            version = version_of(result.get("userAgent"))
            if version is None or version not in self._verified:
                raise StreamRefused("version")
            self.initialized = True
            return Initialized(version)
        if ident == THREAD_START_ID:
            thread = result.get("thread")
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not self.initialized or not isinstance(thread_id, str) or not thread_id:
                raise StreamRefused("protocol")
            self.thread_id = thread_id
            return Ready(thread_id)
        if ident == TURN_START_ID and self.thread_id is not None:
            return None  # the turn runs; its notifications follow
        raise StreamRefused("protocol")

    def _usage(self, params: Mapping[str, Any]) -> Turn | None:
        if params.get("threadId") != self.thread_id or self.thread_id is None:
            raise StreamRefused("usage")
        usage = params.get("tokenUsage")
        total = usage.get("total") if isinstance(usage, dict) else None
        if not isinstance(total, dict):
            raise StreamRefused("usage")
        new_input = _count(total.get("inputTokens"))
        new_output = _count(total.get("outputTokens"))
        if new_input is None or new_output is None:
            raise StreamRefused("usage")
        if new_input < self._input or new_output < self._output:
            raise StreamRefused("usage")
        grown = Turn(new_input - self._input, new_output - self._output)
        self._input, self._output = new_input, new_output
        if grown.input_tokens == 0 and grown.output_tokens == 0:
            return None  # the same total again (with a rate-limit update)
        self.reported = True
        self._seen += 1
        self._last_report = self._seen
        return grown

    def _item(self, item: object) -> None:
        if not isinstance(item, dict):
            raise StreamRefused("malformed")
        kind = item.get("type")
        if kind in PASSING_ITEMS:
            return
        if (
            kind == MCP_ITEM
            and item.get("server") == PAW_SERVER
            and item.get("tool") in self._tools
        ):
            ident = item.get("id")
            if ident not in self._call_ids:  # started, then completed: once
                self._call_ids.add(ident)
                self._seen += 1
                self._last_call = self._seen
            return
        # Everything else can execute something (a command, a file change,
        # another agent, a search, an image), or is a type this table does
        # not know: refused (Decision 0089, 2).
        raise StreamRefused("item")


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _error_info(error: object) -> str | None:
    """A ``TurnError``'s ``codexErrorInfo`` as one name (a unit variant is a
    string, a variant with fields an object of one key)."""
    if not isinstance(error, dict):
        return None
    info = error.get("codexErrorInfo")
    if isinstance(info, str):
        return info
    if isinstance(info, dict) and len(info) == 1:
        (name,) = info
        return name if isinstance(name, str) else "unknown"
    return "unknown"


__all__ = [
    "CREDENTIAL_ERRORS",
    "MAX_LINE_BYTES",
    "PASSING_ITEMS",
    "RATE_ERRORS",
    "VERIFIED_VERSIONS",
    "CodexStream",
    "Ended",
    "Initialized",
    "Ready",
    "StreamRefused",
    "Turn",
    "initialize",
    "notification",
    "thread_start",
    "turn_start",
    "version_of",
]
