"""PostgreSQL implementation of the approval store.

Every state change is **one conditional UPDATE** whose ``WHERE`` clause holds
the whole rule, so that of any number of concurrent callers (two browser tabs,
two backend processes, a retry) exactly one changes the row and the others match
nothing:

* approve / reject: ``status = 'pending' AND expires_at > now AND`` the approver
  is the delegating user and is not the agent (and, to approve a strong
  approval, a step-up was confirmed);
* consume: ``status = 'approved' AND expires_at > now AND`` every field of the
  binding (task, agent, user, tool, level, call hash) equals what was granted.
  With ``require_active_task`` the task's row is first read **locked**
  (``FOR SHARE``) in the same transaction, so a use and the end of the task are
  ordered, never crossed (Decision 0006, section 9);
* revoke: ``status IN ('pending', 'approved') AND expires_at > now``;
  ``revoke_task`` (the listener of a task's end) is one such statement, with its
  history rows, on an abortable connection that is shut down at a deadline.

The history row is written in the same transaction as the change. When an
update matches nothing, the row is read again only to *explain* the refusal
(``diagnose_*``); that explanation is never used to allow anything.

Opening a request is serialised **per (task, user)** by a transaction-scoped
advisory lock, so that the cap on open approvals holds under concurrency (a
count followed by an insert would let simultaneous requests all pass). The
lock is taken before the count and released with the transaction. With
``require_active_task`` the task's row is then read **locked** (``FOR SHARE``)
in the same transaction, like ``consume``: a request is never inserted for a
task whose end was committed, so the revocation that follows that end (which
only sees what was committed before it) cannot miss it.

The database enforces the same rules once more with triggers and CHECK
constraints (migration ``0031``): a wrong statement from a buggy or
compromised application fails there.
"""

import uuid
from datetime import datetime

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.tools.approval_types import (
    ApprovalBinding,
    ApprovalEventKind,
    ApprovalHistoryEntry,
    ApprovalRecord,
    ApprovalStatus,
    ConsumeOutcome,
    DecideOutcome,
    DecideResult,
    NewApproval,
    OpenLimits,
    OpenOutcome,
    OpenResult,
    RevokeOutcome,
    SummaryItem,
    diagnose_consume,
    diagnose_decide,
    diagnose_revoke,
)
from paw_backend.tools.capabilities import ApprovalLevel
from paw_backend.tools.models import ToolApprovalEventRow, ToolApprovalRow
from paw_backend.tools.scope import Target, TargetKind
from paw_backend.tools.task_state import TaskActivity, lock_task_activity

_OPEN = (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
# One statement, so that the revocation and its history are atomic without a
# transaction (it runs on an abortable, autocommit connection). The values are
# the fixed members of the enums; only ``task_id`` and ``now`` are parameters.
_REVOKE_TASK = f"""
WITH revoked AS (
    UPDATE tool_approvals
       SET status = '{ApprovalStatus.REVOKED.value}',
           revoked_at = %(now)s,
           revoked_by = NULL
     WHERE task_id = %(task_id)s
       AND status IN ('{_OPEN[0]}', '{_OPEN[1]}')
       AND expires_at > %(now)s
 RETURNING id
)
INSERT INTO tool_approval_events (approval_id, kind, created_at)
SELECT id, '{ApprovalEventKind.REVOKED.value}', %(now)s FROM revoked
RETURNING approval_id
"""
_OPEN_ATTEMPTS = 3


def _summary_json(items: tuple[SummaryItem, ...]) -> list[dict[str, str]]:
    return [{"name": i.name, "kind": i.kind, "value": i.value} for i in items]


def _summary(rows: list[dict[str, str]]) -> tuple[SummaryItem, ...]:
    return tuple(SummaryItem(r["name"], r["kind"], r["value"]) for r in rows)


def _record(row: ToolApprovalRow) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.id,
        task_id=row.task_id,
        project_id=row.project_id,
        agent_id=row.agent_id,
        requester_user_id=row.requester_user_id,
        tool=row.tool,
        level=ApprovalLevel(row.level),
        call_hash=row.call_hash,
        targets=tuple(
            Target(TargetKind(item["kind"]), item["value"]) for item in row.targets
        ),
        summary=_summary(row.summary),
        status=ApprovalStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        approver_id=row.approver_id,
        decided_at=row.decided_at,
        consumed_at=row.consumed_at,
        step_up_verified=row.step_up_verified,
        revoked_at=row.revoked_at,
        revoked_by=row.revoked_by,
    )


