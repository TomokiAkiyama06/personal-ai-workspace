"""Fan-out of a research request over the registered providers (PAW-051).

``ResearchBroker`` is the only thing the Main Agent talks to. It runs the
matching providers concurrently, each under its own timeout and all under one
global budget, isolates their failures, and returns one ``ResearchResult`` in
which every item has the same ``SourceMetadata``. This module performs no
network I/O itself and makes no authorisation decision: the caller (the Tool
Broker, PAW-031) must have checked the ``network`` capability before calling
``gather`` or ``fetch``.

Privacy (PAW-053, Decision 0010). ``gather`` FAILS CLOSED: a broker that has no
pre-flight (``preflight=None``) refuses to search, raising
``PreflightRequiredError`` before any provider is called, unless it was built
with the one explicit opt-out ``unfiltered=True``, which sends every query to the
providers exactly as given, unchecked and unaudited. ``fetch`` is not affected
(see its docstring).

Logging: one WARNING per failed provider on the logger
``paw_backend.research.providers`` (or a child of it) with the provider id,
kind, error code and ``exception_type``. ``exception_type`` is the name of the
exception class only when that class IS one of ``LOGGED_EXCEPTION_TYPES`` (builtin
exceptions and this package's own, matched by identity); any other class, that is
every class an adapter defines, is logged as ``ADAPTER_ERROR``. The name of such a
class is adapter data (it can hold a credential or a newline that forges a log
record), so it is never logged, cut down or sanitised. Never the exception text,
the query or a locator (Decision 0012).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import CoroutineType
from typing import NamedTuple

from paw_backend.research.providers.contract import (
    DEFAULT_TIME_BUDGET_SECONDS,
    ProviderKind,
    ResearchError,
    ResearchItem,
    ResearchRequest,
    ResearchResult,
    SourceMetadata,
    validate_time_budget,
)
from paw_backend.research.providers.errors import (
    DuplicateProviderError,
    InvalidLocatorError,
    InvalidProviderResponseError,
    ProviderFailure,
    ProviderInterfaceError,
    ProviderRegistryError,
    RegistryFullError,
    ResearchErrorCode,
    UnknownProviderError,
)
from paw_backend.research.providers.locator import canonicalize_locator
from paw_backend.research.providers.normalize import (
    compute_content_hash,
    merge_items,
    normalize_hits,
    normalize_title,
    revalidate_document,
)
from paw_backend.research.providers.preflight import (
    PreflightNotConfiguredError,
    PreflightRequiredError,
    SearchPreflight,
    validate_preflight,
)
from paw_backend.research.providers.registry import ProviderRegistry, RegisteredProvider

logger = logging.getLogger(__name__)


class _Outcome(NamedTuple):
    """What one provider call produced: a response, or the code of its failure."""

    response: object = None
    code: ResearchErrorCode | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


# The value logged for an exception whose class is not on the list below.
ADAPTER_ERROR = "adapter_error"

# The classes that may be named in the log: the builtin exceptions an adapter's
# transport or parser can raise, and this package's own. A closed list that a
# reviewer extends by editing it; never derived from what an adapter raises.
LOGGED_EXCEPTION_TYPES: tuple[type[Exception], ...] = (
    # builtin
    Exception,
    ArithmeticError,
    AssertionError,
    AttributeError,
    BufferError,
    EOFError,
    FloatingPointError,
    ImportError,
    ModuleNotFoundError,
    IndexError,
    KeyError,
    LookupError,
    MemoryError,
    NameError,
    UnboundLocalError,
    NotImplementedError,
    OSError,
    BlockingIOError,
    ChildProcessError,
    ConnectionError,
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionRefusedError,
    ConnectionResetError,
    FileExistsError,
    FileNotFoundError,
    InterruptedError,
    IsADirectoryError,
    NotADirectoryError,
    PermissionError,
    ProcessLookupError,
    TimeoutError,
    OverflowError,
    RecursionError,
    ReferenceError,
    RuntimeError,
    StopAsyncIteration,
    StopIteration,
    SyntaxError,
    SystemError,
    TypeError,
    ValueError,
    UnicodeError,
    UnicodeDecodeError,
    UnicodeEncodeError,
    UnicodeTranslateError,
    ZeroDivisionError,
    ExceptionGroup,
    # this package
    ProviderFailure,
    InvalidLocatorError,
    InvalidProviderResponseError,
    ProviderRegistryError,
    ProviderInterfaceError,
    DuplicateProviderError,
    RegistryFullError,
    UnknownProviderError,
)

# Keyed by ``id``: hashing or comparing the key would call the ``__hash__`` /
# ``__eq__`` of the exception's metaclass (adapter code). Every class above lives
# as long as this module, so its ``id`` cannot belong to another class.
_LOGGED_NAMES: dict[int, str] = {
    id(cls): cls.__name__ for cls in LOGGED_EXCEPTION_TYPES
}


def log_type_name(error: BaseException) -> str:
    """The value to log as ``exception_type`` for ``error``; it never raises.

    The class's name if ``type(error)`` IS (not: derives from, or is named like)
    one of ``LOGGED_EXCEPTION_TYPES``, else ``ADAPTER_ERROR``. No attribute of the
    class is read: ``type()`` gives the class without asking the object for its
    ``__class__``, and the class is looked up by ``id``, so a metaclass hook
    (``__getattribute__``, ``__name__``, ``__hash__``, ``__eq__``, ...) never
    runs, and a ``__name__``, ``__qualname__`` or ``__module__`` set by the
    adapter is never seen. The result is one of about fifty fixed strings.
    """
    return _LOGGED_NAMES.get(id(type(error)), ADAPTER_ERROR)


def _log_failure(
    entry: RegisteredProvider, code: ResearchErrorCode, error: BaseException | None
) -> None:
    """One WARNING per failed provider: identity, code and a fixed exception type.

    ``error`` is ``None`` when the adapter raised nothing but asked for the
    cancellation of the task (see ``_CancelGuard``): the type is ``ADAPTER_ERROR``.
    """
    logger.warning(
        "research provider failed: provider=%s kind=%s code=%s exception_type=%s",
        entry.name,
        entry.kind.value,
        code.value,
        ADAPTER_ERROR if error is None else log_type_name(error),
    )


def _failed(entry: RegisteredProvider, error: BaseException) -> _Outcome:
    """Classify, log and return the failure of one provider call."""
    code = classify_failure(error)
    _log_failure(entry, code, error)
    return _Outcome(code=code)


class _CancelGuard:
    """Retract the cancellation requests that synchronous adapter code makes.

    ``with _CancelGuard() as guard:`` around adapter code that runs without an
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

    def __enter__(self) -> "_CancelGuard":
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


