"""What a task's end must undo, and how it is made to happen (issue #125).

When a task ends (Complete, Fail, Cancel: ``tasks.TERMINAL_STATES``) two things
belong to it and must not outlive it:

* its open tool approvals (Decision 0006, section 9:
  ``ApprovalService.revoke_task``);
* the ``session_only`` memories that came from it (REQUIREMENTS.md "Memory
  Freshness": never kept after a Task ends; Decision 0034:
  ``FreshnessMaintenance.end_task``).

The transaction boundary (Decision 0047, 1 and 2)
------------------------------------------------
Neither runs in the transaction of the terminal transition: both stores work on
connections of their own (the approval store on an abortable one with its own
deadline), and a slow cleanup must not hold up or roll back a Cancel. Instead:

1. **Right after the commit**: :meth:`TaskEndCleanup.on_task_event` is a
   ``TaskService`` listener. On a terminal transition it runs :meth:`finish`.
   A failure is logged by type (``TaskService`` does the same for any listener)
   and the transition stays committed.
2. **The retryable after-step**: :class:`TaskEndResidue` finds, in the stored
   state, terminal tasks that still have something to undo (an open approval, an
   active ``session_only`` memory with a source that names the task), and
   :meth:`TaskEndCleanup.sweep` finishes them. The maintenance loop
   (``freshness_loop.py``) runs it on a schedule. Nothing records that a cleanup
   is "due": the residue itself is the record, so a process that died between the
   commit and the listener, a listener that failed, a version that was locked
   (``SKIP LOCKED``) or a restart in the middle of a cleanup all leave residue the
   next sweep finds. Both steps are idempotent; running them twice changes nothing.

**Fenced to the end** (Decision 0047, 1): a task can be re-opened (Retry /
Restart) between the moment its end was seen (the listener's event, the sweep's
query) and the cleanup. The cleanup matches the task's approvals and memories by
the task id alone, so it would also revoke and retire what the **new** run
created. :meth:`TaskEndCleanup.finish` therefore holds the task row ``FOR SHARE``
(:meth:`TaskEndResidue.hold_ended`) while both steps run, and runs them only if
the task is terminal under that lock. Every transition locks the row ``FOR NO
KEY UPDATE`` (``TaskService._require_task``), so a re-opening waits until the
cleanup is over, and a cleanup that comes after a re-opening does nothing. The
wait is bounded (``FENCE_TIMEOUT_SECONDS`` to take the lock; the steps have
their own deadlines); a fence that could not be taken is a failed step, and the
sweep repeats it.

Meanwhile nothing left over can be used: the Tool Broker refuses an approval of a
task that can no longer act (``tools.task_state``), and the retrieval never offers
a ``session_only`` memory.

A task that is re-opened (Retry / Restart of a failed or cancelled one) is no
longer terminal: the sweep leaves it alone, and the approval listener
(``ApprovalService.revoke_on_task_end``, which also acts on a transition *from* a
terminal state) revokes what survived its end.
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from sqlalchemy import text

from paw_backend.db import Database
from paw_backend.memory.models import FreshnessPolicy, MemoryStatus, SourceType
from paw_backend.memory.versioning import FreshnessMaintenance
from paw_backend.orchestrator.errors import error_class_of
from paw_backend.orchestrator.limits import (
    MAX_END_TASK_ROUNDS,
    MAX_TASK_END_SWEEP,
)
from paw_backend.orchestrator.validation import check_int, check_uuid
from paw_backend.tasks import TERMINAL_STATES
from paw_backend.tools import ApprovalService
from paw_backend.tools.approval_types import ApprovalStatus

logger = logging.getLogger(__name__)

# The longest the memories of one ended task are retired for, in one call of
# :meth:`TaskEndCleanup.finish` (the approval revocation has its own deadline).
RETIRE_TIMEOUT_SECONDS = 10.0
# The longest :meth:`TaskEndResidue.hold_ended` waits for the task row (a
# transition in progress holds it) and for the database to answer.
FENCE_TIMEOUT_SECONDS = 5.0


def _sql_list(values) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


# The values written into the statements are the fixed members of the enums (like
# the approval store's statements); only the limit is a parameter.
_TERMINAL = _sql_list(state.value for state in TERMINAL_STATES)
_OPEN_APPROVALS = _sql_list(
    (ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value)
)
# Terminal tasks with an open, unexpired approval (the ones ``revoke_task`` revokes).
_WITH_OPEN_APPROVALS = text(
    "SELECT DISTINCT a.task_id FROM tool_approvals a"
    " JOIN tasks t ON t.id = a.task_id"
    f" WHERE a.status IN ({_OPEN_APPROVALS}) AND a.expires_at > now()"
    f" AND t.state IN ({_TERMINAL})"
    " AND (CAST(:after AS uuid) IS NULL OR a.task_id > :after)"
    " ORDER BY a.task_id LIMIT :limit"
)
# Terminal tasks named (by the canonical text of the id, as ``end_task`` matches
# it) by a task source of an active ``session_only`` version.
_WITH_SESSION_MEMORIES = text(
    "SELECT DISTINCT t.id FROM memory_versions v"
    " JOIN memory_sources s ON s.memory_version_id = v.id"
    " JOIN tasks t ON s.source_ref = t.id::text"
    f" WHERE v.status = '{MemoryStatus.ACTIVE.value}'"
    f" AND v.freshness_policy = '{FreshnessPolicy.SESSION_ONLY.value}'"
    f" AND s.source_type = '{SourceType.TASK.value}' AND t.state IN ({_TERMINAL})"
    " AND (CAST(:after AS uuid) IS NULL OR t.id > :after)"
    " ORDER BY t.id LIMIT :limit"
)


# The task row, only while it is terminal, locked against every transition.
_HOLD_ENDED = text(
    f"SELECT id FROM tasks WHERE id = :task_id AND state IN ({_TERMINAL}) FOR SHARE"
)


class TaskEndResidue:
    """Terminal tasks that still hold something their end must undo, and the
    fence that keeps a cleanup inside the task's end."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    async def task_ids(
        self, limit: int, *, after: uuid.UUID | None = None
    ) -> tuple[uuid.UUID, ...]:
        """At most ``limit`` ids (1 to ``MAX_TASK_END_SWEEP``), in id order,
        only those greater than ``after`` when it is given."""
        check_int("limit", limit, minimum=1, maximum=MAX_TASK_END_SWEEP)
        if after is not None:
            check_uuid("after", after)
        found: dict[uuid.UUID, None] = {}
        async with self._database.session() as session, session.begin():
            for statement in (_WITH_OPEN_APPROVALS, _WITH_SESSION_MEMORIES):
                rows = await session.execute(
                    statement, {"limit": limit, "after": after}
                )
                found.update(dict.fromkeys(rows.scalars()))
        return tuple(sorted(found))[:limit]

    @contextlib.asynccontextmanager
    async def hold_ended(self, task_id: uuid.UUID) -> AsyncIterator[bool]:
        """Yield whether ``task_id`` is terminal, holding its row ``FOR SHARE``
        until the block ends when it is (module docstring: no transition, a
        re-opening included, commits meanwhile). Raises ``TimeoutError`` (or the
        database's error) when the lock or the database took longer than
        ``FENCE_TIMEOUT_SECONDS``."""
        check_uuid("task_id", task_id)
        timeout_ms = int(FENCE_TIMEOUT_SECONDS * 1000)
        async with contextlib.AsyncExitStack() as stack:
            async with asyncio.timeout(FENCE_TIMEOUT_SECONDS):
                session = await stack.enter_async_context(self._database.session())
                await stack.enter_async_context(session.begin())
                await session.execute(text(f"SET LOCAL lock_timeout = {timeout_ms}"))
                row = await session.execute(_HOLD_ENDED, {"task_id": task_id})
                ended = row.scalar() is not None
            yield ended


