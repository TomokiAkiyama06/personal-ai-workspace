"""The SQL of a retrieval: the permission prefilter and the candidate queries.

**Every statement that reads ``memory_versions`` applies** :func:`visible` **in its
own WHERE clause, next to the text match, before any ordering or limit.** The
permission filter is never applied to a result afterwards: the keyword query
ranks only rows the caller may read, the vector query orders by distance only
rows the caller may read (the ACL condition is on the joined ``memory_versions``
row of the same scan), and the relation query requires both ends to be readable.
So a row the caller may not read cannot take a place in a candidate list, cannot
change a rank or a score, and cannot be counted. ``tests/test_retrieval_plans.py``
checks the plans: the filter sits below the sort, in the scan.

Constants that a partial index depends on (``status = 'active'`` and the scope
names) are written into the SQL text, so that a generic plan can use the index.
Only fixed vocabularies are written that way (``MemoryScope`` members and
literals in this file); nothing a caller sent ever is.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BindParameter,
    ColumnElement,
    Select,
    Text,
    and_,
    bindparam,
    case,
    exists,
    func,
    literal_column,
    or_,
    select,
)
from sqlalchemy.orm import aliased
from sqlalchemy.sql.visitors import replacement_traverse

from paw_backend.memory.acl import readable_memory_versions
from paw_backend.memory.fulltext import search_document_sql
from paw_backend.memory.models import (
    MemoryEmbedding,
    MemoryRelation,
    MemoryScope,
    MemoryVersion,
    RelationType,
)
from paw_backend.memory.retrieval.scopes import ResolvedScopes
from paw_backend.memory.shared import limits as shared_limits
from paw_backend.projects.models import ProjectMemberRow, ProjectRow

_ACTIVE = literal_column("'active'")
_SESSION_ONLY = literal_column("'session_only'")
_EXPIRING = literal_column("'expiring'")
_TS_CONFIG = literal_column("'simple'::regconfig")
# ts_rank normalisation 32: rank / (rank + 1), so 0..1 and independent of length.
_RANK_NORMALISATION = 32

# The relation types the retrieval reads.
CONFLICT = RelationType.CONFLICTS_WITH.value
SUPERSEDES = RelationType.SUPERSEDES.value

# The declared subjects of a shared memory (``attributes.policy_subjects``) are judged
# in SQL with the rules of Shared Memory administration (PAW-046): at most
# ``MAX_POLICY_SUBJECTS`` strings, each of at most ``MAX_SUBJECT_CHARS`` characters
# matching this pattern (the same as ``memory/shared/validation.py``; a test compares).
MAX_POLICY_SUBJECTS = shared_limits.MAX_POLICY_SUBJECTS
MAX_SUBJECT_CHARS = shared_limits.MAX_SUBJECT_CHARS
SUBJECT_PATTERN = r"^(?:[a-z][a-z0-9_]{0,31}(?:\.[a-z][a-z0-9_]{0,31}){0,4})$"

# Membership rows of a project that can be read (Pending deletion stops access).
_READABLE_PROJECT_STATES = ("active", "archived")


def _inline_scope_names(clause: ColumnElement[bool]) -> ColumnElement[bool]:
    """``clause`` with each ``MemoryScope`` bind parameter written into the SQL."""

    def replace(element: Any) -> Any:
        if isinstance(element, BindParameter) and isinstance(
            element.value, MemoryScope
        ):
            return literal_column("'" + element.value.value + "'")
        return None

    return replacement_traverse(clause, {}, replace)


@dataclass(frozen=True, slots=True)
class Eligibility:
    """Conditions beyond permission that a candidate must meet, ahead of every limit.

    A candidate list is cut to its limit; a filter applied after the cut would let
    rows that are not eligible take the places of the ones below them. So these are
    part of the candidate statements (``visible``), not of a later pass.

    * ``exclude_stale``: leave out the stale candidates (``stale_policy = exclude``):
      marked (``stale_since``), ``revalidate`` past its interval, and ``repo_commit``
      whose commit differs from the head in ``repo_heads`` (``ranking.freshness_of``
      is the same rule in Python, for the flag; a test compares them row by row).
    * ``policy_subjects``: the subjects the System Security Policy governs. A
      shared memory that declares a subject below or equal to one of them is left
      out (Decision 0009); so is one whose declaration is malformed.
    """

    exclude_stale: bool = False
    repo_heads: Mapping[UUID, str] = field(default_factory=lambda: MappingProxyType({}))
    policy_subjects: tuple[str, ...] = ()


NO_EXTRA_CONDITIONS = Eligibility()


def _live(version: Any, scopes: ResolvedScopes, now: datetime) -> ColumnElement[bool]:
    """Readable, in a searched scope, ``active``, and long-term and not expired.

    * ``readable_memory_versions``: the ACL condition (``memory/acl.py``) over the
      ids the caller may read, with the scope names inlined;
    * ``scope IN scopes``: only the scopes that were authorised and asked for;
    * ``status = 'active'``: a superseded, deprecated or history version is never
      a candidate;
    * freshness: ``session_only`` memories are not long-term memory, and an
      ``expiring`` one is gone at ``expires_at`` (a missing date fails closed).
    """
    return and_(
        _inline_scope_names(readable_memory_versions(scopes.acl_principal(), version)),
        version.scope.in_(
            [literal_column("'" + scope.value + "'") for scope in sorted(scopes.scopes)]
        ),
        version.status == _ACTIVE,
        version.freshness_policy != _SESSION_ONLY,
        or_(version.freshness_policy != _EXPIRING, version.expires_at > now),
    )


def _superseded_by_a_readable_version(
    version: Any, scopes: ResolvedScopes, now: datetime
) -> ColumnElement[bool]:
    """A ``supersedes`` relation points at ``version`` from a version that is readable.

    Such a ``version`` is not current even when its ``status`` still says
    ``active`` (the status moves in the same write as the relation, but a row that
    disagrees must not be offered as current). Only a successor the caller may read
    counts: a hidden successor never changes what the caller gets. The lookup uses
    the unique partial index on ``memory_relations (to_version_id)`` of supersedes.
    """
    relation = aliased(MemoryRelation)
    successor = aliased(MemoryVersion)
    return exists(
        select(literal_column("1"))
        .select_from(relation)
        .join(successor, successor.id == relation.from_version_id)
        .where(
            relation.to_version_id == version.id,
            relation.relation_type == literal_column("'" + SUPERSEDES + "'"),
            _live(successor, scopes, now),
        )
        .correlate(version)
    )


def _stale(
    version: Any, now: datetime, repo_heads: Mapping[UUID, str]
) -> ColumnElement[bool]:
    """``ranking.freshness_of`` is stale, as a SQL condition that is never NULL.

    ``verified_at + revalidate_after`` is computed in UTC (the retrieval's
    transaction sets the time zone), where a day is 24 hours, as ``timedelta`` is.
    """
    stale = [
        version.stale_since.is_not(None),
        and_(
            version.freshness_policy == literal_column("'revalidate'"),
            or_(
                version.verified_at.is_(None),
                version.revalidate_after.is_(None),
                version.verified_at + version.revalidate_after <= now,
            ),
        ),
    ]
    for repo_id, commit in sorted(repo_heads.items()):
        stale.append(
            and_(
                version.freshness_policy == literal_column("'repo_commit'"),
                version.repo_id.is_not_distinct_from(repo_id),
                version.commit_sha.is_distinct_from(commit),
            )
        )
    return or_(*stale)


def _shared_rules(
    version: Any, policy_subjects: tuple[str, ...]
) -> ColumnElement[bool]:
    """Shared memories a System Policy covers, or that cannot be judged, are out.

    The rules are those of ``memory/shared`` (``_subjects_of`` and
    ``precedence.subject_covers``), in SQL so that they act before the candidate
    limits. A memory that is not shared is not touched. A shared memory declares
    subjects in ``attributes.policy_subjects``: absent means none; otherwise it
    must be an array of at most ``MAX_POLICY_SUBJECTS`` strings, each a valid
    subject (a malformed declaration cannot be judged: left out, as the Shared Memory
    service treats it as corrupt data). It is covered when one of its subjects
    equals a policy subject or lies below it in the dotted hierarchy: ``starts_with``
    of ``policy + '.'``, not ``LIKE`` (``_`` is a wildcard there).
    """
    declared = version.attributes["policy_subjects"]
    is_array = func.jsonb_typeof(declared) == literal_column("'array'")
    # ``CASE`` guarantees the order: no set-returning function sees a non-array.
    array = case((is_array, declared), else_=literal_column("'[]'::jsonb"))

    elements = func.jsonb_array_elements(array).table_valued("element").render_derived()
    element = elements.c.element
    text_of = element.op("#>>")(literal_column("'{}'::text[]"))
    malformed = exists(
        select(literal_column("1"))
        .select_from(elements)
        .where(
            or_(
                func.jsonb_typeof(element) != literal_column("'string'"),
                func.char_length(text_of) > MAX_SUBJECT_CHARS,
                text_of.op("!~")(literal_column("'" + SUBJECT_PATTERN + "'")),
            )
        )
    )
    judged = [
        is_array,
        func.jsonb_array_length(array) <= MAX_POLICY_SUBJECTS,
        ~malformed,
    ]
    if policy_subjects:
        declared_subjects = (
            func.jsonb_array_elements_text(array)
            .table_valued("subject")
            .render_derived()
        )
        subject = declared_subjects.c.subject
        governed_subjects = (
            func.unnest(
                bindparam(
                    "policy_subjects",
                    list(policy_subjects),
                    type_=ARRAY(Text),
                    unique=True,
                )
            )
            .table_valued("governed")
            .render_derived()
        )
        governed = governed_subjects.c.governed
        covered = exists(
            select(literal_column("1"))
            .select_from(declared_subjects)
            .where(
                exists(
                    select(literal_column("1"))
                    .select_from(governed_subjects)
                    .where(
                        or_(
                            subject == governed,
                            func.starts_with(
                                subject, governed.concat(literal_column("'.'"))
                            ),
                        )
                    )
                )
            )
        )
        judged.append(~covered)
    return or_(
        version.scope != literal_column("'shared'"),
        ~version.attributes.has_key("policy_subjects"),
        and_(*judged),
    )


def visible(
    version: Any,
    scopes: ResolvedScopes,
    now: datetime,
    eligible: Eligibility = NO_EXTRA_CONDITIONS,
) -> ColumnElement[bool]:
    """The rows of ``version`` (an alias of ``MemoryVersion``) a retrieval may use.

    Everything that decides whether a row may be a candidate, so that it acts BEFORE
    the ordering and the limit of a statement:

    * permission, scope, ``active``, long-term and not expired (``_live``);
    * not superseded by a readable version (``_superseded_by_a_readable_version``);
    * not stale, when ``eligible.exclude_stale``;
    * for shared memory, not covered by the System Policy (``_shared_rules``; only
      when the shared scope is searched).

    ``stale`` memories otherwise stay: they are lowered and flagged by the ranking.
    """
    conditions = [
        _live(version, scopes, now),
        ~_superseded_by_a_readable_version(version, scopes, now),
    ]
    if eligible.exclude_stale:
        conditions.append(~_stale(version, now, eligible.repo_heads))
    if MemoryScope.SHARED in scopes.scopes:
        conditions.append(_shared_rules(version, eligible.policy_subjects))
    return and_(*conditions)


def _columns(version: Any) -> list[ColumnElement[Any]]:
    return [
        version.id,
        version.memory_id,
        version.version_number,
        version.scope,
        version.project_id,
        version.repo_id,
        version.project_group_id,
        version.memory_type,
        version.title,
        version.content,
        version.importance,
        version.pinned,
        version.confirmation_state,
        version.freshness_policy,
        version.verified_at,
        version.revalidate_after,
        version.stale_since,
        version.commit_sha,
        version.attributes["policy_subjects"].label("policy_subjects"),
    ]


def keyword_statement(
    scopes: ResolvedScopes,
    now: datetime,
    tsquery: str,
    limit: int,
    eligible: Eligibility = NO_EXTRA_CONDITIONS,
) -> Select[Any]:
    """Readable active versions whose text matches ``tsquery``, best rank first."""
    version = aliased(MemoryVersion, name="mv")
    document = literal_column(search_document_sql("mv.title", "mv.content"))
    query = func.to_tsquery(_TS_CONFIG, bindparam("tsquery", tsquery, type_=Text))
    rank = func.ts_rank(document, query, _RANK_NORMALISATION).label("keyword_score")
    return (
        select(*_columns(version), rank)
        .where(visible(version, scopes, now, eligible), document.op("@@")(query))
        .order_by(rank.desc(), version.id)
        .limit(limit)
    )


def vector_statement(
    scopes: ResolvedScopes,
    now: datetime,
    model_id: str,
    dimensions: int,
    vector: Sequence[float],
    limit: int,
    max_distance: float | None = None,
    eligible: Eligibility = NO_EXTRA_CONDITIONS,
) -> Select[Any]:
    """Readable active versions nearest to ``vector`` (cosine distance).

    One embedding model only (its vectors all have one dimension). The
    permission condition is on the joined ``memory_versions`` row, in the WHERE of
    the same query that orders by distance. ``max_distance`` (from the policy's
    ``min_vector_similarity``) is in that WHERE too, so the limit counts only
    memories that are near enough.
    """
    version = aliased(MemoryVersion, name="mv")
    query_vector = list(vector)
    distance = MemoryEmbedding.embedding.cosine_distance(query_vector).label("distance")
    statement = (
        select(*_columns(version), distance)
        .select_from(MemoryEmbedding)
        .join(version, version.id == MemoryEmbedding.memory_version_id)
        .where(
            MemoryEmbedding.embedding_model_id == model_id,
            MemoryEmbedding.dimensions == dimensions,
            visible(version, scopes, now, eligible),
        )
        .order_by(distance, version.id)
        .limit(limit)
    )
    if max_distance is not None:
        statement = statement.where(
            MemoryEmbedding.embedding.cosine_distance(query_vector) <= max_distance
        )
    return statement


def relations_statement(
    scopes: ResolvedScopes,
    now: datetime,
    version_ids: Collection[UUID],
    limit: int,
    eligible: Eligibility = NO_EXTRA_CONDITIONS,
) -> Select[Any]:
    """Conflict relations of ``version_ids`` whose BOTH ends are eligible candidates.

    A relation to a memory the caller may not read (or that is not eligible: stale
    when stale ones are excluded, covered by the System Policy, superseded) is not
    returned: neither its existence nor the other end is ever visible. ``supersedes``
    is not read here: a superseded row is not a candidate at all (``visible``).
    """
    newer = aliased(MemoryVersion, name="nv")
    older = aliased(MemoryVersion, name="ov")
    ids = sorted(version_ids)
    return (
        select(
            MemoryRelation.from_version_id,
            MemoryRelation.to_version_id,
            MemoryRelation.relation_type,
        )
        .join(newer, newer.id == MemoryRelation.from_version_id)
        .join(older, older.id == MemoryRelation.to_version_id)
        .where(
            MemoryRelation.relation_type == literal_column("'" + CONFLICT + "'"),
            or_(
                MemoryRelation.from_version_id.in_(ids),
                MemoryRelation.to_version_id.in_(ids),
            ),
            visible(newer, scopes, now, eligible),
            visible(older, scopes, now, eligible),
        )
        .order_by(
            MemoryRelation.from_version_id,
            MemoryRelation.to_version_id,
            MemoryRelation.relation_type,
        )
        .limit(limit)
    )


def versions_statement(
    scopes: ResolvedScopes,
    now: datetime,
    version_ids: Collection[UUID],
    eligible: Eligibility = NO_EXTRA_CONDITIONS,
) -> Select[Any]:
    """The eligible candidates among ``version_ids``."""
    version = aliased(MemoryVersion, name="mv")
    return select(*_columns(version)).where(
        version.id.in_(sorted(version_ids)), visible(version, scopes, now, eligible)
    )


def memberships_statement(
    user_id: UUID, project_ids: Collection[UUID] | None, limit: int
) -> Select[Any]:
    """The accepted memberships of ``user_id`` in projects that can be read.

    An invitation is not a membership, and a project in Pending deletion or
    Deleted is not readable. ``project_ids`` narrows the query (``None``: all).
    """
    statement = (
        select(ProjectMemberRow.project_id, ProjectMemberRow.role, ProjectRow.status)
        .join(ProjectRow, ProjectRow.id == ProjectMemberRow.project_id)
        .where(
            ProjectMemberRow.user_id == user_id,
            ProjectMemberRow.status == literal_column("'active'"),
            ProjectRow.status.in_(
                [literal_column(f"'{s}'") for s in _READABLE_PROJECT_STATES]
            ),
        )
        .order_by(ProjectMemberRow.project_id)
        .limit(limit)
    )
    if project_ids is not None:
        statement = statement.where(
            ProjectMemberRow.project_id.in_(sorted(project_ids))
        )
    return statement
