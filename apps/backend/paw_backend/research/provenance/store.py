"""The Evidence / Claim Provenance store (PAW-052).

Which sources back a claim, which claims an answer or a task used, and which
claims (or sources) duplicate or contradict each other. Kept apart from
Long-term Memory and from the Research Scratch Store: provenance is permanent
and is never deleted by the application (see ``models.py``).

Who may call it
---------------
The store performs **no authorisation**, like ``TaskService`` and
``ScratchStore``: the API layer (a later issue) decides first and passes only ids
it has checked. There is no HTTP surface yet. The intended mapping to the
capabilities of ``paw_backend.authz`` (a proposal, not enforced here):
``get_claim``, ``trace`` and ``list_relations``: ``project.read``;
``record_claim``, ``add_reference`` and ``mark_related``: ``project.task.run``.

What is written where (this module orchestrates; the rules live in ``rules.py``
and the statements in ``queries.py``)
-----------------------------------------------------------------------------
* ``record_claim`` validates everything, merges duplicate sources of the call,
  then in ONE transaction: checks the task, creates the claim unless the same
  normalised text exists in the project, locks the claim, creates each
  source unless it exists, links it, enforces ``MAX_SOURCES_PER_CLAIM`` and
  registers the task's use of the claim. Any failure rolls everything back.
* Everything is **project scoped**: every method takes ``project_id`` and finds
  rows only inside it. An id of another project is "not found", exactly like a
  missing id. The database backs this with composite foreign keys.
* Recorded rows are never updated: the first record of a source, a claim, a link
  or a relation stays. A contradicting second record is a conflict, not an edit.

Time
----
Every write operation reads the injected clock once (``Clock``: a callable
without arguments returning an aware ``datetime``), validates it and uses that
instant as ``created_at`` of everything it writes.

Concurrency contract
--------------------
READ COMMITTED. Rules that make concurrent calls safe:

1. Two calls that record the same claim text race on the unique fingerprint: the
   second waits for the first to commit and then adds its sources to the same
   claim (exactly one call reports ``created=True``).
2. After the claim exists, a transaction-level advisory lock on the claim is
   taken before any source is linked, so concurrent calls that add sources to
   one claim run one after the other and ``MAX_SOURCES_PER_CLAIM`` cannot be
   exceeded by a race. (An advisory lock needs no table privilege: the
   application role holds SELECT and INSERT only, and ``SELECT ... FOR UPDATE``
   would need UPDATE.) Existence checks are plain reads; the composite foreign
   keys make the database itself refuse a link to a row that is not there.
3. Sources are created in a fixed order (by locator, then content hash), so two
   calls with overlapping sources cannot deadlock on each other.
4. Every write transaction begins with ``SET LOCAL lock_timeout`` set to
   ``lock_timeout_ms``. A lock wait that exceeds it, or a deadlock the database
   resolves against this call, raises :class:`ProvenanceBusyError` (rolled back).
5. Reads take no locks and run in one ``REPEATABLE READ`` read-only transaction,
   so a trace is a consistent snapshot.

Errors
------
Validation problems are :class:`InvalidProvenanceInputError`, raised before the
database is touched (a store on an unconfigured ``Database`` still reports
them), in the order the arguments are listed in each docstring. Messages never
contain caller content. Database errors the store does not handle propagate
unchanged; their text can contain SQL parameters, so a caller must never show
``str(error)`` to a user. The store logs nothing.
"""

import hashlib
import inspect
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg.errors
from sqlalchemy import BigInteger, Table, func, literal, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.research.provenance import queries
from paw_backend.research.provenance.errors import (
    ClaimNotFoundError,
    InputProblem,
    InvalidProvenanceInputError,
    ProvenanceBusyError,
    ProvenanceLimitError,
    SourceNotFoundError,
)
from paw_backend.research.provenance.limits import (
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_TRACE_LIMIT,
    MAX_CLAIM_TEXT_CHARS,
    MAX_CLAIMS_PER_CALL,
    MAX_LOCK_TIMEOUT_MS,
    MAX_SOURCES_PER_CALL,
    MAX_SOURCES_PER_CLAIM,
    MAX_TRACE_LIMIT,
    MIN_LOCK_TIMEOUT_MS,
)
from paw_backend.research.provenance.models import CLAIM_SOURCES, CLAIMS, SOURCES
from paw_backend.research.provenance.records import (
    EntityKind,
    RecordedClaim,
    Reference,
    ReferenceKind,
    Relation,
    RelationKind,
    SourceLink,
    SourceLinkInput,
    Trace,
    TracedClaim,
)
from paw_backend.research.provenance.rules import (
    assemble_traced_claims,
    claim_fingerprint,
    merge_duplicate_sources,
    order_pair,
    order_relations,
)
from paw_backend.research.provenance.validation import (
    validate_bounded_int,
    validate_datetime,
    validate_enum,
    validate_optional_uuid,
    validate_text,
    validate_uuid,
)
from paw_backend.tasks.models import TaskRow

