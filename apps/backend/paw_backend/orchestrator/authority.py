"""The production ``TaskAuthority``: a task's rights from what is stored (issue #125).

The orchestrator asks its ``TaskAuthority`` for the parent grant and the parent
scope of a task **for every tool call** (``Orchestrator._context``), so that a
narrowed ACL, a removed repository or an archived project takes effect on the next
call. :class:`StoredTaskAuthority` answers from the database each time; it keeps
nothing between calls.

The scope (``parent_scope``)
---------------------------
* **The Working Set is read again** (``TaskService.restore``), never taken from the
  snapshot the run started with: a repository added, downgraded or removed since is
  seen at once. The role of each repository is the stored one
  (``tools.scope.with_working_set_roles``, issue #85, Decision 0030); a repository
  that is not in the Working Set never gets a role.
* **Each repository of the Working Set** is resolved by the repository service
  (``RepositoryService.working_set_acl`` for its project and ACL, then
  ``RepositoryService.scope_entries`` for the delegating user's own ``ready``
  checkout: its root, the stored ACL, the registered remotes). A repository that is
  gone (no registration, its project Deleted) or that the delegating user has no
  ready checkout of is **left out**: it is then not in the scope at all, so no call
  can name it and its worktree is below no path root. Any other failure (a checkout
  whose root changed, too many checkouts, the database) propagates: the call fails
  closed, as the orchestrator does with any error of its authority.
* **Other checkouts** that ``scope_entries`` returns (checkouts of the same user
  that enclose the repository's worktree or lie inside it: Decision 0006, section
  8(b)) are ``excluded_repositories`` unless they are in the scope themselves (a
  Working Set repository that was left out above is excluded too): a path in their
  worktree or a URL below their remotes is out of scope.
* ``path_roots`` are the roots of the Working Set's checkouts (the ``target``
  repositories first, so that a relative path resolves in a target); ``hosts`` are
  the hosts of their registered remotes; ``projects`` are the task's project and
  the projects of those repositories, each with its state **now** (a Deleted
  project is left out, with its repositories); ``credential_handles`` is empty:
  nothing maps a stored credential to a task yet (fail closed; Decision 0047, 3).

The grant (``parent_grant``)
---------------------------
``AgentGrant`` of an agent derived from the task and its run (the same run always
gets the same parent id), for the projects of the scope, holding the union of the
node roles' ceilings (``orchestrator.domain.ROLE_CEILING``: what any node may be
given). It never widens the delegating user: the Authorizer intersects it with the
user's current rights at every decision (``authz.policy.decide_agent``), and a node
receives only a subset (``scope.node_grant``). ``project.task.working_set.manage``
is not in it (Decision 0047, 4).
"""

import uuid
from collections.abc import Iterable
from typing import Protocol

from paw_backend.authz import AgentGrant, ProjectState, RepoAcl
from paw_backend.db import Database
from paw_backend.orchestrator.domain import ROLE_CEILING
from paw_backend.projects import store as projects_store
from paw_backend.projects.records import ProjectStatus
from paw_backend.repositories.errors import (
    CheckoutNotFoundError,
    LinuxAccountUnavailableError,
    RepositoryNotFoundError,
)
from paw_backend.tasks import TaskService, TaskSnapshot
from paw_backend.tasks.domain import RepoRole
from paw_backend.tools import ScopedRepository, TaskScope
from paw_backend.tools.interfaces import require_async_method
from paw_backend.tools.scope import normalise_url, with_working_set_roles

# The parent agent of a task's run is derived, never random (like the node agents
# of ``scope.agent_id_of``, in a namespace of its own).
_PARENT_NAMESPACE = uuid.UUID("0b7d4e52-6f1c-4d8a-9a35-12c5f0e1a125")

# What any node role may be given: the parent holds exactly that (Decision 0047, 4).
PARENT_CAPABILITIES = frozenset(
    capability for ceiling in ROLE_CEILING.values() for capability in ceiling
)

# A repository the delegating user cannot work on now is left out of the scope.
_LEFT_OUT = (
    RepositoryNotFoundError,
    CheckoutNotFoundError,
    LinuxAccountUnavailableError,
)


