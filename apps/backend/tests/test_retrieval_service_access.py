"""Who may read what through a retrieval: roles, projects, repositories, audit.

Real PostgreSQL, the real ``Authorizer``. Every memory matches the query, so what
is missing from a result is missing because of permission and nothing else.
"""

from unittest import mock
from uuid import uuid4

from paw_backend.authz import (
    Authorizer,
    Principal,
    ProjectRole,
    RepoAcl,
    RepoPermission,
    SystemRole,
)
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.retrieval import (
    Component,
    RetrievalPermissionError,
    RetrievalScopeLimitError,
    RetrievalSourceError,
)
from paw_backend.memory.retrieval import limits as retrieval_limits
from paw_backend.projects import MemberStatus, ProjectStatus

from .authz_support import FailingSink
from .retrieval_pg_support import (
    PostgresRetrievalTestCase,
    StaticGroups,
    StaticRepoAcls,
    requires_postgres,
    titles,
)

QUERY = "deploy backend friday"
TEXT = "deploy backend friday"


@requires_postgres
class UserScopeTest(PostgresRetrievalTestCase):
    async def test_a_user_reads_their_own_memory_and_not_anyone_elses(self):
        alice, bob = self.user(), self.user()
        self.seed("alice", TEXT, owner=alice.user_id, embed=False)
        self.seed("bob", TEXT, owner=bob.user_id, embed=False)
        self.assertEqual(titles(await self.retrieve(alice, QUERY)), ["alice"])
        self.assertEqual(titles(await self.retrieve(bob, QUERY)), ["bob"])

    async def test_owner_and_admin_do_not_read_another_users_private_memory(self):
        alice = self.user()
        self.seed("alice", TEXT, owner=alice.user_id, embed=False)
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role=role.value):
                result = await self.retrieve(self.user(role), QUERY)
                self.assertEqual(result.hits, ())

    async def test_owner_and_admin_read_their_own_memory(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            with self.subTest(role=role.value):
                me = self.user(role)
                self.seed(f"own {role.value}", TEXT, owner=me.user_id, embed=False)
                self.assertEqual(
                    titles(await self.retrieve(me, QUERY)), [f"own {role.value}"]
                )

    async def test_the_system_role_reads_nothing_at_all(self):
        alice = self.user()
        self.seed("alice", TEXT, owner=alice.user_id, embed=False)
        self.seed("shared", TEXT, scope="shared", embed=False)
        system = Principal(alice.user_id, SystemRole.SYSTEM)
        result = await self.retrieve(system, QUERY)
        self.assertEqual(result.hits, ())

    async def test_a_principal_cannot_read_by_using_another_users_id_in_the_query(self):
        # The query has no owner field; the owner is always the authenticated user.
        alice, bob = self.user(), self.user()
        self.seed("alice", TEXT, owner=alice.user_id, embed=False)
        self.assertEqual((await self.retrieve(bob, QUERY, scopes=["user"])).hits, ())


@requires_postgres
class SharedScopeTest(PostgresRetrievalTestCase):
    async def test_every_active_user_role_reads_shared_memory(self):
        self.seed("company rule", TEXT, scope="shared", embed=False)
        for role in (SystemRole.USER, SystemRole.ADMIN, SystemRole.OWNER):
            with self.subTest(role=role.value):
                result = await self.retrieve(self.user(role), QUERY)
                self.assertEqual(titles(result), ["company rule"])

    async def test_a_deleted_shared_memory_is_not_returned(self):
        self.seed("deleted", TEXT, scope="shared", status="deprecated", embed=False)
        self.assertEqual((await self.retrieve(self.user(), QUERY)).hits, ())


@requires_postgres
class ProjectScopeTest(PostgresRetrievalTestCase):
    def seed_project_memory(self, project, title="note"):
        return self.seed(title, TEXT, scope="project", project=project, embed=False)

    async def test_every_project_role_reads_project_memory(self):
        project = self.seed_project()
        self.seed_project_memory(project)
        for role in ProjectRole:
            with self.subTest(role=role.value):
                who = self.member_of(project, role)
                self.assertEqual(titles(await self.retrieve(who, QUERY)), ["note"])

    async def test_a_user_who_is_not_a_member_reads_none_of_it(self):
        project = self.seed_project()
        self.seed_project_memory(project)
        for role in SystemRole:
            with self.subTest(role=role.value):
                who = Principal(self.seed_user(), role)
                self.assertEqual((await self.retrieve(who, QUERY)).hits, ())

    async def test_an_invitation_is_not_a_membership(self):
        project = self.seed_project()
        self.seed_project_memory(project)
        invited = self.member_of(project, status=MemberStatus.INVITED)
        self.assertEqual((await self.retrieve(invited, QUERY)).hits, ())

    async def test_the_roles_a_principal_carries_are_not_trusted(self):
        project = self.seed_project()
        self.seed_project_memory(project)
        outsider = Principal(
            self.seed_user(), SystemRole.USER, {project: ProjectRole.MANAGER}
        )
        self.assertEqual((await self.retrieve(outsider, QUERY)).hits, ())

    async def test_project_states(self):
        cases = [
            (ProjectStatus.ACTIVE, True),
            (ProjectStatus.ARCHIVED, True),  # read-only, but readable
            (ProjectStatus.PENDING_DELETION, False),  # access stopped
            (ProjectStatus.DELETED, False),
        ]
        for status, readable in cases:
            with self.subTest(status=status.value):
                project = self.seed_project(status)
                self.seed_project_memory(project, f"in {status.value}")
                who = self.member_of(project)
                expected = [f"in {status.value}"] if readable else []
                self.assertEqual(titles(await self.retrieve(who, QUERY)), expected)

    async def test_an_unreadable_project_is_not_asked_about_so_no_denial_appears(
        self,
    ):
        project = self.seed_project(ProjectStatus.PENDING_DELETION)
        self.seed_project_memory(project)
        who = self.member_of(project)
        await self.retrieve(who, QUERY, scopes=["project"])
        self.assertEqual(self.sink.events, [])

    async def test_leaving_the_project_ends_access_at_the_next_call(self):
        project = self.seed_project()
        self.seed_project_memory(project)
        who = self.member_of(project)
        self.assertEqual(titles(await self.retrieve(who, QUERY)), ["note"])
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "DELETE FROM project_members WHERE user_id = %s", (who.user_id,)
            )
        self.assertEqual((await self.retrieve(who, QUERY)).hits, ())

    async def test_only_the_users_own_projects_are_read(self):
        mine, theirs = self.seed_project(), self.seed_project()
        self.seed_project_memory(mine, "mine")
        self.seed_project_memory(theirs, "theirs")
        who = self.member_of(mine)
        self.member_of(theirs)
        self.assertEqual(titles(await self.retrieve(who, QUERY)), ["mine"])

    async def test_a_user_in_too_many_projects_must_narrow_the_query(self):
        who = self.user()
        projects = [self.seed_project() for _ in range(4)]
        for project in projects:
            self.seed_member(project, who.user_id)
        with mock.patch.object(retrieval_limits, "MAX_PROJECTS_PER_CALL", 3):
            with self.assertRaises(RetrievalScopeLimitError):
                await self.retrieve(who, QUERY)
            narrowed = await self.retrieve(who, QUERY, project_ids=projects[:3])
            self.assertEqual(narrowed.hits, ())


