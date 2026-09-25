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


class StaleAttemptError(StaleRunError):
    """A ``StaleRunError`` whose attempt is not the task's current one.

    A Restart started a newer attempt. A worker that only wants to know whether it
    was superseded catches ``StaleRunError``, which covers this as well.
    """

    code = "stale_attempt"
    message = "The task has moved on to a newer attempt"
