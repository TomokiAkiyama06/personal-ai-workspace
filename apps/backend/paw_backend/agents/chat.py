"""One call of a local model server's chat API (Decision 0083, section 1).

:class:`ChatCompletionsClient` sends one non-streaming request to an
OpenAI-compatible ``/chat/completions`` endpoint (vLLM's, serving the main model
as ``main``) and returns a :class:`ChatCompletion`: the answer's text, its
reasoning (vLLM's reasoning parser), its tool calls (the tool-call parser), why
it stopped and the server's ``usage``. It holds no conversation, no tools and no
policy: the runtimes do.

It is used only inside a compute lease (Decision 0083, 1): a node and the
planner inside ``HybridRuntime``'s lease, the Inferred Preference interpreter
inside ``ScheduledInterpreter``'s. It never reads or touches the GPU.

What can go wrong is a :class:`~paw_backend.agents.failures.RuntimeFailure` of
the closed table (``failures.py``), never the server's words:

* the server cannot be reached, or answers 503 (starting, unloading), or the
  connection breaks: ``SERVER_UNAVAILABLE``;
* its error says the GPU memory ran out (``CUDA out of memory`` /
  ``OutOfMemoryError``): ``OUT_OF_MEMORY``;
* its error says the prompt is longer than the model takes: ``CONTEXT_LIMIT``;
* the call took longer than ``timeout_seconds`` (the whole call, not one read):
  ``TIMEOUT``;
* the answer is not the API's JSON, or is larger than ``max_response_bytes``:
  ``MALFORMED_RESPONSE``;
* any other error status: ``OTHER``.

The error body is read (bounded) only to look for those markers; it is never
logged, raised or stored (it may quote the prompt). A cancellation closes the
request: vLLM stops generating when its client goes away.
"""

import asyncio
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from paw_backend.agents.failures import Failure, RuntimeFailure
from paw_backend.agents.reasoning import REASONING_FIELD

# The largest answer read (a 16,384-token answer with its reasoning and tool
# calls is far below this).
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# How much of an error body is searched for the markers below.
_ERROR_BODY_BYTES = 64 * 1024
# The longest a single call may be given (Decision 0083, 8: an HTTP call takes
# at most the time left and 15 minutes).
MAX_CALL_SECONDS = 15 * 60
MAX_OUTPUT_TOKENS = 1 << 20

_OOM_MARKERS = ("cuda out of memory", "outofmemoryerror")
_CONTEXT_MARKERS = ("maximum context length", "maximum model length")
# vLLM before and after its rename of the reasoning field.
_REASONING_FIELDS = (REASONING_FIELD, "reasoning")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call the model asked for. ``arguments`` is the model's JSON text,
    not parsed here (the runtime validates it)."""

    id: str
    name: str
    arguments: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total(self) -> int:
        """What the server processed: the prompt (prefix-cache hits included)
        and the answer (Decision 0083, 5)."""
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True, slots=True)
class ChatCompletion:
    """One answer. Its text and reasoning are never in ``repr``."""

    content: str | None = field(default=None, repr=False)
    reasoning: str | None = field(default=None, repr=False)
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    # ``None``: the server did not say (the caller estimates; never 0).
    usage: Usage | None = None

    def assistant_message(self) -> dict[str, Any]:
        """The answer as the history's next assistant message (the reasoning
        included: :mod:`reasoning` decides what goes back to the model)."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.reasoning is not None:
            message[REASONING_FIELD] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


def _check_base_url(base_url: object) -> str:
    """An ``http(s)`` URL without credentials, a query or a fragment."""
    if not isinstance(base_url, str) or not base_url or len(base_url) > 2048:
        raise ValueError("base_url")
    try:
        parts = urlsplit(base_url)
        parts.port  # noqa: B018 - raises for a bad port
    except ValueError:
        raise ValueError("base_url") from None
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(c.isspace() or ord(c) < 32 for c in base_url)
    ):
        raise ValueError("base_url")
    return base_url.rstrip("/")


def _check_model(model: object) -> str:
    if (
        not isinstance(model, str)
        or not 1 <= len(model) <= 128
        or not model.isprintable()
        or model != model.strip()
    ):
        raise ValueError("model")
    return model


def _positive_int(name: str, value: object, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= maximum
    ):
        raise ValueError(name)
    return value


def _timeout(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or not 0 < value <= MAX_CALL_SECONDS
    ):
        raise ValueError("timeout_seconds")
    return float(value)


