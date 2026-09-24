"""Value types, the storage protocol and the shared rules of tool approvals.

An approval belongs to **one exact call**: its ``call_hash`` covers the tool,
the normalised arguments, the task and the requester (user and agent). It is

* created ``pending`` by the broker when a call needs a human,
* ``approved`` or ``rejected`` once, by the human user the agent works for
  (never by the agent),
* ``consumed`` at most once, by the very call it was granted for, and
* ``expired`` when its time runs out, whatever it was before.

An approval that is neither decided nor used in time ``expires``. ``rejected``,
``consumed`` and ``expired`` are final. The history of every change is
append-only.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from paw_backend.tools.capabilities import ApprovalLevel
from paw_backend.tools.scope import Target

MAX_APPROVAL_TOOL_LENGTH = 64
_HASH = re.compile(r"[0-9a-f]{64}")
APPROVAL_LEVELS = (ApprovalLevel.APPROVAL, ApprovalLevel.STRONG_APPROVAL)


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class ApprovalEventKind(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class DecideOutcome(StrEnum):
    DECIDED = "decided"
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_pending"  # already decided, consumed or expired
    EXPIRED = "expired"
    NOT_AUTHORISED = "not_authorised"  # not the delegating user, or the agent itself


class ConsumeOutcome(StrEnum):
    CONSUMED = "consumed"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"  # granted for a different call
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    REJECTED = "rejected"
    PENDING = "pending"  # no human decision yet


def _aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


@dataclass(frozen=True, slots=True)
class NewApproval:
    approval_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str  # a registered tool name
    level: ApprovalLevel
    call_hash: str
    targets: tuple[Target, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("approval_id", "task_id", "project_id", "agent_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if not isinstance(self.requester_user_id, uuid.UUID):
            raise TypeError("requester_user_id must be a UUID")
        if self.agent_id == self.requester_user_id:
            raise ValueError("an agent cannot be the user it acts for")
        if (
            type(self.tool) is not str
            or not 1 <= len(self.tool) <= MAX_APPROVAL_TOOL_LENGTH
        ):
            raise ValueError("tool is not valid")
        if self.level not in APPROVAL_LEVELS:
            raise ValueError(
                "only APPROVAL and STRONG_APPROVAL are approved by a human"
            )
        if not isinstance(self.call_hash, str) or not _HASH.fullmatch(self.call_hash):
            raise ValueError("call_hash is not a SHA-256 hex digest")
        _aware(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    """The call an approval is used for: all of it must match what was granted."""

    task_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str
    level: ApprovalLevel
    call_hash: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str
    level: ApprovalLevel
    call_hash: str
    targets: tuple[Target, ...]
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    approver_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    consumed_at: datetime | None = None

    def binding(self) -> ApprovalBinding:
        return ApprovalBinding(
            self.task_id,
            self.agent_id,
            self.requester_user_id,
            self.tool,
            self.level,
            self.call_hash,
        )


@dataclass(frozen=True, slots=True)
class ApprovalHistoryEntry:
    seq: int
    approval_id: uuid.UUID
    kind: ApprovalEventKind
    actor_user_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class OpenResult:
    record: ApprovalRecord
    created: bool  # False: an open request for this exact call already existed


@dataclass(frozen=True, slots=True)
class DecideResult:
    outcome: DecideOutcome
    record: ApprovalRecord | None = None


@dataclass(frozen=True, slots=True)
class ApprovalEvent:
    """What listeners are told (after the change was stored): ids and enums only."""

    kind: ApprovalEventKind
    approval_id: uuid.UUID
    task_id: uuid.UUID
    tool: str
    level: ApprovalLevel
    occurred_at: datetime


class ApprovalStore(Protocol):
    """Durable approval state. Every method is one atomic change.

    ``open_request`` returns the open (pending or approved, unexpired) request
    for the same ``call_hash`` when there is one, so a repeated request does not
    create a second approval. ``decide`` and ``consume`` succeed for at most one
    of any number of concurrent callers.
    """

    async def open_request(self, new: NewApproval, *, now: datetime) -> OpenResult: ...

    async def get(self, approval_id: uuid.UUID) -> ApprovalRecord | None: ...

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        approve: bool,
        now: datetime,
    ) -> DecideResult: ...

    async def consume(
        self, approval_id: uuid.UUID, binding: ApprovalBinding, *, now: datetime
    ) -> ConsumeOutcome: ...

    async def history(self, approval_id: uuid.UUID) -> list[ApprovalHistoryEntry]: ...


def is_expired(record: ApprovalRecord, now: datetime) -> bool:
    return record.expires_at <= now


def diagnose_decide(
    record: ApprovalRecord | None, approver_id: uuid.UUID, now: datetime
) -> DecideOutcome:
    """What ``decide`` does with the record in its current state.

    Both stores use it: the in-memory one to decide, PostgreSQL to explain why
    its atomic conditional update matched nothing. The authorisation check
    comes before any state, so a stranger learns nothing about the approval.
    """
    if record is None:
        return DecideOutcome.NOT_FOUND
    if approver_id != record.requester_user_id or approver_id == record.agent_id:
        return DecideOutcome.NOT_AUTHORISED
    if record.status is not ApprovalStatus.PENDING:
        return DecideOutcome.NOT_PENDING
    if is_expired(record, now):
        return DecideOutcome.EXPIRED
    return DecideOutcome.DECIDED


def diagnose_consume(
    record: ApprovalRecord | None, binding: ApprovalBinding, now: datetime
) -> ConsumeOutcome:
    """What ``consume`` does with the record in its current state (see above).

    ``CONSUMED`` means the record is consumable right now.
    """
    if record is None:
        return ConsumeOutcome.NOT_FOUND
    if record.binding() != binding:
        return ConsumeOutcome.MISMATCH
    status = record.status
    if status is ApprovalStatus.CONSUMED:
        return ConsumeOutcome.ALREADY_USED
    if status is ApprovalStatus.REJECTED:
        return ConsumeOutcome.REJECTED
    if status is ApprovalStatus.EXPIRED or is_expired(record, now):
        return ConsumeOutcome.EXPIRED
    if status is ApprovalStatus.PENDING:
        return ConsumeOutcome.PENDING
    return ConsumeOutcome.CONSUMED  # approved and unexpired
