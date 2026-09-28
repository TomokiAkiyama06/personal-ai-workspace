"""The production ``TaskAuthority`` (issue #125): rights from what is stored.

Real: ``TaskService`` on PostgreSQL (the stored Working Set is read with
``restore``), and, in the last test, the whole orchestrator with the real Tool
Broker. Faked: the repository service's backend-internal reads (``working_set_acl``
and ``scope_entries``, which need checkouts on disk and are tested with
``RepositoryService``) and the project states.
"""

import unittest
import uuid

from paw_backend.authz import (
    AgentGrant,
    Capability,
    ProjectState,
    RepoAcl,
)
from paw_backend.authz.capabilities import CAPABILITIES
from paw_backend.orchestrator.authority import (
    PARENT_CAPABILITIES,
    StoredProjectStates,
    StoredTaskAuthority,
    parent_agent_id,
)
from paw_backend.orchestrator.domain import ROLE_CEILING, RunOutcome
from paw_backend.projects.records import ProjectStatus
from paw_backend.repositories.errors import (
    CheckoutChangedError,
    CheckoutNotFoundError,
    LinuxAccountUnavailableError,
    PathProblem,
    RepositoryNotFoundError,
)
from paw_backend.tasks import TaskCommand, TaskService, WorkingSetOperation
from paw_backend.tasks.domain import RepoRole
from paw_backend.tasks.records import WorkingSetEntry
from paw_backend.tools import ExecutionStatus, ScopedRepository

from . import test_orchestrator_tools as through_the_orchestrator
from .gate_support import ALWAYS_ACTIVE
from .orchestrator_support import make_plan, node
from .projects_support import PostgresProjectTestCase
from .task_support import BASELINE, PostgresTaskTestCase, requires_postgres
from .tools_support import REPO, REPO_REMOTE, ROOT


def acl_of(repo_id, project_id):
    return RepoAcl.inherit(repo_id, project_id)


class FakeRepositories:
    """``working_set_acl`` / ``scope_entries`` from a table the test fills.

    ``registered[repo_id] = (project_id, root, remotes)``; ``related[repo_id]`` are
    the other checkouts ``scope_entries`` returns after the repository's own;
    ``failures[repo_id]`` is raised by ``scope_entries``."""

    def __init__(self) -> None:
        self.registered: dict[uuid.UUID, tuple[uuid.UUID, str, tuple[str, ...]]] = {}
        self.related: dict[uuid.UUID, list[uuid.UUID]] = {}
        self.failures: dict[uuid.UUID, Exception] = {}
        self.asked: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []

    def register(self, repo_id, project_id, root, *remotes):
        self.registered[repo_id] = (project_id, root, tuple(remotes))

    def entry(self, repo_id) -> ScopedRepository:
        project_id, root, remotes = self.registered[repo_id]
        return ScopedRepository(
            repo_id, project_id, root, acl_of(repo_id, project_id), remotes=remotes
        )

    async def working_set_acl(self, repository_id):
        found = self.registered.get(repository_id)
        return None if found is None else acl_of(repository_id, found[0])

    async def scope_entries(self, user_id, project_id, repository_id):
        self.asked.append((user_id, project_id, repository_id))
        if repository_id in self.failures:
            raise self.failures[repository_id]
        return (
            self.entry(repository_id),
            *(self.entry(other) for other in self.related.get(repository_id, [])),
        )


class FakeStates:
    def __init__(self, states=None) -> None:
        self.states = dict(states or {})
        self.default = ProjectState.ACTIVE

    async def states_of(self, project_ids):
        found = {}
        for project_id in project_ids:
            state = self.states.get(project_id, self.default)
            if state is not None:
                found[project_id] = state
        return found