@requires_postgres
class NarrowingTest(PostgresRetrievalTestCase):
    async def test_project_ids_narrow_project_and_repo_memory_only(self):
        p1, p2 = self.seed_project(), self.seed_project()
        me = self.member_of(p1)
        self.seed_member(p2, me.user_id)
        r1, r2 = self.new_repo_id(), self.new_repo_id()
        self.seed("p1", TEXT, scope="project", project=p1, embed=False)
        self.seed("p2", TEXT, scope="project", project=p2, embed=False)
        self.seed("r1", TEXT, scope="repo", repo=r1, embed=False)
        self.seed("r2", TEXT, scope="repo", repo=r2, embed=False)
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        self.seed("shared", TEXT, scope="shared", embed=False)
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls([RepoAcl.inherit(r1, p1), RepoAcl.inherit(r2, p2)])
        )
        everything = await self.retrieve(me, QUERY, retriever=retriever, limit=50)
        self.assertEqual(
            sorted(titles(everything)), ["mine", "p1", "p2", "r1", "r2", "shared"]
        )
        narrowed = await self.retrieve(
            me, QUERY, retriever=retriever, project_ids=[p1], limit=50
        )
        self.assertEqual(sorted(titles(narrowed)), ["mine", "p1", "r1", "shared"])

    async def test_repo_ids_narrow_the_repositories(self):
        project = self.seed_project()
        me = self.member_of(project)
        r1, r2 = self.new_repo_id(), self.new_repo_id()
        self.seed("r1", TEXT, scope="repo", repo=r1, embed=False)
        self.seed("r2", TEXT, scope="repo", repo=r2, embed=False)
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls(
                [RepoAcl.inherit(r1, project), RepoAcl.inherit(r2, project)]
            )
        )
        result = await self.retrieve(me, QUERY, retriever=retriever, repo_ids=[r2])
        self.assertEqual(titles(result), ["r2"])
        none = await self.retrieve(me, QUERY, retriever=retriever, repo_ids=[])
        self.assertEqual(none.hits, ())

    async def test_scopes_narrow_the_search(self):
        project = self.seed_project()
        me = self.member_of(project)
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        self.seed("project", TEXT, scope="project", project=project, embed=False)
        self.seed("shared", TEXT, scope="shared", embed=False)
        cases = [
            ([MemoryScope.USER], ["mine"]),
            (["project"], ["project"]),
            (["shared", "user"], ["mine", "shared"]),
            ([], []),
        ]
        for scopes, expected in cases:
            with self.subTest(scopes=scopes):
                result = await self.retrieve(me, QUERY, scopes=scopes)
                self.assertEqual(sorted(titles(result)), expected)

    async def test_naming_a_foreign_project_finds_nothing_and_says_nothing(
        self,
    ):
        theirs = self.seed_project()
        self.seed("theirs", TEXT, scope="project", project=theirs, embed=False)
        me = self.user()
        for named in ([theirs], [theirs, uuid4()], []):
            with self.subTest(named=len(named)):
                result = await self.retrieve(me, QUERY, project_ids=named)
                self.assertEqual(result.hits, ())
                self.assertEqual(result.conflicts, ())

    async def test_an_empty_project_narrowing_does_not_read_memberships_at_all(self):
        me = self.user()
        result = await self.retrieve(
            me, QUERY, scopes=["project", "repo"], project_ids=[]
        )
        self.assertEqual(result.hits, ())


