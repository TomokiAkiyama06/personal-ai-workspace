"""Reading Claude Code's ``stream-json`` output (Decision 0087, 4; #208).

``claude -p --output-format stream-json --verbose --include-partial-messages``
writes one JSON object per line. :class:`ClaudeStream` reads them and says
what the session needs, nothing more (the text of the conversation is not
kept, logged or returned):

* ``system`` / ``init``: the tools and MCP servers the CLI ended up with.
  :meth:`ClaudeStream.check_init` accepts exactly the expected PAW tools
  (``mcp__paw__<name>``) plus ``EndConversation`` (which no flag removes while
  an MCP tool remains), the ``paw`` server ``connected`` and no
  ``mcp_server_errors``; anything else is a refusal (Decision 0087, 4). With
  the verified pairs (Decision 0103, 3 A and 4 A, 2), its
  ``claude_code_version`` and the model the session asked for must be one of
  them, or it is refused too (``model``; a missing version never is one).
* ``stream_event`` (the raw API events): one model call is ``message_start``
  (its id and input: ``input_tokens + cache_creation_input_tokens +
  cache_read_input_tokens``) ... ``message_delta`` (its ``usage.output_tokens``,
  the real count; the ``assistant`` messages carry only a placeholder) ...
  ``message_stop``: a :class:`Turn`. A call is counted once (by its id). A
  version that sends no ``message_delta`` usage cannot report a call's output
  and is not used: the turn is refused (``turn_without_output``). A call
  that started and did not stop is open (:attr:`ClaudeStream.turn_open`): it
  was not reported, so a ``result`` then is refused (``turn_open``) and the
  adapter refuses the end of the output then too.
* ``system`` / ``api_retry``: its ``error`` category (``authentication_failed``
  and the like are an expired credential, ``rate_limit`` a rate limit).
* ``result``: the session's totals from ``modelUsage`` (every model, subagents
  included), its ``subtype`` and ``is_error``.
* a model's refusal and the CLI's fallback to another model (#325): the
  ``system`` / ``model_refusal_fallback`` message, an ``assistant`` content
  block of type ``fallback``, the ``result``'s ``safety_stops`` and the models
  named in its ``modelUsage``. They are kept on the stream
  (:attr:`ClaudeStream.fallbacks`, :attr:`ClaudeStream.models`,
  :attr:`ClaudeStream.safety_stops`) for the adapter to note (and for the
  probe's verdict, ``model_fallback``: Decision 0102, 3); they change
  nothing of what the stream returns or refuses (a malformed one is skipped,
  never refused). Only model names of the form ``validate_model`` accepts are
  kept (any other is ``unknown``): nothing of the conversation.

A token count that is missing (the cache counts may be missing or ``null``),
negative or not a whole number is refused (``malformed``), never read as 0.

Everything else (``assistant``, ``user``, hooks, ...) is skipped.
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from paw_backend.connections.limits import MAX_MODEL_CHARS, MODEL_PATTERN

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
#: The refusals of ``system/init`` (an unsupported CLI, Decision 0087, 4).
INIT_REFUSALS: Final = frozenset({"tools", "mcp"})
#: The refusal of a version and model that are not a verified pair
#: (Decision 0103, 4 A, 2).
MODEL_REFUSAL: Final = "model"
#: A model name the stream showed that is not of the accepted form.
UNKNOWN_MODEL: Final = "unknown"


class StreamRefused(Exception):
    """The stream shows a session this backend does not run. ``reason`` is a
    fixed code (``tools``, ``mcp``, ``model``, ``turn_without_output``,
    ``turn_open``, ``malformed``). ``unsupported``: the CLI's ``system/init``
    showed other tools or servers (``tools``, ``mcp``): its version or setup is
    not one this backend runs (Decision 0087, 4), not a malformed answer.
    ``model``: its version and the model are not a verified pair (Decision
    0103, 4 A, 2)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def unsupported(self) -> bool:
        return self.reason in INIT_REFUSALS


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
class Fallback:
    """The CLI answered on ``to_model`` after ``from_model`` refused (#325)."""

    from_model: str
    to_model: str


