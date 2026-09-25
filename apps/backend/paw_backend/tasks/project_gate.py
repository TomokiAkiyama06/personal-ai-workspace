"""The Project state gate of the task lane (Issue #83, Decision 0008, section 8).

``REQUIREMENTS.md``: an Archived project starts no new Agent Task and a Pending
deletion project stops its tasks. The Authorizer (PAW-025) already refuses
``project.task.run`` in both states, but it decides BEFORE the command, in another
transaction: a Delete that begins between the authorization and the insert leaves
a task (or a queue entry) in a project that is being deleted. The gate closes that
race. ``TaskService.create_task`` / Retry / Restart and ``TaskQueue.enqueue`` call
``ProjectGate.require_active`` **inside the transaction of their own write**: the
implementation locks the project row with ``SELECT ... FOR SHARE`` and refuses
unless it is Active. Every lifecycle change of the project (Archive, Delete,
Restore) holds the row ``FOR UPDATE``, so the two are serialised:

* the write commits first: the lifecycle change waits for it, so the task (or
  entry) exists when the stop processor of a deleted project looks, and is stopped;
* the lifecycle change commits first: the write finds a project that is not Active
  and is refused (``ProjectNotActiveError``), before anything was written.

Layering. ``tasks`` must not import ``projects`` (the dependency runs the other
way: ``projects.task_stop`` drives the task lane). So the gate is a ``Protocol``
that the composition root injects (``TaskService(database, project_gate=...)`` and
``TaskQueue(database, project_gate=...)``); ``projects.task_gate.ProjectStateGate``
is the implementation. Nothing here knows the ``projects`` table. The gate takes the
caller's ``AsyncSession`` (never its own connection) so that the lock lives exactly
as long as the write's transaction. Not passing a gate disables the check: that is
for the tests and tools of the task lane that have no projects; **production
wiring must pass one** (PAW-034 builds the orchestrator, which is where the
services are constructed). Decision 0020 records why it is not mandatory.

Only the four ways to ADMIT new work are gated. Cancel, Fail, Stop Now, Pause and
every other command must work in any project state (the stop processor itself
issues Cancel in a Pending deletion project), and the progress of work that was
admitted earlier (Start, Resume, ...) is not this gate's business (Decision 0020).
"""

import uuid
from typing import Protocol

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
