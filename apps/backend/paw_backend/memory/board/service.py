"""The Memory Board read model (issue #186): what the Memory screen reads.

The Memory screen (PAW-063, PR #178) shows a Scope Tree with counts, the current
version of each memory of a scope, the History Graph of one memory and the sources
of one version. ``MemoryVersioningService`` writes the versions and reads one
memory's versions (``history``) for a person who may change it; this module is the
read side the screen needs on top of that. It changes nothing. The choices it
makes (what is listed, counted and named) are Decision 0068 (Proposed).

What the reader may see
-----------------------
Decided per call, in the backend, before any memory is read
(MEMORY_ARCHITECTURE.md section 12), as the Hybrid Retrieval decides it
(``retrieval/resolver.py``; Decisions 0019 and 0024):

* ``user``: ``memory.read`` on a resource the reader owns (``DENIED_ONLY``: an
  allowed read writes no audit row).
* ``project``: the reader's accepted memberships READ FROM THE DATABASE (never the
  roles a ``Principal`` carries), each project decided with ``project.read`` on its
  stored state. Pending deletion and Deleted projects are not asked about.
* ``repo``: the registered repositories of those projects, each decided with
  ``project.read`` on the repository resource with its stored ACL (an override
  without ``read`` hides it).
* ``shared``: ``shared_memory.read``. Only a shared memory whose current version is
  ``active``: a deleted one (``deprecated``) is for the Owner and Admin, through
  ``SharedMemoryService`` (``include_deleted``).
* ``project_group``: nothing (what a group is is not defined yet, ``acl.py``).

A denial makes a scope contribute nothing; nothing tells the reader which scopes
exist but are hidden. ``audit_unavailable`` is the one denial that is an error.

The SQL (``memory/acl.py``)
---------------------------
Every read of ``memory_versions`` (and of ``memory_relations`` and
``memory_sources`` through it) carries :func:`readable_memory_versions` built from
those grants, so a row the reader may not see never reaches the backend:

* a memory is **listed** when its current version (the highest number) is
  readable; the scope of a memory is the one of its current version;
* a version is **visible** when it is readable itself and its memory is listed
  (the rule of ``MemoryVersioningService.history``: an older version with another
  audience is shown only to a reader of that audience too, and a memory narrowed
  to someone's private memory disappears for the project with all its versions);
* an edge of the History Graph is returned when both of its ends are visible.

``can_write`` is a hint for the screen, decided with the pure policy
(``authz.policy.decide``, no audit row) on the stored role and project state; the
write itself is authorized (and audited) by ``MemoryVersioningService``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ColumnElement,
    and_,
    exists,
    false,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from paw_backend.authz import (
    Authorizer,
    Capability,
    Decision,
    Policy,
    Principal,
    ProjectRole,
    ProjectState,
    Reason,
    RepoAcl,
    RepoPermission,
    Resource,
    SystemRole,
)
from paw_backend.authz.policy import decide
from paw_backend.db import Database
from paw_backend.identity.models import UserRow, UserStatus
from paw_backend.memory.acl import Principal as AclPrincipal
from paw_backend.memory.acl import readable_memory_versions
from paw_backend.memory.board import limits
from paw_backend.memory.board.records import (
    BoardHistory,
    BoardRelation,
    BoardScope,
    BoardScopeKind,
    BoardSource,
    BoardVersion,
    MemoryList,
    ProjectScopeCount,
    RepoScopeCount,
    ScopeTree,
)
from paw_backend.memory.models import (
    ActorType,
    MemoryRelation,
    MemoryScope,
    MemorySource,
    MemoryStatus,
    MemoryVersion,
    RelationType,
    SourceType,
)
from paw_backend.memory.versioning.errors import (
    InputProblem,
    MemoryDatabaseError,
    MemoryNotFoundError,
    MemoryPermissionError,
    raise_detached,
)
from paw_backend.memory.versioning.records import MemoryVersionView
from paw_backend.memory.versioning.service import RESOURCE_MEMORY, version_view
from paw_backend.memory.versioning.validation import (
    reject,
    validate_enum,
    validate_text,
    validate_uuid,
    validate_version_number,
)
from paw_backend.projects.models import ProjectMemberRow, ProjectRow
from paw_backend.repositories.models import RepositoryRow

RESOURCE_SHARED_MEMORY = "shared_memory"

# Projects whose memories can be read (``memberships_statement`` of the retrieval).
_READABLE_PROJECT_STATES = (ProjectState.ACTIVE.value, ProjectState.ARCHIVED.value)
# The system roles of a person; the backend's own identity reads nothing here.
_HUMAN_ROLES = frozenset({SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER})


@dataclass(frozen=True, slots=True)
class _Project:
    name: str
    state: ProjectState
    role: ProjectRole


@dataclass(frozen=True, slots=True)
class _Repo:
    project_id: UUID
    name: str


@dataclass(slots=True)
class _Grants:
    """What the reader may see (see "What the reader may see")."""

    principal: Principal
    user: bool = False
    shared: bool = False
    projects: dict[UUID, _Project] = field(default_factory=dict)
    repos: dict[UUID, _Repo] = field(default_factory=dict)

    def readable(self, version: Any) -> ColumnElement[bool]:
        """``readable_memory_versions`` over these grants, of ``version`` (an alias)."""
        scopes = []
        if self.user:
            scopes.append(MemoryScope.USER.value)
        if self.projects:
            scopes.append(MemoryScope.PROJECT.value)
        if self.repos:
            scopes.append(MemoryScope.REPO.value)
        if self.shared:
            scopes.append(MemoryScope.SHARED.value)
        if not scopes:
            return false()
        principal = AclPrincipal(
            self.principal.user_id,
            project_ids=frozenset(self.projects),
            repo_ids=frozenset(self.repos),
        )
        return and_(
            readable_memory_versions(principal, version), version.scope.in_(scopes)
        )

    def listed(self, version: Any) -> ColumnElement[bool]:
        """``version`` is the current version of a memory the reader may see."""
        newer = aliased(MemoryVersion)
        return and_(
            ~exists().where(
                newer.memory_id == version.memory_id,
                newer.version_number > version.version_number,
            ),
            self.readable(version),
            or_(
                version.scope != MemoryScope.SHARED.value,
                version.status == MemoryStatus.ACTIVE.value,
            ),
        )

    def visible(self, version: Any) -> ColumnElement[bool]:
        """``version`` is readable and its memory is listed."""
        current = aliased(MemoryVersion)
        return and_(
            self.readable(version),
            exists().where(
                current.memory_id == version.memory_id, self.listed(current)
            ),
        )


def _check_actor(actor: object) -> Principal:
    if actor is None:
        raise reject("actor", InputProblem.REQUIRED)
    if not isinstance(actor, Principal):
        raise reject("actor", InputProblem.WRONG_TYPE)
    if actor.system_role not in _HUMAN_ROLES:
        raise MemoryPermissionError(Reason.CAPABILITY_NOT_GRANTED.value)
    return actor


def _like(query: str) -> str:
    """``query`` as a substring pattern of ``ILIKE ... ESCAPE '\\'``."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class MemoryBoard:
    """The read side of the Memory screen (see the module docstring)."""

    def __init__(self, database: Database, authorizer: Authorizer) -> None:
        if not isinstance(database, Database):
            raise reject("database", InputProblem.WRONG_TYPE)
        if not callable(getattr(authorizer, "authorize", None)):
            raise reject("authorizer", InputProblem.WRONG_TYPE)
        # ``can_write`` is decided again without audit; it must use the same
        # grants as the injected Authorizer, not ``DEFAULT_POLICY``.
        policy = getattr(authorizer, "policy", None)
        if not isinstance(policy, Policy):
            raise reject("authorizer", InputProblem.WRONG_TYPE)
        self._database = database
        self._authorizer = authorizer
        self._policy = policy

    # -- helpers ---------------------------------------------------------------

    async def _run[T](self, work: Any) -> T:
        """``work(session)`` in one read-only transaction; a database error is
        ``MemoryDatabaseError`` (detached from the driver's, which carries the
        bound parameters)."""
        failure: MemoryDatabaseError | None = None
        try:
            async with self._database.session() as session, session.begin():
                await session.execute(text("SET TRANSACTION READ ONLY"))
                return await work(session)
        except StatementError as error:
            sqlstate = getattr(getattr(error, "orig", None), "sqlstate", None)
            failure = MemoryDatabaseError(
                sqlstate if isinstance(sqlstate, str) else None
            )
        raise_detached(failure)

    async def _allowed(
        self, principal: Principal, capability: Capability, resource: Resource
    ) -> bool:
        decision = await self._authorizer.authorize(principal, capability, resource)
        if not isinstance(decision, Decision):
            raise MemoryPermissionError("invalid_decision")
        if decision.allowed:
            return True
        if decision.reason is Reason.AUDIT_UNAVAILABLE:
            raise MemoryPermissionError(decision.reason.value)
        return False

    async def _grants(self, session: AsyncSession, actor: Principal) -> _Grants:
        grants = _Grants(actor)
        grants.user = await self._allowed(
            actor,
            Capability.MEMORY_READ,
            Resource.owned_by(actor.user_id, RESOURCE_MEMORY),
        )
        grants.shared = await self._allowed(
            actor, Capability.SHARED_MEMORY_READ, Resource(kind=RESOURCE_SHARED_MEMORY)
        )
        memberships = await session.execute(
            select(
                ProjectRow.id, ProjectRow.name, ProjectRow.status, ProjectMemberRow.role
            )
            .join(ProjectMemberRow, ProjectMemberRow.project_id == ProjectRow.id)
            .where(
                ProjectMemberRow.user_id == actor.user_id,
                ProjectMemberRow.status == "active",
                ProjectRow.status.in_(_READABLE_PROJECT_STATES),
            )
            .order_by(ProjectRow.id)
        )
        roles: dict[UUID, ProjectRole] = {}
        candidates: list[tuple[UUID, _Project]] = []
        for row in memberships:
            project = _Project(
                row.name, ProjectState(row.status), ProjectRole(row.role)
            )
            roles[row.id] = project.role
            candidates.append((row.id, project))
        # Decided with the roles read from the database, never the caller's.
        member = Principal(actor.user_id, actor.system_role, roles)
        for project_id, project in candidates:
            if await self._allowed(
                member,
                Capability.PROJECT_READ,
                Resource.project(project_id, project.state),
            ):
                grants.projects[project_id] = project
        if grants.projects:
            repositories = await session.execute(
                select(
                    RepositoryRow.id,
                    RepositoryRow.project_id,
                    RepositoryRow.name,
                    RepositoryRow.acl_allowed,
                )
                .where(RepositoryRow.project_id.in_(sorted(grants.projects)))
                .order_by(RepositoryRow.id)
            )
            for row in repositories:
                acl = (
                    RepoAcl.inherit(row.id, row.project_id)
                    if row.acl_allowed is None
                    else RepoAcl.override(
                        row.id,
                        row.project_id,
                        (RepoPermission(name) for name in row.acl_allowed),
                    )
                )
                state = grants.projects[row.project_id].state
                if await self._allowed(
                    member,
                    Capability.PROJECT_READ,
                    Resource.repository(row.project_id, state, acl),
                ):
                    grants.repos[row.id] = _Repo(row.project_id, row.name)
        return grants

    @staticmethod
    async def _names(
        session: AsyncSession, versions: Iterable[MemoryVersionView]
    ) -> dict[UUID, str]:
        """The login names of the people who wrote ``versions`` (Decision 0068).

        A deleted account has no name here (the screen says "another user").
        """
        ids = sorted(
            {
                version.actor_user_id
                for version in versions
                if version.actor_type is ActorType.USER
                and version.actor_user_id is not None
            }
        )
        if not ids:
            return {}
        rows = await session.execute(
            select(UserRow.id, UserRow.login_name).where(
                UserRow.id.in_(ids), UserRow.status != UserStatus.DELETED.value
            )
        )
        return {row.id: row.login_name for row in rows}

    @classmethod
    async def _named(
        cls, session: AsyncSession, versions: Sequence[MemoryVersionView]
    ) -> tuple[BoardVersion, ...]:
        names = await cls._names(session, versions)
        return tuple(
            BoardVersion(
                version,
                names.get(version.actor_user_id)
                if version.actor_type is ActorType.USER
                and version.actor_user_id is not None
                else None,
            )
            for version in versions
        )

    # -- the Scope pane -----------------------------------------------------------

    async def scopes(self, actor: Principal) -> ScopeTree:
        """The scopes the reader may see, with the number of listed memories."""
        actor = _check_actor(actor)

        async def work(session: AsyncSession) -> ScopeTree:
            grants = await self._grants(session, actor)
            version = aliased(MemoryVersion)
            rows = await session.execute(
                select(
                    version.scope,
                    version.project_id,
                    version.repo_id,
                    func.count().label("n"),
                )
                .where(grants.listed(version))
                .group_by(version.scope, version.project_id, version.repo_id)
            )
            user = shared = 0
            project_counts: dict[UUID, int] = {}
            repo_counts: dict[UUID, int] = {}
            for row in rows:
                if row.scope == MemoryScope.USER.value:
                    user += row.n
                elif row.scope == MemoryScope.SHARED.value:
                    shared += row.n
                elif row.scope == MemoryScope.PROJECT.value:
                    project_counts[row.project_id] = row.n
                elif row.scope == MemoryScope.REPO.value:
                    repo_counts[row.repo_id] = row.n
            projects = []
            for project_id, project in grants.projects.items():
                repos = tuple(
                    sorted(
                        (
                            RepoScopeCount(
                                repo_id, repo.name, repo_counts.get(repo_id, 0)
                            )
                            for repo_id, repo in grants.repos.items()
                            if repo.project_id == project_id
                        ),
                        key=lambda entry: (entry.name, str(entry.repo_id)),
                    )
                )
                own = project_counts.get(project_id, 0)
                projects.append(
                    ProjectScopeCount(
                        project_id,
                        project.name,
                        own + sum(repo.count for repo in repos),
                        own,
                        repos,
                    )
                )
            projects.sort(key=lambda entry: (entry.name, str(entry.project_id)))
            return ScopeTree(user, tuple(projects), shared)

        return await self._run(work)

    # -- the Memory list -----------------------------------------------------------

    async def list_memories(
        self, actor: Principal, scope: BoardScope, query: str | None = None
    ) -> MemoryList:
        """The current version of each memory of ``scope``, the newest first.

        ``query`` (optional) keeps the memories whose title, content or one of
        whose current version's source references contains it (case-insensitive).
        A scope the reader may not see is :class:`MemoryNotFoundError`, like one
        that does not exist.
        """
        actor = _check_actor(actor)
        if not isinstance(scope, BoardScope):
            raise reject("scope", InputProblem.WRONG_TYPE)
        kind = validate_enum("scope", scope.kind, BoardScopeKind)
        if isinstance(query, str) and not query.strip():
            query = None  # a blank search lists everything
        if query is not None:
            query = validate_text("query", query, max_chars=limits.MAX_QUERY_CHARS)

        async def work(session: AsyncSession) -> MemoryList:
            grants = await self._grants(session, actor)
            version = aliased(MemoryVersion)
            if kind is BoardScopeKind.USER:
                if not grants.user:
                    raise MemoryNotFoundError
                where = and_(
                    version.scope == MemoryScope.USER.value,
                    version.owner_user_id == actor.user_id,
                )
            elif kind is BoardScopeKind.SHARED:
                if not grants.shared:
                    raise MemoryNotFoundError
                where = version.scope == MemoryScope.SHARED.value
            elif kind is BoardScopeKind.PROJECT:
                project_id = validate_uuid("project_id", scope.project_id)
                if project_id not in grants.projects:
                    raise MemoryNotFoundError
                where = and_(
                    version.scope == MemoryScope.PROJECT.value,
                    version.project_id == project_id,
                )
            else:
                project_id = validate_uuid("project_id", scope.project_id)
                repo_id = validate_uuid("repo_id", scope.repo_id)
                repo = grants.repos.get(repo_id)
                if repo is None or repo.project_id != project_id:
                    raise MemoryNotFoundError
                where = and_(
                    version.scope == MemoryScope.REPO.value,
                    version.repo_id == repo_id,
                )
            statement = (
                select(version)
                .where(where, grants.listed(version))
                .order_by(
                    version.pinned.desc(),
                    version.created_at.desc(),
                    version.memory_id,
                )
                .limit(limits.MAX_LIST_ITEMS + 1)
            )
            if query is not None:
                pattern = _like(query)
                source = aliased(MemorySource)
                statement = statement.where(
                    or_(
                        version.title.ilike(pattern, escape="\\"),
                        version.content.ilike(pattern, escape="\\"),
                        exists().where(
                            source.memory_version_id == version.id,
                            source.source_ref.ilike(pattern, escape="\\"),
                        ),
                    )
                )
            found = [
                version_view(row)
                for row in (await session.execute(statement)).scalars()
            ]
            truncated = len(found) > limits.MAX_LIST_ITEMS
            return MemoryList(
                await self._named(session, found[: limits.MAX_LIST_ITEMS]), truncated
            )

        return await self._run(work)

    # -- the History Graph -----------------------------------------------------------

    async def history(self, actor: Principal, memory_id: UUID) -> BoardHistory:
        """Every visible version of a memory (oldest first), the relations between
        visible versions that touch them, the visible versions of other memories
        at the far end of those relations, and whether the reader may change it.

        :class:`MemoryNotFoundError` when the memory is not listed for the reader.
        """
        actor = _check_actor(actor)
        memory_id = validate_uuid("memory_id", memory_id)

        async def work(session: AsyncSession) -> BoardHistory:
            grants = await self._grants(session, actor)
            version = aliased(MemoryVersion)
            versions = [
                version_view(row)
                for row in (
                    await session.execute(
                        select(version)
                        .where(version.memory_id == memory_id, grants.visible(version))
                        .order_by(version.version_number)
                    )
                ).scalars()
            ]
            if not versions:
                raise MemoryNotFoundError
            newer = aliased(MemoryVersion)
            older = aliased(MemoryVersion)
            relation = aliased(MemoryRelation)
            edges = await session.execute(
                select(
                    relation.from_version_id,
                    relation.to_version_id,
                    relation.relation_type,
                    relation.reason,
                )
                .join(newer, newer.id == relation.from_version_id)
                .join(older, older.id == relation.to_version_id)
                .where(
                    or_(newer.memory_id == memory_id, older.memory_id == memory_id),
                    grants.visible(newer),
                    grants.visible(older),
                )
                .order_by(relation.created_at, relation.id)
            )
            relations = tuple(
                BoardRelation(
                    row.from_version_id,
                    row.to_version_id,
                    RelationType(row.relation_type),
                    row.reason,
                )
                for row in edges
            )
            own = {entry.version_id for entry in versions}
            far = sorted(
                {
                    end
                    for edge in relations
                    for end in (edge.from_version_id, edge.to_version_id)
                    if end not in own
                }
            )
            related: list[MemoryVersionView] = []
            if far:
                other = aliased(MemoryVersion)
                related = [
                    version_view(row)
                    for row in (
                        await session.execute(
                            select(other)
                            .where(other.id.in_(far), grants.visible(other))
                            .order_by(other.memory_id, other.version_number)
                        )
                    ).scalars()
                ]
            named = await self._named(session, [*versions, *related])
            can_write = await self._can_write(session, actor, versions[-1], grants)
            return BoardHistory(
                named[: len(versions)], relations, named[len(versions) :], can_write
            )

        return await self._run(work)

    async def _can_write(
        self,
        session: AsyncSession,
        actor: Principal,
        current: MemoryVersionView,
        grants: _Grants,
    ) -> bool:
        """The injected Authorizer's policy's answer for an edit of ``current``
        (no audit row).

        ``MemoryVersioningService`` changes User and Project Memory only; the
        project's role and state are the ones just read from the database.
        """
        if current.scope is MemoryScope.USER and current.owner_user_id is not None:
            return decide(
                actor,
                Capability.MEMORY_USE,
                Resource.owned_by(
                    current.owner_user_id, RESOURCE_MEMORY, current.memory_id
                ),
                policy=self._policy,
            ).allowed
        if current.scope is MemoryScope.PROJECT and current.project_id is not None:
            project = grants.projects.get(current.project_id)
            if project is None:
                return False
            member = Principal(
                actor.user_id, actor.system_role, {current.project_id: project.role}
            )
            return decide(
                member,
                Capability.PROJECT_MEMORY_USE,
                Resource.project(current.project_id, project.state),
                policy=self._policy,
            ).allowed
        return False

    # -- sources -------------------------------------------------------------------

    async def sources(
        self, actor: Principal, memory_id: UUID, version_number: int
    ) -> tuple[BoardSource, ...]:
        """The sources of one visible version (oldest first).

        :class:`MemoryNotFoundError` when the version is not visible to the reader.
        """
        actor = _check_actor(actor)
        memory_id = validate_uuid("memory_id", memory_id)
        version_number = validate_version_number("version_number", version_number)

        async def work(session: AsyncSession) -> tuple[BoardSource, ...]:
            grants = await self._grants(session, actor)
            version = aliased(MemoryVersion)
            version_id = (
                await session.execute(
                    select(version.id).where(
                        version.memory_id == memory_id,
                        version.version_number == version_number,
                        grants.visible(version),
                    )
                )
            ).scalar_one_or_none()
            if version_id is None:
                raise MemoryNotFoundError
            source = aliased(MemorySource)
            rows = await session.execute(
                select(source)
                .where(source.memory_version_id == version_id)
                .order_by(source.created_at, source.id)
            )
            return tuple(
                BoardSource(
                    SourceType(row.source_type),
                    row.conversation_id,
                    row.message_id,
                    row.source_ref,
                    row.source_deleted_at,
                    row.created_at,
                )
                for row in rows.scalars()
            )

        return await self._run(work)

    # -- names of a written version ---------------------------------------------------

    async def named(self, version: MemoryVersionView) -> BoardVersion:
        """``version`` (just written by a person) with its writer's name."""
        if not isinstance(version, MemoryVersionView):
            raise reject("version", InputProblem.WRONG_TYPE)

        async def work(session: AsyncSession) -> BoardVersion:
            (named,) = await self._named(session, [version])
            return named

        return await self._run(work)
