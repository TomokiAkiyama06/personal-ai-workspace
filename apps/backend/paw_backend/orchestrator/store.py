"""Persistence of the DAG, with fencing (PAW-034). SQL only: no policy.

**Fencing.** A worker that takes a DAG over (``acquire``) raises its ``epoch`` by
one and gets the new value; every write presents the epoch it holds. All writes
of one DAG run in a transaction that first locks the DAG row
(``SELECT ... FOR NO KEY UPDATE``), and ``acquire`` takes the same lock. So a
write and a take-over are ordered, never crossed: a worker that was replaced
(it lost its queue lease and another worker took the DAG over) holds an older
epoch and every one of its writes raises ``StaleDagEpochError`` without changing
anything, whether the take-over committed before the write started or while it
waited for the lock. The same lock serialises the writes of one DAG, so two nodes
that finish at the same moment cannot both miss that the other one is done (the
dependent that needs both is made ready by whichever commits second).

**Attempts.** A node is fenced a second time by its attempt: ``start_node`` raises
the node's ``attempt_count`` and returns it, and ``complete_node`` / ``fail_node``
must present it. After a take-over the node is ready again, a new start raises the
count, and the report of the old run (which is still executing somewhere) is a
``StaleNodeAttemptError``.

**Time.** Nothing here decides anything by a clock: timestamps are the database's
``now()`` and only describe.

The store validates every argument before it touches the database and never puts a
caller's text into an error. It does not authorise (the orchestrator's caller
does) and it does not decide what a failure means (``orchestrator.py``).
"""

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import Capability
from paw_backend.db import Database
from paw_backend.orchestrator.domain import (
    AttemptState,
    DagState,
    NextStep,
    NodeState,
)
from paw_backend.orchestrator.errors import (
    DagAlreadyExistsError,
    DagNotFoundError,
    DagStateError,
    InvalidOrchestratorArgumentError,
    NodeStateError,
    StaleDagEpochError,
    StaleNodeAttemptError,
)
from paw_backend.orchestrator.limits import (
    MAX_ATTEMPTS_PER_RUNG,
    MAX_ERROR_CLASS_CHARS,
    MAX_LADDER_LENGTH,
)
from paw_backend.orchestrator.models import (
    DagEdgeRow,
    DagNodeAttemptRow,
    DagNodeRow,
    DagRow,
)
from paw_backend.orchestrator.plan import Plan
from paw_backend.orchestrator.records import AttemptRecord, DagRecord, NodeRecord
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.scheduling import (
    DagVerdict,
    NodeView,
    dag_verdict,
    settle_states,
)
from paw_backend.orchestrator.validation import (
    MAX_INT32,
    check_int,
    check_label,
    check_member,
    check_signature,
    check_uuid,
    check_worker_id,
)
from paw_backend.tasks import TaskNotFoundError, TaskRun
from paw_backend.tasks.queueing.sql import (
    FOREIGN_KEY_VIOLATION,
    UNIQUE_VIOLATION,
    constraint_name,
    sqlstate,
)
from paw_backend.tasks.queueing.validation import MAX_APPROACH

ONE_DAG_PER_ATTEMPT = "uq_agent_dags_task_id"
_SETTLED_FOR_REOPEN = frozenset({NodeState.FAILED, NodeState.BLOCKED})


class _Locked:
    """A DAG whose row this transaction holds locked, with its nodes and edges."""

    def __init__(
        self,
        dag: DagRow,
        nodes: dict[str, DagNodeRow],
        depends: dict[str, tuple[str, ...]],
    ) -> None:
        self.dag = dag
        self.nodes = nodes
        self.depends = depends

    def views(self) -> list[NodeView]:
        ordered = sorted(self.nodes.values(), key=lambda row: row.ordinal)
        return [
            NodeView(
                row.key,
                row.ordinal,
                row.state,
                self.depends.get(row.key, ()),
                row.required,
            )
            for row in ordered
        ]

    def settle(self) -> None:
        """Apply the effects of the nodes' states on the states of the others."""
        for key, state in settle_states(self.views()).items():
            row = self.nodes[key]
            if row.state is not state:
                row.state = state
                row.updated_at = func.now()


