"""PostgreSQL implementation of the approval store.

Every state change is **one conditional UPDATE** whose ``WHERE`` clause holds
the whole rule, so that of any number of concurrent callers (two browser tabs,
two backend processes, a retry) exactly one changes the row and the others match
nothing:

* approve / reject: ``status = 'pending' AND expires_at > now AND`` the approver
  is the delegating user and is not the agent;
* consume: ``status = 'approved' AND expires_at > now AND`` every field of the
  binding (task, agent, user, tool, level, call hash) equals what was granted.

The history row is written in the same transaction as the change. When an
update matches nothing, the row is read again only to *explain* the refusal
(``diagnose_*``); that explanation is never used to allow anything.
"""

import uuid
from datetime import datetime

from sqlalchemy import insert, select, text, update
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
    OpenResult,
    diagnose_consume,
    diagnose_decide,
)
from paw_backend.tools.capabilities import ApprovalLevel
from paw_backend.tools.models import ToolApprovalEventRow, ToolApprovalRow
from paw_backend.tools.scope import Target, TargetKind

_OPEN = (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
_OPEN_ATTEMPTS = 3


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
        status=ApprovalStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        approver_id=row.approver_id,
        decided_at=row.decided_at,
        consumed_at=row.consumed_at,
    )


async def _add_event(
    session: AsyncSession,
    approval_id: uuid.UUID,
    kind: ApprovalEventKind,
    now: datetime,
    *,
    actor_user_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
) -> None:
    await session.execute(
        insert(ToolApprovalEventRow).values(
            approval_id=approval_id,
            kind=kind.value,
            actor_user_id=actor_user_id,
            agent_id=agent_id,
            created_at=now,
        )
    )


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

    def __init__(self, database: Database) -> None:
        self._database = database

    async def open_request(self, new: NewApproval, *, now: datetime) -> OpenResult:
        for _ in range(_OPEN_ATTEMPTS):
            async with self._database.session() as session, session.begin():
                await _mark_expired(session, now, call_hash=new.call_hash)
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
                    )
                    row = await _row(session, new.approval_id)
                    assert row is not None
                    return OpenResult(_record(row), created=True)
                existing = (
                    await session.execute(
                        select(ToolApprovalRow).where(
                            ToolApprovalRow.call_hash == new.call_hash,
                            ToolApprovalRow.status.in_(_OPEN),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    return OpenResult(_record(existing), created=False)
            # The open request was consumed or expired between our conflict and
            # our read: start over (bounded).
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
    ) -> DecideResult:
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        kind = ApprovalEventKind.APPROVED if approve else ApprovalEventKind.REJECTED
        async with self._database.session() as session, session.begin():
            changed = await session.execute(
                update(ToolApprovalRow)
                .where(
                    ToolApprovalRow.id == approval_id,
                    ToolApprovalRow.status == ApprovalStatus.PENDING.value,
                    ToolApprovalRow.expires_at > now,
                    ToolApprovalRow.requester_user_id == approver_id,
                    ToolApprovalRow.agent_id != approver_id,
                )
                .values(status=status.value, approver_id=approver_id, decided_at=now)
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
                None if row is None else _record(row), approver_id, now
            )
            if outcome is DecideOutcome.DECIDED:
                # It changed under us after our update matched nothing: do not
                # claim a decision that this call did not make.
                outcome = DecideOutcome.NOT_PENDING
            elif outcome is DecideOutcome.EXPIRED:
                await _mark_expired(session, now, approval_id=approval_id)
            return DecideResult(outcome)

    async def consume(
        self, approval_id: uuid.UUID, binding: ApprovalBinding, *, now: datetime
    ) -> ConsumeOutcome:
        async with self._database.session() as session, session.begin():
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
                )
                for row in rows
            ]
