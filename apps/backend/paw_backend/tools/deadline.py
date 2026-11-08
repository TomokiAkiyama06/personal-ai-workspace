"""The deadline of the tool call that is running (issue #346).

:class:`~paw_backend.tools.runner.ToolRunner` bounds every executor call with
its ``execution_timeout`` (an ``asyncio.timeout``) and says until when in this
context variable, so that code deep inside the call (the sandbox pool) can tell
whether trying something once more still fits in the call's time, without
being handed the limit through every layer. A nested call (the git shim's call
from inside a sandbox command) keeps the earlier of the two deadlines.

The value is a ``time.monotonic()`` instant (the clock ``asyncio``'s loop
uses), never extended by anything here: it only lets a caller decide NOT to
start something that could not end in time.
"""

import contextlib
import time
from collections.abc import Iterator
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("paw_tool_call_deadline", default=None)


@contextlib.contextmanager
def call_deadline(seconds: float) -> Iterator[None]:
    """The running call ends within ``seconds`` (or earlier, when an enclosing
    call ends earlier)."""
    deadline = time.monotonic() + seconds
    current = _DEADLINE.get()
    if current is not None:
        deadline = min(deadline, current)
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def time_left() -> float | None:
    """Seconds until the running tool call's deadline (``None``: no call says
    one)."""
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


__all__ = ["call_deadline", "time_left"]
