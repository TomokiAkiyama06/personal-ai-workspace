"""Errors of the research provider layer (PAW-051).

Every message is a fixed string. Nothing in this module (or in what the broker
builds from it) ever contains the text of a provider exception, a query, a URL
or a credential: a provider failure is reported to the Main Agent as a member of
the closed ``ResearchErrorCode`` enum and nothing else.
"""

from enum import StrEnum


class ResearchErrorCode(StrEnum):
    """Closed set of failure classifications a provider call can end with.

    The set is closed on purpose: a code is chosen by the broker (or by a
    provider raising ``ProviderFailure`` with a member of this enum) and can
    never be derived from exception text.
    """

    # The provider (or the global time budget) ran out of time; the call was
    # cancelled. Also used for a ``TimeoutError`` raised by the provider itself.
    TIMEOUT = "timeout"
    # The provider reported rate limiting (``ProviderFailure(RATE_LIMITED)``).
    RATE_LIMITED = "rate_limited"
    # The provider cannot be used right now (network down, missing credential
    # in the Tool Broker, provider no longer registered).
    UNAVAILABLE = "unavailable"
    # ``fetch`` only: the provider has no document for the locator.
    NOT_FOUND = "not_found"
    # The provider returned something that breaks the interface contract
    # (wrong type, too many hits, a locator that cannot be canonicalised, ...).
    INVALID_RESPONSE = "invalid_response"
    # Any other exception raised by the provider. Only a fixed classification of
    # its type is ever logged (``broker.log_type_name``), never its text or name.
    INTERNAL_ERROR = "internal_error"


class ProviderFailure(Exception):
    """Raised BY A PROVIDER to report a classified failure.

    It carries a ``ResearchErrorCode`` and no message: ``str(exc)`` is the
    code's value. An adapter that catches its own transport errors and wants
    the Main Agent to see ``RATE_LIMITED`` or ``UNAVAILABLE`` raises this and
    discards the original text.

    ``code`` is a slot of THIS class. The broker reads it through
    ``ProviderFailure.code`` and never through the instance, so that a subclass's
    property, ``__getattribute__`` or ``__class__`` (adapter code) cannot raise or
    lie while a failure is classified. The constructor stores the code the same
    way, so a subclass cannot divert it either.
    """

    __slots__ = ("code",)

    def __init__(self, code: ResearchErrorCode) -> None:
        if type(code) is not ResearchErrorCode:
            raise TypeError("code must be a ResearchErrorCode")
        ProviderFailure.code.__set__(self, code)
        super().__init__(code.value)


class InvalidLocatorError(ValueError):
    """A locator is not an acceptable http(s) URL (message never echoes it)."""

    def __init__(self) -> None:
        super().__init__("Invalid source locator")


class InvalidProviderResponseError(Exception):
    """A provider response broke the interface contract (fixed message)."""

    def __init__(self) -> None:
        super().__init__("Invalid provider response")


class ProviderRegistryError(Exception):
    """Base class of every error raised by ``ProviderRegistry``."""


_INTERFACE_MEMBERS = frozenset({"name", "kind", "search", "fetch"})


class ProviderInterfaceError(ProviderRegistryError, TypeError):
    """An object does not satisfy the ``ResearchProvider`` interface.

    ``member`` is the first failing member: ``"name"``, ``"kind"``,
    ``"search"`` or ``"fetch"``. The message never contains the object's values.
    """

    def __init__(self, member: str) -> None:
        if member not in _INTERFACE_MEMBERS:
            raise ValueError("member must be one of name, kind, search, fetch")
        self.member = member
        super().__init__(f"Provider does not satisfy ResearchProvider: {member}")


class DuplicateProviderError(ProviderRegistryError, ValueError):
    """A provider with this name is already registered."""

    def __init__(self) -> None:
        super().__init__("A provider with this name is already registered")


class RegistryFullError(ProviderRegistryError):
    """The registry already holds the maximum number of providers."""

    def __init__(self) -> None:
        super().__init__("The provider registry is full")


class UnknownProviderError(ProviderRegistryError, LookupError):
    """No provider is registered under this name."""

    def __init__(self) -> None:
        super().__init__("No such provider is registered")