@requires_postgres
class RepoScopeTest(PostgresRetrievalTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.new_repo_id()

    async def setup_member(self):
        project = self.seed_project()
        me = self.member_of(project)
        self.seed("repo note", TEXT, scope="repo", repo=self.repo, embed=False)
        return project, me

    async def test_without_a_repository_source_there_is_no_repo_memory(self):
        _, me = await self.setup_member()
        self.assertEqual((await self.retrieve(me, QUERY)).hits, ())

    async def test_an_inheriting_repository_follows_the_project_role(self):
        project, me = await self.setup_member()
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls([RepoAcl.inherit(self.repo, project)])
        )
        self.assertEqual(
            titles(await self.retrieve(me, QUERY, retriever=retriever)), ["repo note"]
        )

    async def test_an_override_decides_by_its_read_permission(self):
        project, me = await self.setup_member()
        cases = [
            ({RepoPermission.READ}, ["repo note"]),
            (
                {RepoPermission.READ, RepoPermission.WRITE, RepoPermission.AGENT},
                ["repo note"],
            ),
            (set(), []),  # access denied
            ({RepoPermission.WRITE}, []),  # can write but not read
            ({RepoPermission.AGENT}, []),
        ]
        for allowed, expected in cases:
            with self.subTest(allowed=sorted(p.value for p in allowed)):
                acl = RepoAcl.override(self.repo, project, allowed)
                retriever = self.new_retriever(repo_acls=StaticRepoAcls([acl]))
                result = await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(titles(result), expected)

    async def test_an_override_never_widens_what_the_project_role_gives(self):
        project = self.seed_project()
        outsider = Principal(self.seed_user(), SystemRole.USER)
        self.seed("repo note", TEXT, scope="repo", repo=self.repo, embed=False)
        acl = RepoAcl.override(self.repo, project, set(RepoPermission))
        source = StaticRepoAcls([acl])
        retriever = self.new_retriever(repo_acls=source)
        self.assertEqual(
            (await self.retrieve(outsider, QUERY, retriever=retriever)).hits, ()
        )
        # The source is only asked for projects the user can read: none here.
        self.assertEqual(source.calls, [])

    async def test_a_repository_of_a_project_the_user_cannot_read_is_ignored(self):
        mine, theirs = self.seed_project(), self.seed_project()
        me = self.member_of(mine)
        self.seed("repo note", TEXT, scope="repo", repo=self.repo, embed=False)
        source = StaticRepoAcls([RepoAcl.inherit(self.repo, theirs)])
        retriever = self.new_retriever(repo_acls=source)
        self.assertEqual((await self.retrieve(me, QUERY, retriever=retriever)).hits, ())
        self.assertEqual(source.calls, [(me.user_id, frozenset({mine}))])

    async def test_an_archived_project_keeps_its_repo_memory_readable(self):
        project = self.seed_project(ProjectStatus.ARCHIVED)
        me = self.member_of(project)
        self.seed("repo note", TEXT, scope="repo", repo=self.repo, embed=False)
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls([RepoAcl.inherit(self.repo, project)])
        )
        self.assertEqual(
            titles(await self.retrieve(me, QUERY, retriever=retriever)), ["repo note"]
        )

    async def test_a_repository_the_source_does_not_list_stays_invisible(self):
        project, me = await self.setup_member()
        other = self.new_repo_id()
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls([RepoAcl.inherit(other, project)])
        )
        self.assertEqual((await self.retrieve(me, QUERY, retriever=retriever)).hits, ())

    async def test_a_misbehaving_repository_source_fails_the_call_closed(self):
        project, me = await self.setup_member()
        good = RepoAcl.inherit(self.repo, project)
        bad_answers = [
            RuntimeError("boom secret-connection-string"),
            "not a list",
            None,
            [good, good],  # the same repository twice
            [good, "x"],
            [RepoAcl.inherit(uuid4(), project)] * 2,
            [
                RepoAcl.inherit(uuid4(), project)
                for _ in range(retrieval_limits.MAX_REPOS_PER_CALL + 1)
            ],
        ]
        for answer in bad_answers:
            with self.subTest(answer=type(answer).__name__):
                retriever = self.new_retriever(repo_acls=StaticRepoAcls(answer))
                with self.assertRaises(RetrievalSourceError) as caught:
                    await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(caught.exception.component, Component.REPO_ACL_SOURCE)
                self.assertNotIn("secret", str(caught.exception))


