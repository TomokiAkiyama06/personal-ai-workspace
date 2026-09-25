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
  expires;
* an approval can be revoked before it is used, by the user or an Admin / Owner,
  and it is revoked when its task ends (:meth:`ApprovalService.revoke_on_task_end`,
  for every terminal state: completed, failed, cancelled). That revocation runs
  after the terminal transition committed, so a failure of the store **is
  raised** (:class:`ApprovalRevocationError`), not counted as "nothing to
  revoke", and it is **bounded by ONE deadline for the whole revocation**: the
  store call and the report of every revoked approval (lookup, event, audit row)
  share one ``timeout_seconds``, not one each (``TaskService`` awaits its
  listeners: a stalled store must not hold up cancel / complete / retry, however
  many approvals the task has). It
  cannot be retried by ``TaskService`` and it cannot roll the
  transition back, so the broker independently refuses to open or use an
  approval of a task that can no longer act (``task_state.py``): an approval
  that stayed live is still unusable. Decision 0006, section 9.

The store calls of a human's decision are bounded too: ``approve``, ``reject`` and
``revoke`` read the approval and then update it, and those two calls share ONE
``timeout_seconds`` (started with the operation; the step-up has its own limit
and is not counted). A store that stalls, or runs out of the time, gives the
typed outcome ``UNAVAILABLE`` and the call that was cut off is not reported as
done. ``PostgresApprovalStore`` runs these calls on abortable connections, so the
deadline really ends them (a cancelled query on a pooled session waits for the
server to confirm, about ten seconds on a stalled one). The cut-off statement
is atomic and may still be applied by the server: a repeated call shows the
true state. Decision 0006, section 4.

