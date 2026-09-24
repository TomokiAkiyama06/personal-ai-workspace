"""Approving, rejecting and being notified: the human side of a tool approval.

:class:`ApprovalService` is what the future approval endpoint (an authenticated
human session, PAW-022) calls. It is deliberately a separate object from the
:class:`~.broker.ToolBroker`: **the agent runtime is handed the broker and must
never be handed this service**. Even so, the rules do not rest on that:

* the approver is a human :class:`~paw_backend.authz.Principal` and must be the
  user the requesting agent works for; the agent's own id is refused as
  ``self_approval``, and the database refuses an approver other than that user;
* an approval that needs step-up (``STRONG_APPROVAL``) is granted only after the
  :class:`StepUpVerifier` (PAW-023) confirms it. With no verifier installed the
  default fails closed: such an approval stays pending;
* what is granted is one exact call (see ``approval_types``), once, before it
  expires.

Approving and rejecting change no external state, so their audit row is written
after the change is stored and a failure to write it is logged, not fatal: the
durable, append-only ``tool_approval_events`` row is written in the same
transaction as the change itself.
"""

import asyncio
import inspect
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from paw_backend.authz import AuditSink, Principal
from paw_backend.tools.approval_types import (
    ApprovalBinding,
    ApprovalEvent,
    ApprovalEventKind,
    ApprovalHistoryEntry,
    ApprovalRecord,
    ApprovalStatus,
    ApprovalStore,
    ConsumeOutcome,
    DecideOutcome,
    DecideResult,
    NewApproval,
    OpenResult,
    diagnose_consume,
    diagnose_decide,
    is_expired,
)
from paw_backend.tools.audit import build_tool_event, record_event
from paw_backend.tools.capabilities import ApprovalLevel
from paw_backend.tools.interfaces import require_async_method, require_callable

logger = logging.getLogger(__name__)

Listener = Callable[[ApprovalEvent], object]


class ApprovalListeners:
    """Tells listeners about approval events after they were stored.

    A listener may be a plain function or a coroutine function. A failing or
    slow listener is logged (exception type only) and never fails or delays the
    approval itself. Synchronous listeners run on the event loop: keep them
    short.
    """

    def __init__(
        self, listeners: Sequence[Listener] = (), *, timeout_seconds: float = 3.0
    ) -> None:
        for listener in listeners:
            require_callable(listener, "approval listener", 1)
        self._listeners = tuple(listeners)
        self._timeout_seconds = timeout_seconds

    async def emit(self, event: ApprovalEvent) -> None:
        for listener in self._listeners:
            try:
                async with asyncio.timeout(self._timeout_seconds):
                    result = listener(event)
                    if inspect.isawaitable(result):
                        await result
            except Exception as error:
                logger.warning("Approval listener failed (%s)", type(error).__name__)


class StepUpVerifier(Protocol):
    """Confirms a recent step-up authentication of ``user_id`` for one approval.

    Implemented by PAW-023 (passkey / step-up). It must answer ``True`` only when
    the user completed a step-up that covers this approval (within the policy's
    window, 30 minutes for Owner / Admin operations).
    """

    async def verify(self, user_id: uuid.UUID, approval_id: uuid.UUID) -> bool: ...


class FailClosedStepUp:
    """The default until PAW-023: nobody has stepped up, so no strong approval."""

    async def verify(self, user_id: uuid.UUID, approval_id: uuid.UUID) -> bool:
        return False


