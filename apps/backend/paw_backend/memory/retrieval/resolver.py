"""Deciding what a caller may read, in the backend, before any memory is touched.

The order is the point (MEMORY_ARCHITECTURE.md section 12: "ACL / Permission"
comes first): nothing here reads ``memory_versions``. The result,
:class:`~paw_backend.memory.retrieval.scopes.ResolvedScopes`, is the only thing the
SQL prefilter is built from.

* ``user`` scope: ``memory.use`` on a resource owned by the caller. (Its audit mode
  is ``REQUIRED`` in Decision 0004, so this one decision is recorded per call; see
  Decision 0019.)
* ``shared`` scope: ``shared_memory.read`` (``DENIED_ONLY``: nothing is written
  for an allowed read).
* ``project`` scope: the caller's accepted memberships are READ FROM THE DATABASE
  (the roles of the ``Principal`` a caller hands in are not trusted, exactly as
  ``ProjectService`` does), and each project is decided with ``project.read`` on
  its stored state (``DENIED_ONLY``). A project in Pending deletion or Deleted is
  not asked about at all: it is unreadable, and asking would write a denial per
  call. An invitation is not a membership.
* ``repo`` scope: the repositories a :class:`RepoAclSource` describes for the
  projects the caller can read, each decided with ``project.read`` on the
  repository resource (an override without ``read`` denies it). Without a source
  there is no Repo Memory.
* ``project_group`` scope: the ids a :class:`ProjectGroupSource` names, as given.
  Without a source there is no Project Group Memory.

A denial makes a scope contribute nothing; it is not reported (an answer that told
the caller which of the projects it named exist would be a leak). The one failure
that is an error is a decision that could not be recorded (``audit_unavailable``).
"""

import logging
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import (
    Authorizer,
    Capability,
    Decision,
    Principal,
    ProjectRole,
    ProjectState,
    Reason,
    RepoAcl,
    Resource,
)
from paw_backend.db import Database
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.retrieval import limits
from paw_backend.memory.retrieval.errors import (
    Component,
    RetrievalDataError,
    RetrievalPermissionError,
    RetrievalScopeLimitError,
    RetrievalSourceError,
)
from paw_backend.memory.retrieval.protocols import ProjectGroupSource, RepoAclSource
from paw_backend.memory.retrieval.queries import memberships_statement
from paw_backend.memory.retrieval.records import RetrievalQuery
from paw_backend.memory.retrieval.scopes import ResolvedScopes
from paw_backend.memory.retrieval.stages import bounded

logger = logging.getLogger(__name__)

RESOURCE_MEMORY = "memory"
RESOURCE_SHARED_MEMORY = "shared_memory"


