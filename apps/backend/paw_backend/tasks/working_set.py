"""The rules of a task's Working Set that need no database (issue #85, Decision 0030).

``TaskService`` applies them inside the transaction that holds the task's row lock
(so two changes, or a change and a Complete, are judged one after the other: #85
constraint 4); the Tool Broker uses the approval levels and the permissions below
to decide a change before it is made. Nothing here reads or writes anything.

* **Approval level** of each operation (section 3): only adding a repository as
  ``referenced`` is ``SCOPED_AUTO``; every other change (adding or promoting to
  ``working`` / ``target``, a downgrade, a removal) is ``STRONG_APPROVAL``.
* **The repository permission** the change needs on the repository itself, on top
  of ``project.task.working_set.manage`` on the task's project: ``read`` to add
  a ``referenced`` one, ``write`` to add or promote to ``working`` / ``target``,
  and for a downgrade or a removal the permission of the role it had before
  (``read`` for ``referenced``, ``write`` otherwise).
* **Discarded change** (section 3, #85 constraint 1): a repository the attempt
  could have changed is downgraded or removed only when the backend verified, in
  the repository, that the worktree is clean, HEAD is the repository's own
  starting commit, the branch was not pushed and no pull request is open. An
  unknown starting commit, an inspector that cannot tell, or one that fails, all
  refuse (fail-closed).
* **Completion** (section 5, #85 constraint 5): every ``target`` needs the
  evaluation PASSED, a pull request that is ``open`` or ``merged`` and the review
  ``approved``; a ``working`` repository that was changed needs the evaluation
  PASSED; a repository that was changed in the attempt keeps the obligations of
  the strongest role it held, after a downgrade or a removal too.
"""

import asyncio
import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

from paw_backend.authz import Capability, RepoPermission
from paw_backend.tasks.domain import RepoRole, WorkingSetOperation
from paw_backend.tasks.records import (
    EvaluationResult,
    PullRequestState,
    RepositoryChangeState,
    ReviewStatus,
)

logger = logging.getLogger(__name__)

# The approval level names of ``tools.capabilities.ApprovalLevel`` (the task lane
# does not import the tool lane: ``tools`` imports ``tasks``). ``tools.working_set``
# maps them and a test pins that both agree.
SCOPED_AUTO = "scoped_auto"
STRONG_APPROVAL = "strong_approval"

_LEVEL = {
    WorkingSetOperation.ADD_REFERENCED: SCOPED_AUTO,
    WorkingSetOperation.SET_WORKING: STRONG_APPROVAL,
    WorkingSetOperation.SET_TARGET: STRONG_APPROVAL,
    WorkingSetOperation.DOWNGRADE_TO_WORKING: STRONG_APPROVAL,
    WorkingSetOperation.DOWNGRADE_TO_REFERENCED: STRONG_APPROVAL,
    WorkingSetOperation.REMOVE: STRONG_APPROVAL,
}
if set(_LEVEL) != set(WorkingSetOperation):  # pragma: no cover
    raise RuntimeError("every WorkingSetOperation needs an approval level")

# A pull request that "was created" for a target (draft is not yet, closed without
# a merge did not reach the goal). Merging is a human's decision (AGENTS.md), so
# ``open`` is enough.
DELIVERED_PULL_REQUEST_STATES = frozenset(
    {PullRequestState.OPEN, PullRequestState.MERGED}
)


# What each role of the Working Set lets a call do on a repository, for the two
# capabilities that change one (Decision 0030, section 4.2). It applies on top of
# the repository's ACL (both must allow) and an approval cannot lift it.
# ``referenced`` reads only; ``working`` may also write; only a ``target`` may
# also get a pull request. Something executed in a repository (tests, a build, a
# command) needs ``working`` or ``target`` too (#85 constraint 2). The Tool Broker
# applies it on the caller's task scope first, and ``TaskService
# .admit_repository_use`` again on the roles stored now (the one truth, 4.6).
ROLE_GATED_CAPABILITIES = frozenset(
    {Capability.PROJECT_REPO_WRITE, Capability.PROJECT_PR_CREATE}
)
ROLE_WRITE_CEILING: MappingProxyType[RepoRole, frozenset[Capability]] = (
    MappingProxyType(
        {
            RepoRole.REFERENCED: frozenset(),
            RepoRole.WORKING: frozenset({Capability.PROJECT_REPO_WRITE}),
            RepoRole.TARGET: frozenset(
                {Capability.PROJECT_REPO_WRITE, Capability.PROJECT_PR_CREATE}
            ),
        }
    )
)
# The roles in which a repository may run what a tool executes.
EXECUTING_ROLES = frozenset({RepoRole.WORKING, RepoRole.TARGET})


def role_allows(role: RepoRole, capability: Capability, *, executes: bool) -> bool:
    """Whether ``role`` lets a call with ``capability`` (that executes something
    when ``executes``) use the repository (the ceiling above)."""
    if capability in ROLE_GATED_CAPABILITIES and (
        capability not in ROLE_WRITE_CEILING[role]
    ):
        return False
    return not executes or role in EXECUTING_ROLES


def marks_changed(capability: Capability, *, executes: bool) -> bool:
    """Whether an allowed use counts as a change of the repository (section 5): a
    repository write, or something executed in it (a command can write, and the
    backend cannot tell: fail-closed)."""
    return executes or capability in ROLE_GATED_CAPABILITIES


