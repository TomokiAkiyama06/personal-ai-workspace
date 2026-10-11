"""What of the model's reasoning goes back to it (Decision 0083, section 3; #200).

The local runtime keeps the conversation of one node attempt in memory and,
before every request, passes it through a :class:`ReasoningHistoryPolicy`
(``prepare(messages) -> messages``). This module is only the seam: the default,
:class:`KeepAllReasoning`, sends the history unchanged, as the benchmark's
harness did (the numbers of Decision 0074 were measured that way). Which other
policy to use (for example dropping the ``reasoning_content`` of the older
steps) is #200's Decision (0076); it is added here as another policy once that
is approved.

The reasoning is held in memory to give it back to the model only: it is never
written to the database, a log, the audit or a ``NodeResult`` (like a failure
text). A log may carry its length (:func:`reasoning_chars`).

A policy may also shorten a history that nears the prompt limit (a summary, a
cut), through the same seam; the default does not (past the limit the attempt
ends, as in the benchmark).
"""

import copy
import inspect
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

# The field of an assistant message that carries its reasoning (vLLM's reasoning
# parser, the OpenAI-compatible API).
REASONING_FIELD = "reasoning_content"


@runtime_checkable
class ReasoningHistoryPolicy(Protocol):
    """Prepares the history sent with the next request.

    ``messages`` is the conversation so far (OpenAI-compatible chat messages).
    The policy returns the messages to send and must not change its input (the
    runtime keeps the whole history)."""

    def prepare(
        self, messages: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]: ...


class KeepAllReasoning:
    """The default: every message as it is, the reasoning included (the
    benchmark's behaviour). A copy, so the caller's history is never shared."""

    def prepare(self, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [copy.deepcopy(dict(message)) for message in messages]

    def __repr__(self) -> str:
        return "KeepAllReasoning()"


def check_reasoning_policy(policy: object) -> None:
    """``TypeError`` unless ``policy.prepare`` is a plain (not ``async``)
    callable of one argument."""
    method = getattr(policy, "prepare", None)
    if not callable(method) or inspect.iscoroutinefunction(method):
        raise TypeError("policy.prepare must be a plain callable")
    try:
        inspect.signature(method).bind(())
    except (TypeError, ValueError):
        raise TypeError("policy.prepare must take the messages") from None


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
    "REASONING_FIELD",
    "KeepAllReasoning",
    "ReasoningHistoryPolicy",
    "check_reasoning_policy",
    "reasoning_chars",
]
