"""Tasks, their Agent DAG, the operator controls and the pull request records
over HTTP (issue #185, Decision 0067 Approved; the screens of PAW-062, #48).

* ``GET /tasks`` (``tasks.list``, every human role): the latest updated tasks of
  the projects the person may read (``project.read``, decided per project), newest
  first, at most ``limit`` (default 100, up to 200).
* ``GET /tasks/{task_id}`` (``project.read`` on the task's project): the task as
  ``TaskService.restore`` reads it, its Working Set with each repository's state
  in the current attempt, the DAG of the current attempt with every node attempt,
  the tool calls of the current step and the budget.
* ``POST /tasks/{task_id}/controls`` (``project.task.run`` on the task's project,
  audited): one operator control of ``TaskService.execute`` (Pause, Resume,
  Cancel, Retry, Restart, Stop Now) with the version the operator saw
  (``expected_version``, required: a task that changed meanwhile is refused with
  409 ``task_conflict``). The transition table of ``tasks/domain.py`` decides
  (409 ``illegal_transition``); Stop Now needs a reason (422). Resume, Retry and
  Restart put the task to work again, so only the person who created the task may
  send them (403 ``task_creator_only``: the work runs with the creator's identity
  and rights), and they put the task in the queue in the command's own
  transaction. Answers the task as the command left it.
* ``GET /pull-requests`` (``tasks.list``): the pull requests the tasks of those
  projects recorded, with Merge Ready as the backend judges it
  (``task_views.is_merge_ready``). There is no merge route: merging stays the
  human's, on GitHub (``AGENTS.md``).

A repository the person may not read (its ACL override) is left out of every
answer, with its pull request (``task_views``). A task of a project the person
may not read, or that does not exist, is 403 ``forbidden`` alike (the guard
cannot tell them apart, so neither can the caller). The answers carry no task
input, no log line, no node goal or result and no event detail.
"""

import logging
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import HTTPConnection

from paw_backend.api.v1 import task_views as views
from paw_backend.authz import Capability, Principal, Resource, require_capability
from paw_backend.authz.policy import Policy
from paw_backend.db import Database
from paw_backend.errors import ApiError
from paw_backend.orchestrator.composition import TaskExecution
from paw_backend.orchestrator.records import AttemptRecord, DagRecord
from paw_backend.orchestrator.store import DagStore
from paw_backend.tasks import (
    Actor,
    IllegalTransitionError,
    InvalidCommandArgumentError,
    ProjectNotActiveError,
    TaskCommand,
    TaskConflictError,
    TaskError,
    TaskNotFoundError,
    TaskSnapshot,
)
from paw_backend.tasks.models import TaskRow
from paw_backend.tasks.queueing import Priority
from paw_backend.tasks.queueing.errors import (
    BudgetNotConfiguredError,
    TaskAlreadyQueuedError,
)
from paw_backend.tasks.service import MAX_NAME_LENGTH, MAX_REASON_LENGTH

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"])

_MAX_VERSION = 2**31 - 1
_DEFAULT_LIMIT = 100

# The controls that put a task to work again: the creator's alone (module
# docstring) and followed by a queue entry.
_RESTARTING = frozenset({TaskCommand.RESUME, TaskCommand.RETRY, TaskCommand.RESTART})

ControlName = Literal["pause", "resume", "cancel", "retry", "restart", "stop_now"]


async def _task_resource(connection: HTTPConnection) -> Resource:
    """The task's project as stored (its state from the projects table); a task
    that does not exist (or whose project is Deleted) is a resource no capability
    applies to, so the guard refuses it like a project the person may not read."""
    task_id = uuid.UUID(connection.path_params["task_id"])
    database: Database = connection.app.state.database
    async with database.session() as session, session.begin():
        project = await views.project_of_task(session, task_id)
    if project is None:
        return Resource(kind="task", id=task_id)
    return Resource.project(project.id, project.state)


_LIST = Annotated[Principal, Depends(require_capability(Capability.TASKS_LIST))]
_READ = Annotated[
    Principal, Depends(require_capability(Capability.PROJECT_READ, _task_resource))
]
_RUN = Annotated[
    Principal, Depends(require_capability(Capability.PROJECT_TASK_RUN, _task_resource))
]


# -- answers ----------------------------------------------------------------------


class TaskSummaryOut(BaseModel):
    id: uuid.UUID
    title: str
    state: str
    wait_reason: str | None
    # The latest queue entry's (a task never queued has none).
    priority: str | None
    project_id: uuid.UUID
    project_name: str
    # The first ``target`` repository the person may read, else the first one.
    repository: str | None
    # The latest Start (when the current run began), if the task ever started.
    started_at: datetime | None
    updated_at: datetime


