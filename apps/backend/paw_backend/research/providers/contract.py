"""Provider-neutral contract of the research layer (PAW-051).

The Main Agent talks to ``ResearchBroker`` only. It sends a ``ResearchRequest``
and receives a ``ResearchResult`` whose items all carry the same
``SourceMetadata``, whatever provider (Direct Web, Docs, GitHub, a future
OpenCode adapter) found them. Provider adapters implement ``ResearchProvider``
and return ``ProviderHit`` / ``ProviderDocument`` objects; the broker turns them
into the unified shape, so no provider payload, client object or exception text
ever reaches the Main Agent.

This module has no I/O. It defines data, its validation, and the Protocol.
Wrong types raise ``TypeError``, out-of-range or malformed values raise
``ValueError``; both messages are fixed strings that never echo the value.
"""

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from paw_backend.research.providers.errors import ResearchErrorCode

# --- Bounds (everything that comes from a caller or a provider is bounded) ----

MAX_QUERY_CHARS = 512
MAX_RESULTS_LIMIT = 50
MAX_TITLE_CHARS = 300
MAX_LOCATOR_CHARS = 2048
# A search hit carries a short excerpt; ``fetch`` returns a whole document.
MAX_EXCERPT_CHARS = 4_000
MAX_DOCUMENT_CHARS = 200_000
DEFAULT_TIME_BUDGET_SECONDS = 30.0
MAX_TIME_BUDGET_SECONDS = 120.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 10.0
MAX_PROVIDER_TIMEOUT_SECONDS = 120.0
MAX_PROVIDERS = 32

# Provider names / ids: lowercase, digits, ``-`` and ``_``; 1 to 64 characters.
PROVIDER_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
CONTENT_HASH_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


class ProviderKind(StrEnum):
    """The kinds of research provider. Declaration order is the priority order."""

    WEB = "web"  # Direct Web search
    DOCS = "docs"  # Official documentation
    GITHUB = "github"  # GitHub repositories, releases, advisories
    OPENCODE = "opencode"  # OpenCode (one possible future provider)


# Position of each kind; providers and results are ordered by (position, name).
KIND_ORDER: Mapping[ProviderKind, int] = MappingProxyType(
    {kind: position for position, kind in enumerate(ProviderKind)}
)


class SourceType(StrEnum):
    """What a source is (REQUIREMENTS.md "Evidence / provenance").

    The provider declares it; it never decides whether a claim is true.
    """

    OFFICIAL_DOCS = "official_docs"
    OFFICIAL_GITHUB = "official_github"  # official GitHub repository / Release
    PRIMARY = "primary"
    SECONDARY = "secondary"
    COMMUNITY = "community"  # forum / community
    UNKNOWN = "unknown"


# --- Field validators ---------------------------------------------------------


def _require_text(value: object, name: str, *, max_chars: int, min_chars: int = 0):
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    if not min_chars <= len(value) <= max_chars:
        raise ValueError(f"{name} must have {min_chars} to {max_chars} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be encodable as UTF-8") from None


