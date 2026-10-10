"""The PR screen's panels and the mobile boards over HTTP (issue #185 item 6,
Decision 0078; the Design's PullRequest, MobileDiff and MobileApproval
boards).

* ``GET /pull-requests/{record_id}/files`` (``tasks.list``): the files the pull
  request changes, with the lines added and deleted, as they were read when it
  was delivered (``integration/changes.py``); ``recorded`` is false when they
  were not.
* ``GET /pull-requests/{record_id}/files/{index}`` (``tasks.list``): one of them
  with its patch (the diff the MobileDiff board shows; bounded, credentials
  redacted).
* ``GET /pull-requests/{record_id}/review`` (``tasks.list``): the recorded
  review and evaluation of the repository and the reviewer nodes of the DAG of
  the record's attempt (agent, model, state, when they ended).
* ``GET /pull-requests/{record_id}/audit`` (``tasks.list``): the newest audit
  rows of the record's task and of its tool approvals, as a closed projection
  (``board_views.AuditRow``).
* ``GET /approvals`` (``tasks.list``): the pending tool approvals the person is
  asked for (of one task with ``task_id``); ``GET /approvals/{approval_id}`` one
  of them (the sheet opens one the bounded list does not hold).
* ``POST /approvals/{approval_id}/decision`` (``project.task.run`` on the
  approval's project, audited): approve or reject one, through
  ``ApprovalService`` (only the person the agent works for may decide; the
  service writes its own audit row). A ``strong_approval`` is never granted here
  (the service's step-up fails closed; 403 ``strong_approval_unavailable``):
  merging to the default branch and credential changes need a Passkey
  re-authentication bound to the approval, which does not exist yet.
  ``{"decision": "approve_for_task"}`` (「このタスクの間は許可」, Decision 0085)
  approves it and, in the same transaction, grants the later calls of the same
  tool with the same or narrower scope and arguments for the rest of the task's
  run (``ApprovalService.approve_for_task``): only an approval whose
  ``task_grant_allowed`` is true (409 ``task_grant_not_allowed`` otherwise, the
  approval stays pending), while the task can act (409 ``task_not_active``),
  within the cap of active grants (409 ``task_grant_limit_reached``).
* ``GET /tasks/{task_id}/approval-grants`` (``tasks.list``): the person's own
  active grants of the task's current run (「このタスクで許可中」).
* ``POST /approval-grants/{grant_id}/revoke`` (``tasks.list``; the service lets
  only the person, or an Admin / Owner, revoke one, and tells anybody else it
  does not exist): later calls are asked again. Revoking only takes rights away.

The pull request panels answer only for a record the person may read, exactly as
``GET /pull-requests/{record_id}`` (``task_views.list_pull_requests``: its
project and its repository, Decision 0067, 2): any other record, or none, is 404
``pull_request_not_found``. There is no merge route.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from starlette.requests import HTTPConnection

from paw_backend.api.v1 import board_views as boards
from paw_backend.api.v1 import task_views as views
from paw_backend.api.v1.tasks import _execution, _policy, _read_only
from paw_backend.authz import Capability, Principal, Resource, require_capability
from paw_backend.db import Database
from paw_backend.errors import ApiError
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.store import DagStore
from paw_backend.projects.models import ProjectRow
from paw_backend.tasks.models import MAX_PULL_REQUEST_FILES
from paw_backend.tools.approvals import ApprovalOutcome
from paw_backend.tools.models import ToolApprovalRow

router = APIRouter(tags=["pull requests"])

_DEFAULT_AUDIT_ROWS = 50
_DEFAULT_APPROVALS = 50


async def _approval_resource(connection: HTTPConnection) -> Resource:
    """The approval's project as stored; an approval that does not exist (or
    whose project is Deleted) is a resource no capability applies to."""
    approval_id = uuid.UUID(connection.path_params["approval_id"])
    database: Database = connection.app.state.database
    async with database.session() as session, session.begin():
        row = (
            await session.execute(
                select(ProjectRow.id, ProjectRow.name, ProjectRow.status)
                .join(ToolApprovalRow, ToolApprovalRow.project_id == ProjectRow.id)
                .where(ToolApprovalRow.id == approval_id)
            )
        ).one_or_none()
    project = None if row is None else views._project(row)
    if project is None:
        return Resource(kind="tool_approval", id=approval_id)
    return Resource.project(project.id, project.state)


_LIST = Annotated[Principal, Depends(require_capability(Capability.TASKS_LIST))]
_DECIDE = Annotated[
    Principal,
    Depends(require_capability(Capability.PROJECT_TASK_RUN, _approval_resource)),
]
_RECORD = Annotated[int, Path(ge=1, le=2**63 - 1)]


# -- answers ----------------------------------------------------------------------


class ChangedFileOut(BaseModel):
    index: int
    path: str
    # The path before a rename or copy.
    previous_path: str | None
    # GitHub's: added, removed, modified, renamed, copied, changed, unchanged.
    status: str
    additions: int
    deletions: int
    # Whether the file's patch was kept (GitHub gives none for a binary or very
    # large file; only the first files' are read).
    has_patch: bool
    patch_truncated: bool


class ChangesOut(BaseModel):
    # ``False``: the changes were not read when the pull request was delivered.
    recorded: bool
    # The commit the pull request delivered (``None`` when not recorded).
    head_commit: str | None
    # GitHub listed more files than were kept.
    truncated: bool
    additions: int
    deletions: int
    files: list[ChangedFileOut]


class FileDiffOut(ChangedFileOut):
    # How many files were kept (for "1 / 4").
    count: int
    # The unified diff of the file (``None``: not kept).
    patch: str | None


class ReviewerOut(BaseModel):
    key: str
    title: str
    state: str
    agent: str | None
    model: str | None
    finished_at: datetime | None


class ReviewOut(BaseModel):
    review: str
    evaluation: str
    # The reviewer nodes of the DAG of the record's attempt, in the plan's order.
    reviewers: list[ReviewerOut]


class AuditRowOut(BaseModel):
    occurred_at: datetime
    action: str
    decision: str
    reason: str
    actor: Literal["agent", "person", "system"]


class AuditOut(BaseModel):
    rows: list[AuditRowOut]


class SummaryItemOut(BaseModel):
    name: str
    kind: str
    value: str


class ApprovalOut(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    task_title: str
    agent: str | None
    project_id: uuid.UUID
    tool: str
    level: Literal["approval", "strong_approval"]
    # Every argument of the call as the approver sees it (bounded, redacted).
    summary: list[SummaryItemOut]
    # The repositories the call names that the person may read.
    repositories: list[str]
    created_at: datetime
    expires_at: datetime
    # It may be answered with 「このタスクの間は許可」 (Decision 0085).
    task_grant_allowed: bool


class ApprovalListOut(BaseModel):
    approvals: list[ApprovalOut]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "approve_for_task"]


class DecisionOut(BaseModel):
    id: uuid.UUID
    outcome: Literal["approved", "rejected", "approved_for_task"]
    # The grant ``approve_for_task`` created (left out otherwise).
    grant_id: uuid.UUID | None = None


class TaskGrantOut(BaseModel):
    id: uuid.UUID
    approval_id: uuid.UUID
    tool: str
    # What the person was shown when they granted it (the approval's summary).
    summary: list[SummaryItemOut]
    created_at: datetime
    # How many calls it let run without asking again.
    uses: int


class TaskGrantListOut(BaseModel):
    grants: list[TaskGrantOut]


class RevokeGrantOut(BaseModel):
    id: uuid.UUID
    outcome: Literal["revoked"]


# -- helpers ----------------------------------------------------------------------


async def _record(
    request: Request, principal: Principal, record_id: int
) -> views.PullRequestItem:
    """The record, if the principal may read it (else 404 alike)."""
    _execution(request)
    database: Database = request.app.state.database
    policy = _policy(request)
    async with database.session() as session, session.begin():
        await _read_only(session)
        projects = await views.readable_projects(session, principal, policy)
        items = await views.list_pull_requests(
            session, principal, projects, policy, limit=1, record_id=record_id
        )
    if not items:
        raise ApiError(404, "pull_request_not_found", "Pull request not found")
    return items[0]


def _file_out(item: boards.ChangedFileView) -> ChangedFileOut:
    return ChangedFileOut(
        index=item.index,
        path=item.path,
        previous_path=item.previous_path,
        status=item.status,
        additions=item.additions,
        deletions=item.deletions,
        has_patch=item.has_patch,
        patch_truncated=item.patch_truncated,
    )


def _approval_out(item: boards.ApprovalView) -> ApprovalOut:
    return ApprovalOut(
        id=item.id,
        task_id=item.task_id,
        task_title=item.task_title,
        agent=item.agent,
        project_id=item.project_id,
        tool=item.tool,
        level=item.level,
        summary=[
            SummaryItemOut(name=name, kind=kind, value=value)
            for name, kind, value in item.summary
        ],
        repositories=list(item.repositories),
        created_at=item.created_at,
        expires_at=item.expires_at,
        task_grant_allowed=item.task_grant_allowed,
    )


# -- routes ------------------------------------------------------------------------


@router.get(
    "/pull-requests/{record_id}/files",
    response_model=ChangesOut,
    summary="The files the pull request changes (as delivered)",
)
async def list_changed_files(
    request: Request, principal: _LIST, record_id: _RECORD
) -> ChangesOut:
    await _record(request, principal, record_id)
    database: Database = request.app.state.database
    async with database.session() as session, session.begin():
        await _read_only(session)
        changes = await boards.changes_of(session, record_id)
    if changes is None:
        return ChangesOut(
            recorded=False,
            head_commit=None,
            truncated=False,
            additions=0,
            deletions=0,
            files=[],
        )
    return ChangesOut(
        recorded=True,
        head_commit=changes.head_commit,
        truncated=changes.truncated,
        additions=sum(item.additions for item in changes.files),
        deletions=sum(item.deletions for item in changes.files),
        files=[_file_out(item) for item in changes.files],
    )


@router.get(
    "/pull-requests/{record_id}/files/{index}",
    response_model=FileDiffOut,
    summary="One changed file of the pull request with its diff",
)
async def get_changed_file(
    request: Request,
    principal: _LIST,
    record_id: _RECORD,
    index: Annotated[int, Path(ge=0, lt=MAX_PULL_REQUEST_FILES)],
) -> FileDiffOut:
    await _record(request, principal, record_id)
    database: Database = request.app.state.database
    async with database.session() as session, session.begin():
        await _read_only(session)
        found = await boards.file_of(session, record_id, index)
    if found is None:
        raise ApiError(404, "file_not_found", "File not found")
    item, patch, count = found
    return FileDiffOut(**_file_out(item).model_dump(), count=count, patch=patch)


@router.get(
    "/pull-requests/{record_id}/review",
    response_model=ReviewOut,
    summary="The review of the pull request: its result and its reviewers",
)
async def get_review(
    request: Request, principal: _LIST, record_id: _RECORD
) -> ReviewOut:
    item = await _record(request, principal, record_id)
    store = DagStore(request.app.state.database)
    reviewers: list[ReviewerOut] = []
    dag = await store.get(item.task_id, item.attempt)
    if dag is not None:
        latest = {}
        for record in await store.attempts(dag.id):  # oldest first
            latest[record.node_key] = record
        for node in dag.nodes:
            if node.role is not NodeRole.REVIEWER:
                continue
            last = latest.get(node.key)
            reviewers.append(
                ReviewerOut(
                    key=node.key,
                    title=node.title,
                    state=node.state.value,
                    agent=None if last is None else last.placement_agent,
                    model=None if last is None else last.placement_model,
                    finished_at=None if last is None else last.finished_at,
                )
            )
    return ReviewOut(
        review=item.review.value,
        evaluation=item.evaluation.value,
        reviewers=reviewers,
    )


@router.get(
    "/pull-requests/{record_id}/audit",
    response_model=AuditOut,
    summary="The newest audit rows of the pull request's task",
)
async def get_audit(
    request: Request,
    principal: _LIST,
    record_id: _RECORD,
    limit: Annotated[int, Query(ge=1, le=boards.MAX_AUDIT_ROWS)] = _DEFAULT_AUDIT_ROWS,
) -> AuditOut:
    item = await _record(request, principal, record_id)
    database: Database = request.app.state.database
    async with database.session() as session, session.begin():
        await _read_only(session)
        rows = await boards.audit_rows(session, item.task_id, limit=limit)
    return AuditOut(
        rows=[
            AuditRowOut(
                occurred_at=row.occurred_at,
                action=row.action,
                decision=row.decision,
                reason=row.reason,
                actor=row.actor,
            )
            for row in rows
        ]
    )


@router.get(
    "/approvals",
    response_model=ApprovalListOut,
    tags=["approvals"],
    summary="The pending tool approvals the person is asked for",
)
async def list_approvals(
    request: Request,
    principal: _LIST,
    task_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=boards.MAX_APPROVALS)] = _DEFAULT_APPROVALS,
) -> ApprovalListOut:
    _execution(request)
    database: Database = request.app.state.database
    policy = _policy(request)
    async with database.session() as session, session.begin():
        await _read_only(session)
        projects = await views.readable_projects(session, principal, policy)
        items = await boards.pending_approvals(
            session,
            principal,
            projects,
            policy,
            now=datetime.now(UTC),
            limit=limit,
            task_id=task_id,
        )
    return ApprovalListOut(approvals=[_approval_out(item) for item in items])


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalOut,
    tags=["approvals"],
    summary="One pending tool approval the person is asked for",
)
async def get_approval(
    request: Request, principal: _LIST, approval_id: uuid.UUID
) -> ApprovalOut:
    """The sheet opens one the bounded list does not hold (Codex review of #206).
    Another person's, a decided or an expired one, or none, are not found alike."""
    _execution(request)
    database: Database = request.app.state.database
    policy = _policy(request)
    async with database.session() as session, session.begin():
        await _read_only(session)
        projects = await views.readable_projects(session, principal, policy)
        items = await boards.pending_approvals(
            session,
            principal,
            projects,
            policy,
            now=datetime.now(UTC),
            limit=1,
            approval_ids=[approval_id],
        )
    if not items:
        raise ApiError(404, "approval_not_found", "Approval not found")
    return _approval_out(items[0])


_DECISION_ERRORS = {
    ApprovalOutcome.NOT_FOUND: (404, "approval_not_found", "Approval not found"),
    ApprovalOutcome.NOT_PENDING: (
        409,
        "approval_not_pending",
        "The approval was already decided, used or withdrawn",
    ),
    ApprovalOutcome.EXPIRED: (409, "approval_expired", "The approval expired"),
    ApprovalOutcome.STEP_UP_REQUIRED: (
        403,
        "strong_approval_unavailable",
        "This approval needs a Passkey re-authentication that is not available here",
    ),
    ApprovalOutcome.SELF_APPROVAL: (403, "forbidden", "Forbidden"),
    ApprovalOutcome.UNAVAILABLE: (
        503,
        "approvals_unavailable",
        "Approvals are not available now",
    ),
    ApprovalOutcome.NOT_GRANTABLE: (
        409,
        "task_grant_not_allowed",
        "This call cannot be allowed for the rest of the task",
    ),
    ApprovalOutcome.GRANT_LIMIT_REACHED: (
        409,
        "task_grant_limit_reached",
        "Too many calls are allowed for this task already",
    ),
    ApprovalOutcome.TASK_NOT_ACTIVE: (
        409,
        "task_not_active",
        "The task ended or was started again",
    ),
}
_REVOKE_GRANT_ERRORS = {
    ApprovalOutcome.NOT_FOUND: (404, "grant_not_found", "Grant not found"),
    ApprovalOutcome.NOT_OPEN: (409, "grant_not_active", "The grant is not active"),
    ApprovalOutcome.UNAVAILABLE: (
        503,
        "approvals_unavailable",
        "Approvals are not available now",
    ),
}


@router.post(
    "/approvals/{approval_id}/decision",
    response_model=DecisionOut,
    response_model_exclude_none=True,
    tags=["approvals"],
    summary=(
        "Approve or reject one tool approval (once, for this one call), or"
        " approve it for the rest of its task"
    ),
)
async def decide_approval(
    request: Request,
    approval_id: uuid.UUID,
    body: DecisionRequest,
    principal: _DECIDE,
) -> DecisionOut:
    execution = _execution(request)
    service = execution.approvals
    if body.decision == "approve":
        # Never a STRONG_APPROVAL here, even with a step-up verifier wired: the
        # Passkey step-up is bound to the user, not to this approval (Decision 0078
        # 6, Codex review of #206).
        result = await service.approve(approval_id, principal, allow_strong=False)
    elif body.decision == "approve_for_task":
        # Never a STRONG_APPROVAL either: such an approval carries no grant
        # pattern (Decision 0085, 2).
        result = await service.approve_for_task(approval_id, principal)
    else:
        result = await service.reject(approval_id, principal)
    if result.outcome is ApprovalOutcome.APPROVED:
        return DecisionOut(id=approval_id, outcome="approved")
    if result.outcome is ApprovalOutcome.REJECTED:
        return DecisionOut(id=approval_id, outcome="rejected")
    if result.outcome is ApprovalOutcome.APPROVED_FOR_TASK:
        return DecisionOut(
            id=approval_id, outcome="approved_for_task", grant_id=result.grant_id
        )
    status, code, message = _DECISION_ERRORS.get(
        result.outcome, (422, "invalid_approval", "Invalid approval")
    )
    raise ApiError(status, code, message)


@router.get(
    "/tasks/{task_id}/approval-grants",
    response_model=TaskGrantListOut,
    tags=["approvals"],
    summary="The person's active 'allow for this task' grants of a task",
)
async def list_task_grants(
    request: Request, principal: _LIST, task_id: uuid.UUID
) -> TaskGrantListOut:
    _execution(request)
    database: Database = request.app.state.database
    policy = _policy(request)
    async with database.session() as session, session.begin():
        await _read_only(session)
        projects = await views.readable_projects(session, principal, policy)
        items = await boards.task_grants(session, principal, projects, task_id)
    return TaskGrantListOut(
        grants=[
            TaskGrantOut(
                id=item.id,
                approval_id=item.approval_id,
                tool=item.tool,
                summary=[
                    SummaryItemOut(name=name, kind=kind, value=value)
                    for name, kind, value in item.summary
                ],
                created_at=item.created_at,
                uses=item.uses,
            )
            for item in items
        ]
    )


@router.post(
    "/approval-grants/{grant_id}/revoke",
    response_model=RevokeGrantOut,
    tags=["approvals"],
    summary="Withdraw an 'allow for this task' grant: later calls are asked again",
)
async def revoke_task_grant(
    request: Request, grant_id: uuid.UUID, principal: _LIST
) -> RevokeGrantOut:
    result = await _execution(request).approvals.revoke_grant(grant_id, principal)
    if result.outcome is ApprovalOutcome.REVOKED:
        return RevokeGrantOut(id=grant_id, outcome="revoked")
    status, code, message = _REVOKE_GRANT_ERRORS.get(
        result.outcome, (422, "invalid_grant", "Invalid grant")
    )
    raise ApiError(status, code, message)