class InMemoryApprovalStore:
    """Approvals kept in memory: a test double with the same rules as PostgreSQL.

    Not for production (nothing survives a restart and it cannot be shared
    between processes); it holds at most ``max_records`` approvals.
    """

    def __init__(self, *, max_records: int = 10_000) -> None:
        self._records: dict[uuid.UUID, ApprovalRecord] = {}
        self._history: list[ApprovalHistoryEntry] = []
        self._lock = asyncio.Lock()
        self._max_records = max_records

    def _log(
        self,
        approval_id: uuid.UUID,
        kind: ApprovalEventKind,
        now: datetime,
        *,
        actor_user_id: uuid.UUID | None = None,
        agent_id: uuid.UUID | None = None,
    ) -> None:
        self._history.append(
            ApprovalHistoryEntry(
                seq=len(self._history) + 1,
                approval_id=approval_id,
                kind=kind,
                actor_user_id=actor_user_id,
                agent_id=agent_id,
                occurred_at=now,
            )
        )

    def _expire(self, record: ApprovalRecord, now: datetime) -> ApprovalRecord:
        """Mark an open, timed-out approval ``expired`` (with history)."""
        if record.status in (
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
        ) and is_expired(record, now):
            record = replace(record, status=ApprovalStatus.EXPIRED)
            self._records[record.approval_id] = record
            self._log(record.approval_id, ApprovalEventKind.EXPIRED, now)
        return record

    async def open_request(self, new: NewApproval, *, now: datetime) -> OpenResult:
        async with self._lock:
            for record in list(self._records.values()):
                if record.call_hash != new.call_hash:
                    continue
                record = self._expire(record, now)
                if record.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
                    return OpenResult(record, created=False)
            if len(self._records) >= self._max_records:
                raise RuntimeError("the in-memory approval store is full")
            record = ApprovalRecord(
                approval_id=new.approval_id,
                task_id=new.task_id,
                project_id=new.project_id,
                agent_id=new.agent_id,
                requester_user_id=new.requester_user_id,
                tool=new.tool,
                level=new.level,
                call_hash=new.call_hash,
                targets=new.targets,
                status=ApprovalStatus.PENDING,
                created_at=now,
                expires_at=new.expires_at,
            )
            if record.expires_at <= record.created_at:
                raise ValueError("an approval must expire after it is created")
            self._records[record.approval_id] = record
            self._log(
                record.approval_id,
                ApprovalEventKind.REQUESTED,
                now,
                agent_id=new.agent_id,
            )
            return OpenResult(record, created=True)

    async def get(self, approval_id: uuid.UUID) -> ApprovalRecord | None:
        return self._records.get(approval_id)

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        approve: bool,
        now: datetime,
    ) -> DecideResult:
        async with self._lock:
            record = self._records.get(approval_id)
            outcome = diagnose_decide(record, approver_id, now)
            if record is None:
                return DecideResult(outcome)
            if outcome is DecideOutcome.EXPIRED:
                self._expire(record, now)
            if outcome is not DecideOutcome.DECIDED:
                return DecideResult(outcome)
            status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            record = replace(
                record, status=status, approver_id=approver_id, decided_at=now
            )
            self._records[approval_id] = record
            kind = ApprovalEventKind.APPROVED if approve else ApprovalEventKind.REJECTED
            self._log(approval_id, kind, now, actor_user_id=approver_id)
            return DecideResult(DecideOutcome.DECIDED, record)

    async def consume(
        self, approval_id: uuid.UUID, binding: ApprovalBinding, *, now: datetime
    ) -> ConsumeOutcome:
        async with self._lock:
            record = self._records.get(approval_id)
            outcome = diagnose_consume(record, binding, now)
            if record is None:
                return outcome
            if outcome is ConsumeOutcome.EXPIRED:
                self._expire(record, now)
            if outcome is not ConsumeOutcome.CONSUMED:
                return outcome
            self._records[approval_id] = replace(
                record, status=ApprovalStatus.CONSUMED, consumed_at=now
            )
            self._log(
                approval_id,
                ApprovalEventKind.CONSUMED,
                now,
                agent_id=binding.agent_id,
            )
            return ConsumeOutcome.CONSUMED

    async def history(self, approval_id: uuid.UUID) -> list[ApprovalHistoryEntry]:
        return [h for h in self._history if h.approval_id == approval_id]


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_pending"
    EXPIRED = "expired"
    NOT_AUTHORISED = "not_authorised"  # not the user the agent works for
    SELF_APPROVAL = "self_approval"  # the requesting agent itself
    STEP_UP_REQUIRED = "step_up_required"
    UNAVAILABLE = "unavailable"  # the store failed
    INVALID = "invalid"  # not a UUID / not a Principal


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    outcome: ApprovalOutcome
    approval_id: uuid.UUID | None = None

    @property
    def decided(self) -> bool:
        return self.outcome in (ApprovalOutcome.APPROVED, ApprovalOutcome.REJECTED)

    def __bool__(self) -> bool:
        return self.decided


_DECIDE_TO_OUTCOME = {
    DecideOutcome.NOT_FOUND: ApprovalOutcome.NOT_FOUND,
    DecideOutcome.NOT_PENDING: ApprovalOutcome.NOT_PENDING,
    DecideOutcome.EXPIRED: ApprovalOutcome.EXPIRED,
    DecideOutcome.NOT_AUTHORISED: ApprovalOutcome.NOT_AUTHORISED,
}