Clock = Callable[[], datetime]

_TASKS = TaskRow.__table__


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_sequence(
    field: str, value: object, *, item_type: type, maximum: int
) -> tuple[Any, ...]:
    """``value`` must be a non-empty list or tuple of at most ``maximum``
    ``item_type`` objects (every element is checked before anything else)."""
    if value is None:
        raise InvalidProvenanceInputError(field, InputProblem.REQUIRED)
    if not isinstance(value, list | tuple):
        raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    if not value:
        raise InvalidProvenanceInputError(field, InputProblem.EMPTY)
    if len(value) > maximum:
        raise InvalidProvenanceInputError(field, InputProblem.TOO_MANY)
    for element in value:
        if not isinstance(element, item_type):
            raise InvalidProvenanceInputError(field, InputProblem.WRONG_TYPE)
    return tuple(value)


def _validate_reference(value: object) -> Reference:
    if value is None:
        raise InvalidProvenanceInputError("reference", InputProblem.REQUIRED)
    if not isinstance(value, Reference):
        raise InvalidProvenanceInputError("reference", InputProblem.WRONG_TYPE)
    return value


class ProvenanceStore:
    """Persistence and rules of the provenance store (see the module doc)."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Clock | None = None,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        """Validate the collaborators up front; a wrong one fails here, loudly.

        ``database`` must be a :class:`Database` (``TypeError`` otherwise).
        ``clock`` defaults to the current UTC time; it must be callable with no
        arguments (``TypeError`` otherwise). ``lock_timeout_ms`` must be an
        ``int`` (not a ``bool``) from 50 to 60000 (``TypeError`` for another
        type, ``ValueError`` when out of range). Nothing connects here.
        """
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if clock is None:
            clock = _utc_now
        elif not callable(clock):
            raise TypeError("clock must be callable")
        else:
            try:
                inspect.signature(clock).bind()
            except TypeError:
                raise TypeError("clock must be callable without arguments") from None
            except ValueError:
                pass  # a callable without an introspectable signature
        if isinstance(lock_timeout_ms, bool) or not isinstance(lock_timeout_ms, int):
            raise TypeError("lock_timeout_ms must be an int")
        if not MIN_LOCK_TIMEOUT_MS <= lock_timeout_ms <= MAX_LOCK_TIMEOUT_MS:
            raise ValueError("lock_timeout_ms is out of range")
        self._database = database
        self._clock: Clock = clock
        self._lock_timeout_ms = lock_timeout_ms

    # -- helpers ---------------------------------------------------------------

    def _now(self) -> datetime:
        """The clock's instant, validated (aware, UTC). ``clock`` misbehaving is
        reported as ``InvalidProvenanceInputError("clock", ...)``."""
        return validate_datetime("clock", self._clock())

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        """One write transaction with the store's lock timeout (rule 4)."""
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(
                    select(
                        func.set_config(
                            "lock_timeout", str(self._lock_timeout_ms), True
                        )
                    )
                )
                yield session
        except DBAPIError as error:
            # Only the type of the driver's error is read, never its text.
            if isinstance(
                error.orig,
                psycopg.errors.LockNotAvailable | psycopg.errors.DeadlockDetected,
            ):
                raise ProvenanceBusyError() from None
            raise

    @asynccontextmanager
    async def _snapshot(self) -> AsyncIterator[AsyncSession]:
        """One read-only ``REPEATABLE READ`` transaction (rule 5)."""
        async with self._database.session() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            yield session

    @staticmethod
    async def _require_task(
        session: AsyncSession, project_id: UUID, task_id: UUID
    ) -> None:
        """The task exists in the project. Missing, or another project's, is
        ``InvalidProvenanceInputError("task_id", UNKNOWN_REFERENCE)``. (Tasks are
        never deleted by the application, and ``research_claims.task_id`` is a
        foreign key, so no lock is needed.)"""
        statement = select(_TASKS.c.id).where(
            _TASKS.c.id == task_id, _TASKS.c.project_id == project_id
        )
        if (await session.execute(statement)).first() is None:
            raise InvalidProvenanceInputError("task_id", InputProblem.UNKNOWN_REFERENCE)

    @staticmethod
    async def _require_rows(
        session: AsyncSession,
        table: Table,
        project_id: UUID,
        ids: Sequence[UUID],
    ) -> None:
        """Every id names a row of ``table`` in the project, else the matching
        not-found error (claims: ``ClaimNotFoundError``, sources:
        ``SourceNotFoundError``)."""
        statement = select(table.c.id).where(
            table.c.project_id == project_id, table.c.id.in_(list(ids))
        )
        found = {row[0] for row in await session.execute(statement)}
        if found != set(ids):
            raise ClaimNotFoundError() if table is CLAIMS else SourceNotFoundError()

    @staticmethod
    async def _serialize_claim(session: AsyncSession, claim_id: UUID) -> None:
        """Wait for, then hold until the transaction ends, the advisory lock of
        the claim (rule 2). Two claims whose keys collide only wait for each
        other needlessly."""
        digest = hashlib.sha256(b"paw.provenance.claim:" + claim_id.bytes).digest()
        key = int.from_bytes(digest[:8], "big", signed=True)
        await session.execute(
            select(func.pg_advisory_xact_lock(literal(key, BigInteger)))
        )

    # -- write -----------------------------------------------------------------

    async def record_claim(
        self,
        project_id: UUID,
        *,
        created_by: UUID,
        text: str,
        sources: Sequence[SourceLinkInput],
        task_id: UUID | None = None,
    ) -> RecordedClaim:
        """Record a claim with its sources; return what is recorded.

        Validation, in this order, before the database is touched:
        ``project_id``, ``created_by``, ``task_id`` (UUIDs; ``task_id`` may be
        ``None``), ``text`` (a non-blank ``str`` of at most 2000 characters,
        kept as given), ``sources`` (a list or tuple of 1 to 20
        ``SourceLinkInput``: ``None`` is ``REQUIRED``, another type
        ``WRONG_TYPE``, empty ``EMPTY``, more than 20 ``TOO_MANY``, an element of
        another type ``WRONG_TYPE``), then ``merge_duplicate_sources`` (the same
        source twice with another stance is ``sources`` / ``CONFLICT``).

        Then, in one transaction (all or nothing):

        1. ``task_id`` given: the task must exist in ``project_id``, else
           ``InvalidProvenanceInputError("task_id", UNKNOWN_REFERENCE)``.
        2. The claim: if a claim with the same ``claim_fingerprint(text)`` exists
           in the project it is used as it is (``created=False``; its text,
           creator, task and time do not change), otherwise it is created.
        3. Each distinct source is created unless it exists, then linked to the
           claim with its stance. A link that exists with the same stance is
           kept (not counted in ``new_links``); with the other stance the call
           fails with :class:`ProvenanceConflictError`.
        4. If the claim would have more than ``MAX_SOURCES_PER_CLAIM`` (50) links
           in total: :class:`ProvenanceLimitError`.
        5. ``task_id`` given: the task is registered as a user of the claim.

        Recording the same call twice changes nothing the second time
        (``created=False``, ``new_links=0``).
        """
        project = validate_uuid("project_id", project_id)
        creator = validate_uuid("created_by", created_by)
        task = validate_optional_uuid("task_id", task_id)
        claim_text = validate_text("text", text, max_chars=MAX_CLAIM_TEXT_CHARS)
        given = _validate_sequence(
            "sources", sources, item_type=SourceLinkInput, maximum=MAX_SOURCES_PER_CALL
        )
        distinct = merge_duplicate_sources(given)
        fingerprint = claim_fingerprint(claim_text)
        now = self._now()
        # A fixed order for every transaction: overlapping calls cannot deadlock.
        ordered = sorted(
            distinct, key=lambda link: (link.source.locator, link.source.content_hash)
        )
        linked: dict[tuple[str, str], SourceLink] = {}
        new_links = 0
        async with self._transaction() as session:
            if task is not None:
                await self._require_task(session, project, task)
            claim, created = await queries.ensure_claim(
                session,
                project,
                created_by=creator,
                task_id=task,
                text=claim_text,
                fingerprint=fingerprint,
                created_at=now,
            )
            await self._serialize_claim(session, claim.id)
            for entry in ordered:
                source, _ = await queries.ensure_source(
                    session, project, entry.source, now
                )
                link, link_created = await queries.link_claim_source(
                    session, project, claim.id, source, entry.stance, now
                )
                linked[(entry.source.locator, entry.source.content_hash)] = link
                new_links += 1 if link_created else 0
            total = (
                await session.execute(
                    select(func.count())
                    .select_from(CLAIM_SOURCES)
                    .where(CLAIM_SOURCES.c.claim_id == claim.id)
                )
            ).scalar_one()
            if total > MAX_SOURCES_PER_CLAIM:
                raise ProvenanceLimitError()
            if task is not None:
                await queries.add_claim_use(
                    session, project, claim.id, Reference.task(task), creator, now
                )
        return RecordedClaim(
            claim=claim,
            created=created,
            links=tuple(
                linked[(entry.source.locator, entry.source.content_hash)]
                for entry in distinct
            ),
            new_links=new_links,
        )

    async def add_reference(
        self,
        project_id: UUID,
        *,
        reference: Reference,
        claim_ids: Sequence[UUID],
        created_by: UUID,
    ) -> int:
        """Record that the answer or task ``reference`` used the claims; return
        how many uses are new.

        Validation, in this order: ``project_id``, ``reference`` (a
        :class:`Reference`), ``claim_ids`` (a list or tuple of 1 to 50 UUIDs;
        repeated ids count once), ``created_by``. Then, in one transaction: a
        task reference must name a task of the project
        (``InvalidProvenanceInputError("reference", UNKNOWN_REFERENCE)``
        otherwise); every claim must exist in the project
        (:class:`ClaimNotFoundError` otherwise, nothing is recorded). A use that
        already exists is kept and not counted. An answer id is not checked (no
        answers table yet).
        """
        project = validate_uuid("project_id", project_id)
        used_by = _validate_reference(reference)
        ids = _validate_sequence(
            "claim_ids", claim_ids, item_type=UUID, maximum=MAX_CLAIMS_PER_CALL
        )
        creator = validate_uuid("created_by", created_by)
        distinct = sorted(set(ids), key=lambda claim_id: claim_id.int)
        now = self._now()
        added = 0
        async with self._transaction() as session:
            if used_by.kind is ReferenceKind.TASK:
                try:
                    await self._require_task(session, project, used_by.id)
                except InvalidProvenanceInputError:
                    raise InvalidProvenanceInputError(
                        "reference", InputProblem.UNKNOWN_REFERENCE
                    ) from None
            await self._require_rows(session, CLAIMS, project, distinct)
            for claim_id in distinct:
                created = await queries.add_claim_use(
                    session, project, claim_id, used_by, creator, now
                )
                added += 1 if created else 0
        return added

    async def mark_related(
        self,
        project_id: UUID,
        *,
        entity: EntityKind,
        kind: RelationKind,
        first_id: UUID,
        second_id: UUID,
        created_by: UUID,
    ) -> Relation:
        """Record that two claims (or two sources) duplicate or contradict each
        other; return the relation.

        Validation, in this order: ``project_id``, ``entity`` (an
        :class:`EntityKind`), ``kind`` (a :class:`RelationKind`), ``first_id``,
        ``second_id``, ``created_by`` (UUIDs), then
        ``InvalidProvenanceInputError("second_id", SELF_REFERENCE)`` when the two
        ids are equal. Both must exist in the project (:class:`ClaimNotFoundError`
        / :class:`SourceNotFoundError` otherwise). The relation is symmetric:
        naming the pair in either order is the same relation. Marking a pair
        that already has this relation returns the existing one unchanged; a
        pair that has the other kind raises :class:`ProvenanceConflictError`.
        """
        project = validate_uuid("project_id", project_id)
        kind_of_entity = validate_enum("entity", entity, EntityKind)
        relation_kind = validate_enum("kind", kind, RelationKind)
        first = validate_uuid("first_id", first_id)
        second = validate_uuid("second_id", second_id)
        creator = validate_uuid("created_by", created_by)
        if first == second:
            raise InvalidProvenanceInputError("second_id", InputProblem.SELF_REFERENCE)
        low, high = order_pair(first, second)
        now = self._now()
        table = CLAIMS if kind_of_entity is EntityKind.CLAIM else SOURCES
        async with self._transaction() as session:
            await self._require_rows(session, table, project, [low, high])
            relation, _ = await queries.insert_relation(
                session, project, kind_of_entity, relation_kind, low, high, creator, now
            )
        return relation

    # -- read ------------------------------------------------------------------

    async def get_claim(self, project_id: UUID, claim_id: UUID) -> TracedClaim:
        """The claim with its sources and its relations to other claims.

        ``project_id``, ``claim_id`` are validated in that order.
        :class:`ClaimNotFoundError` when the project has no such claim. Order:
        see ``assemble_traced_claims``.
        """
        project = validate_uuid("project_id", project_id)
        claim_uuid = validate_uuid("claim_id", claim_id)
        async with self._snapshot() as session:
            claim = await queries.fetch_claim(session, project, claim_uuid)
            if claim is None:
                raise ClaimNotFoundError()
            links = await queries.fetch_claim_links(session, project, [claim.id])
            relations = await queries.fetch_relations(
                session, project, EntityKind.CLAIM, [claim.id]
            )
        (traced,) = assemble_traced_claims([claim], links, relations)
        return traced

    async def trace(
        self,
        project_id: UUID,
        reference: Reference,
        *,
        limit: int = DEFAULT_TRACE_LIMIT,
    ) -> Trace:
        """The claims that the answer or task ``reference`` used, with their
        sources (dates and types) and relations.

        Validation, in this order: ``project_id``, ``reference``, ``limit`` (an
        ``int``, not a ``bool``, from 1 to 200; default 100). A reference that
        nobody used, an unknown one and one of another project all give an empty
        trace (``claims=()``, ``truncated=False``): the store cannot tell them
        apart, and an answer id has no table to check. ``truncated`` is True
        when more claims than ``limit`` exist. One consistent snapshot.
        """
        project = validate_uuid("project_id", project_id)
        used_by = _validate_reference(reference)
        row_limit = validate_bounded_int(
            "limit", limit, minimum=1, maximum=MAX_TRACE_LIMIT
        )
        async with self._snapshot() as session:
            claims, truncated = await queries.fetch_reference_claims(
                session, project, used_by, row_limit
            )
            ids = [claim.id for claim in claims]
            links = await queries.fetch_claim_links(session, project, ids)
            relations = await queries.fetch_relations(
                session, project, EntityKind.CLAIM, ids
            )
        return Trace(
            reference=used_by,
            claims=assemble_traced_claims(claims, links, relations),
            truncated=truncated,
        )

    async def list_relations(
        self, project_id: UUID, entity: EntityKind, entity_id: UUID
    ) -> tuple[Relation, ...]:
        """Every relation the claim or source ``entity_id`` is part of, ordered
        by ``order_relations``.

        Validation, in this order: ``project_id``, ``entity``, ``entity_id``.
        :class:`ClaimNotFoundError` / :class:`SourceNotFoundError` when the
        project has no such claim / source.
        """
        project = validate_uuid("project_id", project_id)
        kind_of_entity = validate_enum("entity", entity, EntityKind)
        item_id = validate_uuid("entity_id", entity_id)
        table = CLAIMS if kind_of_entity is EntityKind.CLAIM else SOURCES
        async with self._snapshot() as session:
            await self._require_rows(session, table, project, [item_id])
            relations = await queries.fetch_relations(
                session, project, kind_of_entity, [item_id]
            )
        return order_relations(kind_of_entity, item_id, relations)
