"""Helpers that turn database rows into the values of ``records.py``.

Written once so that the query functions (``queries.py``) only have to select
the right columns. Every function takes a mapping with the documented keys
(``row._mapping`` / ``result.mappings()`` of SQLAlchemy Core) and never touches
the database.
"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import Table

from paw_backend.research.provenance.models import (
    CLAIM_RELATIONS,
    SOURCE_RELATIONS,
)
from paw_backend.research.provenance.records import (
    Claim,
    EntityKind,
    Relation,
    RelationKind,
    Source,
    SourceLink,
    Stance,
)
from paw_backend.research.providers.contract import SourceType


def relation_table(entity: EntityKind) -> Table:
    """``research_claim_relations`` for claims, ``research_source_relations`` for
    sources (both have the columns ``low_id``, ``high_id``, ``project_id``,
    ``kind``, ``created_by``, ``created_at``)."""
    return CLAIM_RELATIONS if entity is EntityKind.CLAIM else SOURCE_RELATIONS


def source_from_row(row: Mapping[str, Any]) -> Source:
    """A :class:`Source` from a row with the keys ``id``, ``project_id``,
    ``locator``, ``source_type``, ``title``, ``content_hash``, ``fetched_at``,
    ``published_at`` and ``created_at`` (the columns of ``research_sources``)."""
    return Source(
        id=row["id"],
        project_id=row["project_id"],
        locator=row["locator"],
        source_type=SourceType(row["source_type"]),
        title=row["title"],
        content_hash=row["content_hash"],
        fetched_at=row["fetched_at"],
        published_at=row["published_at"],
        created_at=row["created_at"],
    )


def claim_from_row(row: Mapping[str, Any]) -> Claim:
    """A :class:`Claim` from a row with the keys ``id``, ``project_id``,
    ``task_id``, ``created_by``, ``claim_text`` and ``created_at`` (the columns
    of ``research_claims``; ``claim_text`` becomes ``Claim.text``)."""
    return Claim(
        id=row["id"],
        project_id=row["project_id"],
        text=row["claim_text"],
        task_id=row["task_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def relation_from_row(entity: EntityKind, row: Mapping[str, Any]) -> Relation:
    """A :class:`Relation` from a row of the relation table of ``entity``."""
    return Relation(
        entity=entity,
        kind=RelationKind(row["kind"]),
        project_id=row["project_id"],
        low_id=row["low_id"],
        high_id=row["high_id"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def link_from_row(row: Mapping[str, Any]) -> SourceLink:
    """A :class:`SourceLink` from a joined row: the keys ``claim_id``,
    ``stance`` and ``linked_at`` (``research_claim_sources.created_at`` selected
    under that label) plus every key that :func:`source_from_row` reads."""
    return SourceLink(
        claim_id=row["claim_id"],
        source=source_from_row(row),
        stance=Stance(row["stance"]),
        linked_at=row["linked_at"],
    )
