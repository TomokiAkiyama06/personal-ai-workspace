"""Manual Memory versioning (PAW-042): edit, restore, retire, revalidate, relate.

A person changes a memory through this service (the Memory UI of a later issue
calls it with the authenticated user). It writes the PAW-040 tables and nothing
else; the choices the requirements leave open are proposed in Decision 0034.

Versions are never overwritten
------------------------------
REQUIREMENTS.md "Manual Memory Editing": an edit is a **new version**, and the
old one stays as history.

* ``edit_memory``: the current version ``n`` (``active``) becomes ``superseded``;
  version ``n + 1`` is written ``active`` and ``confirmed`` with the person as its
  actor, and a ``supersedes`` relation (reason: the changed field names) points
  from ``n + 1`` to ``n``. When ``n`` was not confirmed (a worker's ``observed`` or
  ``inferred`` candidate) a ``confirmed_from`` relation records the promotion too.
  An edit that changes nothing writes nothing and returns the current version.
  An edit may also **narrow** a ``project`` memory to the editor's own ``user``
  scope (REQUIREMENTS.md "Scope変更": at once), in the same transaction; it never
  widens one.
* ``restore_version``: MEMORY_ARCHITECTURE.md section 15, "過去versionへ戻す場合、
  古いversionを直接activeへ戻すのではなく": version ``n + 1`` is written with the
  content of the chosen old version; the current one is retired as by an edit
  (a deprecated current version stays ``deprecated``; the relation still says what
  replaced it). ``attributes.restored_from_version`` names the source.
* ``deprecate_memory``: the current version becomes ``deprecated`` (explicitly
  invalidated). Nothing is erased; ``restore_version`` brings the content back.
* ``revalidate_memory``: the stale-candidate answer "still true". Version
  ``n + 1`` repeats the content with ``verified_at`` = now and no stale mark, with
  ``supersedes`` and ``revalidated_from`` relations to ``n``.
* ``relate_memories``: a relation between the current versions of two memories.
  ``supersedes`` retires the older one (only between memories with the same
  audience); ``extends`` and ``conflicts_with`` leave both ``active``.

``expected_version`` is the Optimistic Lock of every change: it must be the
current version number, else :class:`MemoryVersionConflictError` and nothing is
written. The "current version" is the one with the highest ``version_number``.

Every status change is recorded by the database in ``memory_metadata_changes``
with the person as its actor (revision 0071; ``metadata_change_actor`` is named
first in the same transaction). Every new version carries ``actor_type = 'user'``
and the person's id. The retrieval (PAW-043) offers ``active`` versions only, so a
superseded, deprecated or history version never reaches a normal LLM context.

Who may change what (Decision 0034)
-----------------------------------
Only a human :class:`~paw_backend.authz.Principal` calls this service: a manual
edit is a person's act, and the version it writes is ``confirmed``. (Agents and the
background worker write candidates through the Immediate Journal, PAW-041.)

* ``user`` scope: capability ``memory.use`` on a resource owned by the memory's
  owner. Only the owner passes.
* ``project`` scope: capability ``project.memory.use`` on the project, decided with
  the role and the project state READ FROM THE DATABASE (never the roles the
  caller's ``Principal`` carries). A Viewer, and any member of an archived
  project, is refused.
* ``shared``: :class:`MemoryScopeNotSupportedError` (``SharedMemoryService`` owns
  it). ``repo`` and ``project_group``: :class:`MemoryNotFoundError` (their write
  permission is an open point of Decision 0034).

The Authorizer records every decision (both capabilities are ``REQUIRED``). A
denial because the memory is somebody else's (``not_resource_owner``) or belongs to
a project the actor is not a member of (``not_project_member``) is reported as
:class:`MemoryNotFoundError`, like a missing id, so that the answer does not tell
whether such a memory exists; so is any denial of a project memory to a person who
is not a member of the project (the Authorizer checks the project state first, and
``project_state_forbids`` would tell a non-member that the memory exists). Any other
denial is :class:`MemoryPermissionError`. ``history`` returns a version whose
audience differs from the current one only to a reader of that audience too.

Order of every call
-------------------
1. Arguments are validated (:class:`InvalidMemoryInputError`).
2. A transaction starts with ``SET LOCAL lock_timeout``, takes a transaction-level
   advisory lock per memory (``memory_lock_key``; two memories in a fixed order;
   a ``supersedes`` / ``extends`` relation also takes ``RELATION_GRAPH_LOCK_KEY``
   so that its cycle check sees every other such relation), and reads the current
   version's **audience** ``FOR UPDATE`` (see "The ACL in SQL").
3. The actor is authorized against that audience.
4. The version's content is read with the ACL in SQL, then the change is written;
   a lock wait over the timeout is :class:`MemoryBusyError`.

The ACL in SQL
--------------
``memory/acl.py``: every read of ``memory_versions`` applies
:func:`~paw_backend.memory.acl.readable_memory_versions`, so that a row the actor
may not read never reaches the backend, not even to be filtered out afterwards.
Every read of a version's content (title, content, attributes and the other
columns: ``_load``, ``history``) applies that condition, built from the audiences
the Authorizer allowed (``_readable``), and ``scope IN`` those audiences' scopes.

Two reads are documented exceptions, because the ACL cannot be decided without
them and nothing but the ACL's own inputs is returned:

* the **audience** of a version (``_AUDIENCE_COLUMNS``: its scope and the four
  scope ids, plus the id and number of the current version to lock and compare).
  The ACL is derived from exactly these columns; reading them lets the Authorizer
  decide (and audit) the actor's access, and a denial is reported as not found;
* the **cycle check** (``_REACHES``) of a manual ``supersedes`` / ``extends``,
  which walks the whole relation graph (a cycle through versions the actor cannot
  read must be refused too) and returns one boolean.

The duplicate check of a relation reads ``memory_relations`` only between the two
versions the actor was just allowed to change, and only their ids and type.

The freshness jobs (``freshness.py``) run as the ``system`` actor, not for a
person: they UPDATE rows and return a count, and read no content.

The Immediate Journal's consolidator does not take these advisory locks. It
supersedes with ``UPDATE ... WHERE status = 'active'`` and inserts under the unique
``(memory_id, version_number)``, so whichever writer comes second fails (the
consolidator retries its job; this service raises
:class:`MemoryVersionConflictError`) instead of overwriting the other.
"""