@dataclass(frozen=True, slots=True)
class Retry:
    """An ``api_retry`` event's category and its HTTP status (``error_status``,
    ``None`` when it named none)."""

    error: str
    status: int | None = None


def paw_tool_names(tool_names: Iterable[str]) -> frozenset[str]:
    """The names the CLI shows for the PAW tools (``mcp__paw__<name>``)."""
    return frozenset(f"mcp__{PAW_SERVER}__{name}" for name in tool_names)


class ClaudeStream:
    """Reads the lines of one session (see the module)."""

    def __init__(
        self,
        expected_tools: frozenset[str],
        *,
        probe: bool = False,
        verified_models: frozenset[tuple[str, str]] | None = None,
        model: str | None = None,
    ) -> None:
        """``probe`` (Decision 0100, 2: the health probe of a connection): the
        session has no tool and no MCP server at all, and the ``result``'s
        answer is kept (:attr:`answer`) so that the probe can check it. A
        node's session never keeps it. ``verified_models`` (with the
        session's ``model``): the verified ``(Claude Code version, model)``
        pairs ``system/init`` is checked against (``None``: no such check)."""
        if probe and expected_tools:
            raise ValueError("a probe expects no tools")
        if verified_models is not None and not isinstance(model, str):
            raise ValueError("the pairs need the session's model")
        self._expected = expected_tools
        self._probe = probe
        self._pairs = None if verified_models is None else frozenset(verified_models)
        self._model = model
        self._open: dict[str, int] = {}  # message id -> its input tokens
        self._output: dict[str, int] = {}
        self._current: str | None = None
        self._counted: set[str] = set()
        self.initialized = False
        self.answer: str | None = None
        #: The fallbacks the CLI reported, in order, each once (#325).
        self.fallbacks: list[Fallback] = []
        #: The models of the ``result``'s ``modelUsage`` (``None``: none).
        self.models: frozenset[str] | None = None
        #: The ``result``'s ``safety_stops`` (``None``: not reported).
        self.safety_stops: int | None = None

    @property
    def fell_back(self) -> bool:
        """The CLI said a model refused and another one answered, or the
        ``result`` counted a safety stop."""
        return bool(self.fallbacks) or bool(self.safety_stops)

    @property
    def turn_open(self) -> bool:
        """A model call started (``message_start``) and has not stopped: it
        has not been reported."""
        return self._current is not None

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
                status = message.get("error_status")
                if (
                    isinstance(status, bool)
                    or not isinstance(status, int)
                    or not 100 <= status <= 599
                ):
                    status = None
                return Retry(error if isinstance(error, str) else "unknown", status)
            elif subtype == "model_refusal_fallback":
                self._fallback(
                    message.get("original_model"), message.get("fallback_model")
                )
            return None
        if kind == "assistant":
            self._assistant(message)
            return None
        if kind == "stream_event":
            event = message.get("event")
            return self._event(event) if isinstance(event, dict) else None
        if kind == "result":
            if self.turn_open:
                raise StreamRefused("turn_open")
            if self._probe:
                answer = message.get("result")
                self.answer = answer if isinstance(answer, str) else None
            self._result_models(message)
            return _totals(message)
        return None

    def _fallback(self, from_model: object, to_model: object) -> None:
        fallback = Fallback(_model_name(from_model), _model_name(to_model))
        # The system message and the assistant's block say the same fallback.
        if fallback not in self.fallbacks:
            self.fallbacks.append(fallback)

    def _assistant(self, message: Mapping[str, Any]) -> None:
        body = message.get("message")
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if isinstance(block, dict) and block.get("type") == "fallback":
                source, target = block.get("from"), block.get("to")
                self._fallback(
                    source.get("model") if isinstance(source, dict) else None,
                    target.get("model") if isinstance(target, dict) else None,
                )

    def _result_models(self, message: Mapping[str, Any]) -> None:
        stops = message.get("safety_stops")
        if isinstance(stops, int) and not isinstance(stops, bool) and stops >= 0:
            self.safety_stops = stops
        usage = message.get("modelUsage")
        if isinstance(usage, dict) and usage:
            self.models = frozenset(_model_name(name) for name in usage)

    def check_init(self, message: Mapping[str, Any]) -> None:
        self._check_tools(message)
        if self._pairs is not None:
            version = message.get("claude_code_version")
            if (
                not isinstance(version, str)
                or (version, self._model) not in self._pairs
            ):
                raise StreamRefused(MODEL_REFUSAL)

    def _check_tools(self, message: Mapping[str, Any]) -> None:
        tools = message.get("tools")
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise StreamRefused("tools")
        if self._probe:
            # No tool (not even ``EndConversation``: no MCP tool remains) and
            # no MCP server.
            if tools:
                raise StreamRefused("tools")
            if message.get("mcp_servers") not in ([], None) or message.get(
                "mcp_server_errors"
            ):
                raise StreamRefused("mcp")
            return
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
            if self.turn_open:
                # The call before did not stop: it was never reported.
                raise StreamRefused("turn_open")
            input_tokens = (
                _count(usage, "input_tokens")
                + _count(usage, "cache_creation_input_tokens", optional=True)
                + _count(usage, "cache_read_input_tokens", optional=True)
            )
            self._current = message_id
            if message_id not in self._counted:
                self._open[message_id] = input_tokens
            return None
        if kind == "message_delta":
            usage = event.get("usage")
            if self._current is not None and isinstance(usage, dict):
                # Cumulative within the message: the last one counts.
                self._output[self._current] = _count(usage, "output_tokens")
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


