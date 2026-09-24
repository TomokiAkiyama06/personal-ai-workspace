"""Value objects, enums and data of the task queue, budgets and loop detection.

No database and no I/O. Everything here is part of the contract of PAW-033 and is
implemented; the behaviour of ``TaskQueue`` / ``BudgetTracker`` / ``LoopDetector``
/ ``decide_next_action`` is specified in their own modules.

Sources (REQUIREMENTS.md):

* "Task Queue / Priority / Preemption": three priorities HIGH / NORMAL / LOW.
  Priority only affects the order in which NEW tasks start. It never interrupts a
  running task, and the requirements state no aging / starvation rule, so there
  is none.
* "Task execution budget / loop prevention": a budget per task with six items
  (max runtime, agent steps, retry, tool calls, token, GPU time), presets
  Standard / Long / Unlimited, repeated-failure detection, then an alternative
  approach, then escalation. Budget exceeded means: stop at a safe boundary and
  move to Waiting for User / Failed.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from paw_backend.tasks.domain import TaskCommand, WaitReason
from paw_backend.tasks.queueing.errors import InvalidQueueingArgumentError
from paw_backend.tasks.queueing.validation import (
    MAX_APPROACH,
    check_approach,
    check_int,
    check_member,
    check_signature,
)

# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


class Priority(StrEnum):
    HIGH = "high"
    NORMAL = "normal"  # a normal user task
    LOW = "low"  # background memory consolidation / research refresh

    @property
    def rank(self) -> int:
        """0 for HIGH, 1 for NORMAL, 2 for LOW: a smaller rank starts first."""
        return PRIORITY_RANKS[self]


PRIORITY_RANKS: Mapping[Priority, int] = MappingProxyType(
    {Priority.HIGH: 0, Priority.NORMAL: 1, Priority.LOW: 2}
)


class QueueStatus(StrEnum):
    QUEUED = "queued"  # waiting for a worker
    CLAIMED = "claimed"  # leased to a worker
    COMPLETED = "completed"  # the worker finished with it (terminal)
    CANCELLED = "cancelled"  # removed before or during execution (terminal)


ACTIVE_QUEUE_STATUSES = frozenset({QueueStatus.QUEUED, QueueStatus.CLAIMED})


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """A snapshot of one ``queue_entries`` row, as returned by ``TaskQueue``.

    ``claimed_by`` / ``claimed_at`` / ``lease_expires_at`` are set exactly while
    the status is CLAIMED. (After ``complete`` or ``cancel`` of a claimed entry
    ``claimed_by`` and ``claimed_at`` keep their last values as history and
    ``lease_expires_at`` is ``None``.) ``claim_count`` is the number of times
    the entry has been claimed, including reclaims after an expired lease.
    """

    id: int
    task_id: UUID
    priority: Priority
    status: QueueStatus
    enqueued_at: datetime
    claimed_by: str | None
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    claim_count: int
    finished_at: datetime | None


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class BudgetKind(StrEnum):
    """The six budget items. The declaration order is the canonical order."""

    RUNTIME_SECONDS = "runtime_seconds"  # max runtime, whole seconds
    STEPS = "steps"  # max agent steps
    RETRIES = "retries"  # max retry
    TOOL_CALLS = "tool_calls"  # max tool calls
    TOKENS = "tokens"  # max token
    GPU_SECONDS = "gpu_seconds"  # max GPU time, whole seconds


# Every kind whose consumption is reported with ``BudgetTracker.record``. Runtime
# is measured by the tracker itself (``start_runtime`` / ``stop_runtime``).
RECORDABLE_KINDS = tuple(k for k in BudgetKind if k is not BudgetKind.RUNTIME_SECONDS)


class BudgetPreset(StrEnum):
    STANDARD = "standard"
    LONG = "long"
    UNLIMITED = "unlimited"


def _limits(**limits: int | None) -> Mapping[BudgetKind, int | None]:
    return MappingProxyType({BudgetKind(k): v for k, v in limits.items()})


# The presets as data. The requirements name the presets (Standard / Long /
# Unlimited) but give NO numbers ("initial preset examples"; the concrete
# thresholds are an [IMPLEMENTATION_CHOICE]). THE NUMBERS BELOW ARE PLACEHOLDERS
# that a human must confirm (see the PAW-033 section of apps/backend/README.md).
#
# ``None`` means "no limit". The Unlimited preset removes the six numeric limits
# and nothing else: loop detection (``LoopDetector``), Stop Now and the critical
# safety / resource-protection stops of the requirements are independent of the
# preset. An Unlimited task is still stopped by a detected loop.
PRESET_LIMITS: Mapping[BudgetPreset, Mapping[BudgetKind, int | None]] = (
    MappingProxyType(
        {
            BudgetPreset.STANDARD: _limits(
                runtime_seconds=3_600,
                steps=50,
                retries=10,
                tool_calls=300,
                tokens=1_000_000,
                gpu_seconds=3_600,
            ),
            BudgetPreset.LONG: _limits(
                runtime_seconds=14_400,
                steps=200,
                retries=20,
                tool_calls=1_200,
                tokens=4_000_000,
                gpu_seconds=14_400,
            ),
            BudgetPreset.UNLIMITED: _limits(
                runtime_seconds=None,
                steps=None,
                retries=None,
                tool_calls=None,
                tokens=None,
                gpu_seconds=None,
            ),
        }
    )
)


def limit_for(preset: BudgetPreset, kind: BudgetKind) -> int | None:
    """The limit of ``kind`` in ``preset`` (``None`` = unlimited)."""
    check_member("preset", preset, BudgetPreset)
    check_member("kind", kind, BudgetKind)
    return PRESET_LIMITS[preset][kind]


_MAX_LIMIT = 2**63 - 1


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """Consumption of one budget item. ``limit`` ``None`` means unlimited.

    For RUNTIME_SECONDS ``consumed`` includes the time of a run that is in
    progress (see ``BudgetTracker``).
    """

    kind: BudgetKind
    consumed: int
    limit: int | None

    def __post_init__(self) -> None:
        check_member("kind", self.kind, BudgetKind)
        check_int("consumed", self.consumed, minimum=0, maximum=_MAX_LIMIT)
        if self.limit is not None:
            check_int("limit", self.limit, minimum=0, maximum=_MAX_LIMIT)

    @property
    def remaining(self) -> int | None:
        """``limit - consumed`` but never below 0; ``None`` when unlimited."""
        if self.limit is None:
            return None
        return max(self.limit - self.consumed, 0)


class BudgetStatus(StrEnum):
    OK = "ok"
    # At least one item is used up beyond its limit. The requirements define no
    # "warning" threshold, so there is deliberately no WARN status.
    EXCEEDED = "exceeded"


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """Result of ``BudgetTracker.check``.

    ``usage`` has exactly one entry per ``BudgetKind`` in declaration order.
    ``exceeded`` lists the exceeded kinds in declaration order (empty when the
    status is OK). ``status`` is EXCEEDED if and only if ``exceeded`` is not empty.
    """

    status: BudgetStatus
    exceeded: tuple[BudgetKind, ...]
    usage: tuple[BudgetUsage, ...]

    def __post_init__(self) -> None:
        check_member("status", self.status, BudgetStatus)
        if not isinstance(self.exceeded, tuple) or not isinstance(self.usage, tuple):
            raise InvalidQueueingArgumentError("verdict")
        if not all(isinstance(u, BudgetUsage) for u in self.usage) or tuple(
            u.kind for u in self.usage
        ) != tuple(BudgetKind):
            raise InvalidQueueingArgumentError("usage")
        if (
            not all(isinstance(k, BudgetKind) for k in self.exceeded)
            or tuple(k for k in BudgetKind if k in self.exceeded) != self.exceeded
        ):
            raise InvalidQueueingArgumentError("exceeded")
        if (self.status is BudgetStatus.EXCEEDED) != bool(self.exceeded):
            raise InvalidQueueingArgumentError("status")

    def usage_of(self, kind: BudgetKind) -> BudgetUsage:
        return self.usage[tuple(BudgetKind).index(kind)]


# ---------------------------------------------------------------------------
# Loop detection
# ---------------------------------------------------------------------------


class LoopVerdict(StrEnum):
    CONTINUE = "continue"  # no loop
    TRY_ALTERNATIVE = "try_alternative"  # stop this approach, try another one
    ESCALATE = "escalate"  # the alternative failed too: hand over to a stronger agent


@dataclass(frozen=True, slots=True)
class FailureRecord:
    """One recorded failure: its signature and the approach it happened in.

    ``approach`` counts the approaches a task has tried: 0 is the original one,
    1 the first alternative, and so on (0 to ``MAX_APPROACH``). The raw failure
    message is never part of a record.
    """

    signature: str
    approach: int

    def __post_init__(self) -> None:
        check_signature(self.signature)
        check_approach(self.approach)


@dataclass(frozen=True, slots=True)
class LoopPolicy:
    """Thresholds of the loop detector (placeholders, see the README).

    * ``repeat_threshold``: how many failures with the same signature within the
      same approach make a loop.
    * ``window_size``: only the most recent ``window_size`` failures count (and
      the detector stores no more than that per task).
    * ``max_alternatives``: how many alternative approaches are tried before
      escalating. 1 means: original approach, then one alternative, then escalate.
    """

    repeat_threshold: int = 3
    window_size: int = 10
    max_alternatives: int = 1

    def __post_init__(self) -> None:
        check_int("repeat_threshold", self.repeat_threshold, minimum=2, maximum=1000)
        check_int("window_size", self.window_size, minimum=2, maximum=1000)
        check_int(
            "max_alternatives", self.max_alternatives, minimum=0, maximum=MAX_APPROACH
        )
        if self.window_size < self.repeat_threshold:
            raise InvalidQueueingArgumentError("window_size")


DEFAULT_LOOP_POLICY = LoopPolicy()


@dataclass(frozen=True, slots=True)
class LoopAssessment:
    """Result of a loop evaluation.

    ``signature`` and ``approach`` are those of the most recent failure (``None``
    for an empty history) and ``repeats`` is how many failures in the window
    share that signature AND that approach (0 for an empty history).
    """

    verdict: LoopVerdict
    signature: str | None
    approach: int | None
    repeats: int

    def __post_init__(self) -> None:
        check_member("verdict", self.verdict, LoopVerdict)
        if self.signature is not None:
            check_signature(self.signature)
        if self.approach is not None:
            check_approach(self.approach)
        check_int("repeats", self.repeats, minimum=0, maximum=10**9)


# ---------------------------------------------------------------------------
# Escalation decision
# ---------------------------------------------------------------------------


class NextAction(StrEnum):
    CONTINUE = "continue"
    TRY_ALTERNATIVE = "try_alternative"
    ESCALATE_AGENT = (
        "escalate_agent"  # hand over to Codex / Claude (or another stronger agent)
    )
    WAIT_FOR_USER = "wait_for_user"  # stop at a safe boundary; a human decides
    FAIL = "fail"  # the task ends as failed (it can still be retried / restarted)


class DecisionReason(StrEnum):
    NONE = "none"
    BUDGET_EXCEEDED = "budget_exceeded"
    LOOP_TRY_ALTERNATIVE = "loop_try_alternative"
    LOOP_ESCALATE = "loop_escalate"
    LOOP_ESCALATION_UNAVAILABLE = "loop_escalation_unavailable"


@dataclass(frozen=True, slots=True)
class Decision:
    """What the orchestrator must do next, and why.

    ``exceeded`` is copied from the budget verdict; ``loop_verdict`` is the loop
    verdict that was given (even when a budget decision overrides it).
    """

    action: NextAction
    reason: DecisionReason
    exceeded: tuple[BudgetKind, ...]
    loop_verdict: LoopVerdict

    def __post_init__(self) -> None:
        check_member("action", self.action, NextAction)
        check_member("reason", self.reason, DecisionReason)
        check_member("loop_verdict", self.loop_verdict, LoopVerdict)
        if not isinstance(self.exceeded, tuple):
            raise InvalidQueueingArgumentError("exceeded")


# How an action that ends or pauses the work maps onto the task lifecycle of
# PAW-032 (``paw_backend.tasks.domain``). ``None`` means "no state change". The
# orchestrator (PAW-034) issues the command; this package does not.
ACTION_TASK_COMMANDS: Mapping[
    NextAction, tuple[TaskCommand, WaitReason | None] | None
] = MappingProxyType(
    {
        NextAction.CONTINUE: None,
        NextAction.TRY_ALTERNATIVE: None,
        NextAction.ESCALATE_AGENT: None,
        NextAction.WAIT_FOR_USER: (TaskCommand.WAIT, WaitReason.USER),
        NextAction.FAIL: (TaskCommand.FAIL, None),
    }
)

__all__ = [
    "ACTION_TASK_COMMANDS",
    "ACTIVE_QUEUE_STATUSES",
    "DEFAULT_LOOP_POLICY",
    "PRESET_LIMITS",
    "PRIORITY_RANKS",
    "RECORDABLE_KINDS",
    "BudgetKind",
    "BudgetPreset",
    "BudgetStatus",
    "BudgetUsage",
    "BudgetVerdict",
    "Decision",
    "DecisionReason",
    "FailureRecord",
    "LoopAssessment",
    "LoopPolicy",
    "LoopVerdict",
    "NextAction",
    "Priority",
    "QueueEntry",
    "QueueStatus",
    "limit_for",
]
