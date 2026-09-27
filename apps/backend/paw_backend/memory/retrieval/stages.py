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
import math
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


def component_number(value: object) -> float | None:
    """``value`` as a finite ``float``, or ``None`` for anything else. Never raises.

    Only an ``int`` or a ``float`` (a ``bool`` is neither) can be a number a component
    answers with; ``Decimal``, ``Fraction``, text and the rest are refused. The
    conversion is guarded because it can fail in ways an ``isinstance`` check does not
    show: ``float(10**400)`` raises ``OverflowError`` (an int beyond the range of a
    float), and a subclass may make ``__float__`` raise anything. Not a number, or not
    finite, means the component's answer is invalid, not that the call fails.
    """
    try:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None


def unit_vector(numbers: list[float]) -> list[float] | None:
    """``numbers`` scaled to length 1, or ``None`` when it has no direction.

    A cosine distance only uses the direction, so this changes no ranking; it keeps
    every value in -1..1. That matters because the vector goes to pgvector as
    ``float4``: 1e39 is refused by the database (an error that would fail the whole
    retrieval), and 1e30 squared overflows inside the distance (``NaN``). Finite
    doubles up to 1.8e308 are therefore fine: the scaling divides by the largest
    absolute value first, so the sum of squares cannot overflow.
    """
    largest = max((abs(number) for number in numbers), default=0.0)
    if largest == 0.0:
        return None
    scaled = [number / largest for number in numbers]
    length = math.sqrt(sum(number * number for number in scaled))
    return [number / length for number in scaled]