class TaskListOut(BaseModel):
    tasks: list[TaskSummaryOut]


class PullRequestRefOut(BaseModel):
    id: str
    number: int
    url: str
    state: str


class TaskRepositoryOut(BaseModel):
    repository_id: uuid.UUID
    name: str
    role: str
    branch: str | None
    worktree: str | None
    review: str
    evaluation: str
    pull_request: PullRequestRefOut | None


class ToolCallOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    started_at: datetime
    finished_at: datetime | None


class CurrentStepOut(BaseModel):
    name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    # Every started call of the step and its latest finished ones, oldest first.
    tool_calls: list[ToolCallOut]


class BudgetUsageOut(BaseModel):
    kind: str
    consumed: int
    # ``None``: unlimited.
    limit: int | None


class BudgetOut(BaseModel):
    preset: str
    usage: list[BudgetUsageOut]


class NodeAttemptOut(BaseModel):
    number: int
    state: str
    agent: str | None
    model: str | None
    placement: str | None
    # A closed error class, never the error's text.
    error_class: str | None
    started_at: datetime
    finished_at: datetime | None


class DagNodeOut(BaseModel):
    key: str
    title: str
    role: str
    state: str
    required: bool
    depends_on: list[str]
    # Where the latest attempt ran (``None`` before one was placed).
    agent: str | None
    model: str | None
    attempts: list[NodeAttemptOut]


class TaskDetailOut(TaskSummaryOut):
    version: int
    created_by: uuid.UUID
    agent: str | None
    model: str | None
    attempt: int
    retry_count: int
    # The latest step of the current attempt (``status`` says whether it runs).
    current_step: CurrentStepOut | None
    budget: BudgetOut | None
    repositories: list[TaskRepositoryOut]
    # The nodes of the current attempt's DAG in the plan's order; ``None`` before
    # a plan was accepted.
    dag: list[DagNodeOut] | None


class PullRequestOut(BaseModel):
    id: str
    number: int
    url: str
    state: str
    # The pull request's title is the task's (``integration/publish.py``).
    title: str
    task_id: uuid.UUID
    task_title: str
    repository: str
    branch: str | None
    base: str
    review: str
    evaluation: str
    merge_ready: bool
    updated_at: datetime


class PullRequestListOut(BaseModel):
    pull_requests: list[PullRequestOut]


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: ControlName
    # The version of the task the operator decided on (``TaskDetailOut.version``).
    expected_version: StrictInt = Field(ge=1, le=_MAX_VERSION)
    # Stop Now needs one; kept in the history (and Stop Now's Task log line).
    reason: str | None = Field(default=None, max_length=MAX_REASON_LENGTH)
    # Retry and Restart only: another agent / model for the next run.
    agent: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    model: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)


# -- helpers ----------------------------------------------------------------------


def _execution(request: Request) -> TaskExecution:
    execution: TaskExecution | None = getattr(request.app.state, "task_execution", None)
    if execution is None:
        raise ApiError(503, "tasks_not_configured", "Tasks are not configured")
    return execution


def _policy(request: Request) -> Policy:
    return request.app.state.authorizer.policy


async def _read_only(session: AsyncSession) -> None:
    await session.execute(
        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    )


@contextmanager
def _task_errors():
    """The task service's typed errors as answers (fixed messages, never input)."""
    try:
        yield
    except TaskNotFoundError:
        raise ApiError(404, "task_not_found", "Task not found") from None
    except TaskConflictError:
        raise ApiError(
            409, "task_conflict", "The task changed; reload it and decide again"
        ) from None
    except IllegalTransitionError as error:
        raise ApiError(
            409, "illegal_transition", f"The task does not accept this in {error.state}"
        ) from None
    except InvalidCommandArgumentError as error:
        # The service's messages are fixed strings that name the rule.
        raise ApiError(422, "invalid_command_argument", str(error)) from None
    except ProjectNotActiveError:
        raise ApiError(409, "project_not_active", "The project is not active") from None
    except TaskError as error:
        raise ApiError(409, error.code, "The task does not allow this now") from None


def _step(snapshot: TaskSnapshot) -> CurrentStepOut | None:
    step = snapshot.current_step
    if step is None:
        return None
    return CurrentStepOut(
        name=step.name,
        status=step.status.value,
        started_at=step.started_at,
        finished_at=step.finished_at,
        tool_calls=[
            ToolCallOut(
                id=call.id,
                name=call.tool_name,
                status=call.status.value,
                started_at=call.started_at,
                finished_at=call.finished_at,
            )
            for call in snapshot.tool_invocations
        ],
    )


