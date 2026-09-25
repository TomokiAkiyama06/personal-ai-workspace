"""The Project state gate of the task lane (Issue #83, Decisions 0008 and 0020).

``REQUIREMENTS.md``: an Archived project starts no new Agent Task and a Pending
deletion project stops its tasks. The Authorizer (PAW-025) already refuses
``project.task.run`` in both states, but it decides BEFORE the command, in another
transaction: a Delete that begins between the authorization and the insert leaves
a task (or a queue entry) in a project that is being deleted. The gate closes that
race. ``TaskService.create_task`` / Retry / Restart / Start and ``TaskQueue.enqueue``
call ``ProjectGate.require_active`` **inside the transaction of their own write**:
the implementation locks the project row with ``SELECT ... FOR SHARE`` and refuses
unless it is Active. Every lifecycle change of the project (Archive, Delete,
Restore) holds the row ``FOR UPDATE``, so the two are serialised:

* the write commits first: the lifecycle change waits for it, so the task (or
  entry) exists when the stop processor of a deleted project looks, and is stopped;
* the lifecycle change commits first: the write finds a project that is not Active
  and is refused (``ProjectNotActiveError``), before anything was written.

Work that was admitted before an Archive or a Delete is kept from starting too
(Decision 0020, C, approved 2026-09-26): ``TaskQueue.claim_next`` skips every entry
whose task's project is not Active (``ProjectGate.active_condition`` supplies the
SQL condition; the entry stays queued and is claimable again once the project is
Active), and the Start command, the only way a queued task begins to run, is one of
the gated writes above. Work that already RUNS is not stopped: Pause, Resume, Wait,
Unblock, Begin evaluation, Complete, Fail, Cancel, Stop Now, and a lease's heartbeat,
release and completion never ask the gate (the stop processor itself issues Cancel
in a Pending deletion project).

Layering. ``tasks`` must not import ``projects`` (the dependency runs the other
way: ``projects.task_stop`` drives the task lane). So the gate is a ``Protocol``
that the composition root injects (``TaskService(database, project_gate=...)`` and
``TaskQueue(database, project_gate=...)``); ``projects.task_gate.ProjectStateGate``
is the implementation. Nothing here knows the ``projects`` table. The gate takes the
caller's ``AsyncSession`` (never its own connection) so that the lock lives exactly
as long as the write's transaction. **The gate is mandatory** (Decision 0020,
approved 2026-09-26): both constructors take ``project_gate`` as a required
keyword and refuse ``None``, so no composition can skip the check by leaving the
argument out. The task lane's own tests and the tools' tests, which have no
projects, pass a gate that admits everything and says so in its name; it is defined
in the tests' support module and nowhere in ``paw_backend`` (a test fails if a name
of it, or of that module, appears in production code).
"""

import uuid
from typing import Protocol

from sqlalchemy import ColumnElement
from sqlalchemy.ext.asyncio import AsyncSession


class ProjectGate(Protocol):
    async def require_active(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> None:
        """Lock the project row ``FOR SHARE`` in ``session`` and require it Active.

        The lock is held until the caller's transaction ends (the gate never
        commits). Raises ``ProjectNotActiveError`` for a project that is not Active
        and for one that does not exist; an implementation may raise its own error
        for a lock that could not be taken in time. ``session`` is inside a
        transaction that the caller began.
        """
        ...

    def active_condition(self, project_id: ColumnElement) -> ColumnElement[bool]:
        """A SQL condition, true when the project named by ``project_id`` is Active.

        ``project_id`` is a column of the caller's own statement (``tasks.project_id``).
        ``TaskQueue.claim_next`` puts the condition in its WHERE clause, inside an
        ``EXISTS`` over ``tasks``, so an entry of a project that is not Active (or
        does not exist) is skipped and stays queued. It takes no lock and reads
        nothing by itself: it is a piece of the caller's statement.
        """
        ...
