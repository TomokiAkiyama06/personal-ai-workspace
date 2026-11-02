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
  0089, 2 and Decision 0101, 4: only versions whose smoke test passed);
* ``thread/tokenUsage/updated``: the growth of ``total`` since the last one is
  one :class:`Turn` (a repeated notification, sent again with a rate-limit
  update, adds nothing; a ``total`` that shrinks is refused, ``usage``);
  ``inputTokens`` and ``outputTokens`` only (the cached and reasoning counts are
  parts of them: Decision 0089, 3);
* ``item/started`` / ``item/completed``: the item's ``type`` against the closed
  table of Decision 0089, 2 (a non-tool item passes; a PAW MCP call passes; an
  MCP resource call that names ``paw`` passes, Decision 0101, 3; an item that
  can execute, or a type not in the table, is refused, ``item``);

:func:`tool_check` is the smoke test's check of one model request's ``tools``
(Decision 0101, 1: the PAW tools, and only the MCP resource tools besides).
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

#: The CLI versions whose tool list passed the smoke test (Decision 0089, 2 and
#: Decision 0101: every request's ``tools`` passed :func:`tool_check`). No
#: session is started with any other version. ``0.156.1``: issue #305
#: (2026-10-29; the feature list compared, the fake's shapes checked against
#: its ``app-server-protocol``, the recorder smoke passed).
VERIFIED_VERSIONS: Final[frozenset[str]] = frozenset({"0.156.1"})

#: The MCP resource tools Codex offers whenever an MCP server is configured
#: (``add_mcp_resource_tools``; no setting removes them), allowed by Decision
#: 0101 while ``paw`` is the only MCP server: each tool's argument names and
#: required ones as recorded from ``rust-v0.156.1`` (the description is not
#: compared).
MCP_RESOURCE_TOOLS: Final[Mapping[str, tuple[frozenset[str], frozenset[str]]]] = {
    "list_mcp_resources": (frozenset({"cursor", "server"}), frozenset()),
    "list_mcp_resource_templates": (frozenset({"cursor", "server"}), frozenset()),
    "read_mcp_resource": (frozenset({"server", "uri"}), frozenset({"server", "uri"})),
}

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


def model_tool_names(tools: Iterable[object]) -> list[str]:
    """The names the model can call in a request's ``tools``: a namespace's
    tools as ``<namespace>__<name>`` (an MCP server's namespace is
    ``mcp__<server>``), every other tool by its ``name``, or its ``type`` when
    it has none (``tool_search``, ``web_search``, ...)."""
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            names.append("?")
        elif tool.get("type") == "namespace":
            inner = tool.get("tools")
            for item in inner if isinstance(inner, list) else [None]:
                name = item.get("name") if isinstance(item, dict) else None
                names.append(f"{tool.get('name')}__{name}")
        else:
            names.append(str(tool.get("name") or tool.get("type")))
    return names


def tool_check(
    tools: object, paw_tools: Iterable[str], mcp_servers: Iterable[str]
) -> tuple[str, ...]:
    """Decision 0101, 1: the problems of one model request's ``tools`` (none:
    it passes). ``paw_tools`` are the session's ``enabled_tools``,
    ``mcp_servers`` the session config's MCP servers.

    Every PAW tool (``mcp__paw__<name>``) once; besides them only the MCP
    resource tools (any of them), each a top-level function whose argument
    names and required ones are the recorded ones; and ``paw`` the only MCP
    server. Anything else fails (``apply_patch``, ``tool_search``, a skills
    namespace, another server's tools, a resource tool inside a namespace).
    """
    problems: list[str] = []
    servers = sorted(set(mcp_servers))
    if servers != [PAW_SERVER]:
        problems.append(f"mcp servers {servers}, not only {PAW_SERVER}")
    if not isinstance(tools, list):
        return (*problems, "tools is not a list")
    names = model_tool_names(tools)
    expected = sorted(f"mcp__{PAW_SERVER}__{name}" for name in set(paw_tools))
    for name in expected:
        count = names.count(name)
        if count != 1:
            problems.append(f"{name} offered {count} times")
    seen: set[str] = set()
    for tool, name in zip(_flat(tools), names, strict=True):
        if name in expected:
            continue
        shape = MCP_RESOURCE_TOOLS.get(name)
        if shape is None:
            problems.append(f"extra tool {name}")
        elif name in seen:
            problems.append(f"{name} offered more than once")
        elif tool is None or not _shaped(tool, *shape):
            problems.append(f"{name} is not the recorded top-level function")
        seen.add(name)
    return tuple(problems)


def _flat(tools: list[object]) -> list[dict[str, Any] | None]:
    """The tools in :func:`model_tool_names`' order: a top-level tool itself,
    ``None`` for each tool inside a namespace (never a resource tool)."""
    flat: list[dict[str, Any] | None] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            inner = tool.get("tools")
            flat.extend([None] * (len(inner) if isinstance(inner, list) else 1))
        else:
            flat.append(tool if isinstance(tool, dict) else None)
    return flat


def _shaped(
    tool: Mapping[str, Any], names: frozenset[str], required: frozenset[str]
) -> bool:
    parameters = tool.get("parameters")
    if tool.get("type") != "function" or not isinstance(parameters, dict):
        return False
    properties = parameters.get("properties")
    wanted = parameters.get("required", [])
    return (
        isinstance(properties, dict)
        and frozenset(properties) == names
        and isinstance(wanted, list)
        and frozenset(wanted) == required
        and len(wanted) == len(required)
    )


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
        self._resource_ids: set[object] = set()
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
        if (
            kind == MCP_ITEM
            and item.get("server") == PAW_SERVER
            and item.get("tool") in MCP_RESOURCE_TOOLS
        ):
            # Decision 0101, 3: a resource call that names ``paw`` reaches
            # only the bridge, which serves no resources (``Method not
            # found``). The model answers it with another call, so it orders
            # the reports as a tool call does, but the bridge does not count
            # it (``calls_seen``). Without ``server`` the item says ``codex``:
            # refused below, as any other server.
            ident = item.get("id")
            if ident not in self._resource_ids:
                self._resource_ids.add(ident)
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
    "MCP_RESOURCE_TOOLS",
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
    "model_tool_names",
    "notification",
    "thread_start",
    "tool_check",
    "turn_start",
    "version_of",
]
