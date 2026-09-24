"""Provider registry with up-front interface validation (PAW-051).

The registry is populated once at start-up (it is not thread-safe) and then only
read. A provider that does not satisfy ``ResearchProvider`` is rejected when it
is registered, so a wrong adapter fails loudly instead of later producing an
all-failed "successful" result.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from paw_backend.research.providers.contract import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    ProviderKind,
    ResearchProvider,
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
    raise NotImplementedError("PAW-051 stub")


class ProviderRegistry:
    """Unique, bounded, deterministically ordered set of research providers.

    Order is ``(KIND_ORDER[kind], name)``, independent of registration order.
    At most ``MAX_PROVIDERS`` providers can be registered.
    """

    def __init__(self) -> None:
        raise NotImplementedError("PAW-051 stub")

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

        The name and kind are read from the provider once, here.
        """
        raise NotImplementedError("PAW-051 stub")

    def get(self, name: str) -> RegisteredProvider:
        """Return the entry registered as ``name``.

        A non-``str`` raises ``TypeError``; an unknown name raises
        ``UnknownProviderError``.
        """
        raise NotImplementedError("PAW-051 stub")

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
        raise NotImplementedError("PAW-051 stub")

    def names(self) -> tuple[str, ...]:
        """All registered names, in the same order as ``select()``."""
        raise NotImplementedError("PAW-051 stub")

    def __len__(self) -> int:
        raise NotImplementedError("PAW-051 stub")

    def __contains__(self, name: object) -> bool:
        """True if a provider is registered as ``name`` (False for a non-``str``)."""
        raise NotImplementedError("PAW-051 stub")
