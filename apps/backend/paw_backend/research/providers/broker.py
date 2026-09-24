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

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import NamedTuple

from paw_backend.research.providers.contract import (
    DEFAULT_TIME_BUDGET_SECONDS,
    ProviderDocument,
    ProviderKind,
    ResearchError,
    ResearchItem,
    ResearchRequest,
    ResearchResult,
    SourceMetadata,
    validate_time_budget,
)
from paw_backend.research.providers.errors import (
    InvalidProviderResponseError,
    ProviderFailure,
    ResearchErrorCode,
    UnknownProviderError,
)
from paw_backend.research.providers.locator import canonicalize_locator
from paw_backend.research.providers.normalize import (
    compute_content_hash,
    merge_items,
    normalize_hits,
    normalize_title,
    published_utc,
)
from paw_backend.research.providers.registry import ProviderRegistry, RegisteredProvider

logger = logging.getLogger(__name__)


class _Outcome(NamedTuple):
    """What one provider call produced: a response, or the code of its failure."""

    response: object = None
    code: ResearchErrorCode | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _log_failure(
    entry: RegisteredProvider, code: ResearchErrorCode, exception_type: str
) -> None:
    """One WARNING per failed provider: identity, code and the exception TYPE only."""
    logger.warning(
        "research provider failed: provider=%s kind=%s code=%s exception_type=%s",
        entry.name,
        entry.kind.value,
        code.value,
        exception_type,
    )


async def _call_provider(
    entry: RegisteredProvider,
    call: Callable[[], Awaitable[object]],
    deadline: float,
) -> _Outcome:
    """Run ``call()`` until the loop-time ``deadline``; never raise ``Exception``.

    A provider that is not done by then is cancelled and awaited to its end (the
    ``timeout_at`` block does this) and reported as ``TIMEOUT``. Catching
    ``Exception`` is the point of this function: one failing provider must not
    fail the others. ``CancelledError`` is a ``BaseException`` and propagates.
    """
    try:
        async with asyncio.timeout_at(deadline):
            return _Outcome(response=await call())
    except Exception as error:
        code = classify_failure(error)
        _log_failure(entry, code, type(error).__name__)
        return _Outcome(code=code)


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
    if isinstance(error, ProviderFailure) and isinstance(error.code, ResearchErrorCode):
        return error.code
    if isinstance(error, TimeoutError):
        return ResearchErrorCode.TIMEOUT
    return ResearchErrorCode.INTERNAL_ERROR


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
        if not isinstance(registry, ProviderRegistry):
            raise TypeError("registry must be a ProviderRegistry")
        self._registry = registry
        self._clock = _utc_now if clock is None else clock

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
        if not isinstance(request, ResearchRequest):
            raise TypeError("request must be a ResearchRequest")
        entries = self._registry.select(request.kinds)
        loop = asyncio.get_running_loop()
        started = loop.time()
        budget_end = started + request.time_budget_seconds
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(
                    _call_provider(
                        entry,
                        # Bind ``entry`` now: it is the loop variable.
                        lambda entry=entry: entry.provider.search(
                            request.query, limit=request.max_results
                        ),
                        min(started + entry.timeout_seconds, budget_end),
                    )
                )
                for entry in entries
            ]
        outcomes = [task.result() for task in tasks]

        retrieved_at = self._clock()
        errors: list[ResearchError] = []
        batches: list[tuple[ResearchItem, ...]] = []
        for entry, outcome in zip(entries, outcomes, strict=True):
            code = outcome.code
            if code is None:
                try:
                    batches.append(
                        normalize_hits(
                            provider_id=entry.name,
                            kind=entry.kind,
                            hits=outcome.response,
                            limit=request.max_results,
                            retrieved_at=retrieved_at,
                        )
                    )
                except InvalidProviderResponseError as error:
                    code = ResearchErrorCode.INVALID_RESPONSE
                    _log_failure(entry, code, type(error).__name__)
            if code is not None:
                errors.append(ResearchError(entry.name, entry.kind, code))
        items, truncated = merge_items(batches, max_results=request.max_results)
        return ResearchResult(
            items=items,
            errors=tuple(errors),
            providers_queried=len(entries),
            truncated=truncated,
        )

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
        if not isinstance(source, SourceMetadata):
            raise TypeError("source must be a SourceMetadata")
        validate_time_budget(time_budget_seconds)
        locator = canonicalize_locator(source.locator)

        try:
            entry = self._registry.get(source.provider_id)
        except UnknownProviderError:
            entry = None
        if entry is None or entry.kind != source.provider_kind:
            return self._failed_fetch(
                source.provider_id, source.provider_kind, ResearchErrorCode.UNAVAILABLE
            )

        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = await _call_provider(
            entry,
            lambda: entry.provider.fetch(locator),
            started + min(entry.timeout_seconds, time_budget_seconds),
        )
        code = outcome.code
        document = outcome.response
        if code is None and not isinstance(document, ProviderDocument):
            code = ResearchErrorCode.INVALID_RESPONSE
            _log_failure(entry, code, InvalidProviderResponseError.__name__)
        if code is not None:
            return self._failed_fetch(entry.name, entry.kind, code)

        try:
            published_at = published_utc(document.published_at)
        except InvalidProviderResponseError:
            _log_failure(
                entry,
                ResearchErrorCode.INVALID_RESPONSE,
                InvalidProviderResponseError.__name__,
            )
            return self._failed_fetch(
                entry.name, entry.kind, ResearchErrorCode.INVALID_RESPONSE
            )
        item = ResearchItem(
            SourceMetadata(
                provider_kind=source.provider_kind,
                provider_id=source.provider_id,
                locator=locator,
                title=normalize_title(document.title),
                retrieved_at=self._clock(),
                content_hash=compute_content_hash(document.text),
                source_type=document.source_type,
                published_at=published_at,
                # Conservative: private if either side says so.
                private_source=source.private_source or document.private_source,
            ),
            document.text,
        )
        return ResearchResult(items=(item,), providers_queried=1)

    @staticmethod
    def _failed_fetch(
        provider_id: str, kind: ProviderKind, code: ResearchErrorCode
    ) -> ResearchResult:
        return ResearchResult(
            errors=(ResearchError(provider_id, kind, code),), providers_queried=1
        )
