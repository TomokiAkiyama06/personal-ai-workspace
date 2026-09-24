"""The database statements of the provenance store (PAW-052).

Nine small functions, each one job. They run inside a transaction that
``ProvenanceStore`` opens (``session``); they never commit, never open their own
transaction and never sleep or retry. The store validates every argument, reads
the clock once and takes the locks before it calls them, so these functions can
trust their arguments (already-validated values, ids that exist in the project).

Rules that hold for all of them:

* Use SQLAlchemy Core with the table objects of ``models.py`` (``SOURCES``,
  ``CLAIMS``, ``CLAIM_SOURCES``, ``CLAIM_USES``) and ``relation_table`` from
  ``mapping.py``; bind every value as a parameter (never format a value into
  SQL); build results with the helpers of ``mapping.py``.
* Every statement is restricted to ``project_id`` (``WHERE ... project_id =
  :project_id``): rows of another project are invisible. The database also
  refuses a link between two projects (composite foreign keys), but do not rely
  on it.
* The application role may only SELECT and INSERT (no UPDATE, no DELETE), so
  "create it if it is absent" is ``INSERT ... ON CONFLICT ... DO NOTHING`` (use
  ``sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(
  index_elements=[...])``) followed, when nothing was inserted, by a SELECT of
  the existing row. Never ``ON CONFLICT DO UPDATE``.
* Errors: raise only the ones documented here. Catch nothing else; a database
  error propagates unchanged.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.research.provenance.records import (
    Claim,
    EntityKind,
    Reference,
    Relation,
    RelationKind,
    Source,
    SourceInput,
    SourceLink,
    Stance,
)


async def ensure_source(
    session: AsyncSession,
    project_id: UUID,
    source: SourceInput,
    created_at: datetime,
) -> tuple[Source, bool]:
    """Record ``source`` in ``project_id`` unless it is already recorded.

    A source is identified by ``(project_id, locator, content_hash)`` (the
    unique constraint of ``research_sources``). Insert a row with ``locator``,
    ``source_type`` (its ``.value``), ``title``, ``content_hash``,
    ``fetched_at``, ``published_at`` and ``created_at``; the database creates
    the id. Return ``(source, True)`` for the row this call inserted.

    If the identity already exists (this project only), change nothing and
    return ``(existing, False)``: the first record wins, even when the caller's
    ``fetched_at``, ``published_at``, ``source_type`` or ``title`` differ. The same
    locator with another ``content_hash`` is a different source (a new row); the
    same identity in another project is a different source too.

    If the insert did nothing and the row cannot be read back, raise
    ``SourceNotFoundError()`` (cannot happen in practice).
    """
    raise NotImplementedError("PAW-052 stub")


async def ensure_claim(
    session: AsyncSession,
    project_id: UUID,
    *,
    created_by: UUID,
    task_id: UUID | None,
    text: str,
    fingerprint: str,
    created_at: datetime,
) -> tuple[Claim, bool]:
    """Record a claim in ``project_id`` unless the same claim is already recorded.

    A claim is identified by ``(project_id, text_fingerprint)``. Insert a row
    with ``claim_text`` = ``text`` (as given), ``text_fingerprint`` =
    ``fingerprint``, ``created_by``, ``task_id`` and ``created_at``; the
    database creates the id. Return ``(claim, True)`` for the row this call
    inserted.

    If the fingerprint already exists in this project, change nothing and return
    ``(existing, False)``: the first record wins, including its text, its
    ``created_by``, its ``task_id`` and its ``created_at``. The same fingerprint
    in another project is a different claim.

    If the insert did nothing and the row cannot be read back, raise
    ``ClaimNotFoundError()`` (cannot happen in practice).
    """
    raise NotImplementedError("PAW-052 stub")


async def link_claim_source(
    session: AsyncSession,
    project_id: UUID,
    claim_id: UUID,
    source: Source,
    stance: Stance,
    created_at: datetime,
) -> tuple[SourceLink, bool]:
    """Record that ``source`` has ``stance`` towards the claim ``claim_id``.

    One row per ``(claim_id, source.id)`` in ``research_claim_sources`` (columns
    ``claim_id``, ``source_id``, ``project_id``, ``stance`` = ``stance.value``,
    ``created_at``).

    * No link yet: insert it and return ``(SourceLink(claim_id, source, stance,
      created_at), True)``.
    * A link with the same stance exists: change nothing and return
      ``(SourceLink(claim_id, source, stance, <the existing link's created_at>),
      False)``.
    * A link with the other stance exists: change nothing and raise
      ``ProvenanceConflictError()``.

    ``source`` and the claim exist in ``project_id`` (the store checked).
    """
    raise NotImplementedError("PAW-052 stub")


async def add_claim_use(
    session: AsyncSession,
    project_id: UUID,
    claim_id: UUID,
    reference: Reference,
    created_by: UUID,
    created_at: datetime,
) -> bool:
    """Record that the answer or task ``reference`` used the claim ``claim_id``.

    One row per ``(claim_id, reference.kind, reference.id)`` in
    ``research_claim_uses`` (columns ``claim_id``, ``ref_kind`` =
    ``reference.kind.value``, ``ref_id`` = ``reference.id``, ``project_id``,
    ``created_by``, ``created_at``). Return True when this call inserted the
    row, False when it already existed (nothing changes: the first record, with
    its ``created_by`` and ``created_at``, stays).
    """
    raise NotImplementedError("PAW-052 stub")


async def insert_relation(
    session: AsyncSession,
    project_id: UUID,
    entity: EntityKind,
    kind: RelationKind,
    low_id: UUID,
    high_id: UUID,
    created_by: UUID,
    created_at: datetime,
) -> tuple[Relation, bool]:
    """Record the symmetric relation ``kind`` between two claims or two sources.

    ``entity`` selects the table (``relation_table(entity)``); ``low_id <
    high_id`` (the caller used ``order_pair``) and both exist in ``project_id``.
    A pair has at most one relation (the primary key is ``(low_id, high_id)``).

    * No relation for the pair: insert it (columns ``low_id``, ``high_id``,
      ``project_id``, ``kind`` = ``kind.value``, ``created_by``, ``created_at``)
      and return ``(relation, True)`` (``mapping.relation_from_row``).
    * A relation of the same ``kind`` exists: change nothing and return
      ``(existing, False)`` (its own ``created_by`` and ``created_at``).
    * A relation of the other ``kind`` exists: change nothing and raise
      ``ProvenanceConflictError()``.
    """
    raise NotImplementedError("PAW-052 stub")


async def fetch_claim(
    session: AsyncSession, project_id: UUID, claim_id: UUID
) -> Claim | None:
    """The claim ``claim_id`` of ``project_id``, or ``None`` when there is no
    such claim in that project (a claim of another project is ``None`` too)."""
    raise NotImplementedError("PAW-052 stub")


async def fetch_reference_claims(
    session: AsyncSession,
    project_id: UUID,
    reference: Reference,
    limit: int,
) -> tuple[list[Claim], bool]:
    """The claims that the answer or task ``reference`` used, in ``project_id``.

    Join ``research_claim_uses`` (``ref_kind``, ``ref_id``, ``project_id``) with
    ``research_claims``. Order by ``created_at`` ascending, then ``id`` ascending
    (of the claim). Return at most ``limit`` claims and a flag that is True
    exactly when more than ``limit`` claims match (select ``limit + 1`` rows to
    know). A reference nobody used gives ``([], False)``; uses recorded in
    another project are invisible. ``limit`` is 1 or more.
    """
    raise NotImplementedError("PAW-052 stub")


async def fetch_claim_links(
    session: AsyncSession, project_id: UUID, claim_ids: Sequence[UUID]
) -> list[SourceLink]:
    """Every source link of the given claims, with the full source.

    Join ``research_claim_sources`` with ``research_sources`` (same
    ``project_id``). Select ``claim_id``, ``stance``, the link's ``created_at``
    labelled ``linked_at`` and every column of ``research_sources``, and build
    each entry with ``mapping.link_from_row``. The order of the result is
    unspecified. Empty ``claim_ids`` returns ``[]`` without touching the
    database. Claims of another project yield nothing.
    """
    raise NotImplementedError("PAW-052 stub")


async def fetch_relations(
    session: AsyncSession,
    project_id: UUID,
    entity: EntityKind,
    ids: Sequence[UUID],
) -> list[Relation]:
    """Every relation of ``entity`` (claims or sources) that has one of ``ids``
    as ``low_id`` or as ``high_id``, in ``project_id``.

    Each row once (a relation between two of the ``ids`` is one entry). The
    order of the result is unspecified. Empty ``ids`` returns ``[]`` without
    touching the database.
    """
    raise NotImplementedError("PAW-052 stub")
