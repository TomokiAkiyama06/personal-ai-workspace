"""Task-scoped approval grants: "このタスクの間は許可" (Decision 0085).

A grant is made by the human an approval asks (its ``requester_user_id``, never
the agent, never anybody else) when they approve it "for the rest of this task":
the approval itself is approved (the waiting call uses it once, as before) and,
**in the same transaction**, a grant is created that lets the broker run the
later calls of the same tool, in the same task **and run**, for the same agent
and user, whose scope and arguments are the same or narrower
(:class:`~.grant_pattern.GrantPattern`), without asking again.

* Only an approval whose call can be granted carries a pattern (the broker
  stores it when it opens the approval; ``grant_pattern.grantable``): never a
  ``STRONG_APPROVAL``, a deletion, a credential, an external send, a Working Set
  or an ACL / role / permission change.
* A grant lives as long as its run: the task's end (completed, failed,
  cancelled), a Retry or a Restart ends it (the store checks the task's row,
  locked, in the transaction that records a use, as ``consume`` does for an
  approval; the listener of the task's end also revokes it, for the record).
  The person (or an Admin / Owner) can revoke it before.
* Every use is recorded (``tool_task_grant_uses``, in the transaction that
  checks it) and audited with the grant's id by the broker.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from paw_backend.tasks import TaskRun
from paw_backend.tools.approval_types import (
    ApprovalRecord,
    ApprovalStatus,
    RevokeOutcome,
    SummaryItem,
    is_expired,
)
from paw_backend.tools.capabilities import ApprovalLevel
from paw_backend.tools.grant_pattern import GrantPattern
from paw_backend.tools.task_state import TaskActivity

# Active grants per (task, user): a provisional value (Decision 0085, 3).
DEFAULT_MAX_ACTIVE_GRANTS = 20
MIN_MAX_ACTIVE_GRANTS, MAX_MAX_ACTIVE_GRANTS = 1, 100


class TaskGrantStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class GrantOutcome(StrEnum):
    GRANTED = "granted"
    NOT_FOUND = "not_found"
    NOT_AUTHORISED = "not_authorised"  # not the person asked, or the agent itself
    NOT_PENDING = "not_pending"
    EXPIRED = "expired"
    NOT_GRANTABLE = "not_grantable"  # a call that is never granted for a task
    LIMIT_REACHED = "limit_reached"
    TASK_NOT_ACTIVE = "task_not_active"
    TASK_UNKNOWN = "task_unknown"
    TASK_SUPERSEDED = "task_superseded"


class GrantUseOutcome(StrEnum):
    USED = "used"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"  # another task, agent, user or tool
    REVOKED = "revoked"
    SUPERSEDED = "superseded"  # made in another run of the task
    TASK_NOT_ACTIVE = "task_not_active"
    TASK_UNKNOWN = "task_unknown"
    TASK_SUPERSEDED = "task_superseded"


GRANT_TASK_REFUSAL = {
    TaskActivity.ENDED: GrantOutcome.TASK_NOT_ACTIVE,
    TaskActivity.SUPERSEDED: GrantOutcome.TASK_SUPERSEDED,
    TaskActivity.UNKNOWN: GrantOutcome.TASK_UNKNOWN,
}
USE_TASK_REFUSAL = {
    TaskActivity.ENDED: GrantUseOutcome.TASK_NOT_ACTIVE,
    TaskActivity.SUPERSEDED: GrantUseOutcome.TASK_SUPERSEDED,
    TaskActivity.UNKNOWN: GrantUseOutcome.TASK_UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class TaskGrantRecord:
    grant_id: uuid.UUID
    approval_id: uuid.UUID
    task_id: uuid.UUID
    task_run: TaskRun
    project_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str
    pattern: GrantPattern
    # What the person was shown when they granted it (the approval's summary).
    summary: tuple[SummaryItem, ...]
    status: TaskGrantStatus
    created_at: datetime
    revoked_at: datetime | None = None
    # ``None`` with ``revoked_at`` set: revoked by the system (the task ended).
    revoked_by: uuid.UUID | None = None
    # How many calls it let run (filled by the reads that list grants).
    uses: int = 0


@dataclass(frozen=True, slots=True)
class GrantResult:
    outcome: GrantOutcome
    record: TaskGrantRecord | None = None


@dataclass(frozen=True, slots=True)
class GrantUse:
    """The call a grant is used for: its task, run, agent, user and tool must be
    the grant's (its arguments were compared by the broker, with the grant's
    immutable pattern)."""

    task_id: uuid.UUID
    task_run: TaskRun
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str
    call_hash: str
    correlation_id: uuid.UUID


class TaskGrantStore(Protocol):
    """Durable grants. Every method is one atomic change.

    ``grant_from_approval`` approves the pending approval **and** creates the
    grant in one step, only for the person it asks, for an approval that carries
    a pattern, while its task can act in the approval's run (the task's row read
    locked in the same step), and while the (task, user) holds fewer than
    ``max_active`` active grants. ``use`` records a use only while the grant is
    active and its task can act in the grant's run, checked in the same step
    (``require_active_task`` of ``ApprovalStore.consume``, Decision 0006, 9).
    ``revoke`` and ``use`` are ordered by the grant's row: no use is recorded
    after a revocation committed.
    """

    async def grant_from_approval(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        now: datetime,
        max_active: int,
    ) -> GrantResult: ...

    async def active_grants(
        self,
        task_id: uuid.UUID,
        task_run: TaskRun,
        agent_id: uuid.UUID,
        requester_user_id: uuid.UUID,
        tool: str,
    ) -> list[TaskGrantRecord]: ...

    async def use(
        self, grant_id: uuid.UUID, use: GrantUse, *, now: datetime
    ) -> GrantUseOutcome: ...

    async def get(self, grant_id: uuid.UUID) -> TaskGrantRecord | None: ...

    async def list_task(
        self, task_id: uuid.UUID, requester_user_id: uuid.UUID
    ) -> list[TaskGrantRecord]: ...

    async def revoke(
        self, grant_id: uuid.UUID, *, actor_id: uuid.UUID | None, now: datetime
    ) -> RevokeOutcome: ...

    async def revoke_task(
        self, task_id: uuid.UUID, *, now: datetime
    ) -> list[uuid.UUID]: ...


def diagnose_grant(
    record: ApprovalRecord | None, approver_id: uuid.UUID, now: datetime
) -> GrantOutcome:
    """What ``grant_from_approval`` makes of the approval in its current state
    (``GRANTED``: it can be granted, if the task can act and the limit allows).
    The authorisation comes before any state, as for ``diagnose_decide``."""
    if record is None:
        return GrantOutcome.NOT_FOUND
    if approver_id != record.requester_user_id or approver_id == record.agent_id:
        return GrantOutcome.NOT_AUTHORISED
    if record.status is not ApprovalStatus.PENDING:
        return GrantOutcome.NOT_PENDING
    if is_expired(record, now):
        return GrantOutcome.EXPIRED
    if record.level is not ApprovalLevel.APPROVAL or record.grant_pattern is None:
        return GrantOutcome.NOT_GRANTABLE
    return GrantOutcome.GRANTED


def diagnose_use(record: TaskGrantRecord | None, use: GrantUse) -> GrantUseOutcome:
    """What ``use`` makes of the grant in its current state (``USED``: usable,
    if its task can act)."""
    if record is None:
        return GrantUseOutcome.NOT_FOUND
    if (record.task_id, record.agent_id, record.requester_user_id, record.tool) != (
        use.task_id,
        use.agent_id,
        use.requester_user_id,
        use.tool,
    ):
        return GrantUseOutcome.MISMATCH
    if record.status is not TaskGrantStatus.ACTIVE:
        return GrantUseOutcome.REVOKED
    if record.task_run != use.task_run:
        return GrantUseOutcome.SUPERSEDED
    return GrantUseOutcome.USED


def check_max_active(value: int) -> int:
    if type(value) is not int or not (
        MIN_MAX_ACTIVE_GRANTS <= value <= MAX_MAX_ACTIVE_GRANTS
    ):
        raise ValueError("max_task_grants must be between 1 and 100")
    return value
