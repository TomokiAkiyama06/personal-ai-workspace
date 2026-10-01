"""The Memory Board read model on a real PostgreSQL (issue #186, Decision 0068).

What the Memory screen reads: the Scope Tree with counts, the current version of
each memory of a scope (with a search), the History Graph of one memory and the
sources of one version. The point of most tests is what a reader must NOT see:
another person's private memory, a project or a repository the reader is not
allowed to read, an older version of a memory whose audience changed, an edge to a
version the reader may not see, a deleted shared memory.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from paw_backend.authz import Principal, ProjectRole, SystemRole
from paw_backend.db import Database
from paw_backend.memory import metadata_change_actor
from paw_backend.memory.board import (
    BoardScope,
    BoardScopeKind,
    MemoryBoard,
)
from paw_backend.memory.board import limits as board_limits
from paw_backend.memory.models import ActorType, MemoryScope
from paw_backend.memory.versioning import (
    InvalidMemoryInputError,
    MemoryChanges,
    MemoryDraft,
    MemoryNotFoundError,
    MemoryPermissionError,
)
from paw_backend.projects import MemberStatus, ProjectStatus

from .projects_support import T0
from .support import make_settings
from .versioning_support import PostgresVersioningTestCase, requires_postgres

USER = BoardScope(BoardScopeKind.USER)
SHARED = BoardScope(BoardScopeKind.SHARED)


def project_scope(project_id: UUID) -> BoardScope:
    return BoardScope(BoardScopeKind.PROJECT, project_id)


def repo_scope(project_id: UUID, repo_id: UUID) -> BoardScope:
    return BoardScope(BoardScopeKind.REPO, project_id, repo_id)


def titles(found) -> list[str]:
    return [entry.version.title for entry in found.memories]


class BoardTestCase(PostgresVersioningTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        database = Database(make_settings(database_url=self.database_url()))
        self.addAsyncCleanup(database.dispose)
        self.board = MemoryBoard(database, self.authorizer)

    def seed_repo(
        self, project_id: UUID, name: str = "backend", acl: list[str] | None = None
    ) -> UUID:
        repo_id = uuid4()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repositories (id, project_id, name, default_branch,"
                    " source, acl_allowed, created_at, updated_at) VALUES (:id,"
                    " :project, :name, 'main', 'new_local', :acl, :now, :now)"
                ),
                {
                    "id": repo_id,
                    "project": project_id,
                    "name": name,
                    "acl": acl,
                    "now": T0,
                },
            )
        return repo_id

    def seed_source(self, version_id: UUID, **values: Any) -> None:
        values.setdefault("source_type", "task")
        values.setdefault("source_ref", "task-1")
        values.setdefault("created_at", T0)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memory_sources (memory_version_id, "
                    + ", ".join(values)
                    + ") VALUES (:v, "
                    + ", ".join(f":{name}" for name in values)
                    + ")"
                ),
                {"v": version_id, **values},
            )

    def seed_new_version(self, memory_id: UUID, number: int, **values: Any):
        """Version ``number`` of ``memory_id`` (the older one is superseded)."""
        with self.engine.begin() as connection:
            connection.execute(metadata_change_actor(ActorType.SYSTEM))
            connection.execute(
                text(
                    "UPDATE memory_versions SET status = 'superseded'"
                    " WHERE memory_id = :m AND status = 'active'"
                ),
                {"m": memory_id},
            )
        return self.seed(
            values.pop("title", f"v{number}"),
            memory_id=memory_id,
            version_number=number,
            embed=False,
            **values,
        )

    def audit_rows(self) -> list[tuple[str, str]]:
        return [(event.action, event.decision) for event in self.sink.events]


@requires_postgres
class ScopeTreeTest(BoardTestCase):
    async def test_counts_what_the_reader_may_see(self):
        me = self.user()
        other = self.user()
        project = self.seed_project(name="Example")
        self.seed_member(project, me.user_id, role=ProjectRole.VIEWER)
        repo = self.seed_repo(project, "backend")
        hidden_repo = self.seed_repo(project, "secrets", acl=[])
        foreign = self.seed_project(name="Foreign")
        foreign_repo = self.seed_repo(foreign, "foreign")
        self.seed("mine 1", owner=me.user_id, embed=False)
        self.seed("mine 2", owner=me.user_id, embed=False, status="deprecated")
        self.seed("theirs", owner=other.user_id, embed=False)
        self.seed("project", scope="project", project=project, embed=False)
        self.seed("repo", scope="repo", repo=repo, embed=False)
        self.seed("repo 2", scope="repo", repo=repo, embed=False)
        self.seed("hidden repo", scope="repo", repo=hidden_repo, embed=False)
        self.seed("foreign", scope="project", project=foreign, embed=False)
        self.seed("foreign repo", scope="repo", repo=foreign_repo, embed=False)
        self.seed("shared", scope="shared", embed=False)
        self.seed("deleted shared", scope="shared", status="deprecated", embed=False)

        tree = await self.board.scopes(me)

        self.assertEqual(tree.user, 2)
        self.assertEqual(tree.shared, 1)
        (example,) = tree.projects
        self.assertEqual(
            (example.project_id, example.name, example.count, example.project_count),
            (project, "Example", 3, 1),
        )
        self.assertEqual(
            [(r.repo_id, r.name, r.count) for r in example.repos],
            [(repo, "backend", 2)],
        )
        # A permitted read writes no audit row (Decision 0024: DENIED_ONLY); the
        # repository whose override removes ``read`` is a recorded denial.
        self.assertEqual(self.audit_rows(), [("project.read", "deny")])

    async def test_a_memory_counts_where_its_current_version_is(self):
        me = self.user()
        project = self.seed_project()
        self.seed_member(project, me.user_id)
        widened = self.seed("private first", owner=me.user_id, embed=False)
        self.seed_new_version(
            widened.memory_id, 2, scope="project", project=project, title="project now"
        )
        tree = await self.board.scopes(me)
        self.assertEqual(tree.user, 0)
        self.assertEqual(tree.projects[0].project_count, 1)

    async def test_invitations_and_unreadable_projects_are_not_scopes(self):
        me = self.user()
        invited = self.seed_project(name="Invited")
        self.seed_member(invited, me.user_id, status=MemberStatus.INVITED)
        pending = self.seed_project(ProjectStatus.PENDING_DELETION, name="Pending")
        self.seed_member(pending, me.user_id)
        archived = self.seed_project(ProjectStatus.ARCHIVED, name="Archived")
        self.seed_member(archived, me.user_id)
        tree = await self.board.scopes(me)
        self.assertEqual([p.name for p in tree.projects], ["Archived"])

    async def test_the_backend_identity_reads_nothing(self):
        with self.assertRaises(MemoryPermissionError):
            await self.board.scopes(Principal(uuid4(), SystemRole.SYSTEM))
        self.assertEqual(self.audit_rows(), [])


@requires_postgres
class ListTest(BoardTestCase):
    async def test_lists_the_current_version_of_each_memory_of_the_scope(self):
        me = self.user()
        first = self.seed("first", owner=me.user_id, embed=False)
        self.seed_new_version(first.memory_id, 2, owner=me.user_id, title="second")
        self.seed("pinned", owner=me.user_id, pinned=True, embed=False)
        self.seed("theirs", owner=self.user().user_id, embed=False)
        found = await self.board.list_memories(me, USER)
        self.assertEqual(titles(found), ["pinned", "second"])
        self.assertFalse(found.truncated)
        self.assertEqual(found.memories[1].version.version_number, 2)

    async def test_a_project_or_repository_the_reader_may_not_read_is_not_found(self):
        me = self.user()
        project = self.seed_project()
        repo = self.seed_repo(project)
        self.seed("secret", scope="project", project=project, embed=False)
        with self.assertRaises(MemoryNotFoundError):
            await self.board.list_memories(me, project_scope(project))
        with self.assertRaises(MemoryNotFoundError):
            await self.board.list_memories(me, repo_scope(project, repo))
        with self.assertRaises(MemoryNotFoundError):
            await self.board.list_memories(me, project_scope(uuid4()))

    async def test_a_repository_is_listed_under_its_own_project_only(self):
        me = self.user()
        project = self.seed_project()
        self.seed_member(project, me.user_id)
        other = self.seed_project()
        self.seed_member(other, me.user_id)
        repo = self.seed_repo(project)
        self.seed("repo memory", scope="repo", repo=repo, embed=False)
        found = await self.board.list_memories(me, repo_scope(project, repo))
        self.assertEqual(titles(found), ["repo memory"])
        with self.assertRaises(MemoryNotFoundError):
            await self.board.list_memories(me, repo_scope(other, repo))

    async def test_a_repository_override_without_read_hides_it(self):
        me = self.user()
        project = self.seed_project()
        self.seed_member(project, me.user_id)
        repo = self.seed_repo(project, acl=["write"])
        self.seed("repo memory", scope="repo", repo=repo, embed=False)
        with self.assertRaises(MemoryNotFoundError):
            await self.board.list_memories(me, repo_scope(project, repo))

    async def test_the_search_covers_title_content_and_source_references(self):
        me = self.user()
        self.seed("deploy day", "friday", owner=me.user_id, embed=False)
        self.seed("lint", "use ruff", owner=me.user_id, embed=False)
        sourced = self.seed("other", "nothing", owner=me.user_id, embed=False)
        self.seed_source(sourced.version_id, source_ref="Task-DEPLOY-7")
        self.seed("100% sure", "x", owner=me.user_id, embed=False)
        found = await self.board.list_memories(me, USER, "Deploy")
        self.assertEqual(sorted(titles(found)), ["deploy day", "other"])
        found = await self.board.list_memories(me, USER, "RUFF")
        self.assertEqual(titles(found), ["lint"])
        # LIKE wildcards are literal characters.
        self.assertEqual(
            titles(await self.board.list_memories(me, USER, "%")), ["100% sure"]
        )
        self.assertEqual(titles(await self.board.list_memories(me, USER, "_")), [])
        # A blank search lists everything.
        self.assertEqual(
            len((await self.board.list_memories(me, USER, " ")).memories), 4
        )

    async def test_the_search_is_bounded(self):
        me = self.user()
        with self.assertRaises(InvalidMemoryInputError):
            await self.board.list_memories(
                me, USER, "x" * (board_limits.MAX_QUERY_CHARS + 1)
            )

    async def test_a_long_list_is_truncated(self):
        me = self.user()
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "WITH m AS (INSERT INTO memories (created_at)"
                    " SELECT :t FROM generate_series(1, :n) RETURNING id)"
                    " INSERT INTO memory_versions (memory_id, version_number, scope,"
                    " owner_user_id, memory_type, title, content, status,"
                    " confirmation_state, freshness_policy, actor_type, created_at)"
                    " SELECT id, 1, 'user', :owner, 'note', 'bulk', 'bulk', 'active',"
                    " 'confirmed', 'permanent', 'system', :t FROM m"
                ),
                {"t": T0, "n": board_limits.MAX_LIST_ITEMS + 1, "owner": me.user_id},
            )
        found = await self.board.list_memories(me, USER)
        self.assertEqual(len(found.memories), board_limits.MAX_LIST_ITEMS)
        self.assertTrue(found.truncated)

    async def test_deleted_shared_memories_are_not_listed(self):
        me = self.user()
        self.seed("shared", scope="shared", embed=False)
        self.seed("deleted", scope="shared", status="deprecated", embed=False)
        self.assertEqual(titles(await self.board.list_memories(me, SHARED)), ["shared"])

    async def test_the_writer_is_named(self):
        me = self.user()
        created = await self.versioning.create_memory(
            me,
            MemoryDraft(
                scope=MemoryScope.USER, memory_type="note", title="t", content="c"
            ),
        )
        self.seed("by the system", owner=me.user_id, embed=False)
        found = await self.board.list_memories(me, USER)
        names = {e.version.memory_id: e.actor_name for e in found.memories}
        login = self.rows("SELECT login_name FROM users WHERE id = :u", u=me.user_id)
        self.assertEqual(names[created.memory_id], login[0].login_name)
        self.assertEqual(sorted(names.values(), key=str)[0:1], [None])
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET status = 'deleted' WHERE id = :u"),
                {"u": me.user_id},
            )
        # A deleted account is not named (nor can it read anything any more,
        # but the board is asked with its principal directly here).
        found = await self.board.list_memories(me, USER)
        self.assertEqual({e.actor_name for e in found.memories}, {None})


@requires_postgres
class HistoryTest(BoardTestCase):
    async def test_versions_relations_and_related_versions(self):
        me = self.user()
        project = self.seed_project()
        self.seed_member(project, me.user_id)
        v1 = self.seed("v1", scope="project", project=project, embed=False)
        v2 = self.seed_new_version(v1.memory_id, 2, scope="project", project=project)
        self.seed_relation(v2.version_id, v1.version_id, "supersedes")
        observation = self.seed("seen", scope="project", project=project, embed=False)
        self.seed_relation(v2.version_id, observation.version_id, "extends")
        # An edge to another person's private memory is left out.
        private = self.seed("private", owner=self.user().user_id, embed=False)
        self.seed_relation(v2.version_id, private.version_id, "conflicts_with")

        history = await self.board.history(me, v1.memory_id)

        self.assertEqual([e.version.version_number for e in history.versions], [1, 2])
        self.assertEqual(
            [
                (r.from_version_id, r.to_version_id, r.relation.value)
                for r in history.relations
            ],
            [
                (v2.version_id, v1.version_id, "supersedes"),
                (v2.version_id, observation.version_id, "extends"),
            ],
        )
        self.assertEqual(
            [e.version.version_id for e in history.related], [observation.version_id]
        )
        self.assertTrue(history.can_write)

    async def test_a_viewer_reads_but_cannot_write(self):
        project = self.seed_project()
        viewer = self.member_of(project, ProjectRole.VIEWER)
        memory = self.seed("rule", scope="project", project=project, embed=False)
        history = await self.board.history(viewer, memory.memory_id)
        self.assertFalse(history.can_write)
        self.set_project(project, status="archived")
        contributor = self.member_of(project, ProjectRole.CONTRIBUTOR)
        self.assertFalse(
            (await self.board.history(contributor, memory.memory_id)).can_write
        )

    async def test_repo_and_shared_memories_are_read_only_here(self):
        me = self.user()
        project = self.seed_project()
        self.seed_member(project, me.user_id, role=ProjectRole.MANAGER)
        repo = self.seed_repo(project)
        repo_memory = self.seed("repo", scope="repo", repo=repo, embed=False)
        shared = self.seed("shared", scope="shared", embed=False)
        self.assertFalse(
            (await self.board.history(me, repo_memory.memory_id)).can_write
        )
        self.assertFalse((await self.board.history(me, shared.memory_id)).can_write)

    async def test_somebody_elses_memory_is_not_found(self):
        me = self.user()
        theirs = self.seed("theirs", owner=self.user().user_id, embed=False)
        project = self.seed_project()
        foreign = self.seed("foreign", scope="project", project=project, embed=False)
        deleted = self.seed("deleted", scope="shared", status="deprecated", embed=False)
        for memory_id in (
            theirs.memory_id,
            foreign.memory_id,
            deleted.memory_id,
            uuid4(),
        ):
            with self.assertRaises(MemoryNotFoundError):
                await self.board.history(me, memory_id)

    async def test_an_older_audience_is_shown_to_its_readers_only(self):
        owner = self.user()
        project = self.seed_project()
        self.seed_member(project, owner.user_id)
        member = self.member_of(project)
        widened = self.seed("private draft", owner=owner.user_id, embed=False)
        self.seed_new_version(
            widened.memory_id, 2, scope="project", project=project, title="shared rule"
        )
        seen = await self.board.history(member, widened.memory_id)
        self.assertEqual([e.version.title for e in seen.versions], ["shared rule"])
        seen = await self.board.history(owner, widened.memory_id)
        self.assertEqual(
            [e.version.title for e in seen.versions], ["private draft", "shared rule"]
        )

    async def test_a_narrowed_memory_disappears_for_the_project(self):
        project = self.seed_project()
        editor = self.member_of(project)
        member = self.member_of(project)
        created = await self.versioning.create_memory(
            editor,
            MemoryDraft(
                scope=MemoryScope.PROJECT,
                project_id=project,
                memory_type="note",
                title="t",
                content="c",
            ),
        )
        other = self.seed("linked", scope="project", project=project, embed=False)
        self.seed_relation(other.version_id, created.version_id, "extends")
        await self.versioning.edit_memory(
            editor, created.memory_id, 1, MemoryChanges(scope=MemoryScope.USER)
        )
        with self.assertRaises(MemoryNotFoundError):
            await self.board.history(member, created.memory_id)
        # Its old project version is no longer a related version of the other one.
        history = await self.board.history(member, other.memory_id)
        self.assertEqual((history.relations, history.related), ((), ()))
        self.assertEqual(
            len((await self.board.history(editor, created.memory_id)).versions), 2
        )


@requires_postgres
class SourcesTest(BoardTestCase):
    async def test_the_sources_of_a_visible_version(self):
        me = self.user()
        memory = self.seed("m", owner=me.user_id, embed=False)
        self.seed_source(memory.version_id, source_ref="task-9")
        self.seed_source(
            memory.version_id,
            source_ref="task-10",
            source_deleted_at=T0,
            created_at=T0 + timedelta(minutes=1),
        )
        sources = await self.board.sources(me, memory.memory_id, 1)
        self.assertEqual(
            [(s.source_type.value, s.source_ref, s.source_deleted_at) for s in sources],
            [("task", "task-9", None), ("task", "task-10", T0)],
        )
        with self.assertRaises(MemoryNotFoundError):
            await self.board.sources(me, memory.memory_id, 2)

    async def test_another_readers_version_is_not_found(self):
        me = self.user()
        theirs = self.seed("theirs", owner=self.user().user_id, embed=False)
        self.seed_source(theirs.version_id)
        with self.assertRaises(MemoryNotFoundError):
            await self.board.sources(me, theirs.memory_id, 1)