@dataclass(frozen=True, slots=True)
class TaskEndReport:
    """What one :meth:`TaskEndCleanup.finish` did. ``failed`` names the steps
    that raised (``"fence"``, ``"approvals"``, ``"memories"``); ``done`` is
    ``not failed``. A task that is not terminal (any more) gets ``0, 0``."""

    task_id: uuid.UUID
    revoked_approvals: int
    retired_memories: int
    failed: tuple[str, ...] = ()

    @property
    def done(self) -> bool:
        return not self.failed


class TaskEndCleanup:
    """Undoes what belongs to an ended task (module docstring)."""

    def __init__(
        self,
        approvals: ApprovalService,
        freshness: FreshnessMaintenance,
        residue: TaskEndResidue,
    ) -> None:
        if not isinstance(approvals, ApprovalService):
            raise TypeError("approvals must be an ApprovalService")
        if not isinstance(freshness, FreshnessMaintenance):
            raise TypeError("freshness must be a FreshnessMaintenance")
        if not isinstance(residue, TaskEndResidue):
            raise TypeError("residue must be a TaskEndResidue")
        self._approvals = approvals
        self._freshness = freshness
        self._residue = residue
        # Where the next sweep resumes (:meth:`sweep`); ``None``: from the start.
        self._after: uuid.UUID | None = None

    async def on_task_event(self, event: object) -> None:
        """A ``TaskService`` listener (it replaces
        ``ApprovalService.revoke_on_task_end``, so the approvals are revoked once):

        * a transition **to** a terminal state: :meth:`finish`;
        * a transition **from** one (a Retry or Restart re-opened the task): the
          approvals that survived its end are revoked (the run that starts asks
          again); the memories of the ended run stay retired.

        Raises nothing for a step that failed: it is logged, and the sweep
        repeats it (a re-opened task is not terminal, but the Broker refuses the
        approvals of an earlier run anyway)."""
        task_id = getattr(event, "task_id", None)
        if not isinstance(task_id, uuid.UUID):
            return
        if getattr(event, "to_state", None) in TERMINAL_STATES:
            await self.finish(task_id)
        elif getattr(event, "from_state", None) in TERMINAL_STATES:
            try:
                await self._approvals.revoke_task(task_id)
            except Exception as error:
                logger.warning(
                    "Revoking a re-opened task's approvals failed (%s)",
                    error_class_of(error),
                )

    async def finish(self, task_id: uuid.UUID) -> TaskEndReport:
        """Revoke the task's open approvals and retire its ``session_only``
        memories, fenced to the task's end (module docstring): nothing is done
        unless the task is terminal while its row is held. Each step runs even
        when the other failed; a failed step is logged by type and named in the
        report. Idempotent."""
        check_uuid("task_id", task_id)
        try:
            async with self._residue.hold_ended(task_id) as ended:
                if not ended:
                    return TaskEndReport(task_id, 0, 0)
                return await self._undo(task_id)
        except Exception as error:
            logger.warning(
                "Fencing an ended task's cleanup failed (%s)", error_class_of(error)
            )
            return TaskEndReport(task_id, 0, 0, ("fence",))

    async def _undo(self, task_id: uuid.UUID) -> TaskEndReport:
        """The two steps of :meth:`finish` (the caller holds the fence)."""
        failed: list[str] = []
        revoked = retired = 0
        try:
            revoked = await self._approvals.revoke_task(task_id)
        except Exception as error:
            failed.append("approvals")
            logger.warning(
                "Revoking an ended task's approvals failed (%s)", error_class_of(error)
            )
        try:
            # ``end_task`` changes at most one batch per call; a full batch may
            # hide more. Bounded in rounds and in time (``TaskService`` awaits its
            # listeners: a stalled database must not hold up a Cancel), and the
            # sweep finds whatever is left.
            async with asyncio.timeout(RETIRE_TIMEOUT_SECONDS):
                for _ in range(MAX_END_TASK_ROUNDS):
                    changed = await self._freshness.end_task(task_id)
                    retired += changed
                    if changed == 0:
                        break
        except Exception as error:
            failed.append("memories")
            logger.warning(
                "Retiring an ended task's memories failed (%s)", error_class_of(error)
            )
        return TaskEndReport(task_id, revoked, retired, tuple(failed))

    async def sweep(self, limit: int = MAX_TASK_END_SWEEP) -> tuple[TaskEndReport, ...]:
        """Finish the terminal tasks that still hold residue (at most ``limit``).
        An error of the residue query itself propagates (the loop logs it).

        Each sweep resumes after the last task the previous one took (in id
        order) and starts over once a sweep found fewer than ``limit``: tasks
        whose cleanup keeps failing cannot hold every sweep and starve the tasks
        after them. The position is this object's only; a restart begins at the
        start, which is still correct (the residue is the record)."""
        task_ids = await self._residue.task_ids(limit, after=self._after)
        self._after = task_ids[-1] if len(task_ids) == limit else None
        reports = []
        for task_id in task_ids:
            reports.append(await self.finish(task_id))
        return tuple(reports)