@requires_postgres
class ProjectGroupScopeTest(PostgresRetrievalTestCase):
    async def test_group_memory_is_read_only_for_the_groups_the_source_names(self):
        me = self.user()
        mine, other = uuid4(), uuid4()
        self.seed("mine", TEXT, scope="project_group", group=mine, embed=False)
        self.seed("other", TEXT, scope="project_group", group=other, embed=False)
        self.assertEqual((await self.retrieve(me, QUERY)).hits, ())  # no source
        retriever = self.new_retriever(project_groups=StaticGroups({mine}))
        self.assertEqual(
            titles(await self.retrieve(me, QUERY, retriever=retriever)), ["mine"]
        )
        none = self.new_retriever(project_groups=StaticGroups([]))
        self.assertEqual((await self.retrieve(me, QUERY, retriever=none)).hits, ())

    async def test_project_membership_alone_does_not_open_a_group(self):
        project = self.seed_project()
        me = self.member_of(project)
        self.seed("group", TEXT, scope="project_group", group=uuid4(), embed=False)
        retriever = self.new_retriever(project_groups=StaticGroups([]))
        self.assertEqual((await self.retrieve(me, QUERY, retriever=retriever)).hits, ())

    async def test_a_misbehaving_group_source_fails_the_call_closed(self):
        me = self.user()
        bad_answers = [
            RuntimeError("boom"),
            "not-a-collection",
            {"not-a-uuid"},
            [uuid4()] * 2 + ["x"],
            [uuid4() for _ in range(retrieval_limits.MAX_PROJECT_GROUPS + 1)],
            None,
        ]
        for answer in bad_answers:
            with self.subTest(answer=repr(answer)[:30]):
                retriever = self.new_retriever(project_groups=StaticGroups(answer))
                with self.assertRaises(RetrievalSourceError) as caught:
                    await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(
                    caught.exception.component, Component.PROJECT_GROUP_SOURCE
                )

    async def test_the_group_source_is_only_asked_when_group_scope_is_wanted(self):
        me = self.user()
        source = StaticGroups({uuid4()})
        retriever = self.new_retriever(project_groups=source)
        await self.retrieve(me, QUERY, retriever=retriever, scopes=["user"])
        self.assertEqual(source.calls, [])
        await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(source.calls, [me.user_id])