def _validated[T](
    entry: RegisteredProvider, produce: Callable[[], T]
) -> tuple[T | None, ResearchErrorCode | None]:
    """Run ``produce`` (validation of a provider's answer) as one guarded window.

    Returns ``(result, None)``, or ``(None, INVALID_RESPONSE)`` (and the one
    WARNING) if ``produce`` raises ``InvalidProviderResponseError`` or asked for the
    cancellation of the task in the meantime (a ``tzinfo.utcoffset`` that calls
    ``asyncio.current_task().cancel()`` and returns an offset, for example). The
    request is retracted and the answer discarded: an adapter that cancels the
    broker is not a valid one (Decision 0012). Any other exception propagates.
    """
    failure: InvalidProviderResponseError | None = None
    result: T | None = None
    with _CancelGuard() as guard:
        try:
            result = produce()
        except InvalidProviderResponseError as error:
            failure = error
    if failure is None and not guard.retracted:
        return result, None
    _log_failure(entry, ResearchErrorCode.INVALID_RESPONSE, failure)
    return None, ResearchErrorCode.INVALID_RESPONSE


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

    One exception to that: ``call()`` itself (reading ``provider.search`` and
    calling it) is adapter code that runs synchronously, before anything is
    awaited. A cancellation of this task is delivered at an ``await``, never
    there, so whatever ``call()`` raises, ``BaseException`` included (a property
    that raises ``CancelledError``, say), is the adapter's own failure and is
    classified like any other; it must not cancel ``gather()``. Nor may it ask
    for the cancellation: a ``call()`` that runs ``asyncio.current_task().cancel()``
    and returns normally would have the ``await`` below deliver the request, so
    the request is retracted (``_CancelGuard``), the awaitable is dropped without
    being awaited and the provider is an ``INTERNAL_ERROR``. Only the synchronous
    call is guarded this way: the ``await`` below still lets a real
    ``CancelledError`` (a cancelled ``gather()``, or the timeout) through
    (Decision 0012).
    """
    try:
        async with asyncio.timeout_at(deadline):
            failure: BaseException | None = None
            pending: Awaitable[object] | None = None
            with _CancelGuard() as guard:
                try:
                    pending = call()
                except BaseException as error:  # adapter code, synchronous
                    failure = error
            if failure is not None:
                return _failed(entry, failure)
            if guard.retracted:
                if type(pending) is CoroutineType:  # never started: no warning
                    pending.close()
                _log_failure(entry, ResearchErrorCode.INTERNAL_ERROR, None)
                return _Outcome(code=ResearchErrorCode.INTERNAL_ERROR)
            return _Outcome(response=await pending)
    except Exception as error:
        return _failed(entry, error)


def classify_failure(error: BaseException) -> ResearchErrorCode:
    """Map an exception raised by a provider to a member of ``ResearchErrorCode``.

    * a ``ProviderFailure`` gives the ``ResearchErrorCode`` that its constructor
      stored (still) in ``ProviderFailure.code``;
    * a ``TimeoutError`` (``asyncio.TimeoutError`` is the same class) gives
      ``TIMEOUT``;
    * everything else gives ``INTERNAL_ERROR``, including a ``ProviderFailure``
      whose slot is unset (a subclass constructor that did not call
      ``super().__init__``) or was overwritten with something that is not a
      ``ResearchErrorCode``.

    The exception is adapter code: it must not run any of it. The class is read
    with ``type()`` and tested with ``issubclass`` (``isinstance`` would ask the
    object for its ``__class__``), and the code is read ONCE from the slot of
    ``ProviderFailure`` itself, never through the instance, so a subclass's
    property, ``__getattribute__`` or ``__class__`` can neither raise nor lie.
    This function never raises. It does not call ``str()`` or ``repr()`` on the
    exception either (they may raise, or contain secrets).
    """
    cls = type(error)
    if issubclass(cls, ProviderFailure):
        try:
            code = ProviderFailure.code.__get__(error)
        except AttributeError:  # the slot was never set
            code = None
        if type(code) is ResearchErrorCode:
            return code
    if issubclass(cls, TimeoutError):
        return ResearchErrorCode.TIMEOUT
    return ResearchErrorCode.INTERNAL_ERROR


class ResearchBroker:
    """Concurrent, failure-isolating access to every registered provider.

    ``clock`` returns the current time as a timezone-aware datetime (UTC by
    default: ``datetime.now(timezone.utc)``); it is injectable for tests.

    ``preflight`` is a ``SearchPreflight``, for example the ``PrivacyGate`` of
    PAW-053. ``gather`` needs one: with the default ``preflight=None`` it refuses
    to search (``PreflightRequiredError``) and calls no provider.

    ``unfiltered=True`` is the ONE explicit opt-out, for tests and for callers
    that have nothing private to protect: the broker then sends every query to
    the providers AS IT IS, without minimisation, without an audit record and
    without any check. Never use it for a query that an Agent wrote from private
    source, memory, conversation or secret text. It cannot be combined with a
    ``preflight``.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        preflight: SearchPreflight | None = None,
        unfiltered: bool = False,
    ) -> None:
        """``registry`` must be a ``ProviderRegistry`` (else ``TypeError``);
        ``preflight`` must be ``None`` or implement ``SearchPreflight`` (checked
        with ``validate_preflight``: ``TypeError``); ``unfiltered`` must be a
        ``bool`` (``TypeError``, no truthiness) and cannot be ``True`` together
        with a ``preflight`` (``ValueError``). A broker with neither is accepted
        here: it fails closed later, in ``gather``."""
        if not isinstance(registry, ProviderRegistry):
            raise TypeError("registry must be a ProviderRegistry")
        if not isinstance(unfiltered, bool):
            raise TypeError("unfiltered must be a bool")
        if preflight is not None:
            validate_preflight(preflight)
            if unfiltered:
                raise ValueError("unfiltered=True cannot be combined with a preflight")
        self._registry = registry
        self._clock = _utc_now if clock is None else clock
        self._preflight = preflight
        self._unfiltered = unfiltered

    async def gather(
        self, request: ResearchRequest, *, preflight_input: object = None
    ) -> ResearchResult:
        """Search every registered provider whose kind is in ``request.kinds``.

        A ``request`` that is not a ``ResearchRequest`` raises ``TypeError``.
        Provider failures never raise; the method returns a ``ResearchResult``.

        Pre-flight (PAW-053), fail closed. A broker without a ``preflight`` that
        was not built with ``unfiltered=True`` raises ``PreflightRequiredError``
        (a ``PreflightNotConfiguredError``) at once, whatever the registry holds
        and before any provider is called: the query would go out unchecked and
        unaudited. An ``unfiltered`` broker sends ``request.query`` to every
        selected provider as it is (the behaviour of PAW-051), but a
        ``preflight_input`` given to it still raises ``PreflightNotConfiguredError``
        (the caller expected a check that does not exist). With a pre-flight, and
        after step 1 has selected at least one provider,
        ``await preflight.preflight(request, kinds, preflight_input)`` runs
        BEFORE any provider is called, where ``kinds`` is the ``frozenset`` of
        the selected providers' kinds and ``preflight_input`` is passed on as it
        is (``None`` too: the pre-flight decides). Whatever it raises (a refusal)
        propagates and no provider is called. What it returns must be a
        ``ResearchRequest`` (else ``TypeError``) and replaces ``request`` for all
        the steps below, so the provider only ever sees the rewritten query. When
        no provider is selected, nothing can be sent and the pre-flight is not
        called. The time budget starts after the pre-flight.

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
           code)`` and logged (see the module docstring: a class of the adapter is
           logged as ``adapter_error``, never by its name); the other providers are
           unaffected. The exception is read without running any of its own code
           (see ``classify_failure``), so it cannot make ``gather`` raise. This is
           the one place where catching ``Exception`` is intended.
           ``asyncio.CancelledError`` and other ``BaseException`` raised while a
           provider call is awaited are never swallowed: cancelling ``gather``
           cancels every provider call and propagates. The exception is the
           synchronous part of the call (reading ``provider.search`` and calling
           it): no cancellation can be delivered there, so a ``BaseException``
           from it is the adapter's own failure (``INTERNAL_ERROR``, see
           ``_call_provider``). So is a request to cancel the task (a
           ``__getattribute__`` that runs ``asyncio.current_task().cancel()`` and
           returns the method): it is retracted with ``Task.uncancel()``, the
           awaitable is not awaited, and the other providers keep their answers.
        4. ``retrieved_at = clock()`` is read exactly once per ``gather`` call,
           AFTER all providers have finished or timed out, and shared by all
           items.
        5. A provider that returned normally is validated and normalised with
           ``normalize_hits(provider_id=entry.name, kind=entry.kind, hits=<the
           response>, limit=request.max_results, retrieved_at=...)``; that includes
           validating the live fields of every hit again (an object built around
           its constructor is invalid). If it raises
           ``InvalidProviderResponseError`` the provider is reported with
           ``INVALID_RESPONSE`` (logged like any failure), contributes no item,
           and the others are unaffected. The response must be exactly a ``list``
           or ``tuple``: a subclass or a look-alike is invalid before any of its
           hooks (``__len__``, ``__iter__``, ...) can run. The one hook that does
           run is a ``published_at``'s ``tzinfo.utcoffset``; whatever it raises,
           ``asyncio.CancelledError`` included, is an invalid response too
           (see ``published_utc``), and so is a call that asks for the
           cancellation of the task (``asyncio.current_task().cancel()``) and
           returns an offset: the request is retracted (``_validated``), the
           provider contributes no item.
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
        if self._preflight is None:
            if not self._unfiltered:
                raise PreflightRequiredError
            if preflight_input is not None:
                raise PreflightNotConfiguredError
        entries = self._registry.select(request.kinds)
        if self._preflight is not None and entries:
            request = await self._preflight.preflight(
                request, frozenset(entry.kind for entry in entries), preflight_input
            )
            if not isinstance(request, ResearchRequest):
                raise TypeError("preflight must return a ResearchRequest")
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
                batch, code = _validated(
                    entry,
                    lambda outcome=outcome, entry=entry: normalize_hits(
                        provider_id=entry.name,
                        kind=entry.kind,
                        hits=outcome.response,
                        limit=request.max_results,
                        retrieved_at=retrieved_at,
                    ),
                )
                if batch is not None:
                    batches.append(batch)
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

        ``fetch`` carries no query: it passes the canonical locator of a source
        that a provider returned earlier back to that provider. It is NOT gated
        by the pre-flight (Decision 0010) and works on every broker, also one
        without a pre-flight and without ``unfiltered=True``. Whether a private
        source may be fetched is the concern of the Tool Broker (PAW-031) and of
        the adapters.

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
        one WARNING log, ``CancelledError`` at an ``await`` propagates). A response
        that is not a ``ProviderDocument`` is ``INVALID_RESPONSE``, and so is a
        ``ProviderDocument`` whose live fields are invalid (unset slots, wrong
        types, text over ``MAX_DOCUMENT_CHARS``, a ``published_at`` that is naive
        or that UTC cannot express, or whose ``tzinfo`` raises anything at all,
        ``CancelledError`` included, or asks for the cancellation of the task and
        returns an offset: the request is retracted, see ``_CancelGuard``):
        ``revalidate_document`` reads and validates every field again.
        A ``provider.fetch`` that asks for the cancellation of the task while it is
        read and called, and then returns normally, is an ``INTERNAL_ERROR`` and
        is not awaited (see ``_call_provider``).
        On success the result has one item, ``providers_queried=1``, no errors,
        ``truncated=False``:
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
        document = None
        if code is None:
            # Not just ``isinstance``: an object built around the constructor
            # (unset slots, wrong types, over-long text) is invalid, not a crash.
            document, code = _validated(
                entry, lambda: revalidate_document(outcome.response)
            )
        if code is not None:
            return self._failed_fetch(entry.name, entry.kind, code)

        item = ResearchItem(
            SourceMetadata(
                provider_kind=source.provider_kind,
                provider_id=source.provider_id,
                locator=locator,
                title=normalize_title(document.title),
                retrieved_at=self._clock(),
                content_hash=compute_content_hash(document.text),
                source_type=document.source_type,
                published_at=document.published_at,
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
