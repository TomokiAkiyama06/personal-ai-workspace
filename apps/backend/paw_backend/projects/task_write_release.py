"""Releasing, by hand, the repository write a crashed process left (issue #129).

Decision 0049 (it supersedes the last sentence of Decision 0035, section
5). The Tool Broker reserves every repository write (or execution) it admits
(``task_repository_writes``) until the executor returned; meanwhile the task does
not begin evaluation or complete and the repository is not downgraded or removed.
A process that crashed never releases its reservation, which then holds the task
until it expires (about 24 hours). :class:`TaskWriteReleaser` lets a person end
it earlier, in this order:

1. **Authorization** (audited, ``REQUIRED``): ``project.task.write_reservation
   .release`` on the task's project, decided by the ``Authorizer`` on the actor's
   system role and the membership row read here (an accepted member only, as
   ``ProjectService`` does). The project Manager and the Owner / Admin hold it; a
   Contributor, a Viewer and an Agent never do (not delegable). Only an Active
   project allows it (the policy's project state rule). A denial writes nothing
   but the audit event.
2. **The release** (``TaskService.release_stale_repository_write``), in ONE
   transaction under the task's row lock: refused while a worker holds a valid
   lease on the task's queue entry (the process may be alive), or when the
   reservation is not the task's or no longer holds. The repositories count as
   written (evaluation and review again) and a ``release_repository_write`` event
   records who, why and what.
3. **Authorization again, in that transaction** (independent review of #129,
   P2): the project row is locked ``FOR SHARE`` (every lifecycle change and every
   change of a membership holds it ``FOR UPDATE``), and the stored state and the
   actor's membership are read again and decided on the Authorizer's policy. An
   Archive, a Delete, a removal or a demotion that committed after step 1 makes
   the release roll back (the denial is audited then, as in step 1); one that
   comes later waits for the commit. The allowed decision of step 1 is not
   audited a second time.
4. **Passkey Step-up** of the actor's own session inside the policy's window,
   checked IN that transaction before its commit (``StepUpGuard``: it locks the
   session row, so a revocation cannot slip in between). Without one, everything
   of step 2 is rolled back (``StepUpRequiredError``).

The reason is required: it is what the person checked (the executor's process or
host is gone). Nothing here decides that the executor stopped; the lease is only
the evidence that is available, and the person answers for the rest.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import Authorizer, Capability, Principal, ProjectState, Resource
from paw_backend.authz.policy import Reason, decide
from paw_backend.db import Database
from paw_backend.projects import store
from paw_backend.projects.errors import (
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectStateError,
)
from paw_backend.projects.records import MemberStatus, ProjectStatus
from paw_backend.tasks import (
    Actor,
    InvalidCommandArgumentError,
    TaskEvent,
    TaskNotFoundError,
    TaskService,
)
from paw_backend.tasks.models import TaskRow
from paw_backend.tasks.service import _MAX_INTEGER, MAX_REASON_LENGTH, _text

CAPABILITY = Capability.PROJECT_TASK_WRITE_RESERVATION_RELEASE

_AUTHZ_STATE = {
    ProjectStatus.ACTIVE: ProjectState.ACTIVE,
    ProjectStatus.ARCHIVED: ProjectState.ARCHIVED,
    ProjectStatus.PENDING_DELETION: ProjectState.PENDING_DELETION,
}


@runtime_checkable
class StepUpCheck(Protocol):
    """Refuses unless the session has a recent Passkey Step-up (raises).

    ``paw_backend.auth.onboarding.common.StepUpGuard`` is the implementation: the
    same check as every other sensitive operation of an Owner or an Admin."""

    async def require_in(
        self,
        session: AsyncSession,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        now: datetime,
    ) -> None: ...


class TaskWriteReleaser:
    """Authorize, release, check the Step-up. See the module docstring."""

    def __init__(
        self,
        database: Database,
        *,
        tasks: TaskService,
        authorizer: Authorizer,
        step_up: StepUpCheck,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        for name, value, kind in (
            ("database", database, Database),
            ("tasks", tasks, TaskService),
            ("authorizer", authorizer, Authorizer),
        ):
            if not isinstance(value, kind):
                raise TypeError(f"{name} must be a {kind.__name__}")
        if not isinstance(step_up, StepUpCheck):
            raise TypeError("step_up must have require_in")
        self._database = database
        self._tasks = tasks
        self._authorizer = authorizer
        self._step_up = step_up
        self._clock = clock

    async def release(
        self,
        actor: Principal,
        task_id: uuid.UUID,
        reservation_id: uuid.UUID,
        *,
        reason: str,
        session_id: uuid.UUID,
        expected_version: int | None = None,
    ) -> TaskEvent:
        """Release the reservation ``reservation_id`` of the task by hand.

        Returns the ``release_repository_write`` event. Raises, before anything is
        written: ``InvalidCommandArgumentError`` (a wrong argument, a blank
        reason), ``TaskNotFoundError``, ``ProjectNotFoundError`` (no such project,
        or the actor is not a member and holds no system-role grant),
        ``ProjectPermissionDeniedError`` (denied; ``Reason.AUDIT_UNAVAILABLE`` when
        the decision could not be audited), ``ProjectStateError`` (the project is
        not Active); then, from the release itself,
        ``RepositoryWriteNotFoundError``, ``RepositoryWriteNotHeldError``,
        ``RepositoryWriteHolderAliveError``, ``TaskConflictError`` and
        ``StepUpRequiredError`` (or its subclass
        ``StepUpMethodInsufficientError``).
        """
        if not isinstance(actor, Principal):
            raise InvalidCommandArgumentError("actor must be a Principal")
        for name, value in (
            ("task_id", task_id),
            ("reservation_id", reservation_id),
            ("session_id", session_id),
        ):
            if not isinstance(value, uuid.UUID):
                raise InvalidCommandArgumentError(f"{name} must be a UUID")
        # Checked here too, so that a call refused for them leaves no audit event
        # (``TaskService`` checks every rule of them again). The reason goes
        # through the service's own check, so that the two cannot drift apart
        # (text a column cannot store included; Codex review of #149, P2).
        _text("reason", reason, MAX_REASON_LENGTH)
        if expected_version is not None and (
            type(expected_version) is not int
            or not 1 <= expected_version <= _MAX_INTEGER
        ):
            raise InvalidCommandArgumentError(
                f"expected_version must be from 1 to {_MAX_INTEGER}"
            )

        async with self._database.session() as session, session.begin():
            project_id = (
                await session.execute(
                    select(TaskRow.project_id).where(TaskRow.id == task_id)
                )
            ).scalar_one_or_none()
            if project_id is None:
                raise TaskNotFoundError()
            await self._authorize(session, actor, project_id)

        user_id = actor.user_id

        async def step_up(session: AsyncSession, _task: uuid.UUID, project: uuid.UUID):
            await self._authorize(session, actor, project, again=True)
            await self._step_up.require_in(
                session, session_id=session_id, user_id=user_id, now=self._clock()
            )

        return await self._tasks.release_stale_repository_write(
            task_id,
            reservation_id,
            actor=Actor.user(user_id),
            reason=reason,
            expected_version=expected_version,
            in_transaction=step_up,
        )

    async def _authorize(
        self,
        session: AsyncSession,
        actor: Principal,
        project_id: uuid.UUID,
        *,
        again: bool = False,
    ) -> None:
        """Ask the Authorizer on the stored project and membership (audited).

        ``again``: the check in the release's transaction (module docstring, step
        3). The project row is locked ``FOR SHARE`` and the decision is taken on
        the Authorizer's policy without an audit event; only a denial is asked of
        the Authorizer (so it is audited) and raised."""
        if again:
            project = await store.get_project_for_share(session, project_id)
        else:
            project = await store.get_project(session, project_id)
        if project is None or project.status is ProjectStatus.DELETED:
            if again:
                # Deleted after the first check allowed: record the denial it
                # became (Decision 0049, 2), still "not found" to the caller. The
                # Policy has no Deleted state; Pending deletion, the state a
                # project passes through before it, is denied the same way
                # (Codex review of #149, P2).
                member = await store.get_member(session, project_id, actor.user_id)
                roles = (
                    {project_id: member.role}
                    if member is not None and member.status is MemberStatus.ACTIVE
                    else {}
                )
                await self._authorizer.authorize(
                    Principal(actor.user_id, actor.system_role, roles),
                    CAPABILITY,
                    Resource.project(project_id, ProjectState.PENDING_DELETION),
                )
            raise ProjectNotFoundError()
        member = await store.get_member(session, project.id, actor.user_id)
        active = member is not None and member.status is MemberStatus.ACTIVE
        # The actor's project role comes from the row just read, never the caller.
        principal = Principal(
            actor.user_id,
            actor.system_role,
            {project.id: member.role} if active and member is not None else {},
        )
        resource = Resource.project(project.id, _AUTHZ_STATE[project.status])
        if (
            again
            and decide(
                principal, CAPABILITY, resource, policy=self._authorizer.policy
            ).allowed
        ):
            return
        decision = await self._authorizer.authorize(principal, CAPABILITY, resource)
        if decision.allowed:
            if again:  # pragma: no cover - the same policy decided otherwise
                raise ProjectPermissionDeniedError(Reason.CAPABILITY_NOT_GRANTED)
            return
        if decision.reason is Reason.AUDIT_UNAVAILABLE:
            raise ProjectPermissionDeniedError(decision.reason)
        # Only the project's state stands in the way: the same actor would be
        # allowed on an Active project. An Owner / Admin acts without membership,
        # so the state is theirs to learn (Codex review of #149, P2).
        state_only = decision.reason is Reason.PROJECT_STATE_FORBIDS and (
            decide(
                principal,
                CAPABILITY,
                Resource.project(project.id, ProjectState.ACTIVE),
                policy=self._authorizer.policy,
            ).allowed
        )
        if not active and not state_only:
            # Not even whether the project exists (as ``ProjectService``).
            raise ProjectNotFoundError()
        if decision.reason is Reason.PROJECT_STATE_FORBIDS:
            raise ProjectStateError(project.status)
        raise ProjectPermissionDeniedError(decision.reason)
