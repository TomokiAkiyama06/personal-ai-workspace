"""Normalisation of provider output into the unified shape (PAW-051).

Pure functions, no I/O. ``ResearchBroker`` calls them after a provider has
answered; they turn untrusted ``ProviderHit`` objects into ``ResearchItem``
objects and merge the answers of several providers.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime

from paw_backend.research.providers.contract import (
    ProviderHit,
    ProviderKind,
    ResearchItem,
    SourceMetadata,
)
from paw_backend.research.providers.errors import (
    InvalidLocatorError,
    InvalidProviderResponseError,
)
from paw_backend.research.providers.locator import canonicalize_locator


def compute_content_hash(text: str) -> str:
    """Return ``"sha256:"`` plus the lowercase hex SHA-256 of ``text`` as UTF-8.

    The text is hashed exactly as given: no stripping, no Unicode
    normalisation. ``compute_content_hash("")`` is
    ``"sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"``.
    A ``text`` that is not a ``str`` raises ``TypeError``.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_title(title: str) -> str:
    """Collapse every run of whitespace (``str.split()`` semantics) to one space.

    Leading and trailing whitespace is removed: ``"  Hello \\n\\t world "`` becomes
    ``"Hello world"``; a blank title becomes ``""``. Nothing else changes (case,
    punctuation and non-ASCII text are kept). A non-``str`` raises ``TypeError``.
    """
    if not isinstance(title, str):
        raise TypeError("title must be a str")
    return " ".join(title.split())


def published_utc(value: datetime | None) -> datetime | None:
    """``value`` in UTC (``None`` stays ``None``), or ``InvalidProviderResponseError``.

    A time the constructors would refuse (``datetime.max`` at UTC-01:00 is beyond
    UTC's range) can still reach here in an object that was built around them; it
    is the provider's invalid response, never an ``OverflowError`` for the caller.
    """
    if value is None:
        return None
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        raise InvalidProviderResponseError() from None


def normalize_hits(
    *,
    provider_id: str,
    kind: ProviderKind,
    hits: object,
    limit: int,
    retrieved_at: datetime,
) -> tuple[ResearchItem, ...]:
    """Validate one provider's search response and normalise every hit.

    ``hits`` is untrusted (whatever the adapter returned). It must be a ``list``
    or a ``tuple`` (a ``str``, ``bytes``, ``dict``, ``set``, generator or
    ``None`` is not) with at most ``limit`` elements, and every element must be
    a ``ProviderHit``. All elements are checked, and every locator is
    canonicalised, BEFORE anything is returned. Any violation, including a
    locator that ``canonicalize_locator`` rejects, raises
    ``InvalidProviderResponseError`` (fixed message) and yields no item at all:
    a response is accepted or rejected as a whole. Only ``InvalidLocatorError``
    is converted; other exceptions (a bug) propagate.

    ``provider_id``, ``kind``, ``limit`` and ``retrieved_at`` come from the
    broker and are trusted; ``retrieved_at`` is already UTC.

    The result has one ``ResearchItem`` per hit, in the order received, with NO
    deduplication (duplicates inside one response are kept; ``merge_items``
    handles them). Each item is ``ResearchItem(source=SourceMetadata(...),
    text=hit.text)`` where the metadata has ``provider_kind=kind``,
    ``provider_id=provider_id``, ``locator=canonicalize_locator(hit.locator)``,
    ``title=normalize_title(hit.title)``, ``retrieved_at=retrieved_at``,
    ``content_hash=compute_content_hash(hit.text)``,
    ``source_type=hit.source_type``, ``private_source=hit.private_source`` and
    ``published_at`` converted to UTC (``astimezone(timezone.utc)``; ``None``
    stays ``None``). An empty ``hits`` gives ``()``.
    """
    if not isinstance(hits, list | tuple) or len(hits) > limit:
        raise InvalidProviderResponseError()
    # Exactly ``ProviderHit`` (or a subclass): an object that merely has the same
    # attributes could lack ``private_source`` and silently count as public.
    if not all(isinstance(hit, ProviderHit) for hit in hits):
        raise InvalidProviderResponseError()
    try:
        locators = [canonicalize_locator(hit.locator) for hit in hits]
    except InvalidLocatorError:
        raise InvalidProviderResponseError() from None

    published = [published_utc(hit.published_at) for hit in hits]  # all, first
    return tuple(
        ResearchItem(
            SourceMetadata(
                provider_kind=kind,
                provider_id=provider_id,
                locator=locator,
                title=normalize_title(hit.title),
                retrieved_at=retrieved_at,
                content_hash=compute_content_hash(hit.text),
                source_type=hit.source_type,
                published_at=published_at,
                private_source=hit.private_source,
            ),
            hit.text,
        )
        for hit, locator, published_at in zip(hits, locators, published, strict=True)
    )


def merge_items(
    batches: Sequence[Sequence[ResearchItem]], *, max_results: int
) -> tuple[tuple[ResearchItem, ...], bool]:
    """Interleave, deduplicate and truncate the items of several providers.

    ``batches`` has one sequence per provider, already in provider order; each
    keeps its provider's ranking. ``max_results`` is an ``int`` of at least 1
    (a ``bool`` is a ``TypeError``, less than 1 a ``ValueError``). Returns
    ``(items, truncated)``.

    1. Interleave round-robin: rank 0 of every batch (in batch order), then
       rank 1 of every batch, and so on; a batch that has run out is skipped.
    2. Deduplicate by ``item.source.locator``: the FIRST occurrence in that
       order is kept, later ones are dropped. The kept item is otherwise
       unchanged, except that its ``source.private_source`` becomes True if ANY
       duplicate was private (never the other way round).
       (``dataclasses.replace`` returns a new item; items are immutable.)
    3. Truncate to the first ``max_results`` items; ``truncated`` is True exactly
       when more than ``max_results`` distinct items existed.

    Example: batches ``[a1, a2, a3]`` and ``[b1, b2]`` with ``max_results=4``
    give ``([a1, b1, a2, b2], True)``. Empty input gives ``((), False)``.
    """
    if isinstance(max_results, bool) or not isinstance(max_results, int):
        raise TypeError("max_results must be an int")
    if max_results < 1:
        raise ValueError("max_results must be at least 1")

    longest = max((len(batch) for batch in batches), default=0)
    kept: dict[str, ResearchItem] = {}  # insertion order is the merged order
    for rank in range(longest):
        for batch in batches:
            if rank >= len(batch):
                continue
            item = batch[rank]
            first = kept.setdefault(item.source.locator, item)
            if item.source.private_source and not first.source.private_source:
                kept[item.source.locator] = replace(
                    first, source=replace(first.source, private_source=True)
                )
    merged = tuple(kept.values())
    return merged[:max_results], len(merged) > max_results