def _node_record(row: DagNodeRow, depends_on: tuple[str, ...]) -> NodeRecord:
    return NodeRecord(
        key=row.key,
        ordinal=row.ordinal,
        role=row.role,
        title=row.title,
        goal=row.goal,
        input=dict(row.input),
        required=row.required,
        capabilities=(
            None
            if row.capabilities is None
            else tuple(Capability(name) for name in row.capabilities)
        ),
        repositories=(
            None
            if row.repositories is None
            else tuple(uuid.UUID(item) for item in row.repositories)
        ),
        depends_on=depends_on,
        state=row.state,
        agent_index=row.agent_index,
        approach=row.approach,
        attempt_count=row.attempt_count,
        rung_attempts=row.rung_attempts,
        result=None if row.result is None else NodeResult.from_json(row.result),
        error_class=row.error_class,
    )


def _attempt_record(row: DagNodeAttemptRow) -> AttemptRecord:
    return AttemptRecord(
        id=row.id,
        dag_id=row.dag_id,
        node_key=row.node_key,
        number=row.number,
        agent_index=row.agent_index,
        approach=row.approach,
        epoch=row.epoch,
        state=row.state,
        error_class=row.error_class,
        failure_signature=row.failure_signature,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


async def _dependencies(
    session: AsyncSession, dag_id: uuid.UUID
) -> dict[str, tuple[str, ...]]:
    rows = (
        await session.execute(select(DagEdgeRow).where(DagEdgeRow.dag_id == dag_id))
    ).scalars()
    found: dict[str, list[str]] = {}
    for edge in rows:
        found.setdefault(edge.node_key, []).append(edge.depends_on_key)
    return {key: tuple(sorted(items)) for key, items in found.items()}


async def _record(session: AsyncSession, dag: DagRow) -> DagRecord:
    # Re-read the DAG row: after a flush its server-set columns (``updated_at =
    # now()``) are expired, and touching them would lazily load from a coroutine.
    dag = (
        await session.execute(
            select(DagRow)
            .where(DagRow.id == dag.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    nodes = (
        (
            await session.execute(
                select(DagNodeRow)
                .where(DagNodeRow.dag_id == dag.id)
                .order_by(DagNodeRow.ordinal)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    depends = await _dependencies(session, dag.id)
    return DagRecord(
        id=dag.id,
        task_id=dag.task_id,
        attempt=dag.attempt,
        task_retry_count=dag.task_retry_count,
        state=dag.state,
        epoch=dag.epoch,
        owner=dag.owner,
        nodes=tuple(_node_record(row, depends.get(row.key, ())) for row in nodes),
        created_at=dag.created_at,
        updated_at=dag.updated_at,
    )


class DagStore:
    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    # -- creation and reading -----------------------------------------------

    async def create(self, task_id: uuid.UUID, attempt: int, plan: Plan) -> DagRecord:
        """Store an accepted plan as the DAG of ``attempt`` of the task.

        One transaction writes the DAG, the nodes and the edges. Nodes without a
        dependency start ``ready``, the others ``pending``. Raises
        ``TaskNotFoundError`` for an unknown task and ``DagAlreadyExistsError`` when
        the attempt already has a DAG (a plan is accepted once per attempt).
        """
        check_uuid("task_id", task_id)
        check_int("attempt", attempt, minimum=1, maximum=MAX_INT32)
        if not isinstance(plan, Plan):
            raise InvalidOrchestratorArgumentError("plan")
        dag_id = uuid.uuid4()
        views = [
            NodeView(
                node.key, ordinal, NodeState.PENDING, node.depends_on, node.required
            )
            for ordinal, node in enumerate(plan.nodes)
        ]
        initial = settle_states(views)
        try:
            async with self._database.session() as session, session.begin():
                session.add(
                    DagRow(
                        id=dag_id,
                        task_id=task_id,
                        attempt=attempt,
                        node_count=len(plan.nodes),
                        plan_bytes=plan.encoded_bytes,
                    )
                )
                await session.flush()
                for ordinal, node in enumerate(plan.nodes):
                    session.add(
                        DagNodeRow(
                            dag_id=dag_id,
                            key=node.key,
                            ordinal=ordinal,
                            role=node.role,
                            title=node.title,
                            goal=node.goal,
                            input=dict(node.input),
                            required=node.required,
                            capabilities=(
                                None
                                if node.capabilities is None
                                else [c.value for c in node.capabilities]
                            ),
                            repositories=(
                                None
                                if node.repositories is None
                                else [str(r) for r in node.repositories]
                            ),
                            state=initial[node.key],
                        )
                    )
                await session.flush()
                for key, dependency in plan.edges:
                    session.add(
                        DagEdgeRow(
                            dag_id=dag_id, node_key=key, depends_on_key=dependency
                        )
                    )
                await session.flush()
                dag = await session.get(DagRow, dag_id, populate_existing=True)
                return await _record(session, dag)
        except IntegrityError as error:
            if sqlstate(error) == FOREIGN_KEY_VIOLATION:
                raise TaskNotFoundError() from None
            if (
                sqlstate(error) == UNIQUE_VIOLATION
                and constraint_name(error) == ONE_DAG_PER_ATTEMPT
            ):
                raise DagAlreadyExistsError() from None
            raise

    async def get(self, task_id: uuid.UUID, attempt: int) -> DagRecord | None:
        """The DAG of the task attempt, or ``None``. One consistent read."""
        check_uuid("task_id", task_id)
        check_int("attempt", attempt, minimum=1, maximum=MAX_INT32)
        async with self._database.session() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            dag = (
                await session.execute(
                    select(DagRow).where(
                        DagRow.task_id == task_id, DagRow.attempt == attempt
                    )
                )
            ).scalar_one_or_none()
            return None if dag is None else await _record(session, dag)

    async def get_by_id(self, dag_id: uuid.UUID) -> DagRecord:
        """The DAG with this id (``DagNotFoundError`` when there is none)."""
        check_uuid("dag_id", dag_id)
        async with self._database.session() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            dag = await session.get(DagRow, dag_id)
            if dag is None:
                raise DagNotFoundError()
            return await _record(session, dag)

    async def attempts(
        self, dag_id: uuid.UUID, node_key: str | None = None
    ) -> tuple[AttemptRecord, ...]:
        """The node attempts of a DAG (of one node), oldest first."""
        check_uuid("dag_id", dag_id)
        if node_key is not None:
            check_label("node_key", node_key, maximum=32)
        query = select(DagNodeAttemptRow).where(DagNodeAttemptRow.dag_id == dag_id)
        if node_key is not None:
            query = query.where(DagNodeAttemptRow.node_key == node_key)
        async with self._database.session() as session, session.begin():
            rows = (
                await session.execute(query.order_by(DagNodeAttemptRow.id))
            ).scalars()
            return tuple(_attempt_record(row) for row in rows)

    # -- taking a DAG over ------------------------------------------------------

    async def acquire(self, dag_id: uuid.UUID, owner: str, run: TaskRun) -> DagRecord:
        """Take the DAG over for ``owner`` (a worker holding the queue lease).

        Adds 1 to the epoch: every write of an earlier owner is refused from now
        on. Also, in the same transaction:

        * the nodes an earlier owner left running (it died or lost its lease) are
          ready again, and their running attempts are closed as ``interrupted``;
        * when ``run`` is a later run of the task than the one that last opened the
          DAG (a Retry), the failed and blocked nodes are re-opened (their attempts
          on the rung start again from zero) and a failed DAG is active again;
        * the states of the nodes are settled again.

        ``run.attempt`` must be the DAG's attempt. A cancelled DAG cannot be taken
        over (``DagStateError``): a cancelled task is restarted, not retried.
        """
        check_uuid("dag_id", dag_id)
        check_worker_id(owner, "owner")
        if not isinstance(run, TaskRun):
            raise InvalidOrchestratorArgumentError("run")
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, None, require_active=False)
            dag = locked.dag
            if run.attempt != dag.attempt or dag.state is DagState.CANCELLED:
                raise DagStateError()
            dag.epoch += 1
            dag.owner = owner
            dag.updated_at = func.now()
            if run.retry_count > dag.task_retry_count:
                dag.task_retry_count = run.retry_count
                if dag.state is DagState.FAILED:
                    dag.state = DagState.ACTIVE
                for row in locked.nodes.values():
                    if row.state in _SETTLED_FOR_REOPEN:
                        row.state = NodeState.PENDING
                        row.rung_attempts = 0
                        row.error_class = None
                        row.finished_at = None
                        row.updated_at = func.now()
            await self._interrupt_running(session, locked, NodeState.READY)
            locked.settle()
            await session.flush()
            return await _record(session, dag)

    # -- the writes of the owner --------------------------------------------------

    async def start_node(
        self, dag_id: uuid.UUID, epoch: int, key: str, *, max_attempts: int
    ) -> AttemptRecord:
        """Start the next attempt of a ready node (on its current rung and approach).

        The node becomes ``running`` and its ``attempt_count`` grows by one: that is
        the number the owner presents when it reports the outcome. A node that
        already used ``max_attempts`` on this rung is refused (``NodeStateError``),
        as is a node that is not ready.
        """
        check_uuid("dag_id", dag_id)
        check_int("epoch", epoch, minimum=1, maximum=MAX_INT32)
        check_label("key", key, maximum=32)
        check_int(
            "max_attempts", max_attempts, minimum=1, maximum=MAX_ATTEMPTS_PER_RUNG
        )
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, epoch)
            row = locked.nodes.get(key)
            if row is None:
                raise NodeStateError()
            if row.state is not NodeState.READY or row.rung_attempts >= max_attempts:
                raise NodeStateError()
            row.state = NodeState.RUNNING
            row.attempt_count += 1
            row.rung_attempts += 1
            row.finished_at = None
            row.updated_at = func.now()
            attempt = DagNodeAttemptRow(
                dag_id=dag_id,
                node_key=key,
                number=row.attempt_count,
                agent_index=row.agent_index,
                approach=row.approach,
                epoch=epoch,
                state=AttemptState.RUNNING,
            )
            session.add(attempt)
            await session.flush()
            await session.refresh(attempt)
            return _attempt_record(attempt)

    async def complete_node(
        self,
        dag_id: uuid.UUID,
        epoch: int,
        key: str,
        attempt_number: int,
        result: NodeResult,
    ) -> DagRecord:
        """The attempt succeeded: store its result and make ready what waited for it."""
        check_uuid("dag_id", dag_id)
        check_int("epoch", epoch, minimum=1, maximum=MAX_INT32)
        check_label("key", key, maximum=32)
        check_int("attempt_number", attempt_number, minimum=1, maximum=MAX_INT32)
        if not isinstance(result, NodeResult):
            raise InvalidOrchestratorArgumentError("result")
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, epoch)
            row = self._running(locked, key, attempt_number)
            row.state = NodeState.SUCCEEDED
            row.result = result.to_json()
            row.error_class = None
            row.finished_at = func.now()
            row.updated_at = func.now()
            await self._close_attempt(
                session, dag_id, key, attempt_number, AttemptState.SUCCEEDED
            )
            locked.settle()
            await session.flush()
            return await _record(session, locked.dag)

    async def fail_node(
        self,
        dag_id: uuid.UUID,
        epoch: int,
        key: str,
        attempt_number: int,
        *,
        error_class: str,
        signature: str,
        step: NextStep,
        agent_index: int | None = None,
        approach: int | None = None,
    ) -> DagRecord:
        """The attempt failed: record how, and what happens to the node next.

        ``step`` (``NextStep``): ``retry`` and ``hold`` make the node ready again as
        it is; ``alternative`` and ``escalate`` make it ready on a new ``approach`` /
        rung (``agent_index`` and ``approach`` are then required, and never lower
        than the node's own; an escalation also starts the rung's attempt count
        again); ``give_up`` makes the node ``failed`` and blocks its dependents.
        """
        check_uuid("dag_id", dag_id)
        check_int("epoch", epoch, minimum=1, maximum=MAX_INT32)
        check_label("key", key, maximum=32)
        check_int("attempt_number", attempt_number, minimum=1, maximum=MAX_INT32)
        check_label("error_class", error_class, maximum=MAX_ERROR_CLASS_CHARS)
        check_signature(signature)
        step = check_member("step", step, NextStep)
        if step in (NextStep.ALTERNATIVE, NextStep.ESCALATE):
            check_int(
                "agent_index", agent_index, minimum=0, maximum=MAX_LADDER_LENGTH - 1
            )
            check_int("approach", approach, minimum=0, maximum=MAX_APPROACH)
        elif agent_index is not None:
            raise InvalidOrchestratorArgumentError("agent_index")
        elif approach is not None:
            raise InvalidOrchestratorArgumentError("approach")
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, epoch)
            row = self._running(locked, key, attempt_number)
            if step is NextStep.ALTERNATIVE:
                if agent_index != row.agent_index or approach <= row.approach:
                    raise NodeStateError()
                row.approach = approach
            elif step is NextStep.ESCALATE:
                if agent_index <= row.agent_index or approach <= row.approach:
                    raise NodeStateError()
                row.agent_index = agent_index
                row.approach = approach
                row.rung_attempts = 0
            row.error_class = error_class
            row.updated_at = func.now()
            if step is NextStep.GIVE_UP:
                row.state = NodeState.FAILED
                row.finished_at = func.now()
            else:
                row.state = NodeState.READY
            await self._close_attempt(
                session,
                dag_id,
                key,
                attempt_number,
                AttemptState.FAILED,
                error_class=error_class,
                signature=signature,
            )
            locked.settle()
            await session.flush()
            return await _record(session, locked.dag)

    async def give_up_node(
        self, dag_id: uuid.UUID, epoch: int, key: str, *, error_class: str
    ) -> DagRecord:
        """A ready node that can no longer be started (its rung is out of attempts)
        fails; its dependents are blocked. No attempt is involved."""
        check_uuid("dag_id", dag_id)
        check_int("epoch", epoch, minimum=1, maximum=MAX_INT32)
        check_label("key", key, maximum=32)
        check_label("error_class", error_class, maximum=MAX_ERROR_CLASS_CHARS)
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, epoch)
            row = locked.nodes.get(key)
            if row is None or row.state is not NodeState.READY:
                raise NodeStateError()
            row.state = NodeState.FAILED
            row.error_class = error_class
            row.finished_at = func.now()
            row.updated_at = func.now()
            locked.settle()
            await session.flush()
            return await _record(session, locked.dag)

    async def interrupt(self, dag_id: uuid.UUID, epoch: int) -> DagRecord:
        """Stop the running nodes without an outcome (a pause, a lost lease that
        noticed in time): their attempts are ``interrupted`` and the nodes ready."""
        check_uuid("dag_id", dag_id)
        check_int("epoch", epoch, minimum=1, maximum=MAX_INT32)
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, epoch, require_active=False)
            await self._interrupt_running(session, locked, NodeState.READY)
            locked.settle()
            await session.flush()
            return await _record(session, locked.dag)

    async def cancel(self, dag_id: uuid.UUID, epoch: int) -> DagRecord:
        """The task was cancelled: the DAG and every node that did not succeed is
        cancelled, running attempts are ``interrupted``. Nodes that succeeded (and
        failed ones) keep their state as the record of what happened. A DAG that
        had already succeeded or failed is left as it is."""
        check_uuid("dag_id", dag_id)
        check_int("epoch", epoch, minimum=1, maximum=MAX_INT32)
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, epoch, require_active=False)
            if locked.dag.state is not DagState.ACTIVE:
                # A DAG that already ended (succeeded or failed) is history: a
                # cancelled task does not rewrite what happened.
                return await _record(session, locked.dag)
            await self._interrupt_running(session, locked, NodeState.CANCELLED)
            for row in locked.nodes.values():
                if row.state in (
                    NodeState.PENDING,
                    NodeState.READY,
                    NodeState.BLOCKED,
                ):
                    row.state = NodeState.CANCELLED
                    row.finished_at = func.now()
                    row.updated_at = func.now()
            locked.dag.state = DagState.CANCELLED
            locked.dag.updated_at = func.now()
            await session.flush()
            return await _record(session, locked.dag)

    async def finalize(self, dag_id: uuid.UUID, epoch: int) -> DagRecord:
        """Close the DAG: ``succeeded`` when every required node succeeded,
        ``failed`` when nothing can run any more and one did not. Judged from the
        rows under the lock; ``DagStateError`` while a node is ready or running."""
        check_uuid("dag_id", dag_id)
        check_int("epoch", epoch, minimum=1, maximum=MAX_INT32)
        async with self._database.session() as session, session.begin():
            locked = await self._open(session, dag_id, epoch)
            verdict = dag_verdict(locked.views())
            if verdict is DagVerdict.ACTIVE:
                raise DagStateError()
            locked.dag.state = (
                DagState.SUCCEEDED
                if verdict is DagVerdict.SUCCEEDED
                else DagState.FAILED
            )
            locked.dag.updated_at = func.now()
            await session.flush()
            return await _record(session, locked.dag)

    # -- helpers ------------------------------------------------------------------

    async def _open(
        self,
        session: AsyncSession,
        dag_id: uuid.UUID,
        epoch: int | None,
        *,
        require_active: bool = True,
    ) -> _Locked:
        """Lock the DAG row, check the fence (``epoch``; ``None`` skips it: only
        ``acquire``) and load the nodes and edges."""
        dag = (
            await session.execute(
                select(DagRow)
                .where(DagRow.id == dag_id)
                .with_for_update(key_share=True)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if dag is None:
            raise DagNotFoundError()
        if epoch is not None and dag.epoch != epoch:
            raise StaleDagEpochError()
        if require_active and dag.state is not DagState.ACTIVE:
            raise DagStateError()
        rows = (
            await session.execute(
                select(DagNodeRow)
                .where(DagNodeRow.dag_id == dag_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
        nodes = {row.key: row for row in rows}
        return _Locked(dag, nodes, await _dependencies(session, dag_id))

    @staticmethod
    def _running(locked: _Locked, key: str, attempt_number: int) -> DagNodeRow:
        row = locked.nodes.get(key)
        if (
            row is None
            or row.state is not NodeState.RUNNING
            or row.attempt_count != attempt_number
        ):
            raise StaleNodeAttemptError()
        return row

    @staticmethod
    async def _close_attempt(
        session: AsyncSession,
        dag_id: uuid.UUID,
        key: str,
        number: int,
        state: AttemptState,
        *,
        error_class: str | None = None,
        signature: str | None = None,
    ) -> None:
        attempt = (
            await session.execute(
                select(DagNodeAttemptRow)
                .where(
                    DagNodeAttemptRow.dag_id == dag_id,
                    DagNodeAttemptRow.node_key == key,
                    DagNodeAttemptRow.number == number,
                    DagNodeAttemptRow.state == AttemptState.RUNNING,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if attempt is None:
            raise StaleNodeAttemptError()
        attempt.state = state
        attempt.error_class = error_class
        attempt.failure_signature = signature
        attempt.finished_at = func.now()

    @staticmethod
    async def _interrupt_running(
        session: AsyncSession, locked: _Locked, node_state: NodeState
    ) -> None:
        attempts = (
            await session.execute(
                select(DagNodeAttemptRow)
                .where(
                    DagNodeAttemptRow.dag_id == locked.dag.id,
                    DagNodeAttemptRow.state == AttemptState.RUNNING,
                )
                .execution_options(populate_existing=True)
            )
        ).scalars()
        for attempt in attempts:
            attempt.state = AttemptState.INTERRUPTED
            attempt.finished_at = func.now()
        for row in locked.nodes.values():
            if row.state is NodeState.RUNNING:
                row.state = node_state
                row.updated_at = func.now()
                if node_state is NodeState.CANCELLED:
                    row.finished_at = func.now()
