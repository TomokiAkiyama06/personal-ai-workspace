"""Typed errors of the task lifecycle.

Messages are fixed strings. They never contain user-supplied content (titles,
reasons, step names) or driver messages, so they are safe to log and to map to
an API response later. ``code`` is the stable machine-readable identifier.
"""

from typing import ClassVar


class TaskError(Exception):
    """Base class of every error raised by the task lifecycle layer."""

    code: ClassVar[str] = "task_error"


class TaskNotFoundError(TaskError):
    code = "task_not_found"

    def __init__(self) -> None:
        super().__init__("Task not found")


class IllegalTransitionError(TaskError):
    """The command is not allowed in the task's current state."""

    code = "illegal_transition"

    def __init__(self, state: str, command: str) -> None:
        # Both values are members of closed enums, never user input.
        self.state = state
        self.command = command
        super().__init__(f"Command {command} is not allowed in state {state}")


class InvalidCommandArgumentError(TaskError):
    """A command was given an argument it does not accept (or lacks a required one)."""

    code = "invalid_command_argument"


class TaskConflictError(TaskError):
    """Another writer changed the task first (optimistic-concurrency failure)."""

    code = "task_conflict"

    def __init__(self) -> None:
        super().__init__("Task was modified concurrently; reload and retry")


class TaskStepError(TaskError):
    """A step or tool invocation was started or finished where that is not allowed."""

    code = "task_step_error"


class ProjectNotActiveError(TaskError):
    """The project of the task is not Active, so no new work is admitted.

    Raised by ``TaskService.create_task``, Retry, Restart and Start and by
    ``TaskQueue.enqueue`` when the Project state gate (``tasks.project_gate``) finds
    the project Archived, Pending deletion or Deleted (Decisions 0008 and 0020).
    Nothing is written. An unknown project is refused the same way (default deny).
    The message names no project, state or task.
    """

    code = "project_not_active"

    def __init__(self) -> None:
        super().__init__("The project is not active")


class TaskNotActiveError(TaskError):
    """A repository write (or an execution) is asked for a task that ended
    (completed, failed or cancelled: a Stop Now too). Nothing is admitted or
    written (issue #85: no write lands after the task was judged or stopped)."""

    code = "task_not_active"

    def __init__(self) -> None:
        super().__init__("The task has ended")


class StaleRunError(TaskError):
    """The caller works for a run of the task that a Retry or Restart replaced.

    Raised for step, log and attempt-state bookkeeping by a worker whose run
    (``TaskRun``: attempt and retry count) is no longer the task's current one.
    Nothing is written, so a superseded worker cannot disturb the run that took
    over.
    """

    code = "stale_run"
    message: ClassVar[str] = "The task has moved on to a newer run"

    def __init__(self) -> None:
        super().__init__(self.message)


class TaskNotRunningError(TaskError):
    """New work was to start for a run of a task that is not ``running`` now.

    Raised (nothing written) by the writes that START work on behalf of a run:
    a node's attempt (``DagStore.start_node``), the step charged for starting a
    node or a planner call (``BudgetTracker.record(..., require_running=True)``)
    and the runtime timer of a run (``BudgetTracker.start_runtime_in(...,
    run=...)``). A paused or waiting task quiesces: what already runs may finish
    and report, but nothing new starts until it runs again.
    """

    code = "task_not_running"
    message: ClassVar[str] = "The task is not running"

    def __init__(self) -> None:
        super().__init__(self.message)


class StaleAttemptError(StaleRunError):
    """A ``StaleRunError`` whose attempt is not the task's current one.

    A Restart started a newer attempt. A worker that only wants to know whether it
    was superseded catches ``StaleRunError``, which covers this as well. The loop
    failure records of PAW-033 (``record_failure``) also raise it, after a Restart.
    """

    code = "stale_attempt"
    message = "The task has moved on to a newer attempt"


class WorkingSetError(TaskError):
    """A change of the Working Set, or a transition it guards, is refused.

    Nothing is written. The message names no repository, role or task.
    """

    code = "working_set_error"
    message: ClassVar[str] = "The working set does not allow this"

    def __init__(self) -> None:
        super().__init__(self.message)


