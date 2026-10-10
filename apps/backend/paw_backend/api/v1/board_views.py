"""Reads of the PR screen's and the mobile boards' panels (issue #185 item 6,
Decision 0078): the changed files and diff of a pull request, its
reviewers, the audit rows of its task, and the tool approvals a person is asked
for. Read only; who may see a pull request is ``task_views``' (Decision 0067, 2),
decided before any of these is read.

* **Changed files / diff**: what ``integration/changes.py`` stored when the pull
  request was delivered (``pull_request_changes``); nothing is read from GitHub
  or a worktree here.
* **Reviewers**: the reviewer nodes of the DAG of the record's attempt, where they
  ran and how they ended (no node goal or result, Decision 0067, 2; what a check
  said is never stored, ``integration/gate.py``).
* **Audit rows**: the ``audit_events`` rows whose resource is the task (the Tool
  Broker's decisions and executions) or one of its tool approvals, as a closed
  projection: when, the action, allow / deny, the reason code, and whether an
  agent, a person or the system acted. No actor id, request id or detail.
* **Tool approvals**: the pending, unexpired approvals the person is asked for
  (``requester_user_id``: only they may decide one, ``tools/approvals.py``), of
  projects they may read; what the approver is shown is the approval's stored
  ``summary`` (bounded and redacted when it was made, ``approval_types.py``) and
  the names of the repositories it names that they may read, and whether it may
  be granted for the rest of its task (Decision 0085).
* **Task-scoped grants** (「このタスクの間は許可」, Decision 0085): the active
  grants a person made for a task's current run, with their summary and how
  many calls each let run; only their own.
"""

import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.api.v1 import task_views as views
from paw_backend.authz import Principal
from paw_backend.authz.models import AuditEventRecord
from paw_backend.authz.policy import Policy
from paw_backend.tasks.models import PullRequestChangesRow, TaskRow
from paw_backend.tools.approval_types import ApprovalStatus
from paw_backend.tools.capabilities import ApprovalLevel
from paw_backend.tools.models import (
    ToolApprovalRow,
    ToolTaskGrantRow,
    ToolTaskGrantUseRow,
)
from paw_backend.tools.scope import TargetKind
from paw_backend.tools.task_grants import MAX_MAX_ACTIVE_GRANTS, TaskGrantStatus

MAX_AUDIT_ROWS = 100
MAX_APPROVALS = 100
_REPOSITORY_TARGETS = (
    TargetKind.REPOSITORY.value,
    TargetKind.WORKING_SET_REPOSITORY.value,
)


@dataclass(frozen=True, slots=True)
class ChangedFileView:
    index: int
    path: str
    previous_path: str | None
    status: str
    additions: int
    deletions: int
    has_patch: bool
    patch_truncated: bool


@dataclass(frozen=True, slots=True)
class ChangesView:
    head_commit: str
    truncated: bool
    recorded_at: datetime
    files: tuple[ChangedFileView, ...]


@dataclass(frozen=True, slots=True)
class AuditRow:
    occurred_at: datetime
    action: str
    decision: str
    reason: str
    # ``agent``: an agent acted for a person; ``person``: a person; ``system``:
    # the backend itself (an approval revoked when its task ended).
    actor: str


@dataclass(frozen=True, slots=True)
class ApprovalView:
    id: uuid.UUID
    task_id: uuid.UUID
    task_title: str
    agent: str | None
    project_id: uuid.UUID
    tool: str
    level: str
    summary: tuple[tuple[str, str, str], ...]
    repositories: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    # The broker stored how the call may be granted for the rest of its task
    # (「このタスクの間は許可」, Decision 0085): the sheet offers the button.
    task_grant_allowed: bool = False


@dataclass(frozen=True, slots=True)
class TaskGrantView:
    id: uuid.UUID
    approval_id: uuid.UUID
    tool: str
    summary: tuple[tuple[str, str, str], ...]
    created_at: datetime
    uses: int


