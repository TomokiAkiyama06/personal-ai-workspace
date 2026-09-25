"""THROWAWAY reference implementation of queries.py (never committed)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from paw_backend.research.provenance.errors import (
    ClaimNotFoundError,
    ProvenanceConflictError,
    SourceNotFoundError,
)
from paw_backend.research.provenance.mapping import (
    claim_from_row,
    link_from_row,
    relation_from_row,
    relation_table,
    source_from_row,
)
from paw_backend.research.provenance.models import (
    CLAIM_SOURCES,
    CLAIM_USES,
    CLAIMS,
    SOURCES,
)
from paw_backend.research.provenance.records import (
    SourceLink,
)


async def ensure_source(session, project_id, source, created_at):
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
    statement = (
        insert(SOURCES)
        .values(
            project_id=project_id,
            locator=source.locator,
            source_type=source.source_type.value,
            title=source.title,
            content_hash=source.content_hash,
            fetched_at=source.fetched_at,
            published_at=source.published_at,
            created_at=created_at,
        )
        .on_conflict_do_nothing(
            index_elements=["project_id", "locator", "content_hash"]
        )
        .returning(*SOURCES.c)
    )
    row = (await session.execute(statement)).mappings().first()
    if row is not None:
        return source_from_row(row), True
    existing = (
        (
            await session.execute(
                select(SOURCES).where(
                    SOURCES.c.project_id == project_id,
                    SOURCES.c.locator == source.locator,
                    SOURCES.c.content_hash == source.content_hash,
                )
            )
        )
        .mappings()
        .first()
    )
    if existing is None:
        raise SourceNotFoundError()
    return source_from_row(existing), False


async def ensure_claim(
    session, project_id, *, created_by, task_id, text, fingerprint, created_at
):
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
    statement = (
        insert(CLAIMS)
        .values(
            project_id=project_id,
            task_id=task_id,
            created_by=created_by,
            claim_text=text,
            text_fingerprint=fingerprint,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["project_id", "text_fingerprint"])
        .returning(*CLAIMS.c)
    )
    row = (await session.execute(statement)).mappings().first()
    if row is not None:
        return claim_from_row(row), True
    existing = (
        (
            await session.execute(
                select(CLAIMS).where(
                    CLAIMS.c.project_id == project_id,
                    CLAIMS.c.text_fingerprint == fingerprint,
                )
            )
        )
        .mappings()
        .first()
    )
    if existing is None:
        raise ClaimNotFoundError()
    return claim_from_row(existing), False


async def link_claim_source(session, project_id, claim_id, source, stance, created_at):
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
    statement = (
        insert(CLAIM_SOURCES)
        .values(
            claim_id=claim_id,
            source_id=source.id,
            project_id=project_id,
            stance=stance.value,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["claim_id", "source_id"])
        .returning(CLAIM_SOURCES.c.created_at)
    )
    row = (await session.execute(statement)).first()
    if row is not None:
        return SourceLink(claim_id, source, stance, created_at), True
    existing = (
        (
            await session.execute(
                select(CLAIM_SOURCES.c.stance, CLAIM_SOURCES.c.created_at).where(
                    CLAIM_SOURCES.c.claim_id == claim_id,
                    CLAIM_SOURCES.c.source_id == source.id,
                    CLAIM_SOURCES.c.project_id == project_id,
                )
            )
        )
        .mappings()
        .one()
    )
    if existing["stance"] != stance.value:
        raise ProvenanceConflictError()
    return SourceLink(claim_id, source, stance, existing["created_at"]), False


async def add_claim_use(
    session, project_id, claim_id, reference, created_by, created_at
):
    """Record that the answer or task ``reference`` used the claim ``claim_id``.

    One row per ``(claim_id, reference.kind, reference.id)`` in
    ``research_claim_uses`` (columns ``claim_id``, ``ref_kind`` =
    ``reference.kind.value``, ``ref_id`` = ``reference.id``, ``project_id``,
    ``created_by``, ``created_at``). Return True when this call inserted the
    row, False when it already existed (nothing changes: the first record, with
    its ``created_by`` and ``created_at``, stays).
    """
    statement = (
        insert(CLAIM_USES)
        .values(
            claim_id=claim_id,
            ref_kind=reference.kind.value,
            ref_id=reference.id,
            project_id=project_id,
            created_by=created_by,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["claim_id", "ref_kind", "ref_id"])
        .returning(CLAIM_USES.c.claim_id)
    )
    return (await session.execute(statement)).first() is not None


async def insert_relation(
    session, project_id, entity, kind, low_id, high_id, created_by, created_at
):
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
    table = relation_table(entity)
    statement = (
        insert(table)
        .values(
            low_id=low_id,
            high_id=high_id,
            project_id=project_id,
            kind=kind.value,
            created_by=created_by,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["low_id", "high_id"])
        .returning(*table.c)
    )
    row = (await session.execute(statement)).mappings().first()
    if row is not None:
        return relation_from_row(entity, row), True
    existing = (
        (
            await session.execute(
                select(table).where(
                    table.c.project_id == project_id,
                    table.c.low_id == low_id,
                    table.c.high_id == high_id,
                )
            )
        )
        .mappings()
        .one()
    )
    if existing["kind"] != kind.value:
        raise ProvenanceConflictError()
    return relation_from_row(entity, existing), False


async def fetch_claim(session, project_id, claim_id):
    """The claim ``claim_id`` of ``project_id``, or ``None`` when there is no
    such claim in that project (a claim of another project is ``None`` too)."""
    row = (
        (
            await session.execute(
                select(CLAIMS).where(
                    CLAIMS.c.project_id == project_id, CLAIMS.c.id == claim_id
                )
            )
        )
        .mappings()
        .first()
    )
    return None if row is None else claim_from_row(row)


async def fetch_reference_claims(session, project_id, reference, limit):
    """The claims that the answer or task ``reference`` used, in ``project_id``.

    Join ``research_claim_uses`` (``ref_kind``, ``ref_id``, ``project_id``) with
    ``research_claims``. Order by ``created_at`` ascending, then ``id`` ascending
    (of the claim). Return at most ``limit`` claims and a flag that is True
    exactly when more than ``limit`` claims match (select ``limit + 1`` rows to
    know). A reference nobody used gives ``([], False)``; uses recorded in
    another project are invisible. ``limit`` is 1 or more.
    """
    statement = (
        select(CLAIMS)
        .join(
            CLAIM_USES,
            (CLAIM_USES.c.claim_id == CLAIMS.c.id)
            & (CLAIM_USES.c.project_id == CLAIMS.c.project_id),
        )
        .where(
            CLAIM_USES.c.project_id == project_id,
            CLAIM_USES.c.ref_kind == reference.kind.value,
            CLAIM_USES.c.ref_id == reference.id,
        )
        .order_by(CLAIMS.c.created_at, CLAIMS.c.id)
        .limit(limit + 1)
    )
    rows = (await session.execute(statement)).mappings().all()
    return [claim_from_row(row) for row in rows[:limit]], len(rows) > limit


async def fetch_claim_links(session, project_id, claim_ids):
    """Every source link of the given claims, with the full source.

    Join ``research_claim_sources`` with ``research_sources`` (same
    ``project_id``). Select ``claim_id``, ``stance``, the link's ``created_at``
    labelled ``linked_at`` and every column of ``research_sources``, and build
    each entry with ``mapping.link_from_row``. The order of the result is
    unspecified. Empty ``claim_ids`` returns ``[]`` without touching the
    database. Claims of another project yield nothing.
    """
    if not claim_ids:
        return []
    statement = (
        select(
            CLAIM_SOURCES.c.claim_id,
            CLAIM_SOURCES.c.stance,
            CLAIM_SOURCES.c.created_at.label("linked_at"),
            *SOURCES.c,
        )
        .join(
            SOURCES,
            (SOURCES.c.id == CLAIM_SOURCES.c.source_id)
            & (SOURCES.c.project_id == CLAIM_SOURCES.c.project_id),
        )
        .where(
            CLAIM_SOURCES.c.project_id == project_id,
            CLAIM_SOURCES.c.claim_id.in_(list(claim_ids)),
        )
    )
    rows = (await session.execute(statement)).mappings().all()
    return [link_from_row(row) for row in rows]


async def fetch_relations(session, project_id, entity, ids):
    """Every relation of ``entity`` (claims or sources) that has one of ``ids``
    as ``low_id`` or as ``high_id``, in ``project_id``.

    Each row once (a relation between two of the ``ids`` is one entry). The
    order of the result is unspecified. Empty ``ids`` returns ``[]`` without
    touching the database.
    """
    if not ids:
        return []
    table = relation_table(entity)
    statement = select(table).where(
        table.c.project_id == project_id,
        (table.c.low_id.in_(list(ids))) | (table.c.high_id.in_(list(ids))),
    )
    rows = (await session.execute(statement)).mappings().all()
    return [relation_from_row(entity, row) for row in rows]