def _model_name(value: object) -> str:
    """A model name as the stream showed it, when it is of the accepted
    form (``validate_model``); else :data:`UNKNOWN_MODEL`."""
    if (
        isinstance(value, str)
        and len(value) <= MAX_MODEL_CHARS
        and MODEL_PATTERN.fullmatch(value) is not None
    ):
        return value
    return UNKNOWN_MODEL


def fallback_note(stream: "ClaudeStream", configured: str) -> str | None:
    """One log line's worth of a fallback (#325), or ``None`` when the CLI
    reported none (no fallback, no safety stop): ``configured=<m>
    used=<m,...> fallbacks=<a>-><b>,... safety_stops=<n>``. Model names and
    counts only. A ``modelUsage`` name other than ``configured`` alone is not
    one (the CLI may name an alias's full id, or a model of its own)."""
    if not stream.fell_back:
        return None
    used = stream.models
    fallbacks = ",".join(f"{f.from_model}->{f.to_model}" for f in stream.fallbacks)
    return (
        f"configured={configured}"
        f" used={','.join(sorted(used)) if used else '-'}"
        f" fallbacks={fallbacks or '-'}"
        f" safety_stops={'-' if stream.safety_stops is None else stream.safety_stops}"
    )


def _count(usage: Mapping[str, Any], name: str, *, optional: bool = False) -> int:
    """A token count of ``usage``: a whole number of at least 0. A missing or
    malformed count is refused (``malformed``), never read as 0 (that would
    undercount the session and skip the budget's check); only an ``optional``
    one (the cache counts) may be missing or ``null``, which is 0."""
    value = usage.get(name)
    if value is None and optional:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StreamRefused("malformed")
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
        input_tokens += (
            _count(model, "inputTokens")
            + _count(model, "cacheReadInputTokens", optional=True)
            + _count(model, "cacheCreationInputTokens", optional=True)
        )
        output_tokens += _count(model, "outputTokens")
    if input_tokens == 0 and output_tokens == 0:
        # A crash's result may carry zeroed totals: not known.
        return Totals(None, None, is_error, subtype)
    return Totals(input_tokens, output_tokens, is_error, subtype)


__all__ = [
    "CREDENTIAL_ERRORS",
    "END_CONVERSATION",
    "INIT_REFUSALS",
    "MAX_LINE_BYTES",
    "MODEL_REFUSAL",
    "PAW_SERVER",
    "RATE_ERRORS",
    "UNKNOWN_MODEL",
    "ClaudeStream",
    "Fallback",
    "Retry",
    "StreamRefused",
    "Totals",
    "Turn",
    "fallback_note",
    "paw_tool_names",
]