async def _add_event(
    session: AsyncSession,
    approval_id: uuid.UUID,
    kind: ApprovalEventKind,
    now: datetime,
    *,
    actor_user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    summary: tuple[SummaryItem, ...] | None = None,
) -> None:
    values: dict[str, object] = {
        "approval_id": approval_id,
        "kind": kind.value,
        "actor_user_id": actor_user_id,
        "agent_id": agent_id,
        "created_at": now,
    }
    if summary is not None:
        # Left out (SQL NULL) otherwise: a JSON ``null`` is not a missing summary.
        values["summary"] = _summary_json(summary)
    await session.execute(insert(ToolApprovalEventRow).values(**values))


async def _row(session: AsyncSession, approval_id: uuid.UUID) -> ToolApprovalRow | None:
    return (
        await session.execute(
            select(ToolApprovalRow).where(ToolApprovalRow.id == approval_id)
        )
    ).scalar_one_or_none()


async def _mark_expired(
    session: AsyncSession,
    now: datetime,
    *,
    approval_id: uuid.UUID | None = None,
    call_hash: str | None = None,
) -> None:
    """Move open approvals whose time has run out to ``expired`` (with history)."""
    condition = [
        ToolApprovalRow.status.in_(_OPEN),
        ToolApprovalRow.expires_at <= now,
    ]
    if approval_id is not None:
        condition.append(ToolApprovalRow.id == approval_id)
    if call_hash is not None:
        condition.append(ToolApprovalRow.call_hash == call_hash)
    expired = await session.execute(
        update(ToolApprovalRow)
        .where(*condition)
        .values(status=ApprovalStatus.EXPIRED.value)
        .returning(ToolApprovalRow.id)
        .execution_options(synchronize_session=False)
    )
    for expired_id in expired.scalars().all():
        await _add_event(session, expired_id, ApprovalEventKind.EXPIRED, now)