import logging
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg.errors
from sqlalchemy import ColumnElement, and_, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import (
    Authorizer,
    Capability,
    Decision,
    Principal,
    ProjectRole,
    ProjectState,
    Reason,
    Resource,
)
from paw_backend.db import Database
from paw_backend.memory.acl import Principal as AclPrincipal
from paw_backend.memory.acl import readable_memory_versions
from paw_backend.memory.metadata import metadata_change_actor
from paw_backend.memory.models import (
    ActorType,
    ConfirmationState,
    FreshnessPolicy,
    Memory,
    MemoryRelation,
    MemoryScope,
    MemoryStatus,
    MemoryVersion,
    RelationType,
)
from paw_backend.memory.versioning import limits, rules
from paw_backend.memory.versioning.errors import (
    InputProblem,
    MemoryBusyError,
    MemoryDatabaseError,
    MemoryNotFoundError,
    MemoryPermissionError,
    MemoryScopeNotSupportedError,
    MemoryStateError,
    MemoryVersionConflictError,
    MemoryVersioningError,
    StateProblem,
    raise_detached,
)
from paw_backend.memory.versioning.records import (
    FreshnessSpec,
    ManualRelation,
    MemoryChanges,
    MemoryDraft,
    MemoryRelationView,
    MemoryVersionView,
)
from paw_backend.memory.versioning.validation import (
    reject,
    validate_aware_datetime,
    validate_enum,
    validate_int,
    validate_optional_text,
    validate_uuid,
    validate_version_number,
)
from paw_backend.projects.models import ProjectMemberRow, ProjectRow

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

RESOURCE_MEMORY = "memory"

_MEMORIES = Memory.__table__
_VERSIONS = MemoryVersion.__table__
_RELATIONS = MemoryRelation.__table__
_PROJECTS = ProjectRow.__table__
_MEMBERS = ProjectMemberRow.__table__

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
# The denials that mean "not yours": reported as "not found" (see the module text).
_HIDDEN_DENIALS = frozenset({Reason.NOT_RESOURCE_OWNER, Reason.NOT_PROJECT_MEMBER})
# The unique constraints another writer's version can run into.
_VERSION_RACES = frozenset(
    {"uq_memory_versions_memory_id_version_number", "ix_memory_versions_one_active"}
)
_RELATION_RACES = frozenset(
    {
        "uq_memory_relations_from_version_id_to_version_id_relation_type",
        "ix_memory_relations_one_successor",
    }
)

# Reachability over the acyclic relations, from ``start`` towards older versions.
_REACHES = text(
    "WITH RECURSIVE reach(id) AS ("
    " SELECT CAST(:start AS uuid)"
    " UNION"
    " SELECT r.to_version_id FROM memory_relations r"
    " JOIN reach ON r.from_version_id = reach.id"
    " WHERE r.relation_type = ANY (CAST(:kinds AS text[]))"
    ") SELECT EXISTS (SELECT 1 FROM reach WHERE id = CAST(:target AS uuid))"
)


# Taken by every manual relation of an acyclic kind: the cycle check reads the whole
# graph, and two relations of disjoint pairs could otherwise close a cycle together.
RELATION_GRAPH_LOCK_KEY = "paw.memory-relation-graph"


def memory_lock_key(memory_id: UUID) -> str:
    """The advisory-lock key that serialises manual changes of one memory."""
    return f"paw.memory.{memory_id}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


# The columns the ACL is derived from (see "The ACL in SQL"): read before the actor
# is authorized, and nothing else is.
_SCOPE_COLUMNS = (
    _VERSIONS.c.scope,
    _VERSIONS.c.owner_user_id,
    _VERSIONS.c.project_id,
    _VERSIONS.c.project_group_id,
    _VERSIONS.c.repo_id,
)
_AUDIENCE_COLUMNS = (
    _VERSIONS.c.id,
    _VERSIONS.c.memory_id,
    _VERSIONS.c.version_number,
    *_SCOPE_COLUMNS,
)


