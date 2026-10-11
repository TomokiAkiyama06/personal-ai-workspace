"""A CLI session through a shared connection (Decision 0083, 1 and 5; #208).

``ConnectionAdapter.run`` is one prompt and one answer. A Codex / Claude Code CLI
works a node attempt as a *session*: many turns, its tools (through the PAW tool
bridge, a later stage), and a ``submit`` at the end. This module is the port of
such a session, next to the one-call port (``adapter.py``):

* :class:`AgentSessionAdapter`: ``run_session(secret, AgentSessionRequest) ->
  AgentSessionResult``. No adapter ships yet (the CLI runtimes, Decision 0083,
  11's stage 4b, wait for the secret store and the network policy of Decision
  0016): the tests use doubles.
* :class:`AgentSessionRequest` carries ``on_usage(input_tokens, output_tokens)``:
  the adapter calls it at the end of every turn with what that turn used (read
  from the CLI's machine-readable stream). The service counts it into the usage
  row and the task's budget at once, and answers ``False`` when the session must
  stop (the budget is used up, the worker lost its lease, the run was
  replaced): the adapter then stops the CLI (Decision 0083, 5 (b) and 8).
* :class:`AgentSessionResult`: what the session ended with (the ``submit``
  arguments, the totals when known, a failure code).

The service-side request and result (:class:`SessionRequest`,
:class:`SessionResult`) are what ``ConnectionService.execute_session`` takes and
gives back; the gate the caller passes (its placement record, Decision 0083,
1's ``CloudGate``) runs between the admission and the adapter.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from paw_backend.connections.domain import ConnectionKind, FailureCode, UsagePurpose
from paw_backend.connections.limits import MAX_CALL_TIMEOUT_SECONDS, MAX_TOKENS_PER_CALL
from paw_backend.connections.secret import Secret
from paw_backend.connections.validation import (
    validate_enum,
    validate_model,
    validate_optional_tokens,
    validate_prompt,
    validate_seconds,
)

#: Decision 0083, 5 (a): a session starts only when the task has at least this
#: many tokens of its budget left (``cli_session_min_tokens``, provisional).
SESSION_MIN_TOKENS = 50_000
#: Decision 0083, 5 (d): what a session whose totals are unknown is charged in
#: all (``cli_unknown_usage_tokens``, provisional), less what was counted.
UNKNOWN_SESSION_TOKENS = 200_000

#: What the adapter is told after a turn: go on, or stop the CLI now.
UsageCallback = Callable[[int, int], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class AgentSessionRequest:
    """What a session adapter is asked to run. ``prompt`` (the node's task) is
    content: not in the ``repr``, stored and logged nowhere."""

    model: str
    prompt: str = field(repr=False)
    timeout_seconds: float
    on_usage: UsageCallback = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", validate_model("model", self.model))
        object.__setattr__(self, "prompt", validate_prompt("prompt", self.prompt))
        object.__setattr__(
            self,
            "timeout_seconds",
            validate_seconds(
                "timeout_seconds", self.timeout_seconds, MAX_CALL_TIMEOUT_SECONDS
            ),
        )
        if not callable(self.on_usage):
            raise TypeError("on_usage must be callable")


@dataclass(frozen=True, slots=True)
class AgentSessionResult:
    """How a session ended. ``submission``: the arguments of the CLI's
    ``submit`` (``None``: it ended without one). The totals are the session's
    whole input and output tokens when the CLI reported them (``None``: not
    known). ``failure``: a classified failure (``None``: it ended normally)."""

    submission: Mapping[str, Any] | None = field(default=None, repr=False)
    input_tokens: int | None = None
    output_tokens: int | None = None
    failure: FailureCode | None = None

    def __post_init__(self) -> None:
        if self.submission is not None and not isinstance(self.submission, Mapping):
            raise TypeError("submission must be a mapping")
        validate_optional_tokens("input_tokens", self.input_tokens, MAX_TOKENS_PER_CALL)
        validate_optional_tokens(
            "output_tokens", self.output_tokens, MAX_TOKENS_PER_CALL
        )
        if self.failure is not None and type(self.failure) is not FailureCode:
            raise TypeError("failure must be a FailureCode")


class AgentSessionAdapter(Protocol):
    """One session adapter per connection kind (see the module)."""

    kind: ConnectionKind

    async def run_session(
        self, secret: Secret, request: AgentSessionRequest
    ) -> AgentSessionResult: ...


@dataclass(frozen=True, slots=True)
class SessionRequest:
    """One session a node attempt runs through a shared connection. ``purpose``
    is the usage category (Decision 0083, 1: a Worker and the Planner
    ``coding``, a Reviewer ``review``, a Researcher ``research``)."""

    model: str
    purpose: UsagePurpose
    prompt: str = field(repr=False)
    timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", validate_model("model", self.model))
        object.__setattr__(
            self, "purpose", validate_enum("purpose", self.purpose, UsagePurpose)
        )
        object.__setattr__(self, "prompt", validate_prompt("prompt", self.prompt))
        object.__setattr__(
            self,
            "timeout_seconds",
            validate_seconds(
                "timeout_seconds", self.timeout_seconds, MAX_CALL_TIMEOUT_SECONDS
            ),
        )


class SessionStop(StrEnum):
    """Why the service told a session to stop (``on_usage`` answered ``False``)."""

    BUDGET_EXCEEDED = "budget_exceeded"
    LEASE_LOST = "lease_lost"
    RUN_REPLACED = "run_replaced"


@dataclass(frozen=True, slots=True)
class SessionResult:
    """What a session gave back. ``stopped``: the service stopped it (the caller
    raises ``NodeStopped`` for it). The token counts are what the usage row
    holds in the end (counted turn by turn, plus the settled remainder)."""

    usage_id: uuid.UUID
    submission: Mapping[str, Any] | None = field(repr=False)
    input_tokens: int
    output_tokens: int
    failure: FailureCode | None = None
    stopped: SessionStop | None = None