@requires_postgres
class StoredScopeTest(PostgresTaskTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.target = uuid.uuid4()
        self.referenced = uuid.uuid4()
        self.repositories = FakeRepositories()
        self.repositories.register(
            self.target, self.project_id, "/srv/w/target", "https://github.com/o/t"
        )
        self.repositories.register(
            self.referenced,
            self.project_id,
            "/srv/w/ref",
            "https://gitlab.example/o/r",
            "https://gitlab.example/o/r.git",
        )
        self.states = FakeStates()
        self.authority = StoredTaskAuthority(
            self.service, self.repositories, self.states
        )

    async def task(self, *members):
        task_id = await self.create_task(
            repositories=[
                WorkingSetEntry(repo_id, role, BASELINE) for repo_id, role in members
            ]
        )
        return await self.service.restore(task_id)

    async def test_the_scope_is_the_stored_working_set_with_its_roles(self):
        # The referenced repository is listed first: the target still comes first.
        snapshot = await self.task(
            (self.referenced, RepoRole.REFERENCED), (self.target, RepoRole.TARGET)
        )

        scope = await self.authority.parent_scope(snapshot)

        self.assertEqual(scope.path_roots, ("/srv/w/target", "/srv/w/ref"))
        self.assertEqual(
            [(r.repo_id, r.role) for r in scope.repositories],
            [(self.target, RepoRole.TARGET), (self.referenced, RepoRole.REFERENCED)],
        )
        self.assertEqual(scope.hosts, {"github.com", "gitlab.example"})
        self.assertEqual(dict(scope.projects), {self.project_id: ProjectState.ACTIVE})
        self.assertEqual(dict(scope.credential_handles), {})
        self.assertEqual(scope.excluded_repositories, ())
        repository = scope.repository(self.target)
        self.assertEqual(repository.acl, acl_of(self.target, self.project_id))
        self.assertEqual(repository.remotes, ("https://github.com/o/t",))
        # The checkout asked for is the delegating user's, in the repo's project.
        self.assertIn(
            (self.user_id, self.project_id, self.target), self.repositories.asked
        )

    async def test_a_change_after_the_snapshot_is_seen(self):
        snapshot = await self.task((self.target, RepoRole.TARGET))
        await self.service.change_working_set(
            snapshot.id,
            WorkingSetOperation.ADD_REFERENCED,
            self.referenced,
            actor=self.user,
            expected_role=None,
        )

        scope = await self.authority.parent_scope(snapshot)  # the OLD snapshot

        self.assertEqual(
            [(r.repo_id, r.role) for r in scope.repositories],
            [(self.target, RepoRole.TARGET), (self.referenced, RepoRole.REFERENCED)],
        )

    async def test_a_repository_that_cannot_be_worked_on_is_left_out(self):
        gone, no_checkout, no_account = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        for repo_id in (no_checkout, no_account):
            self.repositories.register(repo_id, self.project_id, f"/srv/w/{repo_id}")
        self.repositories.failures[no_checkout] = CheckoutNotFoundError()
        self.repositories.failures[no_account] = LinuxAccountUnavailableError()
        snapshot = await self.task(
            (self.target, RepoRole.TARGET),
            (gone, RepoRole.REFERENCED),  # not registered (or its project Deleted)
            (no_checkout, RepoRole.WORKING),
            (no_account, RepoRole.REFERENCED),
        )

        scope = await self.authority.parent_scope(snapshot)

        self.assertEqual([r.repo_id for r in scope.repositories], [self.target])
        self.assertEqual(scope.path_roots, ("/srv/w/target",))
        for repo_id in (gone, no_checkout, no_account):
            self.assertIsNone(scope.repository(repo_id))

    async def test_a_repository_that_disappeared_meanwhile_is_left_out(self):
        self.repositories.failures[self.referenced] = RepositoryNotFoundError()
        snapshot = await self.task(
            (self.target, RepoRole.TARGET), (self.referenced, RepoRole.REFERENCED)
        )
        scope = await self.authority.parent_scope(snapshot)
        self.assertEqual([r.repo_id for r in scope.repositories], [self.target])

    async def test_other_checkouts_around_the_worktree_are_excluded(self):
        # Another checkout of the user inside the target's worktree, of a project
        # outside the task, and a Working Set repository without a usable
        # checkout of its own that also lies inside it: both are reached through
        # the target's root and must not be touched without their own ACL.
        foreign_project = uuid.uuid4()
        nested, left_out = uuid.uuid4(), uuid.uuid4()
        self.repositories.register(nested, foreign_project, "/srv/w/target/vendor")
        self.repositories.register(left_out, self.project_id, "/srv/w/target/sub")
        self.repositories.related[self.target] = [nested, left_out]
        self.repositories.failures[left_out] = CheckoutNotFoundError()
        snapshot = await self.task(
            (self.target, RepoRole.TARGET), (left_out, RepoRole.WORKING)
        )

        scope = await self.authority.parent_scope(snapshot)

        self.assertEqual([r.repo_id for r in scope.repositories], [self.target])
        self.assertEqual(
            {r.repo_id for r in scope.excluded_repositories}, {nested, left_out}
        )
        self.assertNotIn(foreign_project, scope.projects)
        self.assertEqual(scope.path_roots, ("/srv/w/target",))

    async def test_a_nested_working_set_repository_is_not_excluded(self):
        self.repositories.register(
            self.referenced, self.project_id, "/srv/w/target/ref"
        )
        self.repositories.related[self.target] = [self.referenced]
        self.repositories.related[self.referenced] = [self.target]
        snapshot = await self.task(
            (self.target, RepoRole.TARGET), (self.referenced, RepoRole.REFERENCED)
        )
        scope = await self.authority.parent_scope(snapshot)
        self.assertEqual(
            {r.repo_id for r in scope.repositories}, {self.target, self.referenced}
        )
        self.assertEqual(scope.excluded_repositories, ())

    async def test_a_changed_checkout_fails_the_whole_scope(self):
        self.repositories.failures[self.referenced] = CheckoutChangedError(
            PathProblem.CHANGED, uuid.uuid4()
        )
        snapshot = await self.task(
            (self.target, RepoRole.TARGET), (self.referenced, RepoRole.REFERENCED)
        )
        with self.assertRaises(CheckoutChangedError):
            await self.authority.parent_scope(snapshot)

    async def test_the_projects_are_the_tasks_and_the_repositories_with_their_state(
        self,
    ):
        other_project, deleted_project = uuid.uuid4(), uuid.uuid4()
        in_other, in_deleted = uuid.uuid4(), uuid.uuid4()
        self.repositories.register(in_other, other_project, "/srv/w/other")
        self.repositories.register(in_deleted, deleted_project, "/srv/w/deleted")
        self.states.states = {
            other_project: ProjectState.ARCHIVED,
            deleted_project: None,  # Deleted: no state
        }
        snapshot = await self.task(
            (self.target, RepoRole.TARGET),
            (in_other, RepoRole.REFERENCED),
            (in_deleted, RepoRole.REFERENCED),
        )

        scope = await self.authority.parent_scope(snapshot)
        grant = await self.authority.parent_grant(snapshot)

        self.assertEqual(
            dict(scope.projects),
            {
                self.project_id: ProjectState.ACTIVE,
                other_project: ProjectState.ARCHIVED,
            },
        )
        self.assertEqual(
            {r.repo_id for r in scope.repositories}, {self.target, in_other}
        )
        self.assertEqual(grant.project_ids, {self.project_id, other_project})

    async def test_the_parent_grant(self):
        snapshot = await self.task((self.target, RepoRole.TARGET))

        grant = await self.authority.parent_grant(snapshot)

        self.assertIsInstance(grant, AgentGrant)
        self.assertEqual(grant.capabilities, PARENT_CAPABILITIES)
        self.assertEqual(PARENT_CAPABILITIES, frozenset().union(*ROLE_CEILING.values()))
        self.assertTrue(all(CAPABILITIES[c].delegable for c in grant.capabilities))
        self.assertNotIn(Capability.PROJECT_TASK_WORKING_SET_MANAGE, grant.capabilities)
        self.assertEqual(grant.project_ids, {self.project_id})
        # The same run is the same parent; another run (a Retry) another one.
        self.assertEqual(grant.agent_id, parent_agent_id(snapshot))
        self.assertEqual(
            (await self.authority.parent_grant(snapshot)).agent_id, grant.agent_id
        )
        await self.service.execute(snapshot.id, TaskCommand.START, actor=self.system)
        await self.service.execute(snapshot.id, TaskCommand.FAIL, actor=self.system)
        await self.service.execute(snapshot.id, TaskCommand.RETRY, actor=self.system)
        retried = await self.service.restore(snapshot.id)
        self.assertNotEqual(parent_agent_id(retried), grant.agent_id)

    def test_it_refuses_what_it_cannot_use(self):
        with self.assertRaises(TypeError):
            StoredTaskAuthority(object(), self.repositories, self.states)
        with self.assertRaises(TypeError):
            StoredTaskAuthority(self.service, object(), self.states)
        with self.assertRaises(TypeError):
            StoredTaskAuthority(self.service, self.repositories, object())
        with self.assertRaises(TypeError):
            StoredProjectStates(object())


@requires_postgres
class StoredProjectStatesTest(PostgresProjectTestCase):
    async def test_states_come_from_the_projects_table(self):
        active = self.seed_project(ProjectStatus.ACTIVE)
        archived = self.seed_project(ProjectStatus.ARCHIVED, name="Beta")
        pending = self.seed_project(ProjectStatus.PENDING_DELETION, name="Gamma")
        deleted = self.seed_project(ProjectStatus.DELETED)
        missing = uuid.uuid4()

        states = await StoredProjectStates(self.database).states_of(
            [active, archived, pending, deleted, missing, active]
        )

        self.assertEqual(
            states,
            {
                active: ProjectState.ACTIVE,
                archived: ProjectState.ARCHIVED,
                pending: ProjectState.PENDING_DELETION,
            },
        )


class StoredAuthorityThroughTheOrchestratorTest(
    through_the_orchestrator.ToolsThroughTheOrchestratorTest
):
    """The orchestrator, the real Broker and the production authority together."""

    async def test_a_worker_writes_through_the_scope_the_authority_built(self):
        repositories = FakeRepositories()
        repositories.register(REPO, self.project_id, ROOT, REPO_REMOTE)
        authority = StoredTaskAuthority(
            TaskService(self.new_database(), project_gate=ALWAYS_ACTIVE),
            repositories,
            FakeStates(),
        )
        h, executor, outcomes, _, _ = await self.build(
            authority=authority,
            scripts={"impl": ("repo.write_file", through_the_orchestrator.WRITE)},
        )
        task_id = await self.prepare(h, make_plan(node("impl")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, RunOutcome.DAG_SUCCEEDED)
        (outcome,) = outcomes["impl"]
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        (invocation,) = executor.invocations
        context = invocation.context
        self.assertEqual(
            [(r.repo_id, r.role) for r in context.scope.repositories],
            [(REPO, RepoRole.TARGET)],
        )
        self.assertEqual(context.scope.path_roots, (ROOT,))
        self.assertEqual(context.delegator_id, self.user_id)
        self.assertEqual(context.task_id, task_id)


# The inherited tests run in their own module already.
for _name in list(vars(through_the_orchestrator.ToolsThroughTheOrchestratorTest)):
    if _name.startswith("test_"):
        setattr(StoredAuthorityThroughTheOrchestratorTest, _name, None)
del _name


if __name__ == "__main__":
    unittest.main()
