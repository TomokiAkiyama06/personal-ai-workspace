"""Provider registry with up-front interface validation (PAW-051).

The registry is populated once at start-up (it is not thread-safe) and then only
read. A provider that does not satisfy ``ResearchProvider`` is rejected when it
is registered, so a wrong adapter fails loudly instead of later producing an
all-failed "successful" result.
"""

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from paw_backend.research.providers.contract import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    KIND_ORDER,
    MAX_PROVIDERS,
    PROVIDER_NAME_PATTERN,
    ProviderKind,
    ResearchProvider,
    validate_provider_timeout,
)
from paw_backend.research.providers.errors import (
    DuplicateProviderError,
    ProviderInterfaceError,
    RegistryFullError,
    UnknownProviderError,
)
from paw_backend.research.providers.guard import CancelGuard


@dataclass(frozen=True, slots=True)
class RegisteredProvider:
    """A registered provider with the identity that was read at registration.

    ``name`` and ``kind`` are snapshots: a provider that later changes its own
    attributes does not change what results and errors report.
    ``timeout_seconds`` is the per-provider timeout the broker applies.
    """

    name: str
    kind: ProviderKind
    timeout_seconds: float
    provider: ResearchProvider = field(repr=False, compare=False)


def _adapter_window[T](member: str, read: Callable[[], T]) -> T:
    """Run ``read`` (adapter code that runs synchronously) as one guarded window.

    Reading a member of the provider, and inspecting what it returns, runs the
    adapter's own code: a property, ``__getattribute__``, ``__getattr__`` or
    descriptor, an object's ``__class__`` (``isinstance`` asks for it),
    ``__signature__`` and ``Signature.bind``. Whatever that code does is a fault of
    the adapter, never of the registry, so:

    * ANY ``BaseException`` it raises (``CancelledError``, ``KeyboardInterrupt``,
      ``SystemExit``, ``GeneratorExit`` included) becomes
      ``ProviderInterfaceError(member)``. Registration never awaits, so a real
      ``Task.cancel()`` cannot be delivered inside the window: a ``CancelledError``
      here is the adapter's own, exactly as in the broker's synchronous window
      (Decision 0012, item 10). The price is the same too: a real
      ``KeyboardInterrupt`` that arrives inside this window (start-up wiring, a few
      microseconds) is reported as a rejected adapter.
    * A hook that asks for the cancellation of the running task
      (``asyncio.current_task().cancel()``) and returns normally leaves a request
      that the caller's next ``await`` would deliver. ``CancelGuard`` (the
      broker's) takes back the requests made inside the window and the adapter is
      rejected for ``member``. A request that was there before the call stays, so
      a caller that had been cancelled is still cancelled at its next ``await``.

    The error is raised AFTER the ``try`` block, never inside the ``except``:
    inside it, ``__context__`` would hold the adapter's exception (``from None``
    hides it from the traceback but does not clear it), and a log line that
    formats the error (``logger.exception``) would print the adapter's text. Here
    the error has no ``__cause__`` or ``__context__`` (unless the CALLER is itself
    handling an exception), and no adapter text or object in it.

    Only the reads and the ``inspect`` probes go through here. The checks of the
    values (``type()``, ``fullmatch``, ``str.encode``) are C code and never run
    the adapter's; keeping them outside means a bug of this module is not turned
    into an adapter fault.
    """
    failed = False
    with CancelGuard() as guard:
        try:
            result = read()
        except BaseException:  # adapter code, synchronous; see above
            failed = True
    if failed or guard.retracted:
        raise ProviderInterfaceError(member)
    return result


def _accepts_call(method: object, *args: object, **kwargs: object) -> bool:
    """True if ``method`` is a coroutine function that accepts this call.

    Runs adapter code (see ``_adapter_window``): call it only inside a window.
    """
    if not callable(method) or not inspect.iscoroutinefunction(method):
        return False
    try:
        inspect.signature(method).bind(*args, **kwargs)
    except (TypeError, ValueError):  # signature does not fit / not inspectable
        return False
    return True


