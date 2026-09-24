"""Persistence and commands of the Agent Task lifecycle.

The database is the only source of truth. A command runs in one transaction:
it reads the task, asks ``domain.plan_transition`` whether the command is
allowed, updates the task with an optimistic version check, and appends the
history event. Nothing is kept in process memory, so a restarted backend or a
reconnecting client sees exactly what ``restore`` reads from PostgreSQL.

This layer performs no authorisation. The API layer (PAW-022 / PAW-025) must
decide who may issue a command before calling it, and pass the authenticated
user as the ``Actor``.

Concurrency: every command, and every step / tool / attempt-state write, first
takes the task's row lock (``SELECT ... FOR NO KEY UPDATE``) and only then reads
what it decides on (the task's state, its latest step). They therefore run one
after another per task. A transition cannot miss a step that a concurrent
``begin_step`` is about to commit, and ``begin_step`` cannot start a step after
a transition ended the task: whichever comes second sees the other's result.
Changes of the task's row also increment ``tasks.version`` and are applied with
``UPDATE ... WHERE version = <read>`` as a backstop; a caller that holds a version
it saw earlier passes it as ``expected_version`` so that a stale decision is
rejected (``TaskConflictError``) even if it arrives later.

Bookkeeping by a worker (steps, tool calls, logs, attempt state) names the
attempt it works for. Once Restart has started a newer attempt, a superseded
worker gets ``StaleAttemptError`` and cannot touch the new attempt.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
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
    StaleAttemptError,
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
    TaskToolInvocationRow,
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
    ToolInvocationInfo,
    ToolInvocationStatus,
    WorktreeState,
)

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 200
MAX_REASON_LENGTH = 500
MAX_NAME_LENGTH = 100
MAX_LOG_MESSAGE_LENGTH = 8000
MAX_INPUT_BYTES = 256 * 1024
MAX_RESTORE_LOGS = 1000
MAX_RESTORE_TOOL_INVOCATIONS = 100
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
# Commands that end the step that is running when they arrive, and how it ends.
# Restart does so for the attempt it abandons; Pause and Cancel are graceful and
# leave the step to its worker (``finish_step``).
_STEP_ENDING_COMMANDS = {
    TaskCommand.STOP_NOW: StepStatus.INTERRUPTED,
    TaskCommand.FAIL: StepStatus.FAILED,
    TaskCommand.RESTART: StepStatus.INTERRUPTED,
}
# Their event names the step they ended (or none); every other command names the
# latest step, which is what Retry and Restart need to say where they resume from.
_NAMES_ENDED_STEP = frozenset({TaskCommand.STOP_NOW, TaskCommand.FAIL})
_FINISHED_TOOL_STATUSES = frozenset(
    {
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.FAILED,
        ToolInvocationStatus.INTERRUPTED,
    }
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
        id=row.id,
        attempt=row.attempt,
        sequence=row.sequence,
        name=row.name,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _tool(row: TaskToolInvocationRow) -> ToolInvocationInfo:
    return ToolInvocationInfo(
        id=row.id,
        step_id=row.step_id,
        tool_name=row.tool_name,
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
            # Lock first, then read: whatever a concurrent begin_step commits is
            # visible to the reads below, and whatever it has not committed
            # cannot start after this transaction ended the task.
            task = await self._require_task(session, task_id, lock=True)
            # A stale caller is told so before its command is judged.
            if expected_version is not None and task.version != expected_version:
                raise TaskConflictError()
            plan = plan_transition(task.state, command, wait_reason=wait_reason)

            now = utcnow()
            step = await self._latest_step(session, task.id, task.attempt, lock=True)
            step_name = step.name if step is not None else None
            step_running = step is not None and step.status is StepStatus.RUNNING
            if command is TaskCommand.COMPLETE and step_running:
                # Otherwise a step would stay running on a completed task.
                raise TaskStepError("A step is still running")
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
            # failed. Restart abandons the old attempt, so a step that a
            # graceful Cancel left running is closed too. Pause and Cancel are
            # graceful: the worker finishes its step itself (``finish_step``).
            ended_step: str | None = None  # a step that THIS command ended
            if step is not None and command in _STEP_ENDING_COMMANDS:
                closed = await self._close_step(
                    session, step, _STEP_ENDING_COMMANDS[command], now
                )
                if closed:
                    ended_step = step.name
            if command is TaskCommand.STOP_NOW:
                session.add(
                    TaskLogRow(
                        task_id=task.id,
                        attempt=task.attempt,
                        level=LogLevel.WARNING,
                        message=self._stop_now_message(ended_step, reason),
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
                # Stop Now and Fail name a step only if they actually ended it; a
                # step that had already finished is not "interrupted" by them.
                step_name=ended_step if command in _NAMES_ENDED_STEP else step_name,
                detail=detail or None,
            )
        await self._notify(event)
        return event

    # -- steps, logs and attempt state (bookkeeping by workers) --------------

    async def begin_step(
        self, task_id: uuid.UUID, name: str, *, attempt: int
    ) -> StepInfo:
        """Start a step of ``attempt``; it becomes the current step.

        ``attempt`` is the attempt the worker was started for (``TaskEvent.attempt``
        of its Start event). Allowed while the task is running, waiting
        (independent safe work may continue) or evaluating, and only if no other
        step is running. Raises ``StaleAttemptError`` for a superseded attempt.
        """
        name = _text("name", name, MAX_NAME_LENGTH)
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id, lock=True)
            self._require_current_attempt(task, attempt)
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

    async def finish_step(
        self, task_id: uuid.UUID, step_id: int, status: StepStatus
    ) -> StepInfo:
        """Finish the step ``step_id`` (from ``begin_step``) as ``status``.

        Allowed in any task state, so a worker that was asked to stop can still
        record how its step ended. Tool calls of the step that are still started
        are marked interrupted. Raises ``StaleAttemptError`` if the step belongs
        to an attempt that a Restart replaced, and ``TaskStepError`` if the step
        is unknown or no longer running (for example Stop Now ended it).
        """
        if status not in _FINISHED_STEP_STATUSES:
            raise InvalidCommandArgumentError(
                "A step is finished as succeeded, failed or interrupted"
            )
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id, lock=True)
            step = await session.get(TaskStepRow, step_id, with_for_update=True)
            if step is None or step.task_id != task.id:
                raise TaskStepError("Unknown step")
            self._require_current_attempt(task, step.attempt)
            if step.status is not StepStatus.RUNNING:
                raise TaskStepError("The step is not running")
            await self._close_step(session, step, status, utcnow())
            await session.flush()
            return _step(step)

    async def begin_tool_invocation(
        self,
        task_id: uuid.UUID,
        *,
        step_id: int,
        tool_name: str,
        invocation_id: uuid.UUID | None = None,
    ) -> ToolInvocationInfo:
        """Record that the running step ``step_id`` started a tool call.

        Only the tool's name and the call's status are kept, never its arguments
        or output. The Tool Broker (PAW-031) may pass its own ``invocation_id``
        so that both sides name the same call.
        """
        tool_name = _text("tool_name", tool_name, MAX_NAME_LENGTH)
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id, lock=True)
            step = await session.get(TaskStepRow, step_id)
            if step is None or step.task_id != task.id:
                raise TaskStepError("Unknown step")
            self._require_current_attempt(task, step.attempt)
            if task.state not in _STEP_ACTIVE_STATES:
                raise TaskStepError(
                    "A tool call can only start while the task is active"
                )
            if step.status is not StepStatus.RUNNING:
                raise TaskStepError("The step is not running")
            row = TaskToolInvocationRow(
                id=invocation_id or uuid.uuid4(),
                task_id=task.id,
                step_id=step.id,
                tool_name=tool_name,
                status=ToolInvocationStatus.STARTED,
                started_at=utcnow(),
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                raise TaskConflictError() from None
            return _tool(row)

    async def finish_tool_invocation(
        self,
        task_id: uuid.UUID,
        invocation_id: uuid.UUID,
        status: ToolInvocationStatus,
    ) -> ToolInvocationInfo:
        """Record how a started tool call ended (allowed in any task state)."""
        if status not in _FINISHED_TOOL_STATUSES:
            raise InvalidCommandArgumentError(
                "A tool call is finished as succeeded, failed or interrupted"
            )
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id, lock=True)
            row = await session.get(
                TaskToolInvocationRow, invocation_id, with_for_update=True
            )
            if row is None or row.task_id != task.id:
                raise TaskStepError("Unknown tool invocation")
            step = await session.get(TaskStepRow, row.step_id)
            self._require_current_attempt(task, step.attempt)
            if row.status is not ToolInvocationStatus.STARTED:
                raise TaskStepError("The tool call is not running")
            row.status = status
            row.finished_at = utcnow()
            await session.flush()
            return _tool(row)

    async def add_log(
        self,
        task_id: uuid.UUID,
        message: str,
        *,
        attempt: int,
        level: LogLevel = LogLevel.INFO,
    ) -> LogEntry:
        """Append a log line to ``attempt`` (allowed in any task state).

        Raises ``StaleAttemptError`` unless ``attempt`` is the current attempt.
        Callers must not pass secrets: redaction is not done here. Messages over
        ``MAX_LOG_MESSAGE_LENGTH`` characters are truncated.
        """
        if len(message) > MAX_LOG_MESSAGE_LENGTH:
            message = message[: MAX_LOG_MESSAGE_LENGTH - len(_TRUNCATED)] + _TRUNCATED
        async with self._database.session() as session, session.begin():
            # No lock: the row is tagged with the caller's attempt, so even when a
            # Restart commits at the same moment the line stays with its own
            # attempt and never lands in the new one.
            task = await self._require_task(session, task_id)
            self._require_current_attempt(task, attempt)
            row = TaskLogRow(
                task_id=task.id,
                attempt=attempt,
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
        attempt: int,
        worktree: WorktreeState | None = None,
        review: ReviewState | None = None,
        pull_request: PullRequestInfo | None = None,
    ) -> AttemptSnapshot:
        """Replace the worktree / review / pull request state of ``attempt``.

        Each group that is given replaces the stored one; groups left as ``None``
        are unchanged. Allowed in any task state (a pull request can be merged
        after the task completed) but only for the current attempt
        (``StaleAttemptError`` otherwise).
        """
        async with self._database.session() as session, session.begin():
            task = await self._require_task(session, task_id, lock=True)
            self._require_current_attempt(task, attempt)
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
            invocations = (
                (
                    await session.execute(
                        select(TaskToolInvocationRow)
                        .where(TaskToolInvocationRow.step_id == step.id)
                        .order_by(
                            TaskToolInvocationRow.started_at.desc(),
                            TaskToolInvocationRow.id.desc(),
                        )
                        .limit(MAX_RESTORE_TOOL_INVOCATIONS)
                    )
                )
                .scalars()
                .all()
                if step is not None
                else []
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
                tool_invocations=tuple(_tool(row) for row in reversed(invocations)),
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
    def _stop_now_message(interrupted_step: str | None, reason: str | None) -> str:
        message = (
            f"Stop Now: interrupted step {interrupted_step!r}"
            if interrupted_step
            else "Stop Now: no step was running"
        )
        return f"{message} (reason: {reason})" if reason else message

    @staticmethod
    async def _require_task(
        session: AsyncSession, task_id: uuid.UUID, *, lock: bool = False
    ) -> TaskRow:
        """Load the task; ``lock`` takes the row lock that serialises writers.

        ``FOR NO KEY UPDATE`` is what the UPDATE of the task row takes anyway; it
        does not block the foreign-key checks of concurrent log / event inserts.
        """
        task = await session.get(
            TaskRow, task_id, with_for_update={"key_share": True} if lock else None
        )
        if task is None:
            raise TaskNotFoundError()
        return task

    @staticmethod
    def _require_current_attempt(task: TaskRow, attempt: int) -> None:
        if attempt != task.attempt:
            raise StaleAttemptError()

    @staticmethod
    async def _close_step(
        session: AsyncSession, step: TaskStepRow, status: StepStatus, now: datetime
    ) -> bool:
        """End ``step`` if it is running; a tool call it still runs ends with it.

        Returns whether this call ended a running step. A step that had already
        finished is left exactly as it is and ``False`` is returned.
        """
        if step.status is not StepStatus.RUNNING:
            return False
        step.status = status
        step.finished_at = now
        await session.execute(
            update(TaskToolInvocationRow)
            .where(
                TaskToolInvocationRow.step_id == step.id,
                TaskToolInvocationRow.status == ToolInvocationStatus.STARTED,
            )
            .values(status=ToolInvocationStatus.INTERRUPTED, finished_at=now)
            .execution_options(synchronize_session=False)
        )
        return True

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
