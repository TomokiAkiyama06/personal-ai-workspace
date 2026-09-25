"""PostgreSQL implementation of the approval store.

Every state change is **one conditional UPDATE** whose ``WHERE`` clause holds
the whole rule, so that of any number of concurrent callers (two browser tabs,
two backend processes, a retry) exactly one changes the row and the others match
nothing:

* approve / reject: ``status = 'pending' AND expires_at > now AND`` the approver
  is the delegating user and is not the agent (and, to approve a strong
  approval, a step-up was confirmed);
* consume: ``status = 'approved' AND expires_at > now AND`` every field of the
  binding (task, the task's **run**, agent, user, tool, level, call hash) equals
  what was granted. With ``require_active_task`` the task's row is first read
  **locked** (``FOR SHARE``) in the same transaction, and must be alive *and in
  the run of the binding*, so a use, the end of the task and a Retry / Restart
  are ordered, never crossed (Decision 0006, section 9);
* revoke: ``status IN ('pending', 'approved') AND expires_at > now``.

The history row is written in the same transaction as the change. When an
update matches nothing, the row is read again only to *explain* the refusal
(``diagnose_*``); that explanation is never used to allow anything.

What a **human's decision** and the listener of a **task's end** call is
bounded in time: ``get``, ``decide``, ``revoke`` and ``revoke_task`` are each a
single statement (the change and its history row in one CTE, so still atomic)
on an abortable connection outside the pool (``Database.fetch_abortable``),
which is shut down at a deadline instead of asking a stalled server to cancel:
a query on a pooled session, cancelled, waits for the server for about ten
seconds. A call that needs a second statement (the read that explains a refusal,
the marking of an expired approval) shares ONE deadline with the first. The
statement of a call that ran out of time may still finish on the server: it is
atomic, so it is applied completely or not at all, and repeating the call shows
which. ``open_request``, ``consume`` and ``history`` run in one pooled
transaction each (the broker bounds them with ``asyncio.timeout``, which on a
server that stops answering can take longer than its limit).

Opening a request is serialised **per (task, user)** by a transaction-scoped
advisory lock, so that the cap on open approvals holds under concurrency (a
count followed by an insert would let simultaneous requests all pass). The
lock is taken before the count and released with the transaction. With
``require_active_task`` the task's row is then read **locked** (``FOR SHARE``)
in the same transaction, like ``consume``: a request is never inserted for a
task whose end was committed, so the revocation that follows that end (which
only sees what was committed before it) cannot miss it, nor for a run that a
Retry / Restart has replaced. The open approvals of the task that belong to an
earlier run are revoked in the same transaction (as the system, with history):
nothing can use them any more, and one of them would otherwise keep the new run
from asking for the same call (at most one open approval per call).

The database enforces the same rules once more with triggers and CHECK
constraints (migration ``0031``): a wrong statement from a buggy or
compromised application fails there.
"""