class ScopeResolver:
    """Turns a query of an authenticated user into what that user may read."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        *,
        repo_acls: RepoAclSource | None,
        project_groups: ProjectGroupSource | None,
        stage_timeout_seconds: float,
    ) -> None:
        self._database = database
        self._authorizer = authorizer
        self._repo_acls = repo_acls
        self._project_groups = project_groups
        self._stage_timeout = stage_timeout_seconds

    async def resolve(self, actor: Principal, query: RetrievalQuery) -> ResolvedScopes:
        wanted = query.wanted_scopes
        contributing: set[MemoryScope] = set()

        if MemoryScope.USER in wanted and await self._allowed(
            actor,
            Capability.MEMORY_USE,
            Resource.owned_by(actor.user_id, RESOURCE_MEMORY),
        ):
            contributing.add(MemoryScope.USER)
        if MemoryScope.SHARED in wanted and await self._allowed(
            actor,
            Capability.SHARED_MEMORY_READ,
            Resource(kind=RESOURCE_SHARED_MEMORY),
        ):
            contributing.add(MemoryScope.SHARED)

        readable_projects: dict[UUID, ProjectState] = {}
        roles: dict[UUID, ProjectRole] = {}
        if (
            wanted & {MemoryScope.PROJECT, MemoryScope.REPO}
            and query.project_ids != frozenset()
        ):
            rows = await self._memberships(actor, query)
            roles = {project_id: role for project_id, role, _ in rows}
            # Decide each project with the roles read from the database, never
            # with the ones the caller's Principal carries.
            member = Principal(actor.user_id, actor.system_role, roles)
            for project_id, _, state in rows:
                if await self._allowed(
                    member, Capability.PROJECT_READ, Resource.project(project_id, state)
                ):
                    readable_projects[project_id] = state

        project_ids = (
            frozenset(readable_projects)
            if MemoryScope.PROJECT in wanted
            else frozenset()
        )
        if project_ids:
            contributing.add(MemoryScope.PROJECT)

        repo_ids: frozenset[UUID] = frozenset()
        if MemoryScope.REPO in wanted and readable_projects:
            repo_ids = await self._repos(actor, query, roles, readable_projects)
            if repo_ids:
                contributing.add(MemoryScope.REPO)

        group_ids: frozenset[UUID] = frozenset()
        if MemoryScope.PROJECT_GROUP in wanted:
            group_ids = await self._groups(actor)
            if group_ids:
                contributing.add(MemoryScope.PROJECT_GROUP)

        return ResolvedScopes(
            user_id=actor.user_id,
            scopes=frozenset(contributing),
            project_ids=project_ids,
            repo_ids=repo_ids,
            project_group_ids=group_ids,
        )

    # -- decisions ---------------------------------------------------------------

    async def _allowed(
        self, principal: Principal, capability: Capability, resource: Resource
    ) -> bool:
        """The Authorizer's answer; ``audit_unavailable`` and nonsense are errors."""
        decision = await self._authorizer.authorize(principal, capability, resource)
        if not isinstance(decision, Decision):
            raise RetrievalPermissionError("invalid_decision")
        if decision.allowed:
            return True
        if decision.reason is Reason.AUDIT_UNAVAILABLE:
            raise RetrievalPermissionError(decision.reason.value)
        return False

    # -- project membership --------------------------------------------------------

    async def _read_memberships(
        self, actor: Principal, query: RetrievalQuery
    ) -> list[tuple[UUID, str, str]]:
        statement = memberships_statement(
            actor.user_id, query.project_ids, limits.MAX_PROJECTS_PER_CALL + 1
        )

        async def work(session: AsyncSession) -> list[tuple[UUID, str, str]]:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rows = (await session.execute(statement)).all()
            return [(row.project_id, row.role, row.status) for row in rows]

        return await self._database.run_abortable(work)

    async def _memberships(
        self, actor: Principal, query: RetrievalQuery
    ) -> list[tuple[UUID, ProjectRole, ProjectState]]:
        rows = await self._read_memberships(actor, query)
        if len(rows) > limits.MAX_PROJECTS_PER_CALL:
            raise RetrievalScopeLimitError
        try:
            return [
                (project_id, ProjectRole(role), ProjectState(state))
                for project_id, role, state in rows
            ]
        except ValueError:
            raise RetrievalDataError from None

    # -- repositories and project groups ---------------------------------------------

    async def _repos(
        self,
        actor: Principal,
        query: RetrievalQuery,
        roles: dict[UUID, ProjectRole],
        readable_projects: dict[UUID, ProjectState],
    ) -> frozenset[UUID]:
        source = self._repo_acls
        if source is None:
            return frozenset()
        acls = await bounded(
            lambda: source.repo_acls(actor.user_id, frozenset(readable_projects)),
            Component.REPO_ACL_SOURCE,
            self._stage_timeout,
        )
        if (
            not isinstance(acls, list | tuple)
            or len(acls) > limits.MAX_REPOS_PER_CALL
            or not all(isinstance(acl, RepoAcl) for acl in acls)
            or len({acl.repo_id for acl in acls}) != len(acls)
        ):
            # A source that lists a repository twice cannot be believed about either.
            raise RetrievalSourceError(Component.REPO_ACL_SOURCE)
        member = Principal(actor.user_id, actor.system_role, roles)
        allowed: set[UUID] = set()
        for acl in acls:
            state = readable_projects.get(acl.project_id)
            if state is None:
                continue  # a repository of a project the caller cannot read
            if query.repo_ids is not None and acl.repo_id not in query.repo_ids:
                continue
            resource = Resource.repository(acl.project_id, state, acl)
            if await self._allowed(member, Capability.PROJECT_READ, resource):
                allowed.add(acl.repo_id)
        return frozenset(allowed)

    async def _groups(self, actor: Principal) -> frozenset[UUID]:
        source = self._project_groups
        if source is None:
            return frozenset()
        found = await bounded(
            lambda: source.project_group_ids(actor.user_id),
            Component.PROJECT_GROUP_SOURCE,
            self._stage_timeout,
        )
        if (
            not isinstance(found, list | tuple | set | frozenset)
            or len(found) > limits.MAX_PROJECT_GROUPS
            or not all(isinstance(group, UUID) for group in found)
        ):
            raise RetrievalSourceError(Component.PROJECT_GROUP_SOURCE)
        return frozenset(found)