class ChatCompletionsClient:
    """Calls ``<base_url>/chat/completions`` (``base_url`` ends with the API's
    version, e.g. ``http://127.0.0.1:8000/v1``) as ``model`` (the server's
    ``--served-model-name``). ``transport``: tests pass an
    ``httpx.MockTransport``. The client ignores the environment's proxies and
    never follows a redirect."""

    def __init__(
        self,
        base_url: str,
        *,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self._url = _check_base_url(base_url) + "/chat/completions"
        self._model = _check_model(model)
        self._max_bytes = _positive_int(
            "max_response_bytes", max_response_bytes, MAX_RESPONSE_BYTES
        )
        if transport is not None and not isinstance(
            transport, httpx.AsyncBaseTransport
        ):
            raise TypeError("transport must be an httpx.AsyncBaseTransport")
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=None,  # the whole call is bounded below
        )

    @property
    def model(self) -> str:
        return self._model

    def __repr__(self) -> str:
        return f"ChatCompletionsClient(model={self._model!r})"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int,
        timeout_seconds: float,
        tools: Sequence[Mapping[str, Any]] | None = None,
        tool_choice: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> ChatCompletion:
        """One request; :class:`RuntimeFailure` when it fails (see the module).

        ``options`` are more fields of the request (``chat_template_kwargs``,
        sampling); they cannot replace ``model``, ``messages``, ``max_tokens``,
        ``stream``, ``tools`` or ``tool_choice``. ``ValueError`` /
        ``TypeError`` for a wrong argument, before anything is sent."""
        body = self._body(messages, max_tokens, tools, tool_choice, options)
        seconds = _timeout(timeout_seconds)
        try:
            async with asyncio.timeout(seconds):
                status, raw = await self._send(body)
        except TimeoutError:
            raise RuntimeFailure(Failure.TIMEOUT) from None
        if status == 200:
            if len(raw) > self._max_bytes:
                raise _malformed()
            return _parse(raw)
        raise RuntimeFailure(_error_failure(status, raw))

    def _body(
        self,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
        tools: Sequence[Mapping[str, Any]] | None,
        tool_choice: str | None,
        options: Mapping[str, Any] | None,
    ) -> bytes:
        if isinstance(messages, str | bytes) or not isinstance(messages, Sequence):
            raise TypeError("messages must be a sequence of mappings")
        if not messages or not all(isinstance(m, Mapping) for m in messages):
            raise ValueError("messages")
        body: dict[str, Any] = dict(options or {})
        fixed = {"model", "messages", "max_tokens", "stream", "tools", "tool_choice"}
        if fixed & set(body):
            raise ValueError("options")
        body.update(
            model=self._model,
            messages=[dict(m) for m in messages],
            max_tokens=_positive_int("max_tokens", max_tokens, MAX_OUTPUT_TOKENS),
            stream=False,
        )
        if tools is not None:
            if isinstance(tools, str | bytes) or not all(
                isinstance(t, Mapping) for t in tools
            ):
                raise TypeError("tools must be a sequence of mappings")
            body["tools"] = [dict(t) for t in tools]
        if tool_choice is not None:
            if not isinstance(tool_choice, str):
                raise TypeError("tool_choice must be a string")
            body["tool_choice"] = tool_choice
        try:
            return json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError):
            raise ValueError("messages") from None

    async def _send(self, body: bytes) -> tuple[int, bytes]:
        """The status and the body (at most ``max_response_bytes`` + 1 bytes of a
        success, ``_ERROR_BODY_BYTES`` of an error)."""
        try:
            async with self._client.stream(
                "POST",
                self._url,
                content=body,
                headers={"content-type": "application/json"},
            ) as response:
                limit = (
                    self._max_bytes + 1
                    if response.status_code == 200
                    else _ERROR_BODY_BYTES
                )
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= limit:
                        break
                return response.status_code, b"".join(chunks)[:limit]
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise RuntimeFailure(Failure.SERVER_UNAVAILABLE) from None
        except httpx.TimeoutException:
            raise RuntimeFailure(Failure.TIMEOUT) from None
        except httpx.HTTPError:
            # The connection broke (the server stopped, a protocol error).
            raise RuntimeFailure(Failure.SERVER_UNAVAILABLE) from None


def _error_failure(status: int, raw: bytes) -> Failure:
    """Which failure an error answer is (its text is searched, never kept)."""
    text = raw[:_ERROR_BODY_BYTES].decode("utf-8", "replace").lower()
    if any(marker in text for marker in _OOM_MARKERS):
        return Failure.OUT_OF_MEMORY
    if status == 503:
        return Failure.SERVER_UNAVAILABLE
    if status in (400, 413, 422) and any(m in text for m in _CONTEXT_MARKERS):
        return Failure.CONTEXT_LIMIT
    return Failure.OTHER


def _malformed() -> RuntimeFailure:
    return RuntimeFailure(Failure.MALFORMED_RESPONSE)


def _optional_text(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise _malformed()


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _malformed()
    return value


def _parse(raw: bytes) -> ChatCompletion:
    """The answer of a 200, or ``MALFORMED_RESPONSE``."""
    try:
        document = json.loads(raw)
    except ValueError:
        raise _malformed() from None
    if not isinstance(document, dict):
        raise _malformed()
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise _malformed()
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise _malformed()
    message = choice["message"]
    reasoning = None
    for name in _REASONING_FIELDS:
        reasoning = _optional_text(message.get(name))
        if reasoning is not None:
            break
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list):
        raise _malformed()
    tool_calls = []
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if (
            not isinstance(function, dict)
            or not isinstance(call.get("id"), str)
            or not isinstance(function.get("name"), str)
            or not isinstance(function.get("arguments"), str)
        ):
            raise _malformed()
        tool_calls.append(ToolCall(call["id"], function["name"], function["arguments"]))
    usage = None
    reported = document.get("usage")
    if reported is not None:
        if not isinstance(reported, dict):
            raise _malformed()
        usage = Usage(
            _count(reported.get("prompt_tokens")),
            _count(reported.get("completion_tokens")),
        )
    return ChatCompletion(
        content=_optional_text(message.get("content")),
        reasoning=reasoning,
        tool_calls=tuple(tool_calls),
        finish_reason=_optional_text(choice.get("finish_reason")),
        usage=usage,
    )


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "MAX_CALL_SECONDS",
    "ChatCompletion",
    "ChatCompletionsClient",
    "ToolCall",
    "Usage",
]
