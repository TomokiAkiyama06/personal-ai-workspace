"""Typed errors of the task queue, budgets and loop detection (PAW-033).

They extend ``paw_backend.tasks.errors.TaskError`` so that the API layer can
handle every task-related failure in one place. Messages are fixed strings: they
never contain a caller-supplied value (worker ids, failure messages, error
class names, ...), a driver message or a connection string. ``code`` is the
stable machine-readable identifier.

An unknown task raises the existing ``TaskNotFoundError``.
"""

from paw_backend.tasks.errors import TaskError


class QueueingError(TaskError):
    """Base class of every error raised by ``paw_backend.tasks.queueing``."""

    code = "queueing_error"


class InvalidQueueingArgumentError(QueueingError):
    """A caller passed an argument of the wrong type or outside its bounds.

    ``parameter`` is the name of the offending parameter (a constant of this
    package, never user input). The value itself is never part of the message.
    """

    code = "invalid_queueing_argument"

    def __init__(self, parameter: str) -> None:
        self.parameter = parameter
        super().__init__(f"Invalid value for {parameter}")


class TaskAlreadyQueuedError(QueueingError):
    """The task already has an active (queued or claimed) queue entry."""

    code = "task_already_queued"

    def __init__(self) -> None:
        super().__init__("Task already has an active queue entry")


class LeaseLostError(QueueingError):
    """The worker does not hold a valid lease on the queue entry.

    Raised when the entry does not exist, is not claimed, is claimed by another
    worker, has been claimed again since the caller's claim (another
    ``claim_count``, also by the same worker id), was cancelled, or the worker's
    lease has expired. The cases are not distinguished, so that the error reveals
    nothing about other workers.
    """

    code = "queue_lease_lost"

    def __init__(self) -> None:
        super().__init__("Queue entry is not leased to this worker")


class StaleRuntimeSessionError(QueueingError):
    """The runtime timer of the task has been taken over by a newer session.

    Raised by ``BudgetTracker.stop_runtime`` when the caller's ``generation`` is
    not the generation of the task's current runtime session (a newer
    ``start_runtime`` began a new session, for example the worker that reclaimed
    the entry after this worker's lease expired, or the run of a restarted task).
    Nothing is written: the newer session's ``running_since`` and the accumulated
    runtime stay as they are. Like ``LeaseLostError`` it reveals nothing about
    the other session.
    """

    code = "runtime_session_stale"

    def __init__(self) -> None:
        super().__init__("The runtime timer belongs to a newer session")


class BudgetNotConfiguredError(QueueingError):
    """The task has no budget yet: call ``BudgetTracker.set_preset`` first.

    A task without a budget is never treated as unlimited.
    """

    code = "budget_not_configured"

    def __init__(self) -> None:
        super().__init__("No budget preset is set for this task")
