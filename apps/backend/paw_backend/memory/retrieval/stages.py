"""Calling a component that this code does not own (Embedder, Reranker, sources).

A foreign component can be slow, raise anything, or answer nonsense. Every call is
bounded in time and turned into one typed error carrying only the component's name;
the exception itself is never chained, echoed or stored: its text can hold query
text, memory text or a connection string. The log line carries the component and a
fixed exception type name (a name from the builtin exceptions or ``component_error``),
never an attribute of a foreign class.
"""

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable

from paw_backend.memory.retrieval.errors import Component, RetrievalSourceError

logger = logging.getLogger(__name__)

# The classes whose names may be logged: the builtin exceptions, looked up by
# identity so that a foreign class named "ValueError" is not one of them.
_BUILTIN_ERRORS = frozenset(
    value
    for value in vars(builtins).values()
    if isinstance(value, type) and issubclass(value, Exception)
)


def error_kind(error: BaseException) -> str:
    """The name to log for ``error``: a builtin exception's, else a fixed word."""
    kind = type(error)
    return kind.__name__ if kind in _BUILTIN_ERRORS else "component_error"


async def bounded[T](
    call: Callable[[], Awaitable[T]], component: Component, timeout_seconds: float
) -> T:
    """``await call()`` within ``timeout_seconds``; any failure is a source error.

    ``call`` is invoked inside the guarded block, so a component whose method
    raises before returning an awaitable is handled like one that fails later.
    Cancellation (the caller's own deadline) is not a component failure and
    passes through.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            return await call()
    except Exception as error:
        logger.warning(
            "retrieval component failed: component=%s exception_type=%s",
            component.value,
            error_kind(error),
        )
        raise RetrievalSourceError(component) from None
