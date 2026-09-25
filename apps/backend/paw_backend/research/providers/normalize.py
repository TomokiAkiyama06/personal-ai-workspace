"""Normalisation of provider output into the unified shape (PAW-051).

Pure functions, no I/O. ``ResearchBroker`` calls them after a provider has
answered; they turn untrusted ``ProviderHit`` objects into ``ResearchItem``
objects and merge the answers of several providers.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import fields, replace
from datetime import UTC, datetime

from paw_backend.research.providers.contract import (
    ProviderDocument,
    ProviderHit,
    ProviderKind,
    ResearchItem,
    SourceMetadata,
    SourceType,
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


def published_utc(value: object) -> datetime | None:
    """``value`` as a plain UTC ``datetime`` (``None`` stays ``None``), or invalid.

    ``value`` is untrusted. It must be ``None`` or an aware ``datetime`` that UTC
    can express (``datetime.max`` at UTC-01:00 cannot); anything else, and any
    failure of the conversion, is ``InvalidProviderResponseError``. The result is
    an exact ``datetime`` (never a subclass) whose methods are the standard
    library's. The fields are first copied into an exact ``datetime`` with the
    ``datetime`` methods (``astimezone`` builds its intermediate result with the
    constructor of the subclass, which is adapter code), so a subclass cannot run
    its own code, and the ``tzinfo`` sees a plain ``datetime``.

    Only ``value.tzinfo`` is the provider's code (``utcoffset``), and it runs
    synchronously: nothing is awaited between the call and its answer, so a
    cancellation of the broker's task cannot arrive inside it (``Task.cancel()``
    is delivered at an ``await``). Whatever it raises, ``asyncio.CancelledError``,
    ``KeyboardInterrupt``, ``SystemExit`` or ``GeneratorExit`` included, is
    therefore the provider's own invalid response and never the task's; it does
    not escape to cancel ``gather()`` / ``fetch()`` (Decision 0012). The one thing
    that this cannot tell apart is a real ``KeyboardInterrupt`` that a signal
    handler raises inside this window: it is converted too (Decision 0012).

    A ``utcoffset`` that asks for the cancellation of the task
    (``asyncio.current_task().cancel()``) and then returns an offset raises
    nothing here; the request would be delivered at the next ``await`` or at the
    end of the task. The function does not run in a task of its own (it is a pure
    function), so the caller is the one that retracts it: ``ResearchBroker``
    brackets every call of ``normalize_hits`` / ``revalidate_document`` with a
    ``guard.CancelGuard`` and treats a retracted request as an invalid response.
    """
    if value is None:
        return None
    if not issubclass(type(value), datetime):
        raise InvalidProviderResponseError()
    exact = datetime.combine(datetime.date(value), datetime.timetz(value))
    try:
        offset = exact.utcoffset()
        converted = None if offset is None else exact.astimezone(UTC)
    except BaseException:  # the provider's tzinfo, run synchronously (see above)
        raise InvalidProviderResponseError() from None
    if converted is None:  # naive
        raise InvalidProviderResponseError()
    return datetime.combine(datetime.date(converted), datetime.timetz(converted))


def _plain_str(value: object) -> str:
    """An exact ``str`` copy of ``value``, made without running its class's code.

    A ``str`` subclass can override ``__len__``, ``encode`` and more;
    ``str.__str__`` copies the characters and runs none of that. The constructors
    then check the bounds on the copy, so a subclass cannot lie about its length.
    """
    if not issubclass(type(value), str):
        raise InvalidProviderResponseError()
    return str.__str__(value)


def _exactly(value: object, expected: type) -> object:
    if type(value) is not expected:
        raise InvalidProviderResponseError()
    return value


def _read_fields(cls: type, value: object) -> dict[str, object]:
    """The slot values of ``value``, read through ``cls`` and not through its class.

    ``ProviderHit`` and ``ProviderDocument`` are slotted dataclasses: each field
    is a ``member_descriptor`` on ``cls``, which reads the slot directly. A
    subclass's properties and ``__getattribute__`` are never run, nothing is read
    twice, and a slot that was never set (a constructor that did not call
    ``super().__init__``) is an ``AttributeError`` that we translate.
    ``issubclass(type(...))`` is used instead of ``isinstance`` because an object
    can claim any ``__class__``.
    """
    if not issubclass(type(value), cls):
        raise InvalidProviderResponseError()
    try:
        return {f.name: getattr(cls, f.name).__get__(value) for f in fields(cls)}
    except AttributeError:
        raise InvalidProviderResponseError() from None


def _plain_fields(raw: dict[str, object]) -> dict[str, object]:
    """The fields that a hit and a document share, as plain values (or invalid)."""
    return {
        "title": _plain_str(raw["title"]),
        "text": _plain_str(raw["text"]),
        "published_at": published_utc(raw["published_at"]),
        "source_type": _exactly(raw["source_type"], SourceType),
        "private_source": _exactly(raw["private_source"], bool),
    }


def revalidate_hit(value: object) -> ProviderHit:
    """A clean ``ProviderHit`` with the field values of ``value``, or invalid.

    ``isinstance(value, ProviderHit)`` only proves the class. The object may
    have been built around its constructor (``object.__setattr__``, a subclass
    that sets nothing, ...), so its fields are read once (see ``_read_fields``),
    copied into plain values and validated again by the ``ProviderHit``
    constructor itself (types, bounds, UTF-8, ``published_at`` in UTC).
    Everything that fails, for whatever reason, is ``InvalidProviderResponseError``
    (fixed message): nothing of the value, and no exception text, is kept.
    """
    raw = _read_fields(ProviderHit, value)
    locator = _plain_str(raw["locator"])
    plain = _plain_fields(raw)
    try:
        return ProviderHit(locator, **plain)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise InvalidProviderResponseError() from None


def revalidate_document(value: object) -> ProviderDocument:
    """Like ``revalidate_hit`` for a fetched ``ProviderDocument`` (no locator)."""
    plain = _plain_fields(_read_fields(ProviderDocument, value))
    try:
        return ProviderDocument(**plain)  # type: ignore[arg-type]
    except (TypeError, ValueError):
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

    ``hits`` is untrusted (whatever the adapter returned). It must be EXACTLY a
    ``list`` or a ``tuple`` (a ``str``, ``bytes``, ``dict``, ``set``, generator or
    ``None`` is not) with at most ``limit`` elements. An instance of a subclass,
    or an object that claims to be a list, is invalid too: ``__len__``,
    ``__iter__`` and ``__getitem__`` of a subclass would run the adapter's code
    here, unguarded, and could raise, or lie about the length to get past
    ``limit``. Every element must be
    a ``ProviderHit`` whose live field values are valid: each one is validated
    again by ``revalidate_hit`` (an object built around the constructor, with
    unset slots or values of the wrong type or length, is not a valid hit). All
    elements are checked, and every locator is canonicalised, BEFORE anything is
    returned. Any violation, including a locator that ``canonicalize_locator``
    rejects, raises ``InvalidProviderResponseError`` (fixed message) and yields no
    item at all: a response is accepted or rejected as a whole. Only
    ``InvalidLocatorError`` is converted (and what the validation of untrusted
    objects catches); other exceptions (a bug in the trusted arguments)
    propagate.

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
    # ``type()`` is the object's real class (``isinstance`` would also ask it for
    # ``__class__``), and ``is`` runs no ``__eq__``. Exact types have no adapter
    # code in ``len`` and iteration.
    if (type(hits) is not list and type(hits) is not tuple) or len(hits) > limit:
        raise InvalidProviderResponseError()
    # ``isinstance`` proves the class and nothing else: an object built around
    # the constructor may lack slots or hold values of the wrong type. Every hit
    # is read once and validated again (``revalidate_hit``); everything below
    # works on those clean copies, never on the provider's objects.
    clean = [revalidate_hit(hit) for hit in hits]
    try:
        locators = [canonicalize_locator(hit.locator) for hit in clean]
    except InvalidLocatorError:
        raise InvalidProviderResponseError() from None

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
                published_at=hit.published_at,
                private_source=hit.private_source,
            ),
            hit.text,
        )
        for hit, locator in zip(clean, locators, strict=True)
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
