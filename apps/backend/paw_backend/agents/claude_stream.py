"""Reading Claude Code's ``stream-json`` output (Decision 0087, 4; #208).

``claude -p --output-format stream-json --verbose --include-partial-messages``
writes one JSON object per line. :class:`ClaudeStream` reads them and says
what the session needs, nothing more (the text of the conversation is not
kept, logged or returned):

* ``system`` / ``init``: the tools and MCP servers the CLI ended up with.
  :meth:`ClaudeStream.check_init` accepts exactly the expected PAW tools
  (``mcp__paw__<name>``) plus ``EndConversation`` (which no flag removes while
  an MCP tool remains), the ``paw`` server ``connected`` and no
  ``mcp_server_errors``; anything else is a refusal (Decision 0087, 4).
* ``stream_event`` (the raw API events): one model call is ``message_start``
  (its id and input: ``input_tokens + cache_creation_input_tokens +
  cache_read_input_tokens``) ... ``message_delta`` (its ``usage.output_tokens``,
  the real count; the ``assistant`` messages carry only a placeholder) ...
  ``message_stop``: a :class:`Turn`. A call is counted once (by its id). A
  version that sends no ``message_delta`` usage cannot report a call's output
  and is not used: the turn is refused (``turn_without_output``).
* ``system`` / ``api_retry``: its ``error`` category (``authentication_failed``
  and the like are an expired credential, ``rate_limit`` a rate limit).
* ``result``: the session's totals from ``modelUsage`` (every model, subagents
  included), its ``subtype`` and ``is_error``.

Everything else (``assistant``, ``user``, hooks, ...) is skipped.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

#: The tool every Claude Code session keeps while an MCP tool remains
#: (CLI reference, ``--tools``): it only ends the conversation.
END_CONVERSATION: Final = "EndConversation"
PAW_SERVER: Final = "paw"
#: ``api_retry`` categories that mean the credential no longer works.
CREDENTIAL_ERRORS: Final = frozenset(
    {"authentication_failed", "oauth_org_not_allowed", "account_on_hold"}
)
RATE_ERRORS: Final = frozenset({"rate_limit"})
#: The longest line read (a turn's events are small; a ``result`` with its
#: text can be larger).
MAX_LINE_BYTES: Final = 16 * 1024 * 1024


class StreamRefused(Exception):
    """The stream shows a session this backend does not run. ``reason`` is a
    fixed code (``tools``, ``mcp``, ``turn_without_output``, ``malformed``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Turn:
    """One model call: its input (cache included) and output tokens."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class Totals:
    """The ``result`` message: the session's tokens (``None`` when it did not
    report them), whether it ended in an error, and its subtype."""

    input_tokens: int | None
    output_tokens: int | None
    is_error: bool
    subtype: str


@dataclass(frozen=True, slots=True)
class Retry:
    """An ``api_retry`` event's category."""

    error: str


def paw_tool_names(tool_names: Iterable[str]) -> frozenset[str]:
    """The names the CLI shows for the PAW tools (``mcp__paw__<name>``)."""
    return frozenset(f"mcp__{PAW_SERVER}__{name}" for name in tool_names)


class ClaudeStream:
    """Reads the lines of one session (see the module)."""

    def __init__(self, expected_tools: frozenset[str]) -> None:
        self._expected = expected_tools
        self._open: dict[str, int] = {}  # message id -> its input tokens
        self._output: dict[str, int] = {}
        self._current: str | None = None
        self._counted: set[str] = set()
        self.initialized = False

    def feed(self, line: bytes | str) -> Turn | Totals | Retry | None:
        """Read one line; return what it says that matters, or ``None``."""
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
        kind = message.get("type")
        if kind == "system":
            subtype = message.get("subtype")
            if subtype == "init":
                self.check_init(message)
                self.initialized = True
            elif subtype == "api_retry":
                error = message.get("error")
                return Retry(error if isinstance(error, str) else "unknown")
            return None
        if kind == "stream_event":
            event = message.get("event")
            return self._event(event) if isinstance(event, dict) else None
        if kind == "result":
            return _totals(message)
        return None

    def check_init(self, message: Mapping[str, Any]) -> None:
        tools = message.get("tools")
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise StreamRefused("tools")
        present = set(tools)
        if present - {END_CONVERSATION} != set(self._expected):
            # A built-in tool that stayed, or a PAW tool that is missing.
            raise StreamRefused("tools")
        servers = message.get("mcp_servers")
        if not isinstance(servers, list):
            raise StreamRefused("mcp")
        paw = [
            s for s in servers if isinstance(s, dict) and s.get("name") == PAW_SERVER
        ]
        if len(servers) != 1 or len(paw) != 1 or paw[0].get("status") != "connected":
            raise StreamRefused("mcp")
        if message.get("mcp_server_errors"):
            raise StreamRefused("mcp")

    def _event(self, event: Mapping[str, Any]) -> Turn | None:
        kind = event.get("type")
        if kind == "message_start":
            body = event.get("message")
            if not isinstance(body, dict):
                raise StreamRefused("malformed")
            message_id = body.get("id")
            usage = body.get("usage")
            if not isinstance(message_id, str) or not isinstance(usage, dict):
                raise StreamRefused("malformed")
            self._current = message_id
            if message_id not in self._counted:
                self._open[message_id] = sum(
                    _count(usage.get(name))
                    for name in (
                        "input_tokens",
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                )
            return None
        if kind == "message_delta":
            usage = event.get("usage")
            if self._current is not None and isinstance(usage, dict):
                output = usage.get("output_tokens")
                if isinstance(output, int) and not isinstance(output, bool):
                    # Cumulative within the message: the last one counts.
                    self._output[self._current] = _count(output)
            return None
        if kind == "message_stop":
            message_id, self._current = self._current, None
            if message_id is None or message_id in self._counted:
                return None
            if message_id not in self._output:
                raise StreamRefused("turn_without_output")
            self._counted.add(message_id)
            return Turn(self._open.pop(message_id, 0), self._output.pop(message_id))
        return None


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _totals(message: Mapping[str, Any]) -> Totals:
    subtype = message.get("subtype")
    subtype = subtype if isinstance(subtype, str) else "unknown"
    is_error = message.get("is_error") is True or subtype != "success"
    usage = message.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return Totals(None, None, is_error, subtype)
    input_tokens = output_tokens = 0
    for model in usage.values():
        if not isinstance(model, dict):
            return Totals(None, None, is_error, subtype)
        input_tokens += sum(
            _count(model.get(name))
            for name in (
                "inputTokens",
                "cacheReadInputTokens",
                "cacheCreationInputTokens",
            )
        )
        output_tokens += _count(model.get("outputTokens"))
    if input_tokens == 0 and output_tokens == 0:
        # A crash's result may carry zeroed totals: not known.
        return Totals(None, None, is_error, subtype)
    return Totals(input_tokens, output_tokens, is_error, subtype)


__all__ = [
    "CREDENTIAL_ERRORS",
    "END_CONVERSATION",
    "MAX_LINE_BYTES",
    "PAW_SERVER",
    "RATE_ERRORS",
    "ClaudeStream",
    "Retry",
    "StreamRefused",
    "Totals",
    "Turn",
    "paw_tool_names",
]