def _attempt(record: AttemptRecord) -> NodeAttemptOut:
    return NodeAttemptOut(
        number=record.number,
        state=record.state.value,
        agent=record.placement_agent,
        model=record.placement_model,
        placement=None if record.placement is None else record.placement.value,
        error_class=record.error_class,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


def _dag(dag: DagRecord, attempts: tuple[AttemptRecord, ...]) -> list[DagNodeOut]:
    by_node: dict[str, list[AttemptRecord]] = {}
    for record in attempts:  # oldest first
        by_node.setdefault(record.node_key, []).append(record)
    nodes = []
    for node in dag.nodes:
        mine = by_node.get(node.key, [])
        placed = [record for record in mine if record.placement is not None]
        latest = placed[-1] if placed else None
        nodes.append(
            DagNodeOut(
                key=node.key,
                title=node.title,
                role=node.role.value,
                state=node.state.value,
                required=node.required,
                depends_on=list(node.depends_on),
                agent=None if latest is None else latest.placement_agent,
                model=None if latest is None else latest.placement_model,
                attempts=[_attempt(record) for record in mine],
            )
        )
    return nodes


async def _detail(
    request: Request, principal: Principal, task_id: uuid.UUID
) -> TaskDetailOut:
    execution = _execution(request)
    database: Database = request.app.state.database
    policy = _policy(request)
    with _task_errors():
        snapshot = await execution.tasks.restore(task_id, log_limit=0)
    async with database.session() as session, session.begin():
        await _read_only(session)
        project = await views.project_of_task(session, task_id)
        if project is None:
            raise ApiError(404, "task_not_found", "Task not found")
        members = [entry.repository_id for entry in snapshot.working_set]
        readable = views.readable_repositories(
            principal, project, await views.repositories(session, members), policy
        )
        record_ids = await views.pull_request_ids(
            session, task_id, snapshot.attempt.number
        )
        priority = await views.priority_of(session, task_id)
        started = await views.started_at(session, task_id)
        preset = await views.budget_preset(session, task_id)
    budget = None
    if preset is not None:
        try:
            usage = await execution.budget.usage(task_id)
        except BudgetNotConfiguredError:
            usage = None
        if usage is not None:
            budget = BudgetOut(
                preset=preset.value,
                usage=[
                    BudgetUsageOut(
                        kind=item.kind.value, consumed=item.consumed, limit=item.limit
                    )
                    for item in usage
                ],
            )
    store = DagStore(database)
    dag = await store.get(task_id, snapshot.attempt.number)
    nodes = None if dag is None else _dag(dag, await store.attempts(dag.id))

    repositories = []
    for entry in snapshot.working_set:
        repository = readable.get(entry.repository_id)
        if repository is None:
            continue
        try:
            state = snapshot.attempt.repository(entry.repository_id)
        except KeyError:
            state = None
        pull_request = None if state is None else state.pull_request
        record_id = record_ids.get(entry.repository_id)
        repositories.append(
            TaskRepositoryOut(
                repository_id=entry.repository_id,
                name=repository.name,
                role=entry.role.value,
                branch=None if state is None else state.worktree.branch,
                worktree=None if state is None else state.worktree.path,
                review=(
                    "not_started" if state is None else state.review.review_status.value
                ),
                evaluation=(
                    "not_run" if state is None else state.review.evaluation_result.value
                ),
                pull_request=(
                    None
                    if pull_request is None or record_id is None
                    else PullRequestRefOut(
                        id=str(record_id),
                        number=pull_request.number,
                        url=pull_request.url,
                        state=pull_request.state.value,
                    )
                ),
            )
        )
    target = next(
        (repo.name for repo in repositories if repo.role == "target"),
        repositories[0].name if repositories else None,
    )
    return TaskDetailOut(
        id=snapshot.id,
        title=snapshot.title,
        state=snapshot.state.value,
        wait_reason=None
        if snapshot.wait_reason is None
        else snapshot.wait_reason.value,
        priority=None if priority is None else priority.value,
        project_id=project.id,
        project_name=project.name,
        repository=target,
        started_at=started,
        updated_at=snapshot.updated_at,
        version=snapshot.version,
        created_by=snapshot.created_by,
        agent=snapshot.agent,
        model=snapshot.model,
        attempt=snapshot.attempt.number,
        retry_count=snapshot.retry_count,
        current_step=_step(snapshot),
        budget=budget,
        repositories=repositories,
        dag=nodes,
    )


def _requeue(execution: TaskExecution) -> Callable:
    """The step of Resume / Retry / Restart, in the command's transaction: a queue
    entry with the priority of the task's latest one (``normal`` if it never had
    one), unless the task still has an active entry (a worker that has not quiesced
    yet goes on with the task, as ``compute.holds`` resumes a held task)."""

    async def requeue(
        session: AsyncSession, task_id: uuid.UUID, project_id: uuid.UUID
    ) -> None:
        priority = await views.priority_of(session, task_id)
        try:
            async with session.begin_nested():
                await execution.queue.enqueue_in(
                    session, task_id, priority=priority or Priority.NORMAL
                )
        except TaskAlreadyQueuedError:
            pass

    return requeue


# -- routes ------------------------------------------------------------------------


@router.get(
    "/tasks",
    response_model=TaskListOut,
    summary="The latest updated tasks of the projects the person may read",
)
async def list_tasks(
    request: Request,
    principal: _LIST,
    limit: Annotated[int, Query(ge=1, le=views.MAX_LIST_LIMIT)] = _DEFAULT_LIMIT,
) -> TaskListOut:
    _execution(request)
    database: Database = request.app.state.database
    policy = _policy(request)
    async with database.session() as session, session.begin():
        await _read_only(session)
        projects = await views.readable_projects(session, principal, policy)
        items = await views.list_tasks(
            session, principal, projects, policy, limit=limit
        )
    return TaskListOut(
        tasks=[
            TaskSummaryOut(
                id=item.id,
                title=item.title,
                state=item.state.value,
                wait_reason=None
                if item.wait_reason is None
                else item.wait_reason.value,
                priority=None if item.priority is None else item.priority.value,
                project_id=item.project.id,
                project_name=item.project.name,
                repository=item.repository,
                started_at=item.started_at,
                updated_at=item.updated_at,
            )
            for item in items
        ]
    )


@router.get(
    "/tasks/{task_id}",
    response_model=TaskDetailOut,
    summary="A task with its Working Set, DAG, current step and budget",
)
async def get_task(
    request: Request, task_id: uuid.UUID, principal: _READ
) -> TaskDetailOut:
    return await _detail(request, principal, task_id)


@router.post(
    "/tasks/{task_id}/controls",
    response_model=TaskDetailOut,
    summary=(
        "Pause, Resume, Cancel, Retry, Restart or Stop Now the task (with the "
        "version the operator saw)"
    ),
)
async def control_task(
    request: Request, task_id: uuid.UUID, body: ControlRequest, principal: _RUN
) -> TaskDetailOut:
    execution = _execution(request)
    command = TaskCommand(body.command)
    in_transaction = None
    if command in _RESTARTING:
        database: Database = request.app.state.database
        async with database.session() as session, session.begin():
            created_by = (
                await session.execute(
                    select(TaskRow.created_by).where(TaskRow.id == task_id)
                )
            ).scalar_one_or_none()
        if created_by is None:
            raise ApiError(404, "task_not_found", "Task not found")
        if created_by != principal.user_id:
            raise ApiError(
                403,
                "task_creator_only",
                "Only the person who created the task can put it to work again",
            )
        in_transaction = _requeue(execution)
    with _task_errors():
        await execution.tasks.execute(
            task_id,
            command,
            actor=Actor.user(principal.user_id),
            expected_version=body.expected_version,
            reason=body.reason,
            agent=body.agent,
            model=body.model,
            in_transaction=in_transaction,
        )
    return await _detail(request, principal, task_id)


@router.get(
    "/pull-requests",
    response_model=PullRequestListOut,
    summary="The pull requests the tasks of the readable projects recorded",
)
async def list_pull_requests(
    request: Request,
    principal: _LIST,
    limit: Annotated[int, Query(ge=1, le=views.MAX_LIST_LIMIT)] = _DEFAULT_LIMIT,
) -> PullRequestListOut:
    _execution(request)
    database: Database = request.app.state.database
    policy = _policy(request)
    async with database.session() as session, session.begin():
        await _read_only(session)
        projects = await views.readable_projects(session, principal, policy)
        items = await views.list_pull_requests(
            session, principal, projects, policy, limit=limit
        )
    return PullRequestListOut(pull_requests=[_pull_request(item) for item in items])


@router.get(
    "/pull-requests/{record_id}",
    response_model=PullRequestOut,
    summary="One pull request record of a readable project and repository",
)
async def get_pull_request(
    request: Request, principal: _LIST, record_id: int
) -> PullRequestOut:
    """The record the PR screen opens when the bounded list does not hold it. A
    record of another project, of a repository the principal may not read, or
    none at all are not found alike (Decision 0067, 2)."""
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
    return _pull_request(items[0])


def _pull_request(item: views.PullRequestItem) -> PullRequestOut:
    return PullRequestOut(
        id=str(item.id),
        number=item.number,
        url=item.url,
        state=item.state.value,
        title=item.task_title,
        task_id=item.task_id,
        task_title=item.task_title,
        repository=item.repository,
        branch=item.branch,
        base=item.base,
        review=item.review.value,
        evaluation=item.evaluation.value,
        merge_ready=item.merge_ready,
        updated_at=item.updated_at,
    )
