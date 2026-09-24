"""Values of the provenance store: what callers pass in and what comes back.

Input values (:class:`SourceInput`, :class:`SourceLinkInput`,
:class:`Reference`) validate themselves when they are built, so a wrong value
fails where it is created (``InvalidProvenanceInputError``; the message never
contains the value). All values are immutable. Datetimes that come back are
timezone-aware.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from paw_backend.research.provenance.errors import (
    InputProblem,
    InvalidProvenanceInputError,
)
from paw_backend.research.provenance.limits import MAX_SOURCE_TITLE_CHARS
from paw_backend.research.provenance.validation import (
    validate_content_hash,
    validate_datetime,
    validate_enum,
    validate_locator,
    validate_optional_datetime,
    validate_text,
    validate_uuid,
)
from paw_backend.research.providers.contract import SourceMetadata, SourceType


class Stance(StrEnum):
    """What a source says about a claim. The store records it; it never decides
    which of two sources is right (REQUIREMENTS.md: the source type alone does
    not decide truth)."""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class RelationKind(StrEnum):
    """How two claims (or two sources) relate. Both kinds are symmetric.

    ``duplicate``: they say the same thing. ``contradiction``: they cannot both
    be true. A pair has at most one relation.
    """

    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"


class EntityKind(StrEnum):
    """What a relation connects: two claims or two sources."""

    CLAIM = "claim"
    SOURCE = "source"


class ReferenceKind(StrEnum):
    """Who uses claims: an answer (an opaque id) or a task."""

    ANSWER = "answer"
    TASK = "task"


# --- inputs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceInput:
    """A source as the caller describes it. Full content is never stored.

    Validated on construction (``field`` names in errors are the attribute
    names): ``locator`` is replaced by its canonical form
    (``canonicalize_locator``; so ``SourceInput(locator=x, ...).locator ==
    canonicalize_locator(x)``; malformed is ``INVALID_FORMAT``); ``source_type``
    must be a :class:`SourceType`; ``fetched_at`` (required) and
    ``published_at`` (``None`` when unknown) must be timezone-aware and are
    converted to UTC (the same instant); ``content_hash`` is ``sha256:`` plus 64
    lowercase hex digits (the hash of what was fetched, not the content);
    ``title`` is at most ``MAX_SOURCE_TITLE_CHARS`` characters, may be empty,
    and is kept as given.
    """

    locator: str
    source_type: SourceType
    fetched_at: datetime
    content_hash: str
    published_at: datetime | None = None
    title: str = ""

    def __post_init__(self) -> None:
        # A frozen dataclass: the validated (canonical / UTC) values replace the
        # given ones.
        object.__setattr__(self, "locator", validate_locator("locator", self.locator))
        validate_enum("source_type", self.source_type, SourceType)
        object.__setattr__(
            self, "fetched_at", validate_datetime("fetched_at", self.fetched_at)
        )
        object.__setattr__(
            self,
            "published_at",
            validate_optional_datetime("published_at", self.published_at),
        )
        validate_content_hash("content_hash", self.content_hash)
        validate_text(
            "title", self.title, max_chars=MAX_SOURCE_TITLE_CHARS, allow_blank=True
        )

    @classmethod
    def from_metadata(cls, metadata: SourceMetadata) -> "SourceInput":
        """The source of a PAW-051 ``SourceMetadata``: ``retrieved_at`` becomes
        ``fetched_at``; ``provider_kind``, ``provider_id`` and ``private_source``
        are not recorded. Not a ``SourceMetadata``: ``WRONG_TYPE`` (field
        ``metadata``)."""
        if not isinstance(metadata, SourceMetadata):
            raise InvalidProvenanceInputError("metadata", InputProblem.WRONG_TYPE)
        return cls(
            locator=metadata.locator,
            source_type=metadata.source_type,
            fetched_at=metadata.retrieved_at,
            content_hash=metadata.content_hash,
            published_at=metadata.published_at,
            title=metadata.title,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceLinkInput:
    """One source together with what it says about the claim (``stance``)."""

    source: SourceInput
    stance: Stance = Stance.SUPPORTS

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceInput):
            raise InvalidProvenanceInputError("source", InputProblem.WRONG_TYPE)
        validate_enum("stance", self.stance, Stance)


@dataclass(frozen=True, slots=True)
class Reference:
    """The user of claims: an answer or a task, by id.

    An answer id is opaque to the store (there is no answers table yet): the
    caller guarantees that it belongs to the project it passes.
    """

    kind: ReferenceKind
    id: UUID

    def __post_init__(self) -> None:
        validate_enum("kind", self.kind, ReferenceKind)
        validate_uuid("id", self.id)

    @classmethod
    def answer(cls, answer_id: UUID) -> "Reference":
        return cls(ReferenceKind.ANSWER, answer_id)

    @classmethod
    def task(cls, task_id: UUID) -> "Reference":
        return cls(ReferenceKind.TASK, task_id)


# --- outputs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Source:
    """A recorded source. Immutable: the first record of a
    ``(project, locator, content_hash)`` is the one that stays."""

    id: UUID
    project_id: UUID
    locator: str
    source_type: SourceType
    title: str
    content_hash: str
    fetched_at: datetime
    published_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Claim:
    """A recorded claim. ``task_id`` is the task that first recorded it (or
    ``None``); ``created_by`` is the user or agent identity that did."""

    id: UUID
    project_id: UUID
    text: str
    task_id: UUID | None
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SourceLink:
    """What ``source`` says about the claim ``claim_id``, recorded at ``linked_at``."""

    claim_id: UUID
    source: Source
    stance: Stance
    linked_at: datetime


@dataclass(frozen=True, slots=True)
class Relation:
    """A symmetric relation between two claims or two sources.

    ``low_id < high_id`` always (compared as 128-bit integers): the pair is
    stored once, whichever order the caller named it in.
    """

    entity: EntityKind
    kind: RelationKind
    project_id: UUID
    low_id: UUID
    high_id: UUID
    created_by: UUID
    created_at: datetime

    def other(self, entity_id: UUID) -> UUID:
        """The endpoint that is not ``entity_id``. ``ValueError`` (fixed
        message) when ``entity_id`` is neither endpoint."""
        if entity_id == self.low_id:
            return self.high_id
        if entity_id == self.high_id:
            return self.low_id
        raise ValueError("not an endpoint of this relation")


@dataclass(frozen=True, slots=True)
class TracedClaim:
    """A claim with the sources that back or contradict it and its relations
    to other claims (see ``assemble_traced_claims`` for the order)."""

    claim: Claim
    links: tuple[SourceLink, ...]
    relations: tuple[Relation, ...]


@dataclass(frozen=True, slots=True)
class RecordedClaim:
    """The outcome of ``record_claim``.

    ``created`` is False when a claim with the same normalised text already
    existed (that claim is returned). ``links`` has one entry per distinct
    source of the call, in the order the sources first appear in the call, with
    the stance that is recorded. ``new_links`` counts the links this call
    created (an existing link is not counted).
    """

    claim: Claim
    created: bool
    links: tuple[SourceLink, ...]
    new_links: int


@dataclass(frozen=True, slots=True)
class Trace:
    """The claims an answer or a task used, and their sources.

    ``claims`` is ordered by ``(claim.created_at, claim.id)``. ``truncated`` is
    True when more claims than the requested limit exist (the first ones are
    returned).
    """

    reference: Reference
    claims: tuple[TracedClaim, ...]
    truncated: bool

    @property
    def sources(self) -> tuple[Source, ...]:
        """Every distinct source of the trace (by id), in the order it first
        appears when the claims and their links are read in order."""
        seen: dict[UUID, Source] = {}
        for traced in self.claims:
            for link in traced.links:
                seen.setdefault(link.source.id, link.source)
        return tuple(seen.values())
