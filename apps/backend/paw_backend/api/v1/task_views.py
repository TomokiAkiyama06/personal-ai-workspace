"""Reads of the Task / PR screens (issue #185, Decision 0067 Proposed).

What ``/api/v1/tasks`` and ``/api/v1/pull-requests`` show besides
``TaskService.restore``, the DAG store and the budget tracker: the lists, the
names of the projects and repositories, and who may see which of them. Read
only; no rule of the task lifecycle is decided here.

Who sees what (``project.read``, Decision 0067, 2):

* a task is listed only if its project is one the principal may read
  (:func:`readable_projects`: an accepted membership, a project that is Active or
  Archived), decided by the policy for each project;
* a repository of a task (its name, branch, worktree, review, pull request) is
  shown only if the principal may read THAT repository (its ACL override can
  deny it inside a project the principal belongs to); the others are left out,
  and so is a pull request of one.

The decisions are the Authorizer's policy, not audited: allowed reads of
``project.read`` are never recorded (``DENIED_ONLY``) and a repository that is
left out of a list is not a denied request.
"""

import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import Capability, Principal, ProjectState, RepoAcl, Resource
from paw_backend.authz.capabilities import RepoPermission
from paw_backend.authz.policy import Policy, decide
from paw_backend.projects.models import ProjectRow
from paw_backend.projects.records import ProjectStatus
from paw_backend.repositories.models import RepositoryRow
from paw_backend.tasks.domain import RepoRole, TaskCommand, TaskState, WaitReason
from paw_backend.tasks.models import (
    TaskAttemptRepositoryRow,
    TaskEventRow,
    TaskRepositoryRow,
    TaskRow,
)
from paw_backend.tasks.queueing.domain import BudgetPreset, Priority
from paw_backend.tasks.queueing.models import BudgetUsageRow, QueueEntryRow
from paw_backend.tasks.records import (
    EvaluationResult,
    PullRequestState,
    ReviewStatus,
)

# How many tasks / pull requests one list answers at most.
MAX_LIST_LIMIT = 200

_AUTHZ_STATE = {
    ProjectStatus.ACTIVE: ProjectState.ACTIVE,
    ProjectStatus.ARCHIVED: ProjectState.ARCHIVED,
    ProjectStatus.PENDING_DELETION: ProjectState.PENDING_DELETION,
}


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    id: uuid.UUID
    name: str
    state: ProjectState


@dataclass(frozen=True, slots=True)
class RepositoryInfo:
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    default_branch: str
    acl: RepoAcl


@dataclass(frozen=True, slots=True)
class TaskListItem:
    id: uuid.UUID
    project: ProjectInfo
    title: str
    state: TaskState
    wait_reason: WaitReason | None
    priority: Priority | None
    # The repository shown on the card: the first ``target`` the principal may
    # read, else the first readable one (``None``: none).
    repository: str | None
    started_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PullRequestItem:
    id: int
    number: int
    url: str
    state: PullRequestState
    task_id: uuid.UUID
    task_title: str
    repository: str
    branch: str | None
    base: str
    review: ReviewStatus
    evaluation: EvaluationResult
    merge_ready: bool
    updated_at: datetime


def may_read_project(
    principal: Principal, project: ProjectInfo, policy: Policy
) -> bool:
    resource = Resource.project(project.id, project.state)
    return decide(principal, Capability.PROJECT_READ, resource, policy=policy).allowed


def may_read_repository(
    principal: Principal,
    project: ProjectInfo,
    repository: RepositoryInfo,
    policy: Policy,
) -> bool:
    """``project.read`` on the repository: its stored ACL against the project of
    the task (a repository registered in another project is refused by the
    policy, ``repo_acl_mismatch``)."""
    resource = Resource.repository(project.id, project.state, repository.acl)
    return decide(principal, Capability.PROJECT_READ, resource, policy=policy).allowed


async def project_of_task(
    session: AsyncSession, task_id: uuid.UUID
) -> ProjectInfo | None:
    """The project of the task, or ``None``: no such task, or its project is not
    stored any more or Deleted (nobody may read it)."""
    row = (
        await session.execute(
            select(ProjectRow.id, ProjectRow.name, ProjectRow.status)
            .join(TaskRow, TaskRow.project_id == ProjectRow.id)
            .where(TaskRow.id == task_id)
        )
    ).one_or_none()
    return None if row is None else _project(row)


async def readable_projects(
    session: AsyncSession, principal: Principal, policy: Policy
) -> dict[uuid.UUID, ProjectInfo]:
    """The projects the principal may read, by id (memberships of the principal,
    which the session's provider read from the database)."""
    if not principal.project_roles:
        return {}
    rows = await session.execute(
        select(ProjectRow.id, ProjectRow.name, ProjectRow.status).where(
            ProjectRow.id.in_(list(principal.project_roles))
        )
    )
    found = (_project(row) for row in rows)
    return {
        project.id: project
        for project in found
        if project is not None and may_read_project(principal, project, policy)
    }