def _require_int(value: object, name: str, *, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")


def _require_seconds(value: object, name: str, *, high: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or not 0 < value <= high:
        raise ValueError(f"{name} must be greater than 0 and at most {high}")


def validate_time_budget(value: object) -> None:
    """Raise unless ``value`` is a number in ``(0, MAX_TIME_BUDGET_SECONDS]``."""
    _require_seconds(value, "time_budget_seconds", high=MAX_TIME_BUDGET_SECONDS)


def validate_provider_timeout(value: object) -> None:
    """Raise unless ``value`` is a number in ``(0, MAX_PROVIDER_TIMEOUT_SECONDS]``."""
    _require_seconds(value, "timeout_seconds", high=MAX_PROVIDER_TIMEOUT_SECONDS)


def _require_aware(value: object, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _require_utc_convertible(value: datetime, name: str) -> None:
    """The time must be expressible in UTC: ``datetime.max`` at UTC-01:00 is not."""
    try:
        value.astimezone(UTC)
    except OverflowError:
        raise ValueError(f"{name} cannot be expressed in UTC") from None


def _require_utc(value: object, name: str) -> None:
    _require_aware(value, name)
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be in UTC")


def _require_provider_name(value: object, name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    if PROVIDER_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must match [a-z0-9][a-z0-9_-]{{0,63}}")


def _has_control_or_space(value: str) -> bool:
    return any(ch.isspace() or unicodedata.category(ch) in {"Cc", "Cs"} for ch in value)


# --- What the Main Agent sends ------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    """One research request. Immutable and fully validated at construction.

    ``query`` must already be minimised by the Research Privacy Filter
    (PAW-053); the broker does not inspect or rewrite it. It has 1 to
    ``MAX_QUERY_CHARS`` characters, is not blank, and contains no control
    character (newlines and tabs included).
    ``max_results`` (1 to ``MAX_RESULTS_LIMIT``) is both the limit asked of each
    provider and the maximum number of items in the result.
    ``kinds`` is a non-empty ``frozenset`` of ``ProviderKind`` (a ``set`` or
    ``list`` is a ``TypeError``, nothing is coerced); by default all kinds.
    ``time_budget_seconds`` is the global wall-clock budget of one ``gather``:
    a finite number greater than 0 and at most ``MAX_TIME_BUDGET_SECONDS``.
    """

    query: str
    max_results: int = 10
    kinds: frozenset[ProviderKind] = field(
        default_factory=lambda: frozenset(ProviderKind)
    )
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_SECONDS

    def __post_init__(self) -> None:
        _require_text(self.query, "query", max_chars=MAX_QUERY_CHARS, min_chars=1)
        if not self.query.strip():
            raise ValueError("query must not be blank")
        if any(unicodedata.category(ch) in {"Cc", "Cs"} for ch in self.query):
            raise ValueError("query must not contain control characters")
        _require_int(self.max_results, "max_results", low=1, high=MAX_RESULTS_LIMIT)
        if not isinstance(self.kinds, frozenset):
            raise TypeError("kinds must be a frozenset")
        if not self.kinds:
            raise ValueError("kinds must not be empty")
        if not all(isinstance(kind, ProviderKind) for kind in self.kinds):
            raise TypeError("kinds must contain only ProviderKind values")
        validate_time_budget(self.time_budget_seconds)


# --- What an adapter returns (untrusted, not yet normalised) ------------------


@dataclass(frozen=True, slots=True)
class ProviderHit:
    """One search hit as an adapter reports it.

    ``locator`` is the raw URL (at most ``MAX_LOCATOR_CHARS`` characters); the
    broker canonicalises it and rejects the whole response if it cannot.
    ``title`` has at most ``MAX_TITLE_CHARS`` characters and may be empty.
    ``text`` is a short excerpt of at most ``MAX_EXCERPT_CHARS`` characters
    (may be empty). ``published_at`` is ``None`` or timezone-aware.
    ``private_source`` has NO default: an adapter must state whether the hit
    comes from a private source (for example a private GitHub repository) so
    that the Research Privacy Filter (PAW-053) can rely on it.
    There is no field for a provider name or a raw payload.

    The constructor validates, but the broker does not trust that it ran: every
    field of a received hit is read and validated again (``revalidate_hit``).
    """

    locator: str
    title: str = ""
    text: str = ""
    published_at: datetime | None = None
    source_type: SourceType = SourceType.UNKNOWN
    private_source: bool = field(kw_only=True)

    def __post_init__(self) -> None:
        _require_text(self.locator, "locator", max_chars=MAX_LOCATOR_CHARS, min_chars=1)
        _require_text(self.title, "title", max_chars=MAX_TITLE_CHARS)
        _require_text(self.text, "text", max_chars=MAX_EXCERPT_CHARS)
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")
            _require_utc_convertible(self.published_at, "published_at")
        if not isinstance(self.source_type, SourceType):
            raise TypeError("source_type must be a SourceType")
        _require_bool(self.private_source, "private_source")


@dataclass(frozen=True, slots=True)
class ProviderDocument:
    """A fetched document as an adapter reports it (``ResearchProvider.fetch``).

    Like ``ProviderHit`` but ``text`` may hold up to ``MAX_DOCUMENT_CHARS``
    characters and there is no locator: the broker attaches the locator that
    was requested. ``private_source`` has no default here either. The broker
    validates the fields again (``revalidate_document``).
    """

    title: str = ""
    text: str = ""
    published_at: datetime | None = None
    source_type: SourceType = SourceType.UNKNOWN
    private_source: bool = field(kw_only=True)

    def __post_init__(self) -> None:
        _require_text(self.title, "title", max_chars=MAX_TITLE_CHARS)
        _require_text(self.text, "text", max_chars=MAX_DOCUMENT_CHARS)
        if self.published_at is not None:
            _require_aware(self.published_at, "published_at")
            _require_utc_convertible(self.published_at, "published_at")
        if not isinstance(self.source_type, SourceType):
            raise TypeError("source_type must be a SourceType")
        _require_bool(self.private_source, "private_source")


# --- What the Main Agent receives ---------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """The unified description of a source, identical for every provider.

    ``provider_kind`` and ``provider_id`` come from the registry, never from
    the provider's own claim. ``locator`` is the canonical, sanitised http(s)
    URL produced by ``canonicalize_locator``; this class only refuses what would
    be unsafe (not http(s), user info, a fragment, whitespace or control
    characters, no host). ``title`` may be empty. ``retrieved_at`` and
    ``published_at`` are in UTC. ``content_hash`` is ``sha256:`` plus 64
    lowercase hex digits: the hash of the UTF-8 text carried by the
    ``ResearchItem`` (an excerpt for a search hit, the document for ``fetch``),
    not necessarily of the whole remote document. ``private_source`` is True
    when the source is private (see PAW-053). There is no raw provider payload.
    """

    provider_kind: ProviderKind
    provider_id: str
    locator: str
    title: str
    retrieved_at: datetime
    content_hash: str
    source_type: SourceType = SourceType.UNKNOWN
    published_at: datetime | None = None
    private_source: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.provider_kind, ProviderKind):
            raise TypeError("provider_kind must be a ProviderKind")
        _require_provider_name(self.provider_id, "provider_id")
        _require_text(self.locator, "locator", max_chars=MAX_LOCATOR_CHARS, min_chars=1)
        if _has_control_or_space(self.locator):
            raise ValueError(
                "locator must not contain whitespace or control characters"
            )
        if not self.locator.startswith(("http://", "https://")):
            raise ValueError("locator must be an http(s) URL")
        try:
            parts = urlsplit(self.locator)
            hostname = parts.hostname
        except ValueError:
            raise ValueError("locator is not a valid URL") from None
        if not hostname:
            raise ValueError("locator must have a host")
        if "@" in parts.netloc:
            raise ValueError("locator must not contain user information")
        if "#" in self.locator:
            raise ValueError("locator must not contain a fragment")
        _require_text(self.title, "title", max_chars=MAX_TITLE_CHARS)
        _require_utc(self.retrieved_at, "retrieved_at")
        if not isinstance(self.content_hash, str):
            raise TypeError("content_hash must be a str")
        if CONTENT_HASH_PATTERN.fullmatch(self.content_hash) is None:
            raise ValueError(
                "content_hash must be sha256: plus 64 lowercase hex digits"
            )
        if not isinstance(self.source_type, SourceType):
            raise TypeError("source_type must be a SourceType")
        if self.published_at is not None:
            _require_utc(self.published_at, "published_at")
        _require_bool(self.private_source, "private_source")

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready copy: enums as their values, datetimes as ISO 8601."""
        return {
            "provider_kind": self.provider_kind.value,
            "provider_id": self.provider_id,
            "locator": self.locator,
            "title": self.title,
            "retrieved_at": self.retrieved_at.isoformat(),
            "content_hash": self.content_hash,
            "source_type": self.source_type.value,
            "published_at": (
                None if self.published_at is None else self.published_at.isoformat()
            ),
            "private_source": self.private_source,
        }


@dataclass(frozen=True, slots=True)
class ResearchItem:
    """One found source: unified metadata plus the text it carries.

    ``text`` has at most ``MAX_DOCUMENT_CHARS`` characters. This is the only
    shape in which a search hit or a fetched document reaches the Main Agent.
    """

    source: SourceMetadata
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceMetadata):
            raise TypeError("source must be a SourceMetadata")
        _require_text(self.text, "text", max_chars=MAX_DOCUMENT_CHARS)

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source.to_dict(), "text": self.text}


@dataclass(frozen=True, slots=True)
class ResearchError:
    """A provider failure reported as a code only (never as text).

    ``provider_id`` and ``kind`` are the registry's. ``code`` is a member of the
    closed ``ResearchErrorCode`` enum.
    """

    provider_id: str
    kind: ProviderKind
    code: ResearchErrorCode

    def __post_init__(self) -> None:
        _require_provider_name(self.provider_id, "provider_id")
        if not isinstance(self.kind, ProviderKind):
            raise TypeError("kind must be a ProviderKind")
        if not isinstance(self.code, ResearchErrorCode):
            raise TypeError("code must be a ResearchErrorCode")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "kind": self.kind.value,
            "code": self.code.value,
        }


@dataclass(frozen=True, slots=True)
class ResearchResult:
    """The outcome of ``gather`` / ``fetch``: items, failures, and two facts.

    ``items`` are ordered and hold no two items with the same ``source.locator``
    (at most ``MAX_RESULTS_LIMIT``). ``errors`` hold one entry per failed
    provider, in registry order. ``providers_queried`` (not a bool, at least the
    number of errors) lets the caller tell "nothing found" from "nobody asked".
    ``truncated`` is True when more items were found than ``max_results``.
    """

    items: tuple[ResearchItem, ...] = ()
    errors: tuple[ResearchError, ...] = ()
    providers_queried: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise TypeError("items must be a tuple")
        if not all(isinstance(item, ResearchItem) for item in self.items):
            raise TypeError("items must contain only ResearchItem values")
        if len(self.items) > MAX_RESULTS_LIMIT:
            raise ValueError("too many items")
        locators = [item.source.locator for item in self.items]
        if len(locators) != len(set(locators)):
            raise ValueError("items must have unique source locators")
        if not isinstance(self.errors, tuple):
            raise TypeError("errors must be a tuple")
        if not all(isinstance(error, ResearchError) for error in self.errors):
            raise TypeError("errors must contain only ResearchError values")
        _require_int(
            self.providers_queried, "providers_queried", low=0, high=MAX_PROVIDERS
        )
        if len(self.errors) > self.providers_queried:
            raise ValueError("providers_queried cannot be smaller than the errors")
        _require_bool(self.truncated, "truncated")

    @property
    def all_failed(self) -> bool:
        """True when providers were queried and every one of them failed."""
        return self.providers_queried > 0 and len(self.errors) == self.providers_queried

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "errors": [error.to_dict() for error in self.errors],
            "providers_queried": self.providers_queried,
            "truncated": self.truncated,
        }