@requires_postgres
class AuditTest(PostgresRetrievalTestCase):
    async def test_a_default_call_records_only_the_memory_use_decision(self):
        project = self.seed_project()
        me = self.member_of(project)
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        self.seed("shared", TEXT, scope="shared", embed=False)
        self.seed("project", TEXT, scope="project", project=project, embed=False)
        result = await self.retrieve(me, QUERY)
        self.assertEqual(sorted(titles(result)), ["mine", "project", "shared"])
        (event,) = self.sink.events
        self.assertEqual((event.action, event.decision), ("memory.use", "allow"))
        self.assertEqual(event.actor_id, me.user_id)

    async def test_shared_and_project_reads_write_nothing_when_allowed(self):
        project = self.seed_project()
        me = self.member_of(project)
        self.seed("shared", TEXT, scope="shared", embed=False)
        self.seed("project", TEXT, scope="project", project=project, embed=False)
        result = await self.retrieve(me, QUERY, scopes=["shared", "project"])
        self.assertEqual(len(result.hits), 2)
        self.assertEqual(self.sink.events, [])

    async def test_a_denied_shared_read_is_recorded_as_a_denial(self):
        system = Principal(self.seed_user(), SystemRole.SYSTEM)
        await self.retrieve(system, QUERY, scopes=["shared"])
        (event,) = self.sink.events
        self.assertEqual((event.action, event.decision), ("shared_memory.read", "deny"))

    async def test_no_audit_event_carries_the_query_or_a_memory(self):
        me = self.user()
        self.seed("secret title", "secret content", owner=me.user_id, embed=False)
        await self.retrieve(me, "secret content")
        dumped = repr([e.model_dump() for e in self.sink.events])
        self.assertNotIn("secret", dumped)

    async def test_an_audit_failure_stops_a_call_that_needs_memory_use(self):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        failing = FailingSink()
        retriever = self.new_retriever(authorizer=Authorizer(failing, clock=self.clock))
        with self.assertRaises(RetrievalPermissionError) as caught:
            await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(caught.exception.reason, "audit_unavailable")
        self.assertEqual(failing.attempts, 1)

    async def test_an_audit_failure_does_not_block_reads_whose_allow_is_not_audited(
        self,
    ):
        project = self.seed_project()
        me = self.member_of(project)
        self.seed("shared", TEXT, scope="shared", embed=False)
        self.seed("project", TEXT, scope="project", project=project, embed=False)
        retriever = self.new_retriever(
            authorizer=Authorizer(FailingSink(), clock=self.clock)
        )
        result = await self.retrieve(
            me, QUERY, retriever=retriever, scopes=["shared", "project"]
        )
        self.assertEqual(sorted(titles(result)), ["project", "shared"])

    async def test_a_decision_that_is_not_a_decision_is_an_error(self):
        class Broken:
            async def authorize(self, *args, **kwargs):
                return True

        me = self.user()
        retriever = self.new_retriever(authorizer=Broken())
        with self.assertRaises(RetrievalPermissionError) as caught:
            await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(caught.exception.reason, "invalid_decision")


if __name__ == "__main__":
    import unittest

    unittest.main()