def _project(row) -> ProjectInfo | None:
    status = ProjectStatus(row.status)
    if status is ProjectStatus.DELETED:
        return None
    return ProjectInfo(row.id, row.name, _AUTHZ_STATE[status])


async def repositories(
    session: AsyncSession, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, RepositoryInfo]:
    """The registered repositories among ``ids`` (a purged one is not)."""
    if not ids:
        return {}
    rows = await session.execute(
        select(
            RepositoryRow.id,
            RepositoryRow.project_id,
            RepositoryRow.name,
            RepositoryRow.default_branch,
            RepositoryRow.acl_allowed,
        ).where(RepositoryRow.id.in_(list(ids)))
    )
    return {
        row.id: RepositoryInfo(
            row.id, row.project_id, row.name, row.default_branch, _acl(row)
        )
        for row in rows
    }


def _acl(row) -> RepoAcl:
    if row.acl_allowed is None:
        return RepoAcl.inherit(row.id, row.project_id)
    return RepoAcl.override(
        row.id, row.project_id, (RepoPermission(name) for name in row.acl_allowed)
    )


def readable_repositories(
    principal: Principal,
    project: ProjectInfo,
    found: Mapping[uuid.UUID, RepositoryInfo],
    policy: Policy,
) -> dict[uuid.UUID, RepositoryInfo]:
    return {
        repository_id: repository
        for repository_id, repository in found.items()
        if may_read_repository(principal, project, repository, policy)
    }


async def list_tasks(
    session: AsyncSession,
    principal: Principal,
    projects: Mapping[uuid.UUID, ProjectInfo],
    policy: Policy,
    *,
    limit: int,
) -> list[TaskListItem]:
    """The latest updated tasks of ``projects`` (newest first, at most ``limit``)."""
    if not projects:
        return []
    rows = (
        await session.execute(
            select(
                TaskRow.id,
                TaskRow.project_id,
                TaskRow.title,
                TaskRow.state,
                TaskRow.wait_reason,
                TaskRow.updated_at,
            )
            .where(TaskRow.project_id.in_(list(projects)))
            .order_by(TaskRow.updated_at.desc(), TaskRow.id)
            .limit(limit)
        )
    ).all()
    task_ids = [row.id for row in rows]
    members = await _members(session, task_ids)
    found = await repositories(
        session,
        {repository_id for entries in members.values() for repository_id, _ in entries},
    )
    priorities = await _priorities(session, task_ids)
    started = await _started(session, task_ids)
    items = []
    for row in rows:
        project = projects[row.project_id]
        readable = readable_repositories(principal, project, found, policy)
        items.append(
            TaskListItem(
                id=row.id,
                project=project,
                title=row.title,
                state=row.state,
                wait_reason=row.wait_reason,
                priority=priorities.get(row.id),
                repository=_card_repository(members.get(row.id, ()), readable),
                started_at=started.get(row.id),
                updated_at=row.updated_at,
            )
        )
    return items


def _card_repository(
    entries: Iterable[tuple[uuid.UUID, RepoRole]],
    readable: Mapping[uuid.UUID, RepositoryInfo],
) -> str | None:
    shown = [
        (repository_id, role)
        for repository_id, role in entries
        if repository_id in readable
    ]
    for repository_id, role in shown:
        if role is RepoRole.TARGET:
            return readable[repository_id].name
    return readable[shown[0][0]].name if shown else None


async def _members(
    session: AsyncSession, task_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, RepoRole]]]:
    """The Working Set of each task (repository, role), in the order added."""
    members: dict[uuid.UUID, list[tuple[uuid.UUID, RepoRole]]] = {}
    if not task_ids:
        return members
    rows = await session.execute(
        select(
            TaskRepositoryRow.task_id,
            TaskRepositoryRow.repository_id,
            TaskRepositoryRow.role,
        )
        .where(
            TaskRepositoryRow.task_id.in_(list(task_ids)),
            TaskRepositoryRow.removed_at.is_(None),
        )
        .order_by(TaskRepositoryRow.task_id, TaskRepositoryRow.seq)
    )
    for row in rows:
        members.setdefault(row.task_id, []).append((row.repository_id, row.role))
    return members


async def _priorities(
    session: AsyncSession, task_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, Priority]:
    """The priority of each task's latest queue entry (a task never queued has none)."""
    if not task_ids:
        return {}
    rows = await session.execute(
        select(QueueEntryRow.task_id, QueueEntryRow.priority)
        .where(QueueEntryRow.task_id.in_(list(task_ids)))
        .distinct(QueueEntryRow.task_id)
        .order_by(QueueEntryRow.task_id, QueueEntryRow.id.desc())
    )
    return {row.task_id: row.priority for row in rows}


