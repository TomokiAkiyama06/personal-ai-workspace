"""The optional pre-flight of ``ResearchBroker.gather`` (PAW-053).

A pre-flight runs after the broker has chosen the providers and before any of
them is called. It may rewrite the request (the Research Privacy Filter replaces
the query with a minimised one) or raise to refuse it, in which case no provider
is called at all. This module only defines the boundary, so that the broker does
not depend on any particular filter.

The pre-flight is REQUIRED by ``ResearchBroker.gather``: a broker without one
refuses to search (``PreflightRequiredError``) unless it was built with the
explicit opt-out ``unfiltered=True`` (Decision 0010).
"""

import inspect
from typing import Protocol, runtime_checkable

from paw_backend.research.providers.contract import ProviderKind, ResearchRequest


class PreflightNotConfiguredError(Exception):
    """A pre-flight input was given to a broker that has no pre-flight.

    Ignoring the input would send a query the caller expected to be checked, so
    the broker refuses instead. The message is fixed.
    """

    def __init__(self) -> None:
        super().__init__(
            "a pre-flight input was given but the broker has no pre-flight"
        )


class PreflightRequiredError(PreflightNotConfiguredError):
    """``gather`` was called on a broker that has no pre-flight and is not
    ``unfiltered``.

    Without a pre-flight the query would reach every provider unchecked and
    unaudited, so the broker fails closed and calls no provider. The fix is to
    configure a pre-flight (``ResearchBroker(registry, preflight=gate)``) or, for
    a caller that has nothing private, to say so with ``unfiltered=True``. The
    message is fixed and contains neither the query nor any provider name.
    """

    def __init__(self) -> None:
        Exception.__init__(
            self,
            "the broker has no pre-flight: configure one, or construct it with "
            "unfiltered=True to send queries as they are",
        )


@runtime_checkable
class SearchPreflight(Protocol):
    """Checks and rewrites a search request just before it is sent.

    ``kinds`` are the kinds of the providers that are about to be queried (never
    empty). ``subject`` is whatever the caller passed as ``preflight_input`` (or
    ``None``); the pre-flight alone decides what to make of it and must treat a
    missing or unknown subject as a reason to refuse. It returns the request to
    use (a ``ResearchRequest``) or raises; ``asyncio.CancelledError`` must not be
    suppressed.
    """

    async def preflight(
        self, request: ResearchRequest, kinds: frozenset[ProviderKind], subject: object
    ) -> ResearchRequest: ...


def validate_preflight(preflight: object) -> None:
    """Raise ``TypeError`` unless ``preflight.preflight`` is an ``async def`` that
    accepts ``(request, kinds, subject)`` as three positional arguments."""
    method = getattr(preflight, "preflight", None)
    if not inspect.iscoroutinefunction(method):
        raise TypeError("preflight must implement SearchPreflight")
    try:
        inspect.signature(method).bind(object(), object(), object())
    except (TypeError, ValueError):
        raise TypeError("preflight must implement SearchPreflight") from None