class ApprovalService:
    """Approve or reject a pending approval, as the human it belongs to."""

    def __init__(
        self,
        store: ApprovalStore,
        audit: AuditSink,
        *,
        step_up: StepUpVerifier | None = None,
        listeners: Sequence[Listener] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: float = 3.0,
    ) -> None:
        for name, count in (("get", 1), ("decide", 1)):
            require_async_method(store, name, count)
        require_async_method(audit, "record", 1)
        self._step_up: StepUpVerifier = step_up or FailClosedStepUp()
        require_async_method(self._step_up, "verify", 2)
        if not timeout_seconds > 0:
            raise ValueError("timeout_seconds must be positive")
        self._store = store
        self._audit = audit
        self._listeners = ApprovalListeners(listeners, timeout_seconds=timeout_seconds)
        self._clock = clock
        self._timeout_seconds = timeout_seconds

    async def approve(
        self, approval_id: uuid.UUID, approver: Principal
    ) -> ApprovalResult:
        return await self._decide(approval_id, approver, approve=True)

    async def reject(
        self, approval_id: uuid.UUID, approver: Principal
    ) -> ApprovalResult:
        return await self._decide(approval_id, approver, approve=False)

    async def _decide(
        self, approval_id: uuid.UUID, approver: Principal, *, approve: bool
    ) -> ApprovalResult:
        if not isinstance(approval_id, uuid.UUID) or not isinstance(
            approver, Principal
        ):
            return ApprovalResult(ApprovalOutcome.INVALID)
        now = self._clock()
        try:
            record = await self._store.get(approval_id)
        except Exception as error:
            logger.error("Approval lookup failed (%s)", type(error).__name__)
            return ApprovalResult(ApprovalOutcome.UNAVAILABLE, approval_id)

        outcome = await self._outcome(record, approval_id, approver, approve, now)
        result = ApprovalResult(outcome, approval_id)
        if record is not None and outcome in (
            ApprovalOutcome.APPROVED,
            ApprovalOutcome.REJECTED,
        ):
            await self._listeners.emit(
                ApprovalEvent(
                    kind=(
                        ApprovalEventKind.APPROVED
                        if outcome is ApprovalOutcome.APPROVED
                        else ApprovalEventKind.REJECTED
                    ),
                    approval_id=approval_id,
                    task_id=record.task_id,
                    tool=record.tool,
                    level=record.level,
                    occurred_at=now,
                )
            )
        await record_event(
            self._audit,
            build_tool_event(
                action="tool.approval.approve" if approve else "tool.approval.reject",
                allowed=result.decided,
                reason=outcome.value,
                correlation_id=uuid.uuid4(),
                occurred_at=now,
                resource_kind="tool_approval",
                resource_id=approval_id,
                project_id=None if record is None else record.project_id,
                actor_id=approver.user_id,
                actor_role=approver.system_role.value,
            ),
            self._timeout_seconds,
        )
        return result

    async def _outcome(
        self,
        record: ApprovalRecord | None,
        approval_id: uuid.UUID,
        approver: Principal,
        approve: bool,
        now: datetime,
    ) -> ApprovalOutcome:
        if record is None:
            return ApprovalOutcome.NOT_FOUND
        if approver.user_id == record.agent_id:
            return ApprovalOutcome.SELF_APPROVAL
        if approver.user_id != record.requester_user_id:
            return ApprovalOutcome.NOT_AUTHORISED
        if (
            approve
            and record.level is ApprovalLevel.STRONG_APPROVAL
            and diagnose_decide(record, approver.user_id, now) is DecideOutcome.DECIDED
            and not await self._stepped_up(approver.user_id, approval_id)
        ):
            return ApprovalOutcome.STEP_UP_REQUIRED
        try:
            result = await self._store.decide(
                approval_id, approver_id=approver.user_id, approve=approve, now=now
            )
        except Exception as error:
            logger.error("Approval update failed (%s)", type(error).__name__)
            return ApprovalOutcome.UNAVAILABLE
        if result.outcome is DecideOutcome.DECIDED:
            return ApprovalOutcome.APPROVED if approve else ApprovalOutcome.REJECTED
        return _DECIDE_TO_OUTCOME[result.outcome]

    async def _stepped_up(self, user_id: uuid.UUID, approval_id: uuid.UUID) -> bool:
        """Only an explicit ``True`` counts: a failure, a timeout or any other
        answer means "not stepped up"."""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                answer = await self._step_up.verify(user_id, approval_id)
        except Exception as error:
            logger.warning("Step-up check failed (%s)", type(error).__name__)
            return False
        return answer is True
