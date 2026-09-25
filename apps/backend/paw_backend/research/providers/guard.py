"""Retraction of cancellation requests made by synchronous adapter code (PAW-051).

Shared by the registry (``register`` / ``validate_provider`` read and inspect the
adapter's members) and the broker (it calls the adapter and validates its answers).
Both are windows in which the adapter's code runs without an ``await``.
"""

import asyncio


class CancelGuard:
    """Retract the cancellation requests that synchronous adapter code makes.

    ``with CancelGuard() as guard:`` around adapter code that runs without an
    ``await``. Such code can call ``asyncio.current_task().cancel()`` and return
    normally: nothing is raised, but the request stays on the task and the next
    ``await`` (or the end of the task) delivers it, cancelling ``gather()`` and
    discarding the answers of the healthy providers. On exit the guard compares
    ``Task.cancelling()`` with its value on entry and calls ``Task.uncancel()``
    for the increase, and only for that: a request that was already there (a
    caller's own ``cancel()``) stays and is delivered as before. ``retracted`` is
    the number of requests taken back; a caller treats a non-zero value as a
    failure of the adapter. It works on the task that runs the guard, and does
    nothing outside a task (``asyncio.current_task()`` is ``None``).

    Limit: ``Task.uncancel()`` clears the "cancel at the next await" flag only when
    the count reaches 0, so a task that had a request counted but not pending
    (its ``CancelledError`` was swallowed, ``uncancel()`` never called) keeps the
    flag that the adapter set. An adapter that itself calls ``uncancel()`` on the
    task, lowering the count, is not detected (Decision 0012).
    """

    __slots__ = ("_before", "_task", "retracted")

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._before = 0
        self.retracted = 0

    def __enter__(self) -> "CancelGuard":
        try:
            self._task = asyncio.current_task()
        except RuntimeError:  # no running loop
            self._task = None
        if self._task is not None:
            self._before = self._task.cancelling()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._task is None:
            return
        for _ in range(max(self._task.cancelling() - self._before, 0)):
            self._task.uncancel()
            self.retracted += 1