async def _started(
    session: AsyncSession, task_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, datetime]:
    """When each task last started (its latest ``start`` event)."""
    if not task_ids:
        return {}
    rows = await session.execute(
        select(TaskEventRow.task_id, func.max(TaskEventRow.created_at).label("at"))
        .where(
            TaskEventRow.task_id.in_(list(task_ids)),
            TaskEventRow.command == TaskCommand.START,
        )
        .group_by(TaskEventRow.task_id)
    )
    return {row.task_id: row.at for row in rows}


async def started_at(session: AsyncSession, task_id: uuid.UUID) -> datetime | None:
    return (await _started(session, [task_id])).get(task_id)


async def priority_of(session: AsyncSession, task_id: uuid.UUID) -> Priority | None:
    return (await _priorities(session, [task_id])).get(task_id)


async def budget_preset(
    session: AsyncSession, task_id: uuid.UUID
) -> BudgetPreset | None:
    """The task's budget preset (every row of a task has the same one)."""
    return (
        await session.execute(
            select(BudgetUsageRow.preset)
            .where(BudgetUsageRow.task_id == task_id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def pull_request_ids(
    session: AsyncSession, task_id: uuid.UUID, attempt: int
) -> dict[uuid.UUID, int]:
    """The record id of each repository's state in the attempt (the id of its
    pull request on the PR screen)."""
    rows = await session.execute(
        select(
            TaskAttemptRepositoryRow.repository_id, TaskAttemptRepositoryRow.id
        ).where(
            TaskAttemptRepositoryRow.task_id == task_id,
            TaskAttemptRepositoryRow.attempt == attempt,
        )
    )
    return {row.repository_id: row.id for row in rows}


def is_merge_ready(
    *,
    task_state: TaskState,
    current_attempt: bool,
    pull_request: PullRequestState,
    review: ReviewStatus,
    evaluation: EvaluationResult,
) -> bool:
    """Merge Ready (Decision 0067, 4): the task completed (the Integration Gate
    completes it only after the tests, the Evaluator and the review passed and the
    pull request was delivered), the pull request belongs to the task's current
    attempt, is still open, and its repository's recorded review and evaluation
    passed. The merge itself stays the human's."""
    return (
        task_state is TaskState.COMPLETED
        and current_attempt
        and pull_request is PullRequestState.OPEN
        and review is ReviewStatus.APPROVED
        and evaluation is EvaluationResult.PASSED
    )


async def list_pull_requests(
    session: AsyncSession,
    principal: Principal,
    projects: Mapping[uuid.UUID, ProjectInfo],
    policy: Policy,
    *,
    limit: int,
) -> list[PullRequestItem]:
    """The pull requests the tasks of ``projects`` recorded (newest first, at most
    ``limit``), of the repositories the principal may read."""
    if not projects:
        return []
    columns = TaskAttemptRepositoryRow
    # The repository ACL is a policy decision, not SQL: decide it for every
    # (project, repository) pair that has a record first, so that the limit
    # counts readable records only.
    pairs = (
        await session.execute(
            select(TaskRow.project_id, columns.repository_id)
            .join(TaskRow, TaskRow.id == columns.task_id)
            .where(
                columns.pr_number.is_not(None),
                TaskRow.project_id.in_(list(projects)),
            )
            .distinct()
        )
    ).all()
    found = await repositories(session, {pair.repository_id for pair in pairs})
    readable = [
        (pair.project_id, pair.repository_id)
        for pair in pairs
        if (repository := found.get(pair.repository_id)) is not None
        and may_read_repository(
            principal, projects[pair.project_id], repository, policy
        )
    ]
    if not readable:
        return []
    rows = (
        await session.execute(
            select(
                columns.id,
                columns.task_id,
                columns.attempt,
                columns.repository_id,
                columns.branch,
                columns.review_status,
                columns.evaluation_result,
                columns.pr_number,
                columns.pr_url,
                columns.pr_state,
                columns.updated_at,
                TaskRow.project_id,
                TaskRow.title,
                TaskRow.state,
                TaskRow.attempt.label("task_attempt"),
            )
            .join(TaskRow, TaskRow.id == columns.task_id)
            .where(
                columns.pr_number.is_not(None),
                tuple_(TaskRow.project_id, columns.repository_id).in_(readable),
            )
            .order_by(columns.updated_at.desc(), columns.id.desc())
            .limit(limit)
        )
    ).all()
    items = []
    for row in rows:
        repository = found[row.repository_id]
        items.append(
            PullRequestItem(
                id=row.id,
                number=row.pr_number,
                url=row.pr_url,
                state=row.pr_state,
                task_id=row.task_id,
                task_title=row.title,
                repository=repository.name,
                branch=row.branch,
                base=repository.default_branch,
                review=row.review_status,
                evaluation=row.evaluation_result,
                merge_ready=is_merge_ready(
                    task_state=row.state,
                    current_attempt=row.attempt == row.task_attempt,
                    pull_request=row.pr_state,
                    review=row.review_status,
                    evaluation=row.evaluation_result,
                ),
                updated_at=row.updated_at,
            )
        )
    return items