class WorkingSetChangeInvalidError(WorkingSetError):
    """The operation does not apply to the repository's current role (adding one
    that is already in the Working Set, promoting to the role it has, ...)."""

    code = "working_set_change_invalid"
    message = "The operation does not apply to the repository's current role"


class WorkingSetConflictError(WorkingSetError):
    """The repository's role is not the one the change was decided on.

    The Tool Broker decides a change on the role the task scope showed; the role
    stored now differs (another change came first), so the decision does not hold.
    """

    code = "working_set_conflict"
    message = "The working set changed; decide again"


class LastTargetRemovalRefusedError(WorkingSetError):
    """The change would leave the task without a ``target`` repository
    (Decision 0030, sections 2 and 3: a task always has one)."""

    code = "last_target_removal_refused"
    message = "The only target repository cannot be downgraded or removed"


class ModifiedRepositoryDowngradeRefusedError(WorkingSetError):
    """A repository the current attempt could have changed is downgraded or
    removed, and the backend could not verify that the change was discarded
    (clean worktree, HEAD at the repository's starting commit, branch not pushed,
    no open pull request). Decision 0030, section 3; fail-closed."""

    code = "modified_repository_downgrade_refused"
    message = "The repository's changes were not verifiably discarded"


class RepositoryWriteInFlightError(WorkingSetError):
    """A write (or an execution) that the Tool Broker admitted may still be
    running: its executor has not released the reservation, which has not
    expired either (Codex review of #85, P1). Raised for a downgrade or a removal
    of that repository (a clean worktree says nothing about the write to come, so
    nothing is judged discarded), and for Begin evaluation and Complete of the
    task (neither judges a repository that may still change). The command is
    asked again once the call ended."""

    code = "repository_write_in_flight"
    message = "A write on the repository may still be running"


class RepositoryWriteNotFoundError(WorkingSetError):
    """A manual release names a reservation the task does not have (issue #129)."""

    code = "repository_write_not_found"
    message = "The task has no such repository write"


class RepositoryWriteNotHeldError(WorkingSetError):
    """A manual release names a reservation that no longer holds anything: its
    executor released it, it was released by hand before, or it expired
    (issue #129). Nothing is written."""

    code = "repository_write_not_held"
    message = "The repository write is no longer reserved"


class RepositoryWriteHolderAliveError(WorkingSetError):
    """A manual release while a worker still holds a valid lease on the task's
    queue entry (issue #129, Decision 0049): the process that was
    admitted the write may be alive, so its reservation is not given up by
    hand. Asked again once the lease ended."""

    code = "repository_write_holder_alive"
    message = "A worker of the task is still alive"


class NoTargetRepositoryError(WorkingSetError):
    """Start of a task whose Working Set has no ``target`` repository."""

    code = "no_target_repository"
    message = "The task has no target repository"


class CompletionRequirementsNotMetError(WorkingSetError):
    """Complete of a task one of whose repositories lacks what it must have
    (Decision 0030, section 5): see ``TaskSnapshot`` for which one."""

    code = "completion_requirements_not_met"
    message = "A repository of the task does not meet its completion requirements"


class RepositoryNotInAttemptError(WorkingSetError):
    """Attempt state for a repository the task's current attempt does not have."""

    code = "repository_not_in_attempt"
    message = "The repository is not part of the task's current attempt"


class RepositoryRoleUnresolvedError(WorkingSetError):
    """A use of a repository that is not in the task's Working Set now (never
    added, removed, or without a state row in the current attempt): its role
    cannot be resolved, so nothing may touch it (Decision 0030, 4.5)."""

    code = "repository_role_unresolved"
    message = "The repository has no role in the task's working set"


class RepositoryRoleInsufficientError(WorkingSetError):
    """A use of a repository that its stored role does not allow (a write outside
    ``working`` / ``target``, a pull request outside ``target``, something
    executed in a ``referenced`` one; Decision 0030, 4.2, #85 constraint 2)."""

    code = "repository_role_insufficient"
    message = "The repository's role in the working set does not allow this"
