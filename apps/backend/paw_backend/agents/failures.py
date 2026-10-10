"""How an agent runtime reports a failure: the closed table (Decision 0083, 4).

Every runtime of this package (the local model's tool loop, the Codex / Claude
CLI sessions) turns what went wrong into a :class:`Failure`, and
:class:`ErrorClassifier` turns that into the ``NodeOutcome.failed`` the
orchestrator records. The table lives here, in one place, and
``tests/test_agents_failures.py`` pins it row by row:

* every ``error_class`` is a name of ``RUNTIME_ERROR_CLASSES`` (the closed list a
  runtime may report, Decision 0021's section 3), except the cloud-fallback row,
  which keeps ``HybridRuntime``'s ``ComputeUnavailable`` (recorded as
  ``AdapterError``, as today: Decision 0083 does not change it);
* no row says ``retryable=False`` (the orchestrator would then neither retry nor
  escalate); the rows a higher rung may solve say ``escalate=True``;
* the message is a fixed text per reason (``Failure.value``). It is hashed for
  the loop detector and never stored; nothing a model, a server or a CLI wrote
  (an error body, a prompt, a credential) ever reaches it.

:meth:`ErrorClassifier.from_error` is the same for an exception a runtime caught:
``NodeStopped``, a cancellation and ``MemoryError`` are never turned into an
outcome (they pass, as ``runtime.py`` requires; a ``MemoryError`` is an
out-of-memory failure of its own, Decision 0071).
"""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from paw_backend.orchestrator.errors import (
    ADAPTER_ERROR,
    AGENT_OUT_OF_MEMORY,
    RUNTIME_ERROR_CLASSES,
    InvalidNodeResultError,
    NodeStopped,
)
from paw_backend.orchestrator.runtime import NodeOutcome

# ``HybridRuntime``'s name for "no local capacity now" (``compute/runtimes.py``):
# not in ``RUNTIME_ERROR_CLASSES``, so it is recorded as ``AdapterError``. The
# cloud-fallback path keeps it (Decision 0083, 1 and 4). Kept here as a literal so
# that this package does not import the scheduler; a test checks that both agree.
COMPUTE_UNAVAILABLE = "ComputeUnavailable"


class Failure(StrEnum):
    """What went wrong in a runtime. The value is the fixed failure text."""

    # The model server ran out of GPU memory (a CUDA OOM in its error), its unit
    # was killed by the OOM killer, or a CLI / tool process ended with an
    # ``oom_kill`` of its container's cgroup.
    OUT_OF_MEMORY = "out_of_memory"
    # The model server cannot be reached, or answers 503 (starting, unloading).
    SERVER_UNAVAILABLE = "server_unavailable"
    # The cloud gate refused (Decision 0083, 1): one reason each.
    CLOUD_POLICY_DENIED = "cloud_policy_denied"
    CONNECTION_QUOTA_EXCEEDED = "connection_quota_exceeded"
    CONNECTION_UNAVAILABLE = "connection_unavailable"
    TOKEN_FLOOR = "token_floor"  # less than the session's minimum token budget
    CONNECTION_BUSY = "connection_busy"  # the connection's session slot stayed taken
    # The adapter's own limit (an HTTP call, a CLI session) ran out before the
    # node's timeout.
    TIMEOUT = "timeout"
    # Limits a higher rung may not hit: the context (the prompt limit or the
    # server's maximum context length), the steps, answers without a tool call,
    # a CLI that ended without ``submit``.
    CONTEXT_LIMIT = "context_limit"
    STEP_LIMIT = "step_limit"
    NO_TOOL_CALLS = "no_tool_calls"
    NO_SUBMISSION = "no_submission"
    # The answer is not JSON or its tool calls are broken (three times running).
    MALFORMED_RESPONSE = "malformed_response"
    # The CLI provider's rate limit (``ConnectionService`` records ``rate_limited``).
    RATE_LIMITED = "rate_limited"
    # The CLI credential expired (``ConnectionService`` records ``expired``).
    CREDENTIAL_EXPIRED = "credential_expired"
    # The sandbox could not start (a mask could not be placed).
    SANDBOX_UNAVAILABLE = "sandbox_unavailable"
    # ``submit`` did not make a valid ``NodeResult`` (after three corrections).
    INVALID_RESULT = "invalid_result"
    # Anything else.
    OTHER = "adapter_error"


# The cloud gate's refusals: on the cloud-fallback path of ``HybridRuntime`` they
# are "no capacity now" and keep the node on its local rung.
GATE_REFUSALS = frozenset(
    {
        Failure.CLOUD_POLICY_DENIED,
        Failure.CONNECTION_QUOTA_EXCEEDED,
        Failure.CONNECTION_UNAVAILABLE,
        Failure.TOKEN_FLOOR,
        Failure.CONNECTION_BUSY,
    }
)


