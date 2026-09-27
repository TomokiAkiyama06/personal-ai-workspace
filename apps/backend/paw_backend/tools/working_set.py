"""The Working Set as the Tool Broker sees it (issue #85, Decision 0030).

* :data:`WORKING_SET_TOOL_SPECS`: one tool per change of a Working Set. The
  resulting role is the tool's (its ``working_set_operation``), never an
  argument, so that each tool's minimum level is its operation's: adding a
  ``referenced`` repository is ``SCOPED_AUTO``, every other change
  ``STRONG_APPROVAL`` (``ToolSpec`` refuses a lower one).
* The seams the broker uses, each fail-closed when it is not wired:
  :class:`WorkingSetRegistrations` (the registered ACL of a repository that is
  not in the task's scope yet: ``RepositoryService.working_set_acl``) and
  :class:`RepositoryUseGate` (admits every allowed call that touches a
  repository on the roles stored now, marks a write or an execution as a
  change and reserves the repositories until the call ended:
  ``TaskService.admit_repository_use`` / ``release_repository_use``).
* :class:`WorkingSetExecutor`: runs an allowed Working Set tool call by calling
  ``TaskService.change_working_set``, the same function a human's change goes
  through, which checks again under the task's row lock.
"""

import uuid
from collections.abc import Sequence
from types import MappingProxyType
from typing import Protocol

from paw_backend.authz import Capability, RepoAcl
from paw_backend.tasks import Actor, TaskRun, TaskService, WorkingSetOperation
from paw_backend.tasks.domain import role_after
from paw_backend.tasks.working_set import approval_level
from paw_backend.tools.calls import ToolInvocation
from paw_backend.tools.capabilities import ApprovalLevel, ToolCapability
from paw_backend.tools.registry import ArgumentKind, ArgumentSpec, ToolSpec

# The argument every Working Set tool takes: the repository the change is about.
REPOSITORY_ARGUMENT = "repository"

_O = WorkingSetOperation
TOOL_OPERATIONS: MappingProxyType[str, WorkingSetOperation] = MappingProxyType(
    {
        "task.working_set.add_referenced": _O.ADD_REFERENCED,
        "task.working_set.set_working": _O.SET_WORKING,
        "task.working_set.set_target": _O.SET_TARGET,
        "task.working_set.downgrade_to_working": _O.DOWNGRADE_TO_WORKING,
        "task.working_set.downgrade_to_referenced": _O.DOWNGRADE_TO_REFERENCED,
        "task.working_set.remove": _O.REMOVE,
    }
)
del _O
if set(TOOL_OPERATIONS.values()) != set(WorkingSetOperation):  # pragma: no cover
    raise RuntimeError("every WorkingSetOperation needs its tool")


def _spec(name: str, operation: WorkingSetOperation) -> ToolSpec:
    return ToolSpec(
        name,
        # It changes what the task may do: a write, never a read that is AUTO.
        frozenset({ToolCapability.WRITE}),
        Capability.PROJECT_TASK_WORKING_SET_MANAGE,
        {REPOSITORY_ARGUMENT: ArgumentSpec(ArgumentKind.WORKING_SET_REPOSITORY)},
        min_level=ApprovalLevel(approval_level(operation)),
        working_set_operation=operation,
    )


WORKING_SET_TOOL_SPECS: tuple[ToolSpec, ...] = tuple(
    _spec(name, operation) for name, operation in TOOL_OPERATIONS.items()
)


class WorkingSetRegistrations(Protocol):
    """The stored ACL of a registered repository (``None``: not registered).

    The ACL names the repository's project: the broker requires it to be a
    project of the task's scope.
    """

    async def working_set_acl(self, repository_id: uuid.UUID) -> RepoAcl | None: ...


class FailClosedRegistrations:
    """No registrations are known: every Working Set change is refused."""

    async def working_set_acl(self, repository_id: uuid.UUID) -> RepoAcl | None:
        return None


class RepositoryUseGate(Protocol):
    """Admits a use of repositories by a task's run on the roles stored now, and
    marks a write or an execution as a change (``TaskService
    .admit_repository_use``). It raises ``RepositoryRoleUnresolvedError`` /
    ``RepositoryRoleInsufficientError`` for a use the stored role refuses; any
    other failure refuses the use too.

    A write or an execution returns the id of a reservation that keeps the
    repositories from being downgraded or removed until
    ``release_repository_use`` (the call ended) or its expiry: the admission
    holds through the execution. A read returns ``None``; a write for which no
    reservation is returned is refused (``repository_write_unrecorded``)."""

    async def admit_repository_use(
        self,
        task_id: uuid.UUID,
        run: TaskRun,
        repository_ids: Sequence[uuid.UUID],
        *,
        capability: Capability,
        executes: bool,
    ) -> uuid.UUID | None: ...

    async def release_repository_use(
        self, task_id: uuid.UUID, reservation_id: uuid.UUID
    ) -> None: ...


class FailClosedUseGate:
    """Nothing can be admitted, so no call that touches a repository is allowed."""

    async def admit_repository_use(
        self,
        task_id: uuid.UUID,
        run: TaskRun,
        repository_ids: Sequence[uuid.UUID],
        *,
        capability: Capability,
        executes: bool,
    ) -> uuid.UUID | None:
        raise RuntimeError("no repository use gate is configured")

    async def release_repository_use(
        self, task_id: uuid.UUID, reservation_id: uuid.UUID
    ) -> None:
        return None


class BaselineProvider(Protocol):
    """The commit a repository is at now, in the task's worktree (``None``: not
    known). It becomes the starting commit of a repository that joins the
    Working Set."""

    async def head_commit(
        self, task_id: uuid.UUID, repository_id: uuid.UUID
    ) -> str | None: ...


class UnknownBaseline:
    """No baseline is ever known (nothing can later be verified as discarded)."""

    async def head_commit(
        self, task_id: uuid.UUID, repository_id: uuid.UUID
    ) -> str | None:
        return None


class WorkingSetExecutor:
    """Runs an ALLOWED Working Set tool call (a ``ToolExecutor``).

    The change is made by ``TaskService.change_working_set`` on the role the
    call was decided on (the role in the invocation's task scope): if the stored
    role moved meanwhile it refuses (``WorkingSetConflictError``). The actor is
    the delegating user; the event also names the agent.
    """

    def __init__(
        self, tasks: TaskService, *, baselines: BaselineProvider | None = None
    ) -> None:
        if not isinstance(tasks, TaskService):
            raise TypeError("tasks must be a TaskService")
        self._tasks = tasks
        self._baselines: BaselineProvider = baselines or UnknownBaseline()

    async def execute(self, invocation: ToolInvocation) -> object:
        operation = TOOL_OPERATIONS.get(invocation.tool)
        if operation is None:
            raise ValueError("not a working set tool")
        context = invocation.context
        repository_id = uuid.UUID(str(invocation.arguments[REPOSITORY_ARGUMENT]))
        entry = context.scope.repository(repository_id)
        expected = entry.role if entry is not None else None
        starting_commit = None
        if role_after(operation) is not None:
            starting_commit = await self._baselines.head_commit(
                context.task_id, repository_id
            )
        await self._tasks.change_working_set(
            context.task_id,
            operation,
            repository_id,
            actor=Actor.user(context.delegator_id),
            expected_role=expected,
            starting_commit=starting_commit,
            agent_id=context.grant.agent_id,
        )
        new_role = role_after(operation)
        return {
            "repository": str(repository_id),
            "role": None if new_role is None else new_role.value,
        }