@dataclass(frozen=True, slots=True)
class _Audience:
    """Who can read a version: what the Authorizer decides on (no content)."""

    memory_id: UUID
    scope: MemoryScope
    owner_user_id: UUID | None
    project_id: UUID | None
    project_group_id: UUID | None
    repo_id: UUID | None
    version_id: UUID | None = None
    version_number: int | None = None

    @classmethod
    def of(cls, memory_id: UUID, row: Any) -> "_Audience":
        mapping = row._mapping
        return cls(
            memory_id=memory_id,
            scope=MemoryScope(row.scope),
            owner_user_id=row.owner_user_id,
            project_id=row.project_id,
            project_group_id=row.project_group_id,
            repo_id=row.repo_id,
            version_id=mapping.get("id"),
            version_number=mapping.get("version_number"),
        )

    @property
    def audience(
        self,
    ) -> tuple[str, UUID | None, UUID | None, UUID | None, UUID | None]:
        """The same tuple as :attr:`MemoryVersionView.audience`."""
        return (
            self.scope.value,
            self.owner_user_id,
            self.project_id,
            self.project_group_id,
            self.repo_id,
        )


def _readable(actor: Principal, allowed: Iterable[_Audience]) -> ColumnElement[bool]:
    """The ACL of ``memory/acl.py`` over the audiences the Authorizer allowed.

    Only ``user`` and ``project`` audiences are ever allowed here. ``scope IN``
    those scopes narrows ``readable_memory_versions`` (whose ``shared`` scope is
    open to every principal); with nothing allowed, the condition matches no row.
    """
    audiences = list(allowed)
    principal = AclPrincipal(
        actor.user_id,
        project_ids=frozenset(
            a.project_id
            for a in audiences
            if a.scope is MemoryScope.PROJECT and a.project_id is not None
        ),
    )
    scopes = sorted({a.scope.value for a in audiences})
    return and_(
        readable_memory_versions(principal, MemoryVersion),
        MemoryVersion.scope.in_(scopes),
    )


def _view(row: Any) -> MemoryVersionView:
    return MemoryVersionView(
        memory_id=row.memory_id,
        version_id=row.id,
        version_number=row.version_number,
        scope=MemoryScope(row.scope),
        owner_user_id=row.owner_user_id,
        project_id=row.project_id,
        project_group_id=row.project_group_id,
        repo_id=row.repo_id,
        memory_type=row.memory_type,
        title=row.title,
        content=row.content,
        importance=row.importance,
        pinned=row.pinned,
        status=MemoryStatus(row.status),
        confirmation_state=ConfirmationState(row.confirmation_state),
        freshness_policy=FreshnessPolicy(row.freshness_policy),
        verified_at=row.verified_at,
        revalidate_after=row.revalidate_after,
        revalidate_triggers=tuple(row.revalidate_triggers or ()),
        expires_at=row.expires_at,
        commit_sha=row.commit_sha,
        branch=row.branch,
        stale_since=row.stale_since,
        actor_type=ActorType(row.actor_type),
        actor_user_id=row.actor_user_id,
        change_reason=row.change_reason,
        created_at=row.created_at,
    )


def _freshness_columns(spec: FreshnessSpec, now: datetime) -> dict[str, Any]:
    """The freshness columns of a new version written at ``now`` from ``spec``."""
    return {
        "freshness_policy": spec.policy.value,
        # A person wrote (or re-read) this version now: that is the verification.
        "verified_at": now if spec.policy is FreshnessPolicy.REVALIDATE else None,
        "revalidate_after": spec.revalidate_after,
        "revalidate_triggers": [trigger.value for trigger in spec.revalidate_triggers],
        "expires_at": spec.expires_at,
        "commit_sha": spec.commit_sha,
        "branch": spec.branch,
        "stale_since": None,
    }


def _kept_freshness(version: MemoryVersionView, now: datetime) -> dict[str, Any]:
    """The freshness columns of a new version that keeps ``version``'s policy.

    A ``revalidate`` memory is verified again (a person saved it now) and loses its
    stale mark; the other columns are copied as they are.
    """
    revalidate = version.freshness_policy is FreshnessPolicy.REVALIDATE
    return {
        "freshness_policy": version.freshness_policy.value,
        "verified_at": now if revalidate else version.verified_at,
        "revalidate_after": version.revalidate_after,
        "revalidate_triggers": list(version.revalidate_triggers),
        "expires_at": version.expires_at,
        "commit_sha": version.commit_sha,
        "branch": version.branch,
        "stale_since": None,
    }


def _carried_freshness(version: MemoryVersionView, now: datetime) -> dict[str, Any]:
    """``_kept_freshness`` of a version a person writes again, if still writable.

    A person never writes ``session_only`` (Decision 0034 section 4), nor an
    ``expiring`` version whose time has passed: the caller then has to give a new
    freshness (:class:`InvalidMemoryInputError` on ``freshness`` / ``expires_at``).
    """
    if version.freshness_policy is FreshnessPolicy.SESSION_ONLY:
        raise reject("freshness", InputProblem.NOT_ALLOWED)
    if version.freshness_policy is FreshnessPolicy.EXPIRING and not (
        version.expires_at is not None and version.expires_at > now
    ):
        raise reject("expires_at", InputProblem.OUT_OF_RANGE)
    return _kept_freshness(version, now)


