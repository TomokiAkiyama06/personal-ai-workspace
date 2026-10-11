"""What of the model's reasoning goes back to it (Decision 0083, section 3;
Decision 0076, 1; #200).

The local runtime keeps the conversation of one node attempt in memory and,
before every request, passes it through a :class:`ReasoningHistoryPolicy`
(``prepare(messages, prompt_tokens=...) -> messages``), sends what it returns
and goes on from it: the returned messages are its history from then on, so a
reasoning a policy dropped never comes back (Decision 0076, 1: the prefix cache
then misses one step per request, as in the measured run).

Two policies:

* :class:`KeepAllReasoning` sends the history unchanged, as the benchmark's
  harness did (the numbers of Decision 0074 were measured that way). It stays
  selectable (``LocalModelSetup.reasoning_history``).
* :class:`DropOlderReasoning` (Decision 0076, 1, Approved; its run passed on
  2026-10-11) drops the reasoning of the assistant messages older than the last
  ``keep_last`` once the request's prompt estimate
  (:func:`estimate_prompt_tokens`) passes ``min_prompt_tokens``.
  :data:`MAIN_REASONING_POLICY` (``keep_last=2``, ``min_prompt_tokens=32_768``)
  is the main model's (Qwen3.8-27B-FP8) default.

The reasoning is held in memory to give it back to the model only: it is never
written to the database, a log, the audit or a ``NodeResult`` (like a failure
text), and a dropped reasoning is neither kept nor logged. A log may carry its
length (:func:`reasoning_chars`).

A policy may also shorten a history that nears the prompt limit (a summary, a
cut), through the same seam; neither policy here does (past the limit the
attempt ends, as in the benchmark).
"""

import copy
import inspect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from paw_backend.agents.chat import Usage

# The field of an assistant message that carries its reasoning (vLLM's reasoning
# parser, the OpenAI-compatible API).
REASONING_FIELD = "reasoning_content"
# Every name a reasoning may have in a message (vLLM before and after its rename
# of the field): a policy that drops the reasoning drops all of them.
REASONING_FIELDS = (REASONING_FIELD, "reasoning")

# Decision 0076, 1: tokens per UTF-8 byte of a new tool result in the estimate.
TOOL_RESULT_BYTES_PER_TOKEN = 3


def estimate_prompt_tokens(
    previous: "Usage | None", new_tool_results: Iterable[str]
) -> int | None:
    """The next request's prompt in tokens (Decision 0076, 1 and 3): the last
    answer's ``prompt_tokens`` and ``completion_tokens`` and the UTF-8 bytes of
    the tool results added since, divided by 3. ``None`` before the first
    answer (or when the server gave no usage): nothing is dropped then, as in
    the measured harness."""
    if previous is None:
        return None
    new_bytes = sum(len(text.encode("utf-8")) for text in new_tool_results)
    return (
        previous.prompt_tokens
        + previous.completion_tokens
        + new_bytes // TOOL_RESULT_BYTES_PER_TOKEN
    )


@runtime_checkable
class ReasoningHistoryPolicy(Protocol):
    """Prepares the history sent with the next request.

    ``messages`` is the conversation so far (OpenAI-compatible chat messages);
    ``prompt_tokens`` is this request's prompt estimate
    (:func:`estimate_prompt_tokens`, ``None`` when there is none). The policy
    returns the messages to send, which the runtime keeps as its history, and
    must not change its input or share anything with it."""

    def prepare(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        prompt_tokens: int | None = None,
    ) -> list[dict[str, Any]]: ...


def _copy(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [copy.deepcopy(dict(message)) for message in messages]


def _check_count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


class KeepAllReasoning:
    """Every message as it is, the reasoning included (the benchmark's
    behaviour). A copy, so the caller's history is never shared."""

    def prepare(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        prompt_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        return _copy(messages)

    def __repr__(self) -> str:
        return "KeepAllReasoning()"


@dataclass(frozen=True, slots=True)
class DropOlderReasoning:
    """Decision 0076, 1: when this request's prompt estimate is above
    ``min_prompt_tokens``, the reasoning (every name of the field) of the
    assistant messages before the last ``keep_last`` is dropped. The last
    ``keep_last`` are counted among all the assistant messages, with or without
    reasoning (as the measured harness). The text (``content``), the tool calls
    and the tool results are never dropped. Nothing is logged."""

    keep_last: int = 2
    min_prompt_tokens: int = 32_768

    def __post_init__(self) -> None:
        _check_count(self.keep_last, "keep_last")
        _check_count(self.min_prompt_tokens, "min_prompt_tokens")

    def prepare(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        prompt_tokens: int | None = None,
    ) -> list[dict[str, Any]]:
        if prompt_tokens is not None:
            _check_count(prompt_tokens, "prompt_tokens")
        prepared = _copy(messages)
        if prompt_tokens is None or prompt_tokens <= self.min_prompt_tokens:
            return prepared
        assistants = [m for m in prepared if m.get("role") == "assistant"]
        older = assistants[: max(0, len(assistants) - self.keep_last)]
        for message in older:
            for name in REASONING_FIELDS:
                message.pop(name, None)
        return prepared


# Decision 0076, 1 (Approved; the run of its 2 passed on 2026-10-11): the
# default of the main model's (Qwen3.8-27B-FP8) local agent.
MAIN_REASONING_POLICY = DropOlderReasoning(keep_last=2, min_prompt_tokens=32_768)


def check_reasoning_policy(policy: object) -> None:
    """``TypeError`` unless ``policy.prepare`` is a plain (not ``async``)
    callable of the messages and the ``prompt_tokens`` keyword."""
    method = getattr(policy, "prepare", None)
    if not callable(method) or inspect.iscoroutinefunction(method):
        raise TypeError("policy.prepare must be a plain callable")
    try:
        inspect.signature(method).bind((), prompt_tokens=None)
    except (TypeError, ValueError):
        raise TypeError(
            "policy.prepare must take the messages and prompt_tokens"
        ) from None


def reasoning_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    """How many characters of reasoning ``messages`` carry: what a log may say
    about it (never the text)."""
    total = 0
    for message in messages:
        reasoning = message.get(REASONING_FIELD)
        if isinstance(reasoning, str):
            total += len(reasoning)
    return total


__all__ = [
    "MAIN_REASONING_POLICY",
    "REASONING_FIELD",
    "REASONING_FIELDS",
    "TOOL_RESULT_BYTES_PER_TOKEN",
    "DropOlderReasoning",
    "KeepAllReasoning",
    "ReasoningHistoryPolicy",
    "check_reasoning_policy",
    "estimate_prompt_tokens",
    "reasoning_chars",
]