@dataclass(frozen=True, slots=True)
class FailureRow:
    """One row of the table: the class recorded and whether to escalate."""

    error_class: str
    escalate: bool = False


_ROWS: dict[Failure, FailureRow] = {
    Failure.OUT_OF_MEMORY: FailureRow(AGENT_OUT_OF_MEMORY),
    Failure.SERVER_UNAVAILABLE: FailureRow(ConnectionError.__name__),
    # A refused gate when the CLI runtime plays its own rung: a higher rung
    # (another provider) may pass; the same rung would be refused again.
    **{
        refusal: FailureRow(ConnectionError.__name__, escalate=True)
        for refusal in GATE_REFUSALS
    },
    Failure.TIMEOUT: FailureRow(TimeoutError.__name__),
    Failure.CONTEXT_LIMIT: FailureRow(RuntimeError.__name__, escalate=True),
    Failure.STEP_LIMIT: FailureRow(RuntimeError.__name__, escalate=True),
    Failure.NO_TOOL_CALLS: FailureRow(RuntimeError.__name__, escalate=True),
    Failure.NO_SUBMISSION: FailureRow(RuntimeError.__name__, escalate=True),
    Failure.MALFORMED_RESPONSE: FailureRow(ValueError.__name__),
    Failure.RATE_LIMITED: FailureRow(ConnectionError.__name__),
    Failure.CREDENTIAL_EXPIRED: FailureRow(ConnectionError.__name__, escalate=True),
    Failure.SANDBOX_UNAVAILABLE: FailureRow(PermissionError.__name__),
    Failure.INVALID_RESULT: FailureRow(InvalidNodeResultError.__name__),
    Failure.OTHER: FailureRow(ADAPTER_ERROR),
}
FAILURE_TABLE = MappingProxyType(_ROWS)
# The cloud-fallback path (``HybridRuntime``'s ``cloud``, Decision 0083, 1): a
# refused gate is "no capacity now", retried on the local rung later.
FALLBACK_ROW = FailureRow(COMPUTE_UNAVAILABLE)


class ErrorClassifier:
    """The closed table of Decision 0083, section 4 (see the module)."""

    @staticmethod
    def row(failure: Failure, *, fallback: bool = False) -> FailureRow:
        """The row of ``failure``. ``fallback``: the runtime runs as
        ``HybridRuntime``'s cloud fallback, where a refused gate keeps the node
        on its local rung (only the gate's refusals differ)."""
        failure = Failure(failure)
        if fallback and failure in GATE_REFUSALS:
            return FALLBACK_ROW
        return FAILURE_TABLE[failure]

    @classmethod
    def outcome(cls, failure: Failure, *, fallback: bool = False) -> NodeOutcome:
        """The ``NodeOutcome`` for ``failure``: its class, its fixed text, and
        ``escalate`` as the table says (never ``retryable=False``)."""
        failure = Failure(failure)
        row = cls.row(failure, fallback=fallback)
        return NodeOutcome.failed(row.error_class, failure.value, escalate=row.escalate)

    @classmethod
    def from_error(cls, error: BaseException) -> NodeOutcome:
        """The outcome of an exception a runtime caught. ``NodeStopped``, a
        cancellation and ``MemoryError`` are raised again (they are not the
        runtime's to report); a :class:`RuntimeFailure` is its failure; an
        invalid node result is ``INVALID_RESULT``; anything else is ``OTHER``
        (its class and text are never read: they are an adapter's data)."""
        if isinstance(error, NodeStopped | asyncio.CancelledError | MemoryError):
            raise error
        if isinstance(error, RuntimeFailure):
            return cls.outcome(error.failure)
        if isinstance(error, InvalidNodeResultError):
            return cls.outcome(Failure.INVALID_RESULT)
        return cls.outcome(Failure.OTHER)


class RuntimeFailure(Exception):
    """Raised inside this package for a failure of the table. The message is the
    fixed text of ``failure``: never a server's or a model's words."""

    def __init__(self, failure: Failure) -> None:
        self.failure = Failure(failure)
        super().__init__(f"The agent runtime failed ({self.failure.value})")


def _check_table() -> None:
    """The invariants of the table, checked at import (a typo fails loudly)."""
    if set(FAILURE_TABLE) != set(Failure):
        raise RuntimeError("every failure needs a row")
    for row in FAILURE_TABLE.values():
        if row.error_class not in RUNTIME_ERROR_CLASSES:
            raise RuntimeError("a row names a class a runtime may not report")


_check_table()


__all__ = [
    "COMPUTE_UNAVAILABLE",
    "FAILURE_TABLE",
    "FALLBACK_ROW",
    "GATE_REFUSALS",
    "ErrorClassifier",
    "Failure",
    "FailureRow",
    "RuntimeFailure",
]