# --- What an adapter implements ------------------------------------------------


@runtime_checkable
class ResearchProvider(Protocol):
    """The one boundary every research provider implements.

    An adapter never receives credentials from the Main Agent: it obtains them
    from the Backend Tool Broker (Secret Isolation). It must not retry, must not
    suppress ``asyncio.CancelledError`` (the broker cancels a provider that runs
    out of time), and must not put provider payloads or error text into what it
    returns. To report a classified failure it raises ``ProviderFailure``.

    ``name`` is the unique registry id (``[a-z0-9][a-z0-9_-]{0,63}``) and
    ``kind`` a ``ProviderKind``; both are read once, at registration.
    """

    @property
    def name(self) -> str: ...

    @property
    def kind(self) -> ProviderKind: ...

    async def search(self, query: str, *, limit: int) -> Sequence[ProviderHit]:
        """Return at most ``limit`` hits, best first, as a ``list`` or ``tuple``.

        The type must be exactly ``list`` or ``tuple``: an instance of a subclass
        is an invalid response (its hooks would run adapter code in the broker).
        """
        ...

    async def fetch(self, locator: str) -> ProviderDocument:
        """Return the document at the canonical ``locator``.

        Raise ``ProviderFailure(ResearchErrorCode.NOT_FOUND)`` if there is none.
        """
        ...