class MemoryVersioningService:
    """Manual versioning of User and Project Memory (see the module docstring)."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        *,
        clock: Clock = _utc_now,
        lock_timeout_ms: int = limits.DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        if not isinstance(database, Database):
            raise reject("database", InputProblem.WRONG_TYPE)
        if not callable(getattr(authorizer, "authorize", None)):
            raise reject("authorizer", InputProblem.WRONG_TYPE)
        if not callable(clock):
            raise reject("clock", InputProblem.WRONG_TYPE)
        validate_int(
            "lock_timeout_ms", lock_timeout_ms, low=1, high=limits.MAX_LOCK_TIMEOUT_MS
        )
        self._database = database
        self._authorizer = authorizer
        self._clock = clock
        self._lock_timeout_ms = lock_timeout_ms

    # -- helpers ---------------------------------------------------------------

    def _now(self) -> datetime:
        return validate_aware_datetime("clock", self._clock())

    @staticmethod
    def _check_actor(actor: object) -> Principal:
        if actor is None:
            raise reject("actor", InputProblem.REQUIRED)
        if not isinstance(actor, Principal):
            raise reject("actor", InputProblem.WRONG_TYPE)
        return actor

    @asynccontextmanager
    async def _transaction(
        self,
        *memory_ids: UUID,
        graph: bool = False,
        race: tuple[UUID, int] | None = None,
        relation_race: bool = False,
    ) -> AsyncIterator[AsyncSession]:
        """One transaction with the lock timeout and an advisory lock per memory.

        ``graph`` also takes ``RELATION_GRAPH_LOCK_KEY``. All keys are taken in one
        sorted order, so two transactions never wait for each other in a circle.

        A database error never leaves as it is (``errors`` module text): it becomes
        :class:`MemoryBusyError` (a lock timed out), :class:`MemoryVersionConflictError`
        (``race`` = the memory and the expected version, when another writer's
        version took the number), ``ALREADY_RELATED`` (``relation_race``, when
        another writer recorded the relation) or :class:`MemoryDatabaseError`,
        detached from the original.
        """
        keys = {memory_lock_key(m) for m in memory_ids}
        if graph:
            keys.add(RELATION_GRAPH_LOCK_KEY)
        failure: MemoryVersioningError | None = None
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(
                    select(
                        func.set_config(
                            "lock_timeout", str(self._lock_timeout_ms), True
                        )
                    )
                )
                for key in sorted(keys):
                    await session.execute(_LOCK_SQL, {"key": key})
                yield session
        except StatementError as error:
            failure = await self._translated(error, race, relation_race)
        if failure is not None:
            # Raised outside the handler: nothing links it to the driver's error.
            raise_detached(failure)

    async def _translated(
        self,
        error: StatementError,
        race: tuple[UUID, int] | None,
        relation_race: bool,
    ) -> MemoryVersioningError:
        """The typed error for a database error. Only its type, SQLSTATE and
        constraint name are read, never its text."""
        orig = error.orig if isinstance(error, DBAPIError) else None
        if isinstance(orig, psycopg.errors.LockNotAvailable):
            return MemoryBusyError()
        constraint = _constraint(error) if isinstance(error, IntegrityError) else None
        if race is not None and constraint in _VERSION_RACES:
            memory_id, expected = race
            # The other writer's version is committed: a new session sees it and
            # the conflict names the current number.
            try:
                async with self._database.session() as session:
                    current = await self._current_audience(
                        session, memory_id, lock=False
                    )
            except (StatementError, MemoryNotFoundError):
                return MemoryDatabaseError(_sqlstate(orig))
            assert current.version_number is not None  # _AUDIENCE_COLUMNS has it
            return MemoryVersionConflictError(expected, current.version_number)
        if relation_race and constraint in _RELATION_RACES:
            return MemoryStateError(StateProblem.ALREADY_RELATED)
        return MemoryDatabaseError(_sqlstate(orig))

    @staticmethod
    async def _current_audience(
        session: AsyncSession, memory_id: UUID, *, lock: bool = True
    ) -> _Audience:
        """The current (highest numbered) version's audience, locked ``FOR UPDATE``.

        Only ``_AUDIENCE_COLUMNS`` (see "The ACL in SQL"): the actor is not
        authorized yet.
        """
        statement = (
            select(*_AUDIENCE_COLUMNS)
            .where(_VERSIONS.c.memory_id == memory_id)
            .order_by(_VERSIONS.c.version_number.desc())
            .limit(1)
        )
        if lock:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).first()
        if row is None:
            raise MemoryNotFoundError
        return _Audience.of(memory_id, row)

    @staticmethod
    async def _version_audience(
        session: AsyncSession, memory_id: UUID, number: int
    ) -> _Audience | None:
        """Version ``number``'s audience only (``_SCOPE_COLUMNS``)."""
        row = (
            await session.execute(
                select(*_SCOPE_COLUMNS).where(
                    _VERSIONS.c.memory_id == memory_id,
                    _VERSIONS.c.version_number == number,
                )
            )
        ).first()
        return None if row is None else _Audience.of(memory_id, row)

    @staticmethod
    async def _load(
        session: AsyncSession,
        actor: Principal,
        allowed: _Audience,
        memory_id: UUID,
        number: int,
    ) -> MemoryVersionView:
        """Version ``number`` with its content, read through the ACL in SQL.

        ``allowed`` is an audience the Authorizer allowed; a version of another
        audience is not returned (:class:`MemoryNotFoundError`).
        """
        row = (
            await session.execute(
                select(*_VERSIONS.c).where(
                    _VERSIONS.c.memory_id == memory_id,
                    _VERSIONS.c.version_number == number,
                    _readable(actor, (allowed,)),
                )
            )
        ).first()
        if row is None:
            raise MemoryNotFoundError
        return _view(row)

    async def _current(
        self,
        session: AsyncSession,
        actor: Principal,
        memory_id: UUID,
        *,
        write: bool,
        lock: bool = True,
    ) -> MemoryVersionView:
        """The current version, authorized, then read through the ACL in SQL."""
        audience = await self._current_audience(session, memory_id, lock=lock)
        await self._authorize_version(session, actor, audience, write=write)
        return await self._load_current(session, actor, audience)

    async def _load_current(
        self, session: AsyncSession, actor: Principal, audience: _Audience
    ) -> MemoryVersionView:
        """``_load`` of the version whose audience ``_current_audience`` read."""
        assert audience.version_number is not None  # _AUDIENCE_COLUMNS has it
        return await self._load(
            session, actor, audience, audience.memory_id, audience.version_number
        )

    async def _project_member(
        self, session: AsyncSession, actor: Principal, project_id: UUID
    ) -> tuple[Principal, ProjectState] | None:
        """The actor with its role READ FROM THE DATABASE, and the project's state.

        ``None`` when the project does not exist or is deleted. An invitation is
        not a membership (the principal then has no role in the project).
        """
        row = (
            await session.execute(
                select(_PROJECTS.c.status, _MEMBERS.c.role)
                .select_from(
                    _PROJECTS.outerjoin(
                        _MEMBERS,
                        (_MEMBERS.c.project_id == _PROJECTS.c.id)
                        & (_MEMBERS.c.user_id == actor.user_id)
                        & (_MEMBERS.c.status == "active"),
                    )
                )
                .where(_PROJECTS.c.id == project_id)
            )
        ).first()
        if row is None:
            return None
        try:
            state = ProjectState(row.status)
        except ValueError:
            return None  # ``deleted``: gone for everybody
        roles = {} if row.role is None else {project_id: ProjectRole(row.role)}
        return Principal(actor.user_id, actor.system_role, roles), state

    async def _decide(
        self, principal: Principal, capability: Capability, resource: Resource
    ) -> Decision:
        decision = await self._authorizer.authorize(principal, capability, resource)
        if not isinstance(decision, Decision):
            raise MemoryPermissionError("invalid_decision")
        return decision

    async def _authorize_version(
        self,
        session: AsyncSession,
        actor: Principal,
        version: _Audience,
        *,
        write: bool,
    ) -> None:
        """May ``actor`` change (or, ``write=False``, read) this audience?"""
        if version.scope is MemoryScope.SHARED:
            raise MemoryScopeNotSupportedError
        principal = actor
        if version.scope is MemoryScope.USER:
            assert version.owner_user_id is not None  # the scope CHECK
            decision = await self._decide(
                actor,
                Capability.MEMORY_USE,
                Resource.owned_by(
                    version.owner_user_id, RESOURCE_MEMORY, version.memory_id
                ),
            )
        elif version.scope is MemoryScope.PROJECT:
            assert version.project_id is not None  # the scope CHECK
            member = await self._project_member(session, actor, version.project_id)
            if member is None:
                raise MemoryNotFoundError
            principal, state = member
            decision = await self._decide(
                principal,
                Capability.PROJECT_MEMORY_USE if write else Capability.PROJECT_READ,
                Resource.project(version.project_id, state),
            )
        else:
            raise MemoryNotFoundError  # repo / project_group: not handled here
        if decision.allowed:
            return
        if decision.reason in _HIDDEN_DENIALS or (
            version.scope is MemoryScope.PROJECT
            and version.project_id not in principal.project_roles
        ):
            # The Authorizer checks the project state before the membership, so a
            # non-member of an archived project would otherwise learn from
            # ``project_state_forbids`` that the memory exists.
            raise MemoryNotFoundError
        raise MemoryPermissionError(decision.reason.value)

    async def _authorize_narrowing(self, actor: Principal) -> None:
        """May ``actor`` hold the narrowed memory as its own (``memory.use``)?"""
        decision = await self._decide(
            actor,
            Capability.MEMORY_USE,
            Resource.owned_by(actor.user_id, RESOURCE_MEMORY),
        )
        if not decision.allowed:
            raise MemoryPermissionError(decision.reason.value)

    @staticmethod
    def _check_expected(current: MemoryVersionView, expected: int) -> None:
        if current.version_number != expected:
            raise MemoryVersionConflictError(expected, current.version_number)

    @staticmethod
    async def _set_status(
        session: AsyncSession,
        version: MemoryVersionView,
        new: MemoryStatus,
        actor: Principal,
    ) -> None:
        """``active`` -> ``new`` of one version (row locked by ``_current``)."""
        await session.execute(metadata_change_actor(ActorType.USER, actor.user_id))
        result = await session.execute(
            update(_VERSIONS)
            .where(
                _VERSIONS.c.id == version.version_id,
                _VERSIONS.c.status == version.status.value,
            )
            .values(status=new.value)
        )
        if result.rowcount != 1:
            raise MemoryVersionConflictError(
                version.version_number, version.version_number
            )

    @staticmethod
    async def _relate(
        session: AsyncSession,
        newer: UUID,
        older: UUID,
        kind: RelationType,
        reason: str | None,
        now: datetime,
    ) -> MemoryRelationView:
        await session.execute(
            insert(_RELATIONS).values(
                from_version_id=newer,
                to_version_id=older,
                relation_type=kind.value,
                reason=reason,
                created_at=now,
            )
        )
        return MemoryRelationView(newer, older, kind, reason)

    @staticmethod
    async def _insert_version(
        session: AsyncSession,
        base: MemoryVersionView,
        *,
        values: dict[str, Any],
        actor: Principal,
        now: datetime,
    ) -> MemoryVersionView:
        """Insert version ``base.version_number + 1`` with ``base``'s scope."""
        row = {
            "memory_id": base.memory_id,
            "version_number": base.version_number + 1,
            "scope": base.scope.value,
            "owner_user_id": base.owner_user_id,
            "project_id": base.project_id,
            "project_group_id": base.project_group_id,
            "repo_id": base.repo_id,
            "memory_type": base.memory_type,
            "title": base.title,
            "content": base.content,
            "importance": base.importance,
            "pinned": base.pinned,
            "status": MemoryStatus.ACTIVE.value,
            # A person saved it (REQUIREMENTS.md "Manual Memory Editing").
            "confirmation_state": ConfirmationState.CONFIRMED.value,
            "attributes": {},
            "actor_type": ActorType.USER.value,
            "actor_user_id": actor.user_id,
            "change_reason": None,
            "created_at": now,
        }
        row.update(values)
        inserted = (
            await session.execute(
                insert(_VERSIONS).values(**row).returning(*_VERSIONS.c)
            )
        ).first()
        assert inserted is not None
        return _view(inserted)

    # -- read ------------------------------------------------------------------

    async def history(
        self, actor: Principal, memory_id: UUID
    ) -> tuple[MemoryVersionView, ...]:
        """Every version of a memory, oldest first (the History Graph's nodes).

        Readable by whoever may read the memory's current version: its owner
        (``memory.use``) or a member of its project (``project.read``). A version
        with another audience than the current one (a memory widened by a later
        flow, e.g. PAW-044) is returned only if the actor may read that audience
        too, so widening never shows the earlier private content to the new
        readers.
        """
        actor = self._check_actor(actor)
        memory_id = validate_uuid("memory_id", memory_id)
        async with self._transaction() as session:
            current = await self._current_audience(session, memory_id, lock=False)
            await self._authorize_version(session, actor, current, write=False)
            # The other audiences of the memory: scope columns only (no content).
            others = await session.execute(
                select(*_SCOPE_COLUMNS)
                .where(_VERSIONS.c.memory_id == memory_id)
                .distinct()
            )
            allowed = [current]
            for row in others:
                audience = _Audience.of(memory_id, row)
                if audience.audience == current.audience:
                    continue
                try:
                    await self._authorize_version(session, actor, audience, write=False)
                except (
                    MemoryNotFoundError,
                    MemoryPermissionError,
                    MemoryScopeNotSupportedError,
                ):
                    continue
                allowed.append(audience)
            rows = await session.execute(
                select(*_VERSIONS.c)
                .where(_VERSIONS.c.memory_id == memory_id, _readable(actor, allowed))
                .order_by(_VERSIONS.c.version_number)
            )
            return tuple(_view(row) for row in rows)

    # -- create ----------------------------------------------------------------

    async def create_memory(
        self, actor: Principal, draft: MemoryDraft
    ) -> MemoryVersionView:
        """A new memory written by a person: version 1, ``active``, ``confirmed``."""
        actor = self._check_actor(actor)
        if not isinstance(draft, MemoryDraft):
            raise reject("draft", InputProblem.WRONG_TYPE)
        now = self._now()
        rules.check_manual_freshness(draft.freshness, draft.scope, now)
        async with self._transaction() as session:
            if draft.scope is MemoryScope.USER:
                decision = await self._decide(
                    actor,
                    Capability.MEMORY_USE,
                    Resource.owned_by(actor.user_id, RESOURCE_MEMORY),
                )
            else:
                assert draft.project_id is not None  # MemoryDraft requires it
                member = await self._project_member(session, actor, draft.project_id)
                if member is None:
                    raise MemoryPermissionError(Reason.NOT_PROJECT_MEMBER.value)
                principal, state = member
                decision = await self._decide(
                    principal,
                    Capability.PROJECT_MEMORY_USE,
                    Resource.project(draft.project_id, state),
                )
            if not decision.allowed:
                raise MemoryPermissionError(decision.reason.value)
            memory_id = (
                await session.execute(
                    insert(_MEMORIES).values(created_at=now).returning(_MEMORIES.c.id)
                )
            ).scalar_one()
            row = {
                "memory_id": memory_id,
                "version_number": 1,
                "scope": draft.scope.value,
                "owner_user_id": actor.user_id
                if draft.scope is MemoryScope.USER
                else None,
                "project_id": draft.project_id,
                "memory_type": draft.memory_type,
                "title": draft.title,
                "content": draft.content,
                "importance": draft.importance,
                "status": MemoryStatus.ACTIVE.value,
                "confirmation_state": ConfirmationState.CONFIRMED.value,
                "attributes": {},
                "actor_type": ActorType.USER.value,
                "actor_user_id": actor.user_id,
                "change_reason": draft.reason,
                "created_at": now,
                **_freshness_columns(draft.freshness, now),
            }
            inserted = (
                await session.execute(
                    insert(_VERSIONS).values(**row).returning(*_VERSIONS.c)
                )
            ).first()
            assert inserted is not None
            return _view(inserted)

    # -- edit / restore / retire / revalidate ------------------------------------

    async def edit_memory(
        self,
        actor: Principal,
        memory_id: UUID,
        expected_version: int,
        changes: MemoryChanges,
    ) -> MemoryVersionView:
        """A new version with ``changes``; the current one becomes ``superseded``.

        ``changes.scope`` = ``user`` on a ``project`` memory narrows it to the
        actor's own private memory (REQUIREMENTS.md "Scope変更": at once). The
        project version is retired in the same transaction, so no moment exists in
        which both are active or neither is. Narrowing needs the right to change the
        project memory (``project.memory.use``, as any edit) and ``memory.use`` for
        the actor's own memory (as ``create_memory``). Any other change of scope
        widens the audience and is ``NOT_ALLOWED`` (the confirmation flow, PAW-044).
        """
        actor = self._check_actor(actor)
        memory_id = validate_uuid("memory_id", memory_id)
        expected_version = validate_version_number("expected_version", expected_version)
        if not isinstance(changes, MemoryChanges):
            raise reject("changes", InputProblem.WRONG_TYPE)
        now = self._now()
        async with self._transaction(
            memory_id, race=(memory_id, expected_version)
        ) as session:
            current = await self._current(session, actor, memory_id, write=True)
            self._check_expected(current, expected_version)
            rules.check_active(current)
            changed = rules.changed_fields(current, changes)
            if not changed:
                return current
            values: dict[str, Any] = {
                name: getattr(changes, name)
                for name in ("title", "content", "memory_type", "importance")
                if name in changed
            }
            scope = current.scope
            if changes.scope is not None and "scope" in changed:
                rules.check_narrowing(current.scope, changes.scope)
                await self._authorize_narrowing(actor)
                scope = changes.scope
                values.update(
                    scope=scope.value,
                    owner_user_id=actor.user_id,
                    project_id=None,
                    project_group_id=None,
                    repo_id=None,
                )
            if changes.freshness is not None and "freshness" in changed:
                rules.check_manual_freshness(changes.freshness, scope, now)
                values.update(_freshness_columns(changes.freshness, now))
            else:
                values.update(_carried_freshness(current, now))
            values["change_reason"] = changes.reason
            values["attributes"] = {"edited_from_version": current.version_number}
            await self._set_status(session, current, MemoryStatus.SUPERSEDED, actor)
            new = await self._insert_version(
                session, current, values=values, actor=actor, now=now
            )
            reason = ", ".join(changed)
            await self._relate(
                session,
                new.version_id,
                current.version_id,
                RelationType.SUPERSEDES,
                reason,
                now,
            )
            for kind in rules.confirmation_after_edit(current):
                await self._relate(
                    session, new.version_id, current.version_id, kind, reason, now
                )
            return new

    async def restore_version(
        self,
        actor: Principal,
        memory_id: UUID,
        expected_version: int,
        source_version: int,
        *,
        freshness: FreshnessSpec | None = None,
        reason: str | None = None,
    ) -> MemoryVersionView:
        """A new active version with the content of ``source_version``.

        ``freshness`` replaces the source's freshness (needed when the source was
        an ``expiring`` memory whose time has passed: an expiry that is not ahead
        is refused). The current version is retired: ``superseded`` if it was
        ``active``; a ``deprecated`` one stays ``deprecated``.
        """
        actor = self._check_actor(actor)
        memory_id = validate_uuid("memory_id", memory_id)
        expected_version = validate_version_number("expected_version", expected_version)
        source_version = validate_version_number("source_version", source_version)
        if freshness is not None and not isinstance(freshness, FreshnessSpec):
            raise reject("freshness", InputProblem.WRONG_TYPE)
        reason = validate_optional_text(
            "reason", reason, max_chars=limits.MAX_REASON_CHARS
        )
        now = self._now()
        async with self._transaction(
            memory_id, race=(memory_id, expected_version)
        ) as session:
            current = await self._current(session, actor, memory_id, write=True)
            self._check_expected(current, expected_version)
            audience = await self._version_audience(session, memory_id, source_version)
            if audience is None:
                raise MemoryStateError(StateProblem.UNKNOWN_VERSION)
            if audience.audience != current.audience:
                # A restore never changes who can read the memory. Decided on
                # the audience alone: the source's content is not read.
                raise MemoryStateError(StateProblem.SCOPE_MISMATCH)
            source = await self._load(
                session, actor, audience, memory_id, source_version
            )
            rules.check_restorable(current, source)
            if freshness is not None:
                rules.check_manual_freshness(freshness, current.scope, now)
                kept = _freshness_columns(freshness, now)
            else:
                kept = _carried_freshness(source, now)
            values = {
                "memory_type": source.memory_type,
                "title": source.title,
                "content": source.content,
                "importance": source.importance,
                "change_reason": reason,
                "attributes": {"restored_from_version": source.version_number},
                **kept,
            }
            if current.status is MemoryStatus.ACTIVE:
                await self._set_status(session, current, MemoryStatus.SUPERSEDED, actor)
            new = await self._insert_version(
                session, current, values=values, actor=actor, now=now
            )
            await self._relate(
                session,
                new.version_id,
                current.version_id,
                RelationType.SUPERSEDES,
                "restore",
                now,
            )
            return new

    async def deprecate_memory(
        self, actor: Principal, memory_id: UUID, expected_version: int
    ) -> MemoryVersionView:
        """The current version becomes ``deprecated``. Nothing is erased."""
        actor = self._check_actor(actor)
        memory_id = validate_uuid("memory_id", memory_id)
        expected_version = validate_version_number("expected_version", expected_version)
        async with self._transaction(memory_id) as session:
            current = await self._current(session, actor, memory_id, write=True)
            self._check_expected(current, expected_version)
            rules.check_active(current)
            await self._set_status(session, current, MemoryStatus.DEPRECATED, actor)
            return replace(current, status=MemoryStatus.DEPRECATED)

    async def revalidate_memory(
        self,
        actor: Principal,
        memory_id: UUID,
        expected_version: int,
        *,
        reason: str | None = None,
    ) -> MemoryVersionView:
        """ "Still true": a new version verified now, without the stale mark.

        Only a ``revalidate`` memory (MEMORY_ARCHITECTURE.md section 11: a stale
        candidate is re-checked when it is needed). A memory that is no longer true
        is edited (``edit_memory``) or retired (``deprecate_memory``) instead.
        """
        actor = self._check_actor(actor)
        memory_id = validate_uuid("memory_id", memory_id)
        expected_version = validate_version_number("expected_version", expected_version)
        reason = validate_optional_text(
            "reason", reason, max_chars=limits.MAX_REASON_CHARS
        )
        now = self._now()
        async with self._transaction(
            memory_id, race=(memory_id, expected_version)
        ) as session:
            current = await self._current(session, actor, memory_id, write=True)
            self._check_expected(current, expected_version)
            rules.check_revalidatable(current)
            values = {
                **_kept_freshness(current, now),
                "change_reason": reason,
                "attributes": {"revalidated_from_version": current.version_number},
            }
            await self._set_status(session, current, MemoryStatus.SUPERSEDED, actor)
            new = await self._insert_version(
                session, current, values=values, actor=actor, now=now
            )
            for kind in (RelationType.SUPERSEDES, RelationType.REVALIDATED_FROM):
                await self._relate(
                    session,
                    new.version_id,
                    current.version_id,
                    kind,
                    "revalidate",
                    now,
                )
            return new

    # -- relations -------------------------------------------------------------

    async def relate_memories(
        self,
        actor: Principal,
        relation: ManualRelation,
        *,
        newer_memory_id: UUID,
        newer_expected_version: int,
        older_memory_id: UUID,
        older_expected_version: int,
        reason: str | None = None,
    ) -> MemoryRelationView:
        """Record ``relation`` from the newer memory's current version to the older's.

        Both memories must be changeable by the actor and ``active``, and both
        expected versions current. ``supersedes`` retires the older memory and
        needs the same audience on both sides; ``extends`` and ``conflicts_with``
        keep both active. A relation that would make the history graph cyclic, or
        that exists already (for ``conflicts_with``: in either direction), is
        refused.
        """
        actor = self._check_actor(actor)
        relation = validate_enum("relation", relation, ManualRelation)
        newer_id = validate_uuid("newer_memory_id", newer_memory_id)
        older_id = validate_uuid("older_memory_id", older_memory_id)
        newer_expected = validate_version_number(
            "newer_expected_version", newer_expected_version
        )
        older_expected = validate_version_number(
            "older_expected_version", older_expected_version
        )
        reason = validate_optional_text(
            "reason", reason, max_chars=limits.MAX_REASON_CHARS
        )
        if newer_id == older_id:
            raise MemoryStateError(StateProblem.SAME_MEMORY)
        now = self._now()
        kind = relation.relation_type
        async with self._transaction(
            newer_id,
            older_id,
            graph=kind in rules.ACYCLIC_RELATIONS,
            relation_race=True,
        ) as session:
            # Rows are locked in the order of the ids, like the advisory locks.
            audiences: dict[UUID, _Audience] = {}
            for memory_id in sorted((newer_id, older_id)):
                audiences[memory_id] = await self._current_audience(session, memory_id)
            for memory_id in (newer_id, older_id):
                await self._authorize_version(
                    session, actor, audiences[memory_id], write=True
                )
            newer = await self._load_current(session, actor, audiences[newer_id])
            older = await self._load_current(session, actor, audiences[older_id])
            self._check_expected(newer, newer_expected)
            self._check_expected(older, older_expected)
            rules.check_relatable(relation, newer, older)
            await self._check_graph(session, kind, newer, older)
            if relation is ManualRelation.SUPERSEDES:
                await self._set_status(session, older, MemoryStatus.SUPERSEDED, actor)
            return await self._relate(
                session, newer.version_id, older.version_id, kind, reason, now
            )

    @staticmethod
    async def _check_graph(
        session: AsyncSession,
        kind: RelationType,
        newer: MemoryVersionView,
        older: MemoryVersionView,
    ) -> None:
        existing = await session.execute(
            select(_RELATIONS.c.from_version_id, _RELATIONS.c.relation_type).where(
                _RELATIONS.c.from_version_id.in_((newer.version_id, older.version_id)),
                _RELATIONS.c.to_version_id.in_((newer.version_id, older.version_id)),
            )
        )
        for row in existing:
            same_direction = row.from_version_id == newer.version_id
            if row.relation_type == kind.value and (
                same_direction or kind is RelationType.CONFLICTS_WITH
            ):
                raise MemoryStateError(StateProblem.ALREADY_RELATED)
        if kind in rules.ACYCLIC_RELATIONS:
            cyclic = (
                await session.execute(
                    _REACHES,
                    {
                        "start": older.version_id,
                        "target": newer.version_id,
                        "kinds": sorted(k.value for k in rules.ACYCLIC_RELATIONS),
                    },
                )
            ).scalar_one()
            if cyclic:
                raise MemoryStateError(StateProblem.WOULD_CYCLE)


def _constraint(error: IntegrityError) -> str | None:
    """The name of the constraint a driver error names (never its message)."""
    diag = getattr(error.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _sqlstate(orig: object) -> str | None:
    sqlstate = getattr(orig, "sqlstate", None)
    return sqlstate if isinstance(sqlstate, str) else None
