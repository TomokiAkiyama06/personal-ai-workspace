"""Fan-out of a research request over the registered providers (PAW-051).

``ResearchBroker`` is the only thing the Main Agent talks to. It runs the
matching providers concurrently, each under its own timeout and all under one
global budget, isolates their failures, and returns one ``ResearchResult`` in
which every item has the same ``SourceMetadata``. This module performs no
network I/O itself and makes no authorisation decision: the caller (the Tool
Broker, PAW-031) must have checked the ``network`` capability before calling
``gather`` or ``fetch``.

Logging: one WARNING per failed provider on the logger
``paw_backend.research.providers`` (or a child of it) with the provider id,
kind, error code and the exception TYPE name. Never the exception text, the
query or a locator.
"""

from collections.abc import Callable
from datetime import datetime

from paw_backend.research.providers.contract import (
    DEFAULT_TIME_BUDGET_SECONDS,
    ResearchRequest,
    ResearchResult,
    SourceMetadata,
)
from paw_backend.research.providers.errors import ResearchErrorCode
from paw_backend.research.providers.registry import ProviderRegistry


def classify_failure(error: Exception) -> ResearchErrorCode:
    """Map an exception raised by a provider to a member of ``ResearchErrorCode``.

    * a ``ProviderFailure`` whose ``code`` attribute is (still) a
      ``ResearchErrorCode`` gives that code;
    * a ``TimeoutError`` (``asyncio.TimeoutError`` is the same class) gives
      ``TIMEOUT``;
    * everything else gives ``INTERNAL_ERROR``, including a ``ProviderFailure``
      whose ``code`` attribute was overwritten with something that is not a
      ``ResearchErrorCode``.

    The text of the exception is never read: the function must not call
    ``str()`` or ``repr()`` on it (they may raise, or contain secrets).
    """
    raise NotImplementedError("PAW-051 stub")


class ResearchBroker:
    """Concurrent, failure-isolating access to every registered provider.

    ``clock`` returns the current time as a timezone-aware datetime (UTC by
    default: ``datetime.now(timezone.utc)``); it is injectable for tests.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """``registry`` must be a ``ProviderRegistry`` (else ``TypeError``)."""
        raise NotImplementedError("PAW-051 stub")

    async def gather(self, request: ResearchRequest) -> ResearchResult:
        """Search every registered provider whose kind is in ``request.kinds``.

        A ``request`` that is not a ``ResearchRequest`` raises ``TypeError``.
        Provider failures never raise; the method returns a ``ResearchResult``.

        1. Select providers with ``registry.select(request.kinds)``. None
           selected: ``ResearchResult(items=(), errors=(), providers_queried=0)``
           (the clock is still read once).
        2. Start ``provider.search(request.query, limit=request.max_results)`` for
           ALL selected providers concurrently (never one after another). Each
           call may take at most ``min(entry.timeout_seconds, time still left of
           request.time_budget_seconds)``; a provider that is not done by then is
           cancelled, awaited until it has really finished, and reported with
           ``TIMEOUT``. When ``gather`` returns, no task it started is left
           running.
        3. An ``Exception`` raised by a provider is converted with
           ``classify_failure`` into one ``ResearchError(entry.name, entry.kind,
           code)`` and logged (see the module docstring); the other providers are
           unaffected. This is the one place where catching ``Exception`` is
           intended. ``asyncio.CancelledError`` and other ``BaseException`` are
           never swallowed: cancelling ``gather`` cancels every provider call and
           propagates.
        4. ``retrieved_at = clock()`` is read exactly once per ``gather`` call,
           AFTER all providers have finished or timed out, and shared by all
           items.
        5. A provider that returned normally is validated and normalised with
           ``normalize_hits(provider_id=entry.name, kind=entry.kind, hits=<the
           response>, limit=request.max_results, retrieved_at=...)``. If that
           raises ``InvalidProviderResponseError`` the provider is reported with
           ``INVALID_RESPONSE`` (logged like any failure), contributes no item,
           and the others are unaffected.
        6. ``merge_items`` over the successful providers' items (in registry
           order) with ``max_results=request.max_results`` gives ``items`` and
           ``truncated``.
        7. ``errors`` are in registry order (not completion order);
           ``providers_queried`` is the number of selected providers.

        Identity always comes from the registry entry (its snapshot ``name`` and
        ``kind``), never from the provider object at call time.
        """
        raise NotImplementedError("PAW-051 stub")

    async def fetch(
        self,
        source: SourceMetadata,
        *,
        time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS,
    ) -> ResearchResult:
        """Fetch the document of a source found earlier, from the same provider.

        ``source`` must be a ``SourceMetadata`` (else ``TypeError``);
        ``time_budget_seconds`` is checked with ``validate_time_budget``
        (``TypeError`` / ``ValueError``). ``source.locator`` is passed through
        ``canonicalize_locator`` first (its ``InvalidLocatorError`` is NOT
        caught: it means the caller built a bad ``SourceMetadata``).

        The provider is looked up by ``source.provider_id``. If it is not
        registered any more, or is registered with another kind than
        ``source.provider_kind``, the result has no items and one
        ``ResearchError(source.provider_id, source.provider_kind, UNAVAILABLE)``,
        with ``providers_queried=1``.

        Otherwise ``provider.fetch(<canonical locator>)`` runs under
        ``min(entry.timeout_seconds, time_budget_seconds)`` with the same failure
        rules as ``gather`` (``classify_failure``, ``TIMEOUT`` with cancellation,
        one WARNING log, ``CancelledError`` propagates). A response that is not a
        ``ProviderDocument`` is ``INVALID_RESPONSE``. On success the result has
        one item, ``providers_queried=1``, no errors, ``truncated=False``:
        ``ResearchItem(source=SourceMetadata(provider_kind=source.provider_kind,
        provider_id=source.provider_id, locator=<canonical locator>,
        title=normalize_title(doc.title), retrieved_at=clock(),
        content_hash=compute_content_hash(doc.text), source_type=doc.source_type,
        published_at=<doc.published_at in UTC or None>,
        private_source=source.private_source or doc.private_source),
        text=doc.text)``.
        """
        raise NotImplementedError("PAW-051 stub")