def approval_level(operation: WorkingSetOperation) -> str:
    """The least approval level of ``operation`` (a value of ``ApprovalLevel``)."""
    return _LEVEL[operation]


def required_permission(
    operation: WorkingSetOperation, current: RepoRole | None
) -> RepoPermission:
    """The permission the change needs on the repository (Decision 0030, 3.3).

    ``current`` is the role before the change (``None``: not in the Working Set).
    """
    if operation is WorkingSetOperation.ADD_REFERENCED:
        return RepoPermission.READ
    if operation in (WorkingSetOperation.SET_WORKING, WorkingSetOperation.SET_TARGET):
        return RepoPermission.WRITE
    # A downgrade or a removal: the permission of the role it had.
    if current is None or current is RepoRole.REFERENCED:
        return RepoPermission.READ
    return RepoPermission.WRITE


class RepositoryChangeInspector(Protocol):
    """Looks at a repository of a task's attempt, to verify a discarded change.

    Returns what it found, or ``None`` when it cannot tell (the worktree is gone,
    git fails, ...). It must look at the repository itself (git status, HEAD, the
    remote branch, the pull request), never at what the agent reported.
    """

    async def inspect(
        self, task_id: uuid.UUID, attempt: int, repository_id: uuid.UUID
    ) -> RepositoryChangeState | None: ...


class FailClosedChangeInspector:
    """The inspector of a backend that has none: nothing is ever verified."""

    async def inspect(
        self, task_id: uuid.UUID, attempt: int, repository_id: uuid.UUID
    ) -> RepositoryChangeState | None:
        return None


async def verify_discarded(
    inspector: RepositoryChangeInspector,
    *,
    task_id: uuid.UUID,
    attempt: int,
    repository_id: uuid.UUID,
    starting_commit: str | None,
    open_pull_request: bool,
    timeout_seconds: float,
) -> RepositoryChangeState | None:
    """What the inspector found when the change is verifiably discarded, else
    ``None``.

    Discarded means: the stored pull request is not open, the repository has a
    known starting commit, and the inspector (within ``timeout_seconds``) reports
    a clean worktree whose HEAD is that commit, a branch that was not pushed and
    no open pull request. Everything else, a failing inspector included, is
    "not discarded".
    """
    if open_pull_request or starting_commit is None:
        return None
    try:
        async with asyncio.timeout(timeout_seconds):
            found = await inspector.inspect(task_id, attempt, repository_id)
    except Exception as error:
        # The type only: a git error can name paths.
        logger.warning("Repository inspection failed (%s)", type(error).__name__)
        return None
    if not isinstance(found, RepositoryChangeState):
        return None
    if (
        found.clean is True
        and found.head_commit == starting_commit
        and found.branch_pushed is False
        and found.open_pull_request is False
    ):
        return found
    return None


@dataclass(frozen=True, slots=True)
class AttemptRepositoryFacts:
    """What completion is judged on for one repository of the current attempt."""

    repository_id: uuid.UUID
    # The role in the Working Set now (``None``: removed from it).
    role: RepoRole | None
    starting_commit: str | None
    strongest_role: RepoRole
    modified: bool
    head_commit: str | None
    evaluation: EvaluationResult
    review: ReviewStatus
    pull_request: PullRequestState | None


def was_changed(facts: AttemptRepositoryFacts) -> bool:
    """Whether the attempt changed the repository, as far as the backend knows.

    A repository write was allowed on it, a pull request is recorded, or its
    recorded HEAD is not its starting commit (an unknown starting commit with a
    recorded HEAD cannot be told apart: changed, fail-closed).
    """
    if facts.modified or facts.pull_request is not None:
        return True
    if facts.head_commit is None:
        return False
    return facts.starting_commit is None or facts.head_commit != facts.starting_commit


def obligation(facts: AttemptRepositoryFacts) -> RepoRole | None:
    """The role whose completion requirements the repository must meet.

    ``target`` for a repository that is a target now; for a changed one, the
    strongest role it held in the attempt (a changed repository that only ever
    was ``referenced`` must still pass its evaluation); ``None`` otherwise.
    """
    if facts.role is RepoRole.TARGET:
        return RepoRole.TARGET
    if not was_changed(facts):
        return None
    strongest = facts.strongest_role
    if facts.role is not None and facts.role.strength > strongest.strength:
        strongest = facts.role
    if strongest is RepoRole.TARGET:
        return RepoRole.TARGET
    return RepoRole.WORKING


def meets(facts: AttemptRepositoryFacts, role: RepoRole) -> bool:
    """Whether the repository meets the completion requirements of ``role``."""
    if facts.evaluation is not EvaluationResult.PASSED:
        return False
    if role is not RepoRole.TARGET:
        return True
    return (
        facts.pull_request in DELIVERED_PULL_REQUEST_STATES
        and facts.review is ReviewStatus.APPROVED
    )


def unmet_repositories(
    facts: Iterable[AttemptRepositoryFacts],
) -> tuple[uuid.UUID, ...]:
    """The repositories that keep the task from completing (in the given order).

    Without any ``target`` in the Working Set nothing can complete: the task's own
    id is not a repository, so the caller checks that separately (``has_target``).
    """
    return tuple(
        item.repository_id
        for item in facts
        if (needed := obligation(item)) is not None and not meets(item, needed)
    )


def has_target(roles: Mapping[uuid.UUID, RepoRole]) -> bool:
    return any(role is RepoRole.TARGET for role in roles.values())