class PostgresApprovalStore:
    """Approvals in ``tool_approvals`` / ``tool_approval_events`` (migration 0031)."""

    def __init__(
        self, database: Database, *, revoke_timeout_seconds: float = 3.0
    ) -> None:
        if not revoke_timeout_seconds > 0:
            raise ValueError("revoke_timeout_seconds must be positive")
        self._database = database
        self._revoke_timeout_seconds = revoke_timeout_seconds

    async def open_request(
        self,
        new: NewApproval,
        *,
        now: datetime,
        limits: OpenLimits,
        require_active_task: bool = False,
    ) -> OpenResult:
        cooldown_start = now - limits.rejection_cooldown
        for _ in range(_OPEN_ATTEMPTS):
            async with self._database.session() as session, session.begin():
                # One request at a time per (task, user): the cap below is a
                # count, which only holds if nobody inserts between the count
                # and our insert.
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"tool_approvals:{new.task_id}:{new.requester_user_id}"},
                )
                if require_active_task:
                    # The task row is read **locked** in this very transaction,
                    # as ``consume`` does: an end that is in flight is waited
                    # for (and seen here), one that starts later waits for this
                    # transaction, so its revocation finds the request we insert.
                    # A check made before could be overtaken by the end, whose
                    # revocation had then already found nothing: the request
                    # created afterwards would be open for an ended task.
                    activity = await lock_task_activity(session, new.task_id)
                    if activity is not TaskActivity.ACTIVE:
                        return OpenResult(
                            OpenOutcome.TASK_NOT_ACTIVE
                            if activity is TaskActivity.ENDED
                            else OpenOutcome.TASK_UNKNOWN
                        )
                await _mark_expired(session, now, call_hash=new.call_hash)
                existing = (
                    await session.execute(
                        select(ToolApprovalRow).where(
                            ToolApprovalRow.call_hash == new.call_hash,
                            ToolApprovalRow.status.in_(_OPEN),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return OpenResult(OpenOutcome.EXISTING, _record(existing))
                rejected_lately = (
                    await session.execute(
                        select(ToolApprovalRow.id)
                        .where(
                            ToolApprovalRow.call_hash == new.call_hash,
                            ToolApprovalRow.status == ApprovalStatus.REJECTED.value,
                            ToolApprovalRow.decided_at > cooldown_start,
                        )
                        .limit(1)
                    )
                ).first()
                if rejected_lately is not None:
                    return OpenResult(OpenOutcome.COOLING_DOWN)
                open_now = (
                    await session.execute(
                        select(func.count())
                        .select_from(ToolApprovalRow)
                        .where(
                            ToolApprovalRow.task_id == new.task_id,
                            ToolApprovalRow.requester_user_id == new.requester_user_id,
                            ToolApprovalRow.status.in_(_OPEN),
                            ToolApprovalRow.expires_at > now,
                        )
                    )
                ).scalar_one()
                if open_now >= limits.max_pending:
                    return OpenResult(OpenOutcome.TOO_MANY_PENDING)
                inserted = await session.execute(
                    pg_insert(ToolApprovalRow)
                    .values(
                        id=new.approval_id,
                        task_id=new.task_id,
                        project_id=new.project_id,
                        agent_id=new.agent_id,
                        requester_user_id=new.requester_user_id,
                        tool=new.tool,
                        level=new.level.value,
                        call_hash=new.call_hash,
                        targets=[
                            {"kind": t.kind.value, "value": t.value}
                            for t in new.targets
                        ],
                        summary=_summary_json(new.summary),
                        status=ApprovalStatus.PENDING.value,
                        created_at=now,
                        expires_at=new.expires_at,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["call_hash"],
                        index_where=text("status IN ('pending', 'approved')"),
                    )
                    .returning(ToolApprovalRow.id)
                )
                if inserted.scalar_one_or_none() is not None:
                    await _add_event(
                        session,
                        new.approval_id,
                        ApprovalEventKind.REQUESTED,
                        now,
                        agent_id=new.agent_id,
                        summary=new.summary,
                    )
                    row = await _row(session, new.approval_id)
                    assert row is not None
                    return OpenResult(OpenOutcome.CREATED, _record(row))
            # Somebody else opened the same call first: read it on the next turn.
        raise RuntimeError("could not open an approval request")

    async def get(self, approval_id: uuid.UUID) -> ApprovalRecord | None:
        async with self._database.session() as session:
            row = await _row(session, approval_id)
            return None if row is None else _record(row)

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        approve: bool,
        now: datetime,
        step_up_verified: bool = False,
    ) -> DecideResult:
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        kind = ApprovalEventKind.APPROVED if approve else ApprovalEventKind.REJECTED
        # Only an approval carries a step-up; a strong one needs it.
        verified = approve and step_up_verified
        conditions = [
            ToolApprovalRow.id == approval_id,
            ToolApprovalRow.status == ApprovalStatus.PENDING.value,
            ToolApprovalRow.expires_at > now,
            ToolApprovalRow.requester_user_id == approver_id,
            ToolApprovalRow.agent_id != approver_id,
        ]
        if approve and not verified:
            conditions.append(
                ToolApprovalRow.level != ApprovalLevel.STRONG_APPROVAL.value
            )
        async with self._database.session() as session, session.begin():
            changed = await session.execute(
                update(ToolApprovalRow)
                .where(*conditions)
                .values(
                    status=status.value,
                    approver_id=approver_id,
                    decided_at=now,
                    step_up_verified=verified,
                )
                .returning(ToolApprovalRow.id)
                .execution_options(synchronize_session=False)
            )
            if changed.scalar_one_or_none() is not None:
                await _add_event(
                    session, approval_id, kind, now, actor_user_id=approver_id
                )
                row = await _row(session, approval_id)
                assert row is not None
                return DecideResult(DecideOutcome.DECIDED, _record(row))
            row = await _row(session, approval_id)
            outcome = diagnose_decide(
                None if row is None else _record(row),
                approver_id,
                now,
                approve=approve,
                step_up_verified=step_up_verified,
            )
            if outcome is DecideOutcome.DECIDED:
                # It changed under us after our update matched nothing: do not
                # claim a decision that this call did not make.
                outcome = DecideOutcome.NOT_PENDING
            elif outcome is DecideOutcome.EXPIRED:
                await _mark_expired(session, now, approval_id=approval_id)
            return DecideResult(outcome)

    async def consume(
        self,
        approval_id: uuid.UUID,
        binding: ApprovalBinding,
        *,
        now: datetime,
        require_active_task: bool = False,
    ) -> ConsumeOutcome:
        async with self._database.session() as session, session.begin():
            if require_active_task:
                # The task row is read **locked** in this very transaction: a
                # terminal transition that is in flight is waited for (and its
                # end is then seen here), one that starts later waits for this
                # transaction. So the use is ordered before or after the end of
                # the task, never across it (a check made earlier could be
                # overtaken by the end, and the consumption would then win
                # against the revocation that follows it).
                activity = await lock_task_activity(session, binding.task_id)
                if activity is not TaskActivity.ACTIVE:
                    row = await _row(session, approval_id)
                    outcome = diagnose_consume(
                        None if row is None else _record(row), binding, now
                    )
                    if outcome is ConsumeOutcome.CONSUMED:
                        # It could have been used: the task is why it is not.
                        # (A reason about the approval itself - revoked, used,
                        # for another call - is the more precise one.)
                        outcome = (
                            ConsumeOutcome.TASK_NOT_ACTIVE
                            if activity is TaskActivity.ENDED
                            else ConsumeOutcome.TASK_UNKNOWN
                        )
                    return outcome
            changed = await session.execute(
                update(ToolApprovalRow)
                .where(
                    ToolApprovalRow.id == approval_id,
                    ToolApprovalRow.status == ApprovalStatus.APPROVED.value,
                    ToolApprovalRow.expires_at > now,
                    ToolApprovalRow.task_id == binding.task_id,
                    ToolApprovalRow.agent_id == binding.agent_id,
                    ToolApprovalRow.requester_user_id == binding.requester_user_id,
                    ToolApprovalRow.tool == binding.tool,
                    ToolApprovalRow.level == binding.level.value,
                    ToolApprovalRow.call_hash == binding.call_hash,
                )
                .values(status=ApprovalStatus.CONSUMED.value, consumed_at=now)
                .returning(ToolApprovalRow.id)
                .execution_options(synchronize_session=False)
            )
            if changed.scalar_one_or_none() is not None:
                await _add_event(
                    session,
                    approval_id,
                    ApprovalEventKind.CONSUMED,
                    now,
                    agent_id=binding.agent_id,
                )
                return ConsumeOutcome.CONSUMED
            row = await _row(session, approval_id)
            outcome = diagnose_consume(
                None if row is None else _record(row), binding, now
            )
            if outcome is ConsumeOutcome.CONSUMED:
                # Consumable now, but it was not when our update ran: not ours.
                outcome = ConsumeOutcome.PENDING
            elif outcome is ConsumeOutcome.EXPIRED:
                await _mark_expired(session, now, approval_id=approval_id)
            return outcome

    async def revoke(
        self, approval_id: uuid.UUID, *, actor_id: uuid.UUID | None, now: datetime
    ) -> RevokeOutcome:
        async with self._database.session() as session, session.begin():
            changed = await session.execute(
                update(ToolApprovalRow)
                .where(
                    ToolApprovalRow.id == approval_id,
                    ToolApprovalRow.status.in_(_OPEN),
                    ToolApprovalRow.expires_at > now,
                )
                .values(
                    status=ApprovalStatus.REVOKED.value,
                    revoked_at=now,
                    revoked_by=actor_id,
                )
                .returning(ToolApprovalRow.id)
                .execution_options(synchronize_session=False)
            )
            if changed.scalar_one_or_none() is not None:
                await _add_event(
                    session,
                    approval_id,
                    ApprovalEventKind.REVOKED,
                    now,
                    actor_user_id=actor_id,
                )
                return RevokeOutcome.REVOKED
            row = await _row(session, approval_id)
            outcome = diagnose_revoke(None if row is None else _record(row), now)
            if outcome is RevokeOutcome.REVOKED:
                outcome = RevokeOutcome.NOT_OPEN  # changed under us: not ours
            return outcome

    async def revoke_task(
        self, task_id: uuid.UUID, *, now: datetime
    ) -> list[uuid.UUID]:
        """Revoke every open approval of a task, as one statement with a deadline.

        It runs on an abortable connection (``Database.fetch_abortable``), not on
        a pooled session: it is the listener of a task's end, ``TaskService``
        awaits it after the transition has committed, and a database that
        accepts the connection but stalls the statement must not hold up every
        cancel / complete / retry request. At ``revoke_timeout_seconds`` the
        socket is shut down and ``TimeoutError`` is raised; the statement may or
        may not have committed (it is idempotent, so it can simply be run
        again, and the broker refuses the task's approvals meanwhile).
        """
        rows = await self._database.fetch_abortable(
            _REVOKE_TASK,
            {"task_id": task_id, "now": now},
            timeout_seconds=self._revoke_timeout_seconds,
        )
        return [row[0] for row in rows]

    async def history(self, approval_id: uuid.UUID) -> list[ApprovalHistoryEntry]:
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(ToolApprovalEventRow)
                    .where(ToolApprovalEventRow.approval_id == approval_id)
                    .order_by(ToolApprovalEventRow.seq)
                )
            ).scalars()
            return [
                ApprovalHistoryEntry(
                    seq=row.seq,
                    approval_id=row.approval_id,
                    kind=ApprovalEventKind(row.kind),
                    actor_user_id=row.actor_user_id,
                    agent_id=row.agent_id,
                    occurred_at=row.created_at,
                    summary=None if row.summary is None else _summary(row.summary),
                )
                for row in rows
            ]
