"""Provider registry with up-front interface validation (PAW-051).

The registry is populated once at start-up (it is not thread-safe) and then only
read. A provider that does not satisfy ``ResearchProvider`` is rejected when it
is registered, so a wrong adapter fails loudly instead of later producing an
all-failed "successful" result.
"""

import inspect
from collections.abc import Iterable
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


def _accepts_call(method: object, *args: object, **kwargs: object) -> bool:
    """True if ``method`` is a coroutine function that accepts this call."""
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

    1. ``"name"``: ``provider.name`` exists and is a ``str`` that fully matches
       ``PROVIDER_NAME_PATTERN``.
    2. ``"kind"``: ``provider.kind`` is a ``ProviderKind`` member (the plain string
       ``"web"`` is not accepted).
    3. ``"search"``: ``provider.search`` exists, is a coroutine function
       (``inspect.iscoroutinefunction``; a plain ``def`` returning a coroutine
       is not accepted) and its signature (``inspect.signature`` of the bound
       method) accepts the call ``search("q", limit=1)``
       (``Signature.bind`` does not raise ``TypeError``). If the signature cannot
       be inspected (``ValueError``) it is a failure too.
    4. ``"fetch"``: likewise, and the signature accepts ``fetch("https://x/")``.

    Nothing is called on the provider except reading these attributes. The
    provider is never awaited and its values never appear in the error.
    """
    _identity(provider)
    _check_methods(provider)


def _identity(provider: object) -> tuple[str, ProviderKind]:
    """Read ``name`` and ``kind`` ONCE and return the validated pair.

    A property can return something else on the next read, so what is checked
    here is the very value that is used afterwards (the registry stores this
    pair, and never reads the provider's identity again).
    """
    name = getattr(provider, "name", None)
    if not isinstance(name, str) or PROVIDER_NAME_PATTERN.fullmatch(name) is None:
        raise ProviderInterfaceError("name")
    kind = getattr(provider, "kind", None)
    if not isinstance(kind, ProviderKind):
        raise ProviderInterfaceError("kind")
    return name, kind


def _check_methods(provider: object) -> None:
    if not _accepts_call(getattr(provider, "search", None), "q", limit=1):
        raise ProviderInterfaceError("search")
    if not _accepts_call(getattr(provider, "fetch", None), "https://x/"):
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
        that were validated are the ones stored.
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