class RepositoryScopes(Protocol):
    """What the authority needs of ``RepositoryService`` (backend-internal)."""

    async def working_set_acl(self, repository_id: uuid.UUID) -> RepoAcl | None: ...

    async def scope_entries(
        self, user_id: uuid.UUID, project_id: uuid.UUID, repository_id: uuid.UUID
    ) -> tuple[ScopedRepository, ...]: ...


class ProjectStates(Protocol):
    """The stored state of projects (``None`` for a missing or Deleted one)."""

    async def states_of(
        self, project_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, ProjectState]: ...


class StoredProjectStates:
    """:class:`ProjectStates` read from the ``projects`` table."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    async def states_of(
        self, project_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, ProjectState]:
        states: dict[uuid.UUID, ProjectState] = {}
        async with self._database.session() as session, session.begin():
            for project_id in dict.fromkeys(project_ids):
                project = await projects_store.get_project(session, project_id)
                if project is None or project.status is ProjectStatus.DELETED:
                    continue
                states[project_id] = ProjectState(project.status.value)
        return states


def parent_agent_id(task: TaskSnapshot) -> uuid.UUID:
    """The id of the agent that works for ``task`` in the run of the snapshot."""
    run = task.run
    return uuid.uuid5(_PARENT_NAMESPACE, f"{task.id}/{run.attempt}.{run.retry_count}")


class StoredTaskAuthority:
    """The ``TaskAuthority`` of the application (module docstring)."""

    def __init__(
        self,
        tasks: TaskService,
        repositories: RepositoryScopes,
        projects: ProjectStates,
    ) -> None:
        if not isinstance(tasks, TaskService):
            raise TypeError("tasks must be a TaskService")
        require_async_method(repositories, "working_set_acl", 1)
        require_async_method(repositories, "scope_entries", 3)
        require_async_method(projects, "states_of", 1)
        self._tasks = tasks
        self._repositories = repositories
        self._projects = projects

    async def parent_grant(self, task: TaskSnapshot) -> AgentGrant:
        stored = await self._tasks.restore(task.id, log_limit=0)
        _scope, projects = await self._resolve(stored)
        # The run is the one the orchestrator started with (like a node's id).
        return AgentGrant(parent_agent_id(task), PARENT_CAPABILITIES, projects)

    async def parent_scope(self, task: TaskSnapshot) -> TaskScope:
        stored = await self._tasks.restore(task.id, log_limit=0)
        scope, _projects = await self._resolve(stored)
        return scope

    async def _resolve(self, task: TaskSnapshot) -> tuple[TaskScope, frozenset]:
        """The scope of ``task`` (the stored snapshot) and its projects."""
        # Targets first: the first root is where a relative path resolves.
        members = sorted(
            task.working_set, key=lambda member: member.role is not RepoRole.TARGET
        )
        found: list[tuple[ScopedRepository, tuple[ScopedRepository, ...]]] = []
        for member in members:
            acl = await self._repositories.working_set_acl(member.repository_id)
            if acl is None:
                continue  # not registered, or its project is Deleted
            try:
                entries = await self._repositories.scope_entries(
                    task.created_by, acl.project_id, member.repository_id
                )
            except _LEFT_OUT:
                continue
            own, *related = entries
            found.append((own, tuple(related)))
        states = await self._projects.states_of(
            [task.project_id, *(own.project_id for own, _ in found)]
        )
        repositories: list[ScopedRepository] = []
        roots: list[str] = []
        hosts: set[str] = set()
        for own, _related in found:
            if own.project_id not in states:
                continue  # Deleted since its ACL was read
            repositories.append(own)
            if own.root is not None:
                roots.append(own.root)
            hosts.update(normalise_url(remote)[1] for remote in own.remotes)
        # Every other checkout that encloses or lies in one of them is excluded,
        # also a Working Set repository that was left out above (its worktree must
        # not be reached through another repository's root without its own ACL).
        included = {repository.repo_id for repository in repositories}
        excluded: dict[uuid.UUID, ScopedRepository] = {}
        for own, related in found:
            if own.repo_id not in included:
                continue
            for other in related:
                if other.repo_id not in included:
                    excluded.setdefault(other.repo_id, other)
        scope = TaskScope(
            path_roots=roots,
            hosts=hosts,
            projects=states,
            credential_handles={},
            repositories=with_working_set_roles(repositories, task.working_set),
            excluded_repositories=tuple(excluded.values()),
        )
        return scope, frozenset(states)
