"""Persistence and commands of the Agent Task lifecycle.

The database is the only source of truth. A command runs in one transaction:
it reads the task, asks ``domain.plan_transition`` whether the command is
allowed, updates the task with an optimistic version check, and appends the
history event. Nothing is kept in process memory, so a restarted backend or a
reconnecting client sees exactly what ``restore`` reads from PostgreSQL.

This layer performs no authorisation. The API layer (PAW-022 / PAW-025) must
decide who may issue a command before calling it, and pass the authenticated
user as the ``Actor``.

Concurrency: every change of a task's row (state, attempt, agent) increments
``tasks.version`` and is applied with ``UPDATE ... WHERE version = <read>``. Of
two commands that read the same version only one updates a row; the other gets
``TaskConflictError`` and its transaction (including its event) is rolled back.
A caller that holds a version it saw earlier passes it as ``expected_version``
so that a stale decision is rejected even if it arrives later. Step, log and
attempt bookkeeping does not change the version.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from paw_backend.db import Database
from paw_backend.tasks.domain import (
    Actor,
    TaskCommand,
    TaskState,
    WaitReason,
    plan_transition,
)
from paw_backend.tasks.errors import (
    InvalidCommandArgumentError,
    TaskConflictError,
    TaskNotFoundError,
    TaskStepError,
)
from paw_backend.tasks.models import (
    TaskAttemptRow,
    TaskEventRow,
    TaskLogRow,
    TaskRow,
    TaskStepRow,
    utcnow,
)
from paw_backend.tasks.records import (
    AttemptSnapshot,
    LogEntry,
    LogLevel,
    PullRequestInfo,
    ReviewState,
    StepInfo,
    StepStatus,
    TaskEvent,
    TaskSnapshot,
    WorktreeState,
)

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 200
MAX_REASON_LENGTH = 500
MAX_NAME_LENGTH = 100
MAX_LOG_MESSAGE_LENGTH = 8000
MAX_INPUT_BYTES = 256 * 1024
MAX_RESTORE_LOGS = 1000
_TRUNCATED = "...[truncated]"

# Called after a transition has been committed, with the event that was written.
# This is the hook for audit (PAW-025) and notifications; the durable record is
# the ``task_events`` table, which a consumer can also read by ``seq``.
TransitionListener = Callable[[TaskEvent], Awaitable[None]]

_STEP_ACTIVE_STATES = frozenset(
    {TaskState.RUNNING, TaskState.WAITING, TaskState.EVALUATING}
)
_FINISHED_STEP_STATUSES = frozenset(
    {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.INTERRUPTED}
)


def _text(name: str, value: str, limit: int) -> str:
    if not value.strip() or len(value) > limit:
        raise InvalidCommandArgumentError(f"{name} must be 1 to {limit} characters")
    return value


def _optional_text(name: str, value: str | None, limit: int) -> str | None:
    return None if value is None else _text(name, value, limit)


def _event(row: TaskEventRow) -> TaskEvent:
    return TaskEvent(
        seq=row.seq,
        task_id=row.task_id,
        attempt=row.attempt,
        command=row.command,
        from_state=row.from_state,
        to_state=row.to_state,
        wait_reason=row.wait_reason,
        actor=Actor(row.actor_kind, row.actor_id),
        reason=row.reason,
        step_name=row.step_name,
        detail=row.detail,
        task_version=row.task_version,
        created_at=row.created_at,
    )


def _step(row: TaskStepRow) -> StepInfo:
    return StepInfo(
        sequence=row.sequence,
        name=row.name,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _attempt(row: TaskAttemptRow) -> AttemptSnapshot:
    pull_request = None
    if (
        row.pr_number is not None
        and row.pr_url is not None
        and row.pr_state is not None
    ):
        pull_request = PullRequestInfo(row.pr_number, row.pr_url, row.pr_state)
    return AttemptSnapshot(
        number=row.number,
        worktree=WorktreeState(row.branch, row.worktree_path, row.head_commit),
        review=ReviewState(row.review_status, row.evaluation_result),
        pull_request=pull_request,
    )


class TaskService:
    def __init__(
        self, database: Database, *, listeners: Sequence[TransitionListener] = ()
    ) -> None:
        self._database = database
        self._listeners = tuple(listeners)

    # -- creation and commands ---------------------------------------------

    async def create_task(
        self,
        *,
        project_id: uuid.UUID,
        created_by: uuid.UUID,
        title: str,
        input: dict[str, Any] | None = None,
        starting_commit: str | None = None,
        agent: str | None = None,
        model: str | None = None,
    ) -> TaskEvent:
        """Create a queued task (attempt 1) and its ``create`` event."""
        title = _text("title", title, MAX_TITLE_LENGTH)
        input = self._checked_input(input)
        now = utcnow()
        task = TaskRow(
            id=uuid.uuid4(),
            project_id=project_id,
            created_by=created_by,
            title=title,
            input=input,
            starting_commit=_optional_text("starting_commit", starting_commit, 64),
            state=TaskState.QUEUED,
            wait_reason=None,
            agent=_optional_text("agent", agent, MAX_NAME_LENGTH),
            model=_optional_text("model", model, MAX_NAME_LENGTH),
            attempt=1,
            retry_count=0,
            created_at=now,
            updated_at=now,
        )
        async with self._database.session() as session, session.begin():
            session.add(task)
            # No relationship() links the rows, so the task must be flushed
            # before the rows that reference it.
            await session.flush()
            session.add(
                TaskAttemptRow(
                    task_id=task.id, number=1, created_at=now, updated_at=now
                )
            )
            await session.flush()
            event = await self._append_event(
                session,
                task,
                command=TaskCommand.CREATE,
                from_state=None,
                actor=Actor.user(created_by),
                reason=None,
                step_name=None,
                detail=None,
            )
        await self._notify(event)
        return event

    async def execute(
        self,
        task_id: uuid.UUID,
        command: TaskCommand,
        *,
        actor: Actor,
        expected_version: int | None = None,
        reason: str | None = None,
        wait_reason: WaitReason | None = None,
        agent: str | None = None,
        model: str | None = None,
    ) -> TaskEvent:
        """Apply ``command`` to the task and return the history event it wrote.

        ``agent`` / ``model`` (Retry and Restart only) switch the agent or model
        for the next run. Raises ``TaskNotFoundError``,
        ``TaskConflictError`` (stale ``expected_version`` or a concurrent
        writer), ``IllegalTransitionError`` and ``InvalidCommandArgumentError``.
        """
        reason = _optional_text("reason", reason, MAX_REASON_LENGTH)
        agent = _optional_text("agent", agent, MAX_NAME_LENGTH)
        model = _optional_text("model", model, MAX_NAME_LENGTH)
        if (agent or model) and command not in (TaskCommand.RETRY, TaskCommand.RESTART):
            raise InvalidCommandArgumentError(
                "Only Retry and Restart accept an agent or model"
            )

        async with self._database.session() as session, session.begin():
            task = await session.get(TaskRow, task_id)
            if task is None:
                raise TaskNotFoundError()
            # A stale caller is told so before its command is judged.
            if expected_version is not None and task.version != expected_version:
                raise TaskConflictError()
            plan = plan_transition(task.state, command, wait_reason=wait_reason)

            now = utcnow()
            step = await self._latest_step(session, task.id, task.attempt, lock=True)
            step_name = step.name if step is not None else None
            detail: dict[str, Any] = {}

            task.state = plan.target
            task.wait_reason = plan.wait_reason
            task.updated_at = now
            if command in (TaskCommand.RETRY, TaskCommand.RESTART):
                for key, new in (("agent", agent), ("model", model)):
                    if new is not None and new != getattr(task, key):
                        detail[key] = {"from": getattr(task, key), "to": new}
                        setattr(task, key, new)
            if command is TaskCommand.RETRY:
                task.retry_count += 1
            if command is TaskCommand.RESTART:
                detail["previous_attempt"] = task.attempt
                task.attempt += 1
                session.add(
                    TaskAttemptRow(
                        task_id=task.id,
                        number=task.attempt,
                        created_at=now,
                        updated_at=now,
                    )
                )
            # Stop Now aborts the running step on the spot and Fail ends it as
            # failed. Pause and Cancel are graceful: the worker finishes its
            # step itself (``finish_step``).
            if command in (TaskCommand.STOP_NOW, TaskCommand.FAIL):
                if step is not None and step.status is StepStatus.RUNNING:
                    step.status = (
                        StepStatus.INTERRUPTED
                        if command is TaskCommand.STOP_NOW
                        else StepStatus.FAILED
                    )
                    step.finished_at = now
            if command is TaskCommand.STOP_NOW:
                session.add(
                    TaskLogRow(
                        task_id=task.id,
                        attempt=task.attempt,
                        level=LogLevel.WARNING,
                        message=self._stop_now_message(step_name, reason),
                        created_at=now,
                    )
                )
            try:
                await session.flush()
            except StaleDataError:
                raise TaskConflictError() from None
            event = await self._append_event(
                session,
                task,
                command=command,
                from_state=plan.from_state,
                actor=actor,
                reason=reason,
                step_name=step_name,
                detail=detail or None,
            )
        await self._notify(event)
        return event

    # -- steps, logs and attempt state (bookkeeping by workers) --------------

    async def begin_step(self, task_id: uuid.UUID, name: str) -> StepInfo:
        """Start a step of the current attempt; it becomes the current step.

        Allowed while the task is running, waiting (independent safe work may
        continue) or evaluating, and only if no other step is running.
        """
        name = _text("name", name, MAX_NAME_LENGTH)
        async with self._database.session() as session, session.begin():
            # A shared lock keeps a concurrent state change from committing
            # between the state check and the insert.
            task = await self._require_task(session, task_id, share_lock=True)
            if task.state not in _STEP_ACTIVE_STATES:
                raise TaskStepError("A step can only start while the task is active")
            latest = await self._latest_step(session, task.id, task.attempt)
            if latest is not None and latest.status is StepStatus.RUNNING:
                raise TaskStepError("Another step is already running")
            row = TaskStepRow(
                task_id=task.id,
                attempt=task.attempt,
                sequence=(latest.sequence + 1) if latest is not None else 1,
                name=name,
                status=StepStatus.RUNNING,
                started_at=utcnow(),
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                raise TaskConflictError() from None
            return _step(row)

    async def finish_step(self, task_id: uuid.UUID, status: StepStatus) -> StepInfo:
        """Finish the running step of the current attempt.

        Allowed in any task state, so a worker that was asked to stop can still
        record how its step ended.
        """
        if status not in _FINISHED_STEP_STATUSES:
            raise InvalidCommandArgumentError(
                "A step is finished as succeeded, failed or interrupted"
            )
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id)
            step = await self._latest_step(session, task.id, task.attempt, lock=True)
            if step is None or step.status is not StepStatus.RUNNING:
                raise TaskStepError("No step is running")
            step.status = status
            step.finished_at = utcnow()
            await session.flush()
            return _step(step)

    async def add_log(
        self, task_id: uuid.UUID, message: str, *, level: LogLevel = LogLevel.INFO
    ) -> LogEntry:
        """Append a log line to the current attempt (allowed in any state).

        Callers must not pass secrets: redaction is not done here. Messages over
        ``MAX_LOG_MESSAGE_LENGTH`` characters are truncated.
        """
        if len(message) > MAX_LOG_MESSAGE_LENGTH:
            message = message[: MAX_LOG_MESSAGE_LENGTH - len(_TRUNCATED)] + _TRUNCATED
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id)
            row = TaskLogRow(
                task_id=task.id,
                attempt=task.attempt,
                level=level,
                message=message,
                created_at=utcnow(),
            )
            session.add(row)
            await session.flush()
            return LogEntry(
                row.seq, row.attempt, row.level, row.message, row.created_at
            )

    async def update_attempt(
        self,
        task_id: uuid.UUID,
        *,
        worktree: WorktreeState | None = None,
        review: ReviewState | None = None,
        pull_request: PullRequestInfo | None = None,
    ) -> AttemptSnapshot:
        """Replace the worktree / review / pull request state of the current attempt.

        Each group that is given replaces the stored one; groups left as ``None``
        are unchanged. Allowed in any state (a pull request can be merged after
        the task completed).
        """
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id, share_lock=True)
            row = (
                await session.execute(
                    select(TaskAttemptRow).where(
                        TaskAttemptRow.task_id == task.id,
                        TaskAttemptRow.number == task.attempt,
                    )
                )
            ).scalar_one()
            if worktree is not None:
                row.branch, row.worktree_path, row.head_commit = (
                    worktree.branch,
                    worktree.path,
                    worktree.head_commit,
                )
            if review is not None:
                row.review_status = review.review_status
                row.evaluation_result = review.evaluation_result
            if pull_request is not None:
                row.pr_number = pull_request.number
                row.pr_url = pull_request.url
                row.pr_state = pull_request.state
            row.updated_at = utcnow()
            await session.flush()
            return _attempt(row)

    # -- reading ---------------------------------------------------------------

    async def restore(
        self, task_id: uuid.UUID, *, log_limit: int = 100
    ) -> TaskSnapshot:
        """Rebuild the task's picture from the database alone.

        Read in one repeatable-read transaction so that state, current step,
        logs and worktree / review / PR state belong to the same moment.
        """
        if not 0 <= log_limit <= MAX_RESTORE_LOGS:
            raise InvalidCommandArgumentError(
                f"log_limit must be 0 to {MAX_RESTORE_LOGS}"
            )
        async with self._database.session() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            task = await self._require_task(session, task_id)
            attempts = (
                (
                    await session.execute(
                        select(TaskAttemptRow)
                        .where(TaskAttemptRow.task_id == task.id)
                        .order_by(TaskAttemptRow.number)
                    )
                )
                .scalars()
                .all()
            )
            current = next(row for row in attempts if row.number == task.attempt)
            step = await self._latest_step(session, task.id, task.attempt)
            logs = (
                (
                    await session.execute(
                        select(TaskLogRow)
                        .where(
                            TaskLogRow.task_id == task.id,
                            TaskLogRow.attempt == task.attempt,
                        )
                        .order_by(TaskLogRow.seq.desc())
                        .limit(log_limit)
                    )
                )
                .scalars()
                .all()
            )
            last_event = (
                await session.execute(
                    select(TaskEventRow)
                    .where(TaskEventRow.task_id == task.id)
                    .order_by(TaskEventRow.seq.desc())
                    .limit(1)
                )
            ).scalar_one()
            return TaskSnapshot(
                id=task.id,
                project_id=task.project_id,
                created_by=task.created_by,
                title=task.title,
                input=task.input,
                starting_commit=task.starting_commit,
                state=task.state,
                wait_reason=task.wait_reason,
                version=task.version,
                agent=task.agent,
                model=task.model,
                attempt=_attempt(current),
                retry_count=task.retry_count,
                current_step=_step(step) if step is not None else None,
                recent_logs=tuple(
                    LogEntry(
                        row.seq, row.attempt, row.level, row.message, row.created_at
                    )
                    for row in reversed(logs)
                ),
                last_event=_event(last_event),
                created_at=task.created_at,
                updated_at=task.updated_at,
                previous_attempts=tuple(
                    _attempt(row) for row in attempts if row is not current
                ),
            )

    async def history(
        self, task_id: uuid.UUID, *, after_seq: int = 0, limit: int = 500
    ) -> list[TaskEvent]:
        """The task's events in order, starting after ``after_seq``."""
        if not 1 <= limit <= 5000:
            raise InvalidCommandArgumentError("limit must be 1 to 5000")
        async with self._database.session() as session, session.begin():
            await self._require_task(session, task_id)
            rows = (
                (
                    await session.execute(
                        select(TaskEventRow)
                        .where(
                            TaskEventRow.task_id == task_id,
                            TaskEventRow.seq > after_seq,
                        )
                        .order_by(TaskEventRow.seq)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return [_event(row) for row in rows]

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _checked_input(value: dict[str, Any] | None) -> dict[str, Any]:
        value = {} if value is None else value
        try:
            encoded = json.dumps(value)
        except (TypeError, ValueError):
            raise InvalidCommandArgumentError("input must be a JSON object") from None
        if not isinstance(value, dict) or len(encoded) > MAX_INPUT_BYTES:
            raise InvalidCommandArgumentError(
                f"input must be a JSON object of at most {MAX_INPUT_BYTES} bytes"
            )
        return value

    @staticmethod
    def _stop_now_message(step_name: str | None, reason: str | None) -> str:
        message = (
            f"Stop Now: interrupted step {step_name!r}"
            if step_name
            else "Stop Now: no step"
        )
        return f"{message} (reason: {reason})" if reason else message

    @staticmethod
    async def _require_task(
        session: AsyncSession, task_id: uuid.UUID, *, share_lock: bool = False
    ) -> TaskRow:
        task = await session.get(
            TaskRow, task_id, with_for_update={"read": True} if share_lock else None
        )
        if task is None:
            raise TaskNotFoundError()
        return task

    @staticmethod
    async def _latest_step(
        session: AsyncSession, task_id: uuid.UUID, attempt: int, *, lock: bool = False
    ) -> TaskStepRow | None:
        query = (
            select(TaskStepRow)
            .where(TaskStepRow.task_id == task_id, TaskStepRow.attempt == attempt)
            .order_by(TaskStepRow.sequence.desc())
            .limit(1)
        )
        if lock:
            query = query.with_for_update()
        return (await session.execute(query)).scalar_one_or_none()

    @staticmethod
    async def _append_event(
        session: AsyncSession,
        task: TaskRow,
        *,
        command: TaskCommand,
        from_state: TaskState | None,
        actor: Actor,
        reason: str | None,
        step_name: str | None,
        detail: dict[str, Any] | None,
    ) -> TaskEvent:
        row = TaskEventRow(
            task_id=task.id,
            attempt=task.attempt,
            command=command,
            from_state=from_state,
            to_state=task.state,
            wait_reason=task.wait_reason,
            actor_kind=actor.kind,
            actor_id=actor.id,
            reason=reason,
            step_name=step_name,
            detail=detail,
            task_version=task.version,
            created_at=task.updated_at,
        )
        session.add(row)
        await session.flush()
        return _event(row)

    async def _notify(self, event: TaskEvent) -> None:
        for listener in self._listeners:
            try:
                await listener(event)
            except Exception as error:
                # The transition is already committed and durable; a failing
                # subscriber must not turn it into an error for the caller.
                logger.warning(
                    "Task transition listener failed: %s", type(error).__name__
                )