def _file(index: int, item: dict, patch: object) -> ChangedFileView:
    return ChangedFileView(
        index=index,
        path=item["path"],
        previous_path=item.get("previous_path"),
        status=item["status"],
        additions=item["additions"],
        deletions=item["deletions"],
        has_patch=isinstance(patch, str),
        patch_truncated=bool(item.get("patch_truncated")),
    )


async def changes_of(session: AsyncSession, record_id: int) -> ChangesView | None:
    """The stored changes of the record (``None``: none were recorded). The
    patches stay in the database: only whether each file has one is read."""
    row = (
        await session.execute(
            select(
                PullRequestChangesRow.head_commit,
                PullRequestChangesRow.truncated,
                PullRequestChangesRow.recorded_at,
                PullRequestChangesRow.files,
                func.jsonb_path_query_array(
                    PullRequestChangesRow.patches, cast("$[*].type()", JSONPATH)
                ).label("kinds"),
            ).where(PullRequestChangesRow.record_id == record_id)
        )
    ).one_or_none()
    if row is None:
        return None
    files = tuple(
        ChangedFileView(
            index=index,
            path=item["path"],
            previous_path=item.get("previous_path"),
            status=item["status"],
            additions=item["additions"],
            deletions=item["deletions"],
            has_patch=kind == "string",
            patch_truncated=bool(item.get("patch_truncated")),
        )
        for index, (item, kind) in enumerate(zip(row.files, row.kinds, strict=True))
    )
    return ChangesView(row.head_commit, row.truncated, row.recorded_at, files)


async def file_of(
    session: AsyncSession, record_id: int, index: int
) -> tuple[ChangedFileView, str | None, int] | None:
    """One stored file with its patch, and how many files were stored; ``None``
    when the record has no changes or no such file."""
    row = (
        await session.execute(
            select(
                PullRequestChangesRow.files[index].label("file"),
                PullRequestChangesRow.patches[index].label("patch"),
                func.jsonb_array_length(PullRequestChangesRow.files).label("count"),
            ).where(PullRequestChangesRow.record_id == record_id)
        )
    ).one_or_none()
    if row is None or row.file is None:
        return None
    patch = row.patch if isinstance(row.patch, str) else None
    return _file(index, row.file, patch), patch, row.count


async def audit_rows(
    session: AsyncSession, task_id: uuid.UUID, *, limit: int
) -> list[AuditRow]:
    """The newest audit rows of the task and of its tool approvals."""
    events = AuditEventRecord
    approvals = select(ToolApprovalRow.id).where(ToolApprovalRow.task_id == task_id)
    rows = await session.execute(
        select(
            events.occurred_at,
            events.action,
            events.decision,
            events.reason,
            events.actor_id,
            events.agent_id,
        )
        .where(
            or_(
                (events.resource_kind == "task") & (events.resource_id == task_id),
                (events.resource_kind == "tool_approval")
                & events.resource_id.in_(approvals.scalar_subquery()),
            )
        )
        .order_by(events.occurred_at.desc(), events.id.desc())
        .limit(limit)
    )
    return [
        AuditRow(
            occurred_at=row.occurred_at,
            action=row.action,
            decision=row.decision,
            reason=row.reason,
            actor="agent"
            if row.agent_id is not None
            else "system"
            if row.actor_id is None
            else "person",
        )
        for row in rows
    ]


def _repository_ids(targets: object) -> list[uuid.UUID]:
    found = []
    for target in targets if isinstance(targets, list) else ():
        if not isinstance(target, dict):
            continue
        if target.get("kind") not in _REPOSITORY_TARGETS:
            continue
        try:
            found.append(uuid.UUID(str(target.get("value"))))
        except ValueError:
            continue
    return found