def validate_provider(provider: object) -> None:
    """Raise ``ProviderInterfaceError(member)`` unless ``provider`` fits the interface.

    Members are checked in this order and the first failure is reported in
    ``error.member``:

    1. ``"name"``: ``provider.name`` exists and is a ``str`` (a subclass is
       accepted, but stored as a plain ``str`` copy; an object that only claims
       to be one is rejected) that fully matches ``PROVIDER_NAME_PATTERN`` as it
       is: nothing is normalised, so look-alikes are rejected.
    2. ``"kind"``: ``provider.kind`` is a ``ProviderKind`` member (the plain string
       ``"web"`` is not accepted, nor an object that only claims the class).
    3. ``"search"``: ``provider.search`` exists, is a coroutine function
       (``inspect.iscoroutinefunction``; a plain ``def`` returning a coroutine
       is not accepted) and its signature (``inspect.signature`` of the bound
       method) accepts the call ``search("q", limit=1)``
       (``Signature.bind`` does not raise ``TypeError``). If the signature cannot
       be inspected (``ValueError``) it is a failure too.
    4. ``"fetch"``: likewise, and the signature accepts ``fetch("https://x/")``.

    Nothing is called on the provider except reading these attributes. The
    provider is never awaited and its values never appear in the error.

    Reading and inspecting a member is adapter code. Whatever it raises (any
    ``BaseException``), and a request it makes to cancel the running task, is the
    fixed ``ProviderInterfaceError`` of that member, with nothing of the adapter
    in it and nothing chained to it (``_adapter_window``, Decision 0012). This
    function is synchronous: it never awaits, so a real cancellation of the
    caller is not delivered inside it, and one that was requested before the call
    is still delivered at the caller's next ``await``.
    """
    _identity(provider)
    _check_methods(provider)


def _identity(provider: object) -> tuple[str, ProviderKind]:
    """Read ``name`` and ``kind`` ONCE and return the validated pair.

    A property can return something else on the next read, so what is checked
    here is the very value that is used afterwards (the registry stores this
    pair, and never reads the provider's identity again).

    The provider is adapter code, and so is every object it returns: none of the
    object's own methods may run in the registry or the broker. The two reads
    themselves run adapter code (a property, ``__getattribute__``, ...), so each
    is a guarded window that turns any exception, or a cancellation request, into
    the fixed error of that member (``_adapter_window``).

    * The class is read with ``type()`` (``isinstance`` would also believe an
      object's ``__class__``, and ``re`` would then raise a ``TypeError``
      instead of ``ProviderInterfaceError``). ``kind`` must be a
      ``ProviderKind`` member itself; an enum with members cannot be subclassed.
    * The name is checked as it was given: ``fullmatch`` reads the characters of
      a ``str`` (also of a subclass) without calling any of its methods, and the
      pattern is ASCII only, so a name that differs only by Unicode
      normalisation, case, width or whitespace (including a trailing newline)
      does not match, and nothing is normalised into a form that could equal
      another name.
    * What is returned is an exact ``str`` copy of the checked characters. A
      ``str`` subclass (a ``StrEnum`` member, say) is accepted, but the object
      itself is never stored: an overridden ``__hash__``, ``__eq__``,
      ``__lt__`` or ``__str__`` could otherwise raise in ``register`` /
      ``select`` / ``gather``, get past the uniqueness check, or put credential
      text into a log line. ``str.encode`` is the C method, so the copy does not
      call an override either (the name is ASCII here, so this cannot fail).
    """
    name = _adapter_window("name", lambda: getattr(provider, "name", None))
    if not issubclass(type(name), str) or PROVIDER_NAME_PATTERN.fullmatch(name) is None:
        raise ProviderInterfaceError("name")
    name = str.encode(name, "ascii").decode("ascii")
    kind = _adapter_window("kind", lambda: getattr(provider, "kind", None))
    if type(kind) is not ProviderKind:
        raise ProviderInterfaceError("kind")
    return name, kind


