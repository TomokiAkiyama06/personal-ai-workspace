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
    """A step was started or finished in a situation that does not allow it."""

    code = "task_step_error"