async def pending_approvals(
    session: AsyncSession,
    principal: Principal,
    projects: Mapping[uuid.UUID, views.ProjectInfo],
    policy: Policy,
    *,
    now: datetime,
    limit: int,
    task_id: uuid.UUID | None = None,
    approval_ids: Collection[uuid.UUID] | None = None,
) -> list[ApprovalView]:
    """The pending, unexpired approvals ``principal`` is asked for in
    ``projects`` (newest first, at most ``limit``); of one task, or only
    ``approval_ids``, when given."""
    if not projects:
        return []
    approvals = ToolApprovalRow
    conditions = [
        approvals.requester_user_id == principal.user_id,
        approvals.status == ApprovalStatus.PENDING.value,
        approvals.expires_at > now,
        approvals.project_id.in_(list(projects)),
    ]
    if task_id is not None:
        conditions.append(approvals.task_id == task_id)
    if approval_ids is not None:
        conditions.append(approvals.id.in_(list(approval_ids)))
    rows = (
        await session.execute(
            select(
                approvals.id,
                approvals.task_id,
                approvals.project_id,
                approvals.tool,
                approvals.level,
                approvals.summary,
                approvals.targets,
                approvals.created_at,
                approvals.expires_at,
                approvals.grant_pattern.is_not(None).label("grantable"),
                TaskRow.title,
                TaskRow.agent,
            )
            .join(TaskRow, TaskRow.id == approvals.task_id)
            .where(*conditions)
            .order_by(approvals.created_at.desc(), approvals.id)
            .limit(limit)
        )
    ).all()
    named = {row.id: _repository_ids(row.targets) for row in rows}
    found = await views.repositories(
        session, {repository for ids in named.values() for repository in ids}
    )
    # Each repository against its OWN project (a Working Set may hold another
    # project's repository; Codex review of #206): one the principal may not
    # read, or of a project they may not read, is left out.
    readable = {
        repository_id: repository
        for repository_id, repository in found.items()
        if (owner := projects.get(repository.project_id)) is not None
        and views.may_read_repository(principal, owner, repository, policy)
    }
    items = []
    for row in rows:
        items.append(
            ApprovalView(
                id=row.id,
                task_id=row.task_id,
                task_title=row.title,
                agent=row.agent,
                project_id=row.project_id,
                tool=row.tool,
                level=row.level,
                summary=tuple(
                    (item["name"], item["kind"], item["value"]) for item in row.summary
                ),
                repositories=tuple(
                    dict.fromkeys(
                        readable[repository].name
                        for repository in named[row.id]
                        if repository in readable
                    )
                ),
                created_at=row.created_at,
                expires_at=row.expires_at,
                task_grant_allowed=bool(row.grantable)
                and row.level == ApprovalLevel.APPROVAL.value,
            )
        )
    return items


async def task_grants(
    session: AsyncSession,
    principal: Principal,
    projects: Mapping[uuid.UUID, views.ProjectInfo],
    task_id: uuid.UUID,
) -> list[TaskGrantView]:
    """The active task-scoped grants (Decision 0085) ``principal`` made for
    ``task_id``'s current run, oldest first, with how many calls each let run;
    nothing for a task of a project they may not read. Bounded by the cap of
    active grants per (task, user) (``task_grants.MAX_MAX_ACTIVE_GRANTS``)."""
    if not projects:
        return []
    grants = ToolTaskGrantRow
    uses = (
        select(func.count())
        .select_from(ToolTaskGrantUseRow)
        .where(ToolTaskGrantUseRow.grant_id == grants.id)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(
                grants.id,
                grants.approval_id,
                grants.tool,
                grants.summary,
                grants.created_at,
                uses.label("uses"),
            )
            .join(TaskRow, TaskRow.id == grants.task_id)
            .where(
                grants.task_id == task_id,
                grants.requester_user_id == principal.user_id,
                grants.status == TaskGrantStatus.ACTIVE.value,
                grants.project_id.in_(list(projects)),
                grants.task_attempt == TaskRow.attempt,
                grants.task_retry_count == TaskRow.retry_count,
            )
            .order_by(grants.created_at, grants.id)
            .limit(MAX_MAX_ACTIVE_GRANTS)
        )
    ).all()
    return [
        TaskGrantView(
            id=row.id,
            approval_id=row.approval_id,
            tool=row.tool,
            summary=tuple(
                (item["name"], item["kind"], item["value"]) for item in row.summary
            ),
            created_at=row.created_at,
            uses=int(row.uses),
        )
        for row in rows
    ]