import asyncio
import uuid
from datetime import datetime

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.tools.approval_types import (
    CONSUME_TASK_REFUSAL,
    OPEN_TASK_REFUSAL,
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
from paw_backend.tools.task_state import TaskActivity, TaskRun, lock_task_activity

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
# The columns of an approval, in the order of ``ApprovalRecord``'s fields.
_COLUMNS = (
    "id, task_id, task_attempt, task_retry_count, project_id, agent_id,"
    " requester_user_id, tool, level, call_hash, targets, summary, status,"
    " created_at, expires_at, approver_id, decided_at, consumed_at,"
    " step_up_verified, revoked_at, revoked_by"
)
_GET = f"SELECT {_COLUMNS} FROM tool_approvals WHERE id = %(id)s"
# One statement per change, like ``_REVOKE_TASK``: the update and its history row
# are atomic without a transaction, and it runs on an abortable connection. A
# data-modifying CTE runs to completion whether or not the main query reads it.
# The values are the fixed members of the enums; the ids, the time and the
# actor are parameters (``::uuid`` types a NULL actor).
_REVOKE = f"""
WITH revoked AS (
    UPDATE tool_approvals
       SET status = '{ApprovalStatus.REVOKED.value}',
           revoked_at = %(now)s,
           revoked_by = %(actor)s
     WHERE id = %(id)s
       AND status IN ('{_OPEN[0]}', '{_OPEN[1]}')
       AND expires_at > %(now)s
 RETURNING id
), event AS (
    INSERT INTO tool_approval_events (approval_id, kind, actor_user_id, created_at)
    SELECT id, '{ApprovalEventKind.REVOKED.value}', %(actor)s::uuid, %(now)s
      FROM revoked
)
SELECT id FROM revoked
"""
_MARK_EXPIRED = f"""
WITH expired AS (
    UPDATE tool_approvals
       SET status = '{ApprovalStatus.EXPIRED.value}'
     WHERE id = %(id)s
       AND status IN ('{_OPEN[0]}', '{_OPEN[1]}')
       AND expires_at <= %(now)s
 RETURNING id
)
INSERT INTO tool_approval_events (approval_id, kind, created_at)
SELECT id, '{ApprovalEventKind.EXPIRED.value}', %(now)s FROM expired
"""


def _decide_statement(approve: bool, *, without_strong: bool) -> str:
    """The conditional update of ``decide``: the whole rule is in its ``WHERE``.

    ``without_strong`` excludes a strong approval (approving one needs a step-up).
    """
    status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    kind = ApprovalEventKind.APPROVED if approve else ApprovalEventKind.REJECTED
    not_strong = (
        f" AND level <> '{ApprovalLevel.STRONG_APPROVAL.value}'"
        if without_strong
        else ""
    )
    return f"""
WITH decided AS (
    UPDATE tool_approvals
       SET status = '{status.value}',
           approver_id = %(approver)s,
           decided_at = %(now)s,
           step_up_verified = %(verified)s
     WHERE id = %(id)s
       AND status = '{ApprovalStatus.PENDING.value}'
       AND expires_at > %(now)s
       AND requester_user_id = %(approver)s
       AND agent_id <> %(approver)s{not_strong}
 RETURNING {_COLUMNS}
), event AS (
    INSERT INTO tool_approval_events (approval_id, kind, actor_user_id, created_at)
    SELECT id, '{kind.value}', %(approver)s::uuid, %(now)s FROM decided
)
SELECT {_COLUMNS} FROM decided
"""


_DECIDE = {
    (approve, without_strong): _decide_statement(approve, without_strong=without_strong)
    for approve in (True, False)
    for without_strong in (True, False)
}
_OPEN_ATTEMPTS = 3


def _summary_json(items: tuple[SummaryItem, ...]) -> list[dict[str, str]]:
    return [{"name": i.name, "kind": i.kind, "value": i.value} for i in items]


def _summary(rows: list[dict[str, str]]) -> tuple[SummaryItem, ...]:
    return tuple(SummaryItem(r["name"], r["kind"], r["value"]) for r in rows)


def _record(row: ToolApprovalRow) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.id,
        task_id=row.task_id,
        task_run=TaskRun(row.task_attempt, row.task_retry_count),
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


def _record_of_values(values: tuple) -> ApprovalRecord:
    """An ``ApprovalRecord`` from a row of ``_COLUMNS`` read on a raw connection."""
    (
        approval_id,
        task_id,
        task_attempt,
        task_retry_count,
        project_id,
        agent_id,
        requester_user_id,
        tool,
        level,
        call_hash,
        targets,
        summary,
        status,
        created_at,
        expires_at,
        approver_id,
        decided_at,
        consumed_at,
        step_up_verified,
        revoked_at,
        revoked_by,
    ) = values
    return ApprovalRecord(
        approval_id=approval_id,
        task_id=task_id,
        task_run=TaskRun(task_attempt, task_retry_count),
        project_id=project_id,
        agent_id=agent_id,
        requester_user_id=requester_user_id,
        tool=tool,
        level=ApprovalLevel(level),
        call_hash=call_hash,
        targets=tuple(
            Target(TargetKind(item["kind"]), item["value"]) for item in targets
        ),
        summary=_summary(summary),
        status=ApprovalStatus(status),
        created_at=created_at,
        expires_at=expires_at,
        approver_id=approver_id,
        decided_at=decided_at,
        consumed_at=consumed_at,
        step_up_verified=step_up_verified,
        revoked_at=revoked_at,
        revoked_by=revoked_by,
    )


class _Deadline:
    """ONE time limit for the statements of one store call: each gets what is left."""

    def __init__(self, seconds: float) -> None:
        self._loop = asyncio.get_running_loop()
        self._end = self._loop.time() + seconds

    def left(self) -> float:
        remaining = self._end - self._loop.time()
        if remaining <= 0:
            raise TimeoutError
        return remaining


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


async def _revoke_earlier_runs(
    session: AsyncSession, task_id: uuid.UUID, run: TaskRun, now: datetime
) -> None:
    """Revoke (as the system, with history) the task's open approvals whose run
    is not ``run``, the task's current run: what a failed revocation left behind
    when a Retry / Restart re-opened the task. Nothing could use them (``consume``
    requires the run), but one of them would keep the new run from asking for the
    same call. Runs in the transaction that holds the task's row locked."""
    revoked = await session.execute(
        update(ToolApprovalRow)
        .where(
            ToolApprovalRow.task_id == task_id,
            ToolApprovalRow.status.in_(_OPEN),
            ToolApprovalRow.expires_at > now,
            or_(
                ToolApprovalRow.task_attempt != run.attempt,
                ToolApprovalRow.task_retry_count != run.retry_count,
            ),
        )
        .values(status=ApprovalStatus.REVOKED.value, revoked_at=now, revoked_by=None)
        .returning(ToolApprovalRow.id)
        .execution_options(synchronize_session=False)
    )
    for revoked_id in revoked.scalars().all():
        await _add_event(session, revoked_id, ApprovalEventKind.REVOKED, now)


class PostgresApprovalStore:
    """Approvals in ``tool_approvals`` / ``tool_approval_events`` (migration 0031)."""

    def __init__(
        self,
        database: Database,
        *,
        revoke_timeout_seconds: float = 3.0,
        decision_timeout_seconds: float = 3.0,
    ) -> None:
        """``revoke_timeout_seconds`` bounds ``revoke_task``;
        ``decision_timeout_seconds`` bounds ``get``, ``decide`` and ``revoke``
        (each call as a whole, all its statements together)."""
        for name, value in (
            ("revoke_timeout_seconds", revoke_timeout_seconds),
            ("decision_timeout_seconds", decision_timeout_seconds),
        ):
            if not value > 0:
                raise ValueError(f"{name} must be positive")
        self._database = database
        self._revoke_timeout_seconds = revoke_timeout_seconds
        self._decision_timeout_seconds = decision_timeout_seconds

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
                    activity = await lock_task_activity(
                        session, new.task_id, new.task_run
                    )
                    if activity is not TaskActivity.ACTIVE:
                        return OpenResult(OPEN_TASK_REFUSAL[activity])
                await _mark_expired(session, now, call_hash=new.call_hash)
                if require_active_task:
                    # ``new.task_run`` was just found to be the task's current
                    # run (under the lock), so any other run is an earlier one.
                    await _revoke_earlier_runs(session, new.task_id, new.task_run, now)
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
                        task_attempt=new.task_run.attempt,
                        task_retry_count=new.task_run.retry_count,
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
        """The approval, read on an abortable connection within
        ``decision_timeout_seconds`` (``TimeoutError`` past it)."""
        return await self._read(approval_id, _Deadline(self._decision_timeout_seconds))

    async def _read(
        self, approval_id: uuid.UUID, deadline: _Deadline
    ) -> ApprovalRecord | None:
        rows = await self._database.fetch_abortable(
            _GET, {"id": approval_id}, timeout_seconds=deadline.left()
        )
        return None if not rows else _record_of_values(rows[0])

    async def _mark_expired_abortably(
        self, approval_id: uuid.UUID, now: datetime, deadline: _Deadline
    ) -> None:
        """Move an open approval that ran out of time to ``expired`` (with history)."""
        await self._database.execute_abortable(
            _MARK_EXPIRED,
            {"id": approval_id, "now": now},
            timeout_seconds=deadline.left(),
        )

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        approve: bool,
        now: datetime,
        step_up_verified: bool = False,
    ) -> DecideResult:
        """Decide as one atomic statement (the change and its history row) on an
        abortable connection; the whole call, with the read that explains a
        refusal, shares one ``decision_timeout_seconds`` (``TimeoutError`` past
        it: the statement may or may not have been applied, completely)."""
        deadline = _Deadline(self._decision_timeout_seconds)
        # Only an approval carries a step-up; a strong one needs it.
        verified = approve and step_up_verified
        statement = _DECIDE[(approve, approve and not verified)]
        rows = await self._database.fetch_abortable(
            statement,
            {
                "id": approval_id,
                "approver": approver_id,
                "now": now,
                "verified": verified,
            },
            timeout_seconds=deadline.left(),
        )
        if rows:
            return DecideResult(DecideOutcome.DECIDED, _record_of_values(rows[0]))
        record = await self._read(approval_id, deadline)
        outcome = diagnose_decide(
            record,
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
            await self._mark_expired_abortably(approval_id, now, deadline)
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
                # terminal transition, a Retry or a Restart that is in flight is
                # waited for (and its result is then seen here), one that starts
                # later waits for this transaction. So the use is ordered before
                # or after it, never across it (a check made earlier could be
                # overtaken, and the consumption would then win against the
                # revocation that follows the transition). ``ACTIVE`` means the
                # task is alive AND still in the run of the binding; the
                # approval's own run is compared by the UPDATE below.
                activity = await lock_task_activity(
                    session, binding.task_id, binding.task_run
                )
                if activity is not TaskActivity.ACTIVE:
                    row = await _row(session, approval_id)
                    outcome = diagnose_consume(
                        None if row is None else _record(row), binding, now
                    )
                    if outcome in (ConsumeOutcome.CONSUMED, ConsumeOutcome.SUPERSEDED):
                        # It could have been used, or is for another run than
                        # the caller's, which the task's state explains better:
                        # the task is why it is not used. (A reason about the
                        # approval itself - revoked, used, for another call -
                        # is the more precise one.)
                        outcome = CONSUME_TASK_REFUSAL[activity]
                    return outcome
            changed = await session.execute(
                update(ToolApprovalRow)
                .where(
                    ToolApprovalRow.id == approval_id,
                    ToolApprovalRow.status == ApprovalStatus.APPROVED.value,
                    ToolApprovalRow.expires_at > now,
                    ToolApprovalRow.task_id == binding.task_id,
                    ToolApprovalRow.task_attempt == binding.task_run.attempt,
                    ToolApprovalRow.task_retry_count == binding.task_run.retry_count,
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
        """Revoke as one atomic statement (the change and its history row) on an
        abortable connection, within ``decision_timeout_seconds`` for the whole
        call (see ``decide``)."""
        deadline = _Deadline(self._decision_timeout_seconds)
        rows = await self._database.fetch_abortable(
            _REVOKE,
            {"id": approval_id, "actor": actor_id, "now": now},
            timeout_seconds=deadline.left(),
        )
        if rows:
            return RevokeOutcome.REVOKED
        outcome = diagnose_revoke(await self._read(approval_id, deadline), now)
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