A user who is not the delegating user is told the approval does not exist (no
oracle for other users' approvals); the audit row records ``not_authorised``.

Approving and rejecting change no external state, so their audit row is written
after the change is stored and a failure to write it is logged, not fatal: the
durable, append-only ``tool_approval_events`` row is written in the same
transaction as the change itself.
"""

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from paw_backend.authz import AuditSink, Principal, SystemRole
from paw_backend.tools.approval_types import (
    ApprovalEvent,
    ApprovalEventKind,
    ApprovalRecord,
    ApprovalStore,
    DecideOutcome,
    RevokeOutcome,
    diagnose_decide,
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


class _StoreDeadline:
    """ONE time limit for all the store calls of one operation.

    A human's decision (approve, reject, revoke) reads the approval and then
    updates it: two store calls. Each with its own ``timeout_seconds`` would let
    one operation take twice the limit, so they share one: it starts with the
    operation and every call gets what is left of it (a call that finds nothing
    left is not started). A call that runs out is cancelled, which shuts down
    the abortable connection of ``PostgresApprovalStore``, and raises
    ``TimeoutError``. Only the store is counted: the step-up, the listeners and
    the audit row each have their own limit.
    """

    def __init__(self, seconds: float) -> None:
        self._left = seconds

    async def call[T](
        self, method: Callable[..., Awaitable[T]], *args: object, **kwargs: object
    ) -> T:
        if self._left <= 0:
            raise TimeoutError
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            async with asyncio.timeout(self._left):
                return await method(*args, **kwargs)
        finally:
            self._left -= loop.time() - started


class ApprovalRevocationError(Exception):
    """The open approvals of an ended task could not be revoked (the store failed).

    Carries no detail: the driver's message can name hosts or users; only the
    exception type of the cause is logged. Approvals of that task may still be
    open in the store; :meth:`ApprovalService.revoke_task` is idempotent, so it
    can be run again (the broker refuses them meanwhile). It is also raised when
    the deadline ended the revocation after the store had revoked the approvals:
    they stay revoked (running it again returns 0), but the event and the audit
    row of the approvals not yet reported are lost (the log says how many).
    """


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


class ApprovalOutcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"
    # Also what a stranger is told: the existence of another user's approval is
    # not revealed (the audit row still says ``not_authorised``).
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_pending"
    NOT_OPEN = "not_open"  # revoking an approval that is already finished
    EXPIRED = "expired"
    NOT_AUTHORISED = "not_authorised"  # kept for the audit reason
    SELF_APPROVAL = "self_approval"  # the requesting agent itself
    STEP_UP_REQUIRED = "step_up_required"
    UNAVAILABLE = "unavailable"  # the store failed or did not answer in time
    INVALID = "invalid"  # not a UUID / not a Principal


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    outcome: ApprovalOutcome
    approval_id: uuid.UUID | None = None

    @property
    def decided(self) -> bool:
        return self.outcome in (
            ApprovalOutcome.APPROVED,
            ApprovalOutcome.REJECTED,
            ApprovalOutcome.REVOKED,
        )

    def __bool__(self) -> bool:
        return self.decided


_DECIDE_TO_OUTCOME = {
    DecideOutcome.NOT_FOUND: ApprovalOutcome.NOT_FOUND,
    DecideOutcome.NOT_PENDING: ApprovalOutcome.NOT_PENDING,
    DecideOutcome.EXPIRED: ApprovalOutcome.EXPIRED,
    DecideOutcome.NOT_AUTHORISED: ApprovalOutcome.NOT_AUTHORISED,
    DecideOutcome.STEP_UP_REQUIRED: ApprovalOutcome.STEP_UP_REQUIRED,
}
# Task states after which nothing the task was approved for should stay usable.
_TASK_END_STATES = frozenset({"cancelled", "failed", "completed"})
# Who may revoke someone else's approval: revoking only takes rights away, so
# the administrators may do it on the user's behalf.
_MAY_REVOKE_ANY = frozenset({SystemRole.ADMIN, SystemRole.OWNER})


class ApprovalService:
    """Approve, reject or revoke an approval, as the human it belongs to."""

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
        for name, count in (
            ("get", 1),
            ("decide", 1),
            ("revoke", 1),
            ("revoke_task", 1),
        ):
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

    async def revoke(self, approval_id: uuid.UUID, actor: Principal) -> ApprovalResult:
        """Withdraw a pending or approved approval before it is used.

        The delegating user may, and so may an Admin or Owner (revoking only
        takes rights away). Anybody else is told the approval does not exist.
        """
        if not isinstance(approval_id, uuid.UUID) or not isinstance(actor, Principal):
            return ApprovalResult(ApprovalOutcome.INVALID)
        now = self._clock()
        store = _StoreDeadline(self._timeout_seconds)
        try:
            record = await store.call(self._store.get, approval_id)
        except Exception as error:
            logger.error("Approval lookup failed (%s)", type(error).__name__)
            return ApprovalResult(ApprovalOutcome.UNAVAILABLE, approval_id)
        audit_reason: ApprovalOutcome
        if record is None:
            outcome = audit_reason = ApprovalOutcome.NOT_FOUND
        elif actor.user_id == record.agent_id or (
            actor.user_id != record.requester_user_id
            and actor.system_role not in _MAY_REVOKE_ANY
        ):
            outcome, audit_reason = (
                ApprovalOutcome.NOT_FOUND,
                ApprovalOutcome.NOT_AUTHORISED,
            )
        else:
            try:
                revoked = await store.call(
                    self._store.revoke, approval_id, actor_id=actor.user_id, now=now
                )
            except Exception as error:
                logger.error("Approval revoke failed (%s)", type(error).__name__)
                revoked = None
            outcome = audit_reason = {
                RevokeOutcome.REVOKED: ApprovalOutcome.REVOKED,
                RevokeOutcome.NOT_FOUND: ApprovalOutcome.NOT_FOUND,
                RevokeOutcome.NOT_OPEN: ApprovalOutcome.NOT_OPEN,
            }.get(revoked, ApprovalOutcome.UNAVAILABLE)
        if record is not None and outcome is ApprovalOutcome.REVOKED:
            await self._emit(ApprovalEventKind.REVOKED, record, now)
        await self._audit_row(
            "tool.approval.revoke",
            outcome is ApprovalOutcome.REVOKED,
            audit_reason.value,
            approval_id,
            None if record is None else record.project_id,
            actor,
            now,
        )
        return ApprovalResult(outcome, approval_id)

    async def revoke_task(self, task_id: uuid.UUID) -> int:
        """Revoke every open approval of a task (it ended); returns how many.

        A system action: nobody's approval may outlive its task. Wire
        :meth:`revoke_on_task_end` into ``TaskService(listeners=[...])``.
        Idempotent. Raises :class:`ApprovalRevocationError` when the store
        fails, or when the whole revocation (the store call **and** reporting
        every revoked approval: lookup, event, audit row) did not finish within
        **one** ``timeout_seconds``: a failure must not look like "nothing was
        open", and a task with many approvals must not take many times the limit.
        """
        if not isinstance(task_id, uuid.UUID):
            raise TypeError("task_id must be a UUID")
        now = self._clock()
        revoked: Sequence[uuid.UUID] | None = None
        reported = 0
        try:
            # ``TaskService`` awaits this after the transition committed: a store
            # that stalls must not hold up cancel / complete / retry requests.
            # ONE deadline covers every step below (started once, here): a limit
            # that restarted for each of up to 100 approvals would multiply it.
            # Cancelling a step also shuts down the abortable connection of
            # ``PostgresApprovalStore``. Whether it was done in time is unknown,
            # like any other failure: it is raised, and can be run again.
            async with asyncio.timeout(self._timeout_seconds):
                try:
                    revoked = await self._store.revoke_task(task_id, now=now)
                except Exception as error:
                    logger.error(
                        "Task approval revoke failed (%s)", type(error).__name__
                    )
                    raise ApprovalRevocationError(
                        "the open approvals of a task could not be revoked"
                    ) from None
                for approval_id in revoked:
                    await self._report_task_end(approval_id, now)
                    reported += 1
        except TimeoutError:
            if revoked is None:
                logger.error("Task approval revoke failed (%s)", "TimeoutError")
                raise ApprovalRevocationError(
                    "the open approvals of a task could not be revoked"
                ) from None
            # The approvals are revoked; only telling about them was cut off
            # (the run that would repeat it finds nothing left to revoke).
            logger.error(
                "Task approval revoke did not finish reporting in time "
                "(%s): %d of %d approvals reported",
                "TimeoutError",
                reported,
                len(revoked),
            )
            raise ApprovalRevocationError(
                "the approvals of a task were revoked, but not all of them "
                "were reported within the deadline"
            ) from None
        return len(revoked)

    async def _report_task_end(self, approval_id: uuid.UUID, now: datetime) -> None:
        """Event and audit row of one approval that was revoked with its task.

        Runs inside the deadline of :meth:`revoke_task`: nothing here restarts it.
        """
        try:
            record = await self._store.get(approval_id)
        except Exception:
            # Only what the audit row and the event say about the approval
            # is lost; the approval itself is revoked.
            record = None
        if record is not None:
            await self._emit(ApprovalEventKind.REVOKED, record, now)
        await self._audit_row(
            "tool.approval.revoke",
            True,
            "task_ended",
            approval_id,
            None if record is None else record.project_id,
            None,
            now,
        )

    async def revoke_on_task_end(self, event: object) -> None:
        """A ``TaskService`` listener: a task that was cancelled, failed or
        completed (``paw_backend.tasks.TERMINAL_STATES``) keeps no usable
        approval; nor does one that is re-opened (Retry / Restart of a failed
        or cancelled task) keep any that survived its end, and the run that
        starts asks again. Reads only ``task_id``, ``from_state`` and
        ``to_state`` of the event. A store failure is raised
        (:class:`ApprovalRevocationError`); ``TaskService`` logs it (type only)
        and the transition stays committed."""
        task_id = getattr(event, "task_id", None)
        if not isinstance(task_id, uuid.UUID):
            return
        if any(
            getattr(state, "value", state) in _TASK_END_STATES
            for state in (
                getattr(event, "to_state", None),
                getattr(event, "from_state", None),
            )
        ):
            await self.revoke_task(task_id)

    async def _emit(
        self, kind: ApprovalEventKind, record: ApprovalRecord, now: datetime
    ) -> None:
        await self._listeners.emit(
            ApprovalEvent(
                kind=kind,
                approval_id=record.approval_id,
                task_id=record.task_id,
                tool=record.tool,
                level=record.level,
                occurred_at=now,
            )
        )

    async def _audit_row(
        self,
        action: str,
        allowed: bool,
        reason: str,
        approval_id: uuid.UUID,
        project_id: uuid.UUID | None,
        actor: Principal | None,
        now: datetime,
    ) -> None:
        await record_event(
            self._audit,
            build_tool_event(
                action=action,
                allowed=allowed,
                reason=reason,
                correlation_id=uuid.uuid4(),
                occurred_at=now,
                resource_kind="tool_approval",
                resource_id=approval_id,
                project_id=project_id,
                actor_id=None if actor is None else actor.user_id,
                actor_role=None if actor is None else actor.system_role.value,
            ),
            self._timeout_seconds,
        )

    async def _decide(
        self, approval_id: uuid.UUID, approver: Principal, *, approve: bool
    ) -> ApprovalResult:
        if not isinstance(approval_id, uuid.UUID) or not isinstance(
            approver, Principal
        ):
            return ApprovalResult(ApprovalOutcome.INVALID)
        now = self._clock()
        store = _StoreDeadline(self._timeout_seconds)
        try:
            record = await store.call(self._store.get, approval_id)
        except Exception as error:
            logger.error("Approval lookup failed (%s)", type(error).__name__)
            return ApprovalResult(ApprovalOutcome.UNAVAILABLE, approval_id)

        internal = await self._outcome(
            record, approval_id, approver, approve, now, store
        )
        # A stranger is told "not found", not that the approval exists.
        outcome = (
            ApprovalOutcome.NOT_FOUND
            if internal is ApprovalOutcome.NOT_AUTHORISED
            else internal
        )
        result = ApprovalResult(outcome, approval_id)
        if record is not None and outcome in (
            ApprovalOutcome.APPROVED,
            ApprovalOutcome.REJECTED,
        ):
            await self._emit(
                ApprovalEventKind.APPROVED
                if outcome is ApprovalOutcome.APPROVED
                else ApprovalEventKind.REJECTED,
                record,
                now,
            )
        await self._audit_row(
            "tool.approval.approve" if approve else "tool.approval.reject",
            result.decided,
            internal.value,
            approval_id,
            None if record is None else record.project_id,
            approver,
            now,
        )
        return result

    async def _outcome(
        self,
        record: ApprovalRecord | None,
        approval_id: uuid.UUID,
        approver: Principal,
        approve: bool,
        now: datetime,
        store: _StoreDeadline,
    ) -> ApprovalOutcome:
        if record is None:
            return ApprovalOutcome.NOT_FOUND
        if approver.user_id == record.agent_id:
            return ApprovalOutcome.SELF_APPROVAL
        if approver.user_id != record.requester_user_id:
            return ApprovalOutcome.NOT_AUTHORISED
        stepped_up = False
        if (
            approve
            and record.level is ApprovalLevel.STRONG_APPROVAL
            and diagnose_decide(record, approver.user_id, now, step_up_verified=True)
            is DecideOutcome.DECIDED
        ):
            if not await self._stepped_up(approver.user_id, approval_id):
                return ApprovalOutcome.STEP_UP_REQUIRED
            stepped_up = True
        try:
            result = await store.call(
                self._store.decide,
                approval_id,
                approver_id=approver.user_id,
                approve=approve,
                now=now,
                step_up_verified=stepped_up,
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