def _check_methods(provider: object) -> None:
    """Check ``search`` and ``fetch``; each read and probe is one guarded window."""
    if not _adapter_window(
        "search",
        lambda: _accepts_call(getattr(provider, "search", None), "q", limit=1),
    ):
        raise ProviderInterfaceError("search")
    if not _adapter_window(
        "fetch",
        lambda: _accepts_call(getattr(provider, "fetch", None), "https://x/"),
    ):
        raise ProviderInterfaceError("fetch")


class ProviderRegistry:
    """Unique, bounded, deterministically ordered set of research providers.

    Order is ``(KIND_ORDER[kind], name)``, independent of registration order.
    At most ``MAX_PROVIDERS`` providers can be registered.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RegisteredProvider] = {}

    def register(
        self,
        provider: ResearchProvider,
        *,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ) -> RegisteredProvider:
        """Validate and register ``provider``; return its ``RegisteredProvider``.

        Checks, in this order (each raises before anything is stored, so a failed
        call never changes the registry):

        1. ``validate_provider(provider)``: ``ProviderInterfaceError``.
        2. ``validate_provider_timeout(timeout_seconds)``: ``TypeError`` for a
           non-number or ``bool``, ``ValueError`` for a non-finite value, ``<= 0``
           or ``> MAX_PROVIDER_TIMEOUT_SECONDS``.
        3. The name is not registered yet: ``DuplicateProviderError``.
        4. Fewer than ``MAX_PROVIDERS`` providers are registered:
           ``RegistryFullError``.

        The name and kind are read from the provider once, here, and the values
        that were validated are the ones stored (the name as an exact ``str``
        copy, see ``_identity``).

        Every read of the provider's members is guarded (``_adapter_window``): an
        exception of any kind that adapter code raises while it is read or
        inspected is the fixed ``ProviderInterfaceError`` of that member, and a
        request to cancel the running task is retracted and rejected the same
        way. Registration is synchronous (it never awaits), so a real cancellation
        of the caller cannot be delivered in the middle of it; one requested
        before the call is left alone and delivered at the caller's next ``await``.
        """
        name, kind = _identity(provider)  # read once, here, and validated
        _check_methods(provider)
        validate_provider_timeout(timeout_seconds)
        if name in self._entries:
            raise DuplicateProviderError()
        if len(self._entries) >= MAX_PROVIDERS:
            raise RegistryFullError()
        entry = RegisteredProvider(name, kind, timeout_seconds, provider)
        self._entries[name] = entry
        return entry

    def get(self, name: str) -> RegisteredProvider:
        """Return the entry registered as ``name``.

        A non-``str`` raises ``TypeError``; an unknown name raises
        ``UnknownProviderError``.
        """
        if not isinstance(name, str):
            raise TypeError("name must be a str")
        try:
            return self._entries[name]
        except KeyError:
            raise UnknownProviderError() from None

    def select(
        self, kinds: Iterable[ProviderKind] | None = None
    ) -> tuple[RegisteredProvider, ...]:
        """Return a snapshot tuple of the providers whose kind is in ``kinds``.

        ``None`` selects every provider. Every element of ``kinds`` must be a
        ``ProviderKind`` (otherwise ``TypeError``, also for a plain string
        such as ``"web"``); an empty iterable selects nothing. The result is
        ordered by ``(KIND_ORDER[kind], name)`` and is not affected by later
        registrations.
        """
        wanted: set[ProviderKind] | None = None
        if kinds is not None:
            wanted = set()
            for kind in kinds:
                if not isinstance(kind, ProviderKind):
                    raise TypeError("kinds must contain only ProviderKind values")
                wanted.add(kind)
        selected = [
            entry
            for entry in self._entries.values()
            if wanted is None or entry.kind in wanted
        ]
        selected.sort(key=lambda entry: (KIND_ORDER[entry.kind], entry.name))
        return tuple(selected)

    def names(self) -> tuple[str, ...]:
        """All registered names, in the same order as ``select()``."""
        return tuple(entry.name for entry in self.select())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: object) -> bool:
        """True if a provider is registered as ``name`` (False for a non-``str``)."""
        return isinstance(name, str) and name in self._entries
