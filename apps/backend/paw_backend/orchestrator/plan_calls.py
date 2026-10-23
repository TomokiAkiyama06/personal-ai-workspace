"""The record of the planner's calls (issue #297, Decision 0099, 3).

A node's attempt records where it runs on its attempt row (Decision 0048,
``placement.py``). The planner has no DAG and no attempt row (the DAG is made
from the plan it returns), so its calls have their own table,
``agent_plan_calls`` (revision 0281):

* **The row** is made by ``PlanCallStore.start_in`` in the transaction that
  starts the call (``Orchestrator._start_planner_call``: the task running in
  this run, the call's step charged, the worker's queue lease proved with
  ``TaskQueue.heartbeat_in``). Its ``seq`` is the run's largest plus one, read
  in that transaction: the queue entry is locked by the proof, so a worker that
  took the run over, or a Retry (a new ``retry_count``), never collides with an
  earlier call. The ladder's rung stays the claim's own count (``claim_call``,
  Decision 0021, 3).
* **The placement** is recorded through :class:`PlanCallPlacementHandle`, the
  call's ``NodeAssignment.placement``, once, BEFORE the call runs there. In ONE
  transaction ``PlanCallStore.record_in`` checks (1) that the task runs in this
  run (task row share-locked), (2) that this worker still holds the queue lease
  (``TaskQueue.heartbeat_in``: the entry row locked until the commit) and (3)
  that the row is this worker's and the run's latest call, and only then writes
  the placement columns and, for the cloud, the ``audit_events`` row of the send
  (``audit.py``, ``reason`` ``cloud_planning``). Anything else writes nothing and
  the handle raises ``NodeStopped``: the runtime (``CloudGate``) does not send.
  ``RunGuard`` and ``AttemptFence`` are only the earlier, cheaper stops.
* **The fingerprint** of what is sent is the orchestrator's
  (``content_digest`` of what it handed the planner: the task's title and
  input), never the runtime's.
* A call whose run was replaced, or whose worker lost the lease, before its
  placement was recorded keeps a row whose placement columns are all NULL: the
  record of a call that started but sent nothing. Nobody rewrites or deletes it.

Lock order of the record: the task row (share), the queue entry, the call's
row. The start takes the task row, the budget rows and the entry, in that order
(as ``Orchestrator._start_runtime_with_lease``); nothing locks them the other
way round.

**The policy asked again** (Decision 0099, 2, condition 3): before a cloud
send is written, the record's transaction asks the store's ``cloud_send_check``
(``ConfiguredCloudPolicy.recheck_in``: the switch, then the active users counted
under the single-user lock taken shared), the same check as a node attempt's
placement (``DagStore.record_placement``; the orchestrator hands this store the
DAG store's check). It is asked for ``CLOUD`` only, after the fencing and before
anything is written; anything but ``True`` writes nothing and raises
``CloudSendRefusedError`` (``CloudGate``: ``CLOUD_POLICY_DENIED``). ``None``: no
check there (the policy's own ``allows`` is then the only one).
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.orchestrator.audit import (
    CLOUD_PLANNING_REASON,
    CloudSend,
    cloud_send_event,
    record_cloud_send,
)
from paw_backend.orchestrator.domain import ExecutionPlacement
from paw_backend.orchestrator.errors import (
    CloudSendRefusedError,
    InvalidOrchestratorArgumentError,
    NodeStopped,
    StalePlanCallError,
    StopReason,
)
from paw_backend.orchestrator.gateway import AttemptFence, RunGuard
from paw_backend.orchestrator.limits import MAX_PLAN_ATTEMPTS
from paw_backend.orchestrator.models import PlanCallRow
from paw_backend.orchestrator.store import CloudSendCheck
from paw_backend.orchestrator.validation import (
    MAX_INT32,
    check_agent_label,
    check_int,
    check_member,
    check_model,
    check_uuid,
    check_worker_id,
)
from paw_backend.tasks import (
    StaleRunError,
    TaskNotRunningError,
    TaskRun,
    TaskState,
)
from paw_backend.tasks.models import TaskRow
from paw_backend.tasks.queueing import LeaseLostError, QueueEntry, TaskQueue

_ENDED_TASK_STATES = frozenset(
    {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED}
)


@dataclass(frozen=True, slots=True)
class PlanCallRecord:
    """One row of ``agent_plan_calls``."""

    task_id: uuid.UUID
    run: TaskRun
    seq: int
    claim_call: int
    owner: str
    started_at: datetime
    placement: ExecutionPlacement | None
    placement_agent: str | None
    placement_model: str | None
    placed_at: datetime | None
    content_fingerprint: str | None
    content_bytes: int | None
    placement_audit_id: uuid.UUID | None


def _record(row: PlanCallRow) -> PlanCallRecord:
    return PlanCallRecord(
        task_id=row.task_id,
        run=TaskRun(row.task_attempt, row.retry_count),
        seq=row.seq,
        claim_call=row.claim_call,
        owner=row.owner,
        started_at=row.started_at,
        placement=row.placement,
        placement_agent=row.placement_agent,
        placement_model=row.placement_model,
        placed_at=row.placed_at,
        content_fingerprint=row.content_fingerprint,
        content_bytes=row.content_bytes,
        placement_audit_id=row.placement_audit_id,
    )


def _check_session(session: object) -> None:
    if not isinstance(session, AsyncSession) or not session.in_transaction():
        raise InvalidOrchestratorArgumentError("session")


def _check_run(run: object) -> TaskRun:
    if not isinstance(run, TaskRun):
        raise InvalidOrchestratorArgumentError("run")
    return run


class PlanCallStore:
    """SQL of ``agent_plan_calls`` (no policy; see the module)."""

    def __init__(
        self,
        database: Database,
        *,
        cloud_send_check: CloudSendCheck | None = None,
    ) -> None:
        if cloud_send_check is not None and not callable(cloud_send_check):
            raise TypeError("cloud_send_check must be callable")
        self._database = database
        self._cloud_send_check = cloud_send_check

    async def start_in(
        self,
        session: AsyncSession,
        task_id: uuid.UUID,
        run: TaskRun,
        *,
        claim_call: int,
        owner: str,
    ) -> int:
        """Make the row of a new call of the planner in the caller's
        transaction and return its ``seq`` (the run's largest plus one). The
        caller has proved its queue lease in the same transaction (the entry is
        locked until the commit), so no other call of the run is made
        meanwhile."""
        _check_session(session)
        check_uuid("task_id", task_id)
        _check_run(run)
        check_int("claim_call", claim_call, minimum=1, maximum=MAX_PLAN_ATTEMPTS)
        check_worker_id(owner, "owner")
        latest = (
            await session.execute(
                select(func.max(PlanCallRow.seq)).where(
                    PlanCallRow.task_id == task_id,
                    PlanCallRow.task_attempt == run.attempt,
                    PlanCallRow.retry_count == run.retry_count,
                )
            )
        ).scalar_one()
        row = PlanCallRow(
            task_id=task_id,
            task_attempt=run.attempt,
            retry_count=run.retry_count,
            seq=(latest or 0) + 1,
            claim_call=claim_call,
            owner=owner,
        )
        session.add(row)
        await session.flush()
        return row.seq

    async def record_in(
        self,
        session: AsyncSession,
        queue: TaskQueue,
        entry: QueueEntry,
        run: TaskRun,
        seq: int,
        *,
        owner: str,
        placement: ExecutionPlacement,
        agent: str,
        model: str,
        cloud: CloudSend | None = None,
    ) -> PlanCallRecord:
        """Record where the call ``seq`` of ``run`` runs, in the caller's
        transaction (module docstring). Raises, writing nothing:
        ``StaleRunError`` (the task ended, or another run replaced this one),
        ``TaskNotRunningError`` (it is paused or waiting), ``LeaseLostError``
        (``owner`` no longer holds the entry's lease), ``StalePlanCallError``
        (the row is not ``owner``'s, or a later call of the run was made),
        ``InvalidOrchestratorArgumentError("placement")`` (already recorded)
        and ``CloudSendRefusedError`` (the ``cloud_send_check`` refused a cloud
        send). The caller rolls the transaction back after any of them."""
        _check_session(session)
        if not isinstance(queue, TaskQueue):
            raise InvalidOrchestratorArgumentError("queue")
        if not isinstance(entry, QueueEntry):
            raise InvalidOrchestratorArgumentError("entry")
        _check_run(run)
        check_int("seq", seq, minimum=1, maximum=MAX_INT32)
        check_worker_id(owner, "owner")
        placement = check_member("placement", placement, ExecutionPlacement)
        check_agent_label("agent", agent)
        check_model("model", model)
        if (placement is ExecutionPlacement.CLOUD) != (cloud is not None):
            raise InvalidOrchestratorArgumentError("cloud")
        if cloud is not None and type(cloud) is not CloudSend:
            raise InvalidOrchestratorArgumentError("cloud")
        task_id = entry.task_id
        # (3) The task runs in this run: its row share-locked to the commit.
        task = (
            await session.execute(
                select(
                    TaskRow.state,
                    TaskRow.attempt,
                    TaskRow.retry_count,
                    TaskRow.project_id,
                    TaskRow.created_by,
                )
                .where(TaskRow.id == task_id)
                .with_for_update(read=True)
            )
        ).one_or_none()
        if (
            task is None
            or task.state in _ENDED_TASK_STATES
            or TaskRun(task.attempt, task.retry_count) != run
        ):
            raise StaleRunError()
        if TaskState(task.state) is not TaskState.RUNNING:
            raise TaskNotRunningError()
        # (1) This worker still holds the lease: the entry locked to the commit.
        await queue.heartbeat_in(session, entry.id, owner, entry.claim_count)
        # (2) The row is this worker's, and the run's latest call.
        row = (
            await session.execute(
                select(PlanCallRow)
                .where(
                    PlanCallRow.task_id == task_id,
                    PlanCallRow.task_attempt == run.attempt,
                    PlanCallRow.retry_count == run.retry_count,
                    PlanCallRow.seq == seq,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None or row.owner != owner:
            raise StalePlanCallError()
        latest = (
            await session.execute(
                select(func.max(PlanCallRow.seq)).where(
                    PlanCallRow.task_id == task_id,
                    PlanCallRow.task_attempt == run.attempt,
                    PlanCallRow.retry_count == run.retry_count,
                )
            )
        ).scalar_one()
        if latest != seq:
            raise StalePlanCallError()
        if row.placement is not None:
            raise InvalidOrchestratorArgumentError("placement")
        if cloud is not None:
            # Decision 0099, 2: the policy asked again under the single-user
            # lock (shared) before anything is written; a refusal writes
            # nothing and nothing is sent (module docstring).
            check = self._cloud_send_check
            if check is not None and await check(session) is not True:
                raise CloudSendRefusedError()
            # The audit row first: the call's columns are set together
            # afterwards (a flush in between would write a cloud placement
            # without its audit id, which the database refuses).
            audit_id = uuid.uuid4()
            await record_cloud_send(
                session,
                cloud_send_event(
                    cloud,
                    event_id=audit_id,
                    task_id=task_id,
                    project_id=task.project_id,
                    delegator_id=task.created_by,
                    reason=CLOUD_PLANNING_REASON,
                ),
            )
            row.content_fingerprint = cloud.content_fingerprint
            row.content_bytes = cloud.content_bytes
            row.placement_audit_id = audit_id
        row.placement = placement
        row.placement_agent = agent
        row.placement_model = model
        row.placed_at = func.now()
        await session.flush()
        await session.refresh(row)
        return _record(row)

    async def calls(self, task_id: uuid.UUID) -> tuple[PlanCallRecord, ...]:
        """The planner calls of a task, oldest first."""
        check_uuid("task_id", task_id)
        async with self._database.session() as session, session.begin():
            rows = (
                await session.execute(
                    select(PlanCallRow)
                    .where(PlanCallRow.task_id == task_id)
                    .order_by(
                        PlanCallRow.task_attempt,
                        PlanCallRow.retry_count,
                        PlanCallRow.seq,
                    )
                )
            ).scalars()
            return tuple(_record(row) for row in rows)


class PlanCallPlacementHandle:
    """The ``placement`` of one call of the planner (see the module)."""

    def __init__(
        self,
        guard: RunGuard,
        store: PlanCallStore,
        queue: TaskQueue,
        *,
        entry: QueueEntry,
        worker_id: str,
        seq: int,
        agent_id: uuid.UUID,
        content: tuple[str, int],
        fence: AttemptFence | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._guard = guard
        self._store = store
        self._queue = queue
        self._entry = entry
        self._worker_id = worker_id
        self._seq = seq
        self._agent_id = agent_id
        self._content = content
        self._fence = fence or AttemptFence()
        self._now = now or (lambda: datetime.now(UTC))
        self._recorded = False

    async def ensure_active(self) -> None:
        """Raise ``NodeStopped`` when the call can no longer act: abandoned (its
        fence is closed), or its run stopped, replaced or ended. Writes
        nothing."""
        self._fence.ensure_open()
        await self._guard.ensure_active()
        self._fence.ensure_open()

    async def record(
        self, placement: ExecutionPlacement, *, agent: str, model: str
    ) -> None:
        self._fence.ensure_open()
        if self._guard.stop_reason is not None:
            raise NodeStopped(self._guard.stop_reason)
        if self._recorded:
            raise InvalidOrchestratorArgumentError("placement")
        cloud = None
        if placement is ExecutionPlacement.CLOUD:
            fingerprint, size = self._content
            cloud = CloudSend(
                content_fingerprint=fingerprint,
                content_bytes=size,
                agent_id=self._agent_id,
                occurred_at=self._now(),
            )
        try:
            async with (
                self._queue.database.session() as session,
                session.begin(),
            ):
                await self._store.record_in(
                    session,
                    self._queue,
                    self._entry,
                    self._guard.run,
                    self._seq,
                    owner=self._worker_id,
                    placement=placement,
                    agent=agent,
                    model=model,
                    cloud=cloud,
                )
        except StaleRunError:
            await self._guard.refused()
        except TaskNotRunningError:
            # Paused or waiting: nothing new starts (Decision 0099, 3: the call
            # is placed only while the task runs). The run is not over.
            raise NodeStopped(StopReason.ABANDONED) from None
        except LeaseLostError:
            self._guard.stop(StopReason.LEASE_LOST)
            raise NodeStopped(StopReason.LEASE_LOST) from None
        except StalePlanCallError:
            # A later call of the run was made (by this worker after it gave
            # up on this call, or by another one): this call is over.
            raise NodeStopped(StopReason.ABANDONED) from None
        self._recorded = True
        # The call may have been abandoned while the write was on its way: the
        # record stays (the call was placed), but nothing more may act.
        self._fence.ensure_open()
