"""Up-front validation of the adapters a broker is built with.

A wrong adapter (a missing method, a plain function where a coroutine is
required, a wrong number of parameters) must fail loudly when the broker is
built, not later as an "everything failed" outcome.
"""

import inspect
from collections.abc import Callable
from typing import Any


def require_async_method(obj: object, name: str, parameters: int) -> None:
    """``obj.<name>``: a coroutine function taking ``parameters`` positional args."""
    method = getattr(obj, name, None)
    if not callable(method) or not inspect.iscoroutinefunction(method):
        raise TypeError(f"{type(obj).__name__}.{name} must be an async method")
    _require_arity(method, f"{type(obj).__name__}.{name}", parameters)


def require_callable(fn: object, label: str, parameters: int) -> None:
    """A plain or async callable that accepts ``parameters`` positional arguments."""
    if not callable(fn):
        raise TypeError(f"{label} must be callable")
    _require_arity(fn, label, parameters)


def _require_arity(fn: Callable[..., Any], label: str, parameters: int) -> None:
    """``fn`` must be callable with exactly ``parameters`` positional arguments.

    Required keyword-only parameters (``now=``, ``correlation_id=``) are the
    caller's to pass and are not counted.
    """
    try:
        signature = inspect.signature(fn)
    except ValueError:  # a builtin without a signature: nothing to check
        return
    positional = [
        p
        for p in signature.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    variadic = any(p.kind is p.VAR_POSITIONAL for p in signature.parameters.values())
    required = [p for p in positional if p.default is p.empty]
    if len(required) > parameters or (not variadic and parameters > len(positional)):
        raise TypeError(f"{label} must accept {parameters} positional argument(s)")
