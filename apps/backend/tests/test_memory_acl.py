"""ACL filtering in SQL: a principal never reads what it may not see (real PostgreSQL).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. The tests build a small world
(users, projects, repositories with an ACL override, memories in every scope,
old versions) and check, for every principal, the exact set of rows returned.
"""

import unittest
from uuid import UUID, uuid4

from sqlalchemy import insert, select, text
from sqlalchemy.orm import aliased

from paw_backend.memory.acl import (
    Principal,
    readable_conversations,
    readable_memory_versions,
)
from paw_backend.memory.models import (
    Conversation,
    MemoryEmbedding,
    MemoryRelation,
    MemoryVersion,
)

from .memory_support import MemoryDatabaseTestCase, requires_postgres

MODEL = "test-embedding-model"


class PrincipalTest(unittest.TestCase):
    def test_a_principal_needs_a_uuid_user(self):
        for user_id in (None, "alice", 7, str(uuid4())):
            with self.subTest(user_id=user_id), self.assertRaises(TypeError):
                Principal(user_id)

    def test_grants_must_be_uuids(self):
        with self.assertRaises(TypeError):
            Principal(uuid4(), project_ids=frozenset({"p1"}))
        with self.assertRaises(TypeError):
            Principal(uuid4(), repo_ids=frozenset({None}))

    def test_project_group_grants_must_be_uuids(self):
        with self.assertRaises(TypeError):
            Principal(uuid4(), project_group_ids=frozenset({"development"}))

    def test_grants_are_normalised_to_frozen_sets(self):
        project, group = uuid4(), uuid4()
        principal = Principal(
            uuid4(),
            project_ids=[project, project],
            repo_ids=(),
            project_group_ids=[group],
        )
        self.assertEqual(principal.project_ids, frozenset({project}))
        self.assertEqual(principal.repo_ids, frozenset())
        self.assertEqual(principal.project_group_ids, frozenset({group}))
        with self.assertRaises(AttributeError):
            principal.user_id = uuid4()


@requires_postgres
class AclFilterTest(MemoryDatabaseTestCase):
    """Alice and Bob share project P1; Bob is barred from repo R2 (ACL override)."""

    def setUp(self) -> None:
        super().setUp()
        self.alice, self.bob, self.carol, self.dave = (uuid4() for _ in range(4))
        self.p1, self.p2 = uuid4(), uuid4()
        # Project groups (for example "development projects") of the caller.
        self.g1, self.g2 = uuid4(), uuid4()
        self.r1, self.r2, self.r3 = uuid4(), uuid4(), uuid4()
        self.embeddings: dict[str, list[float]] = {}
        self.register_embedding_model(MODEL, 3)

        def memory(title: str, *, versions=None, **columns) -> None:
            """One memory; ``versions`` lists extra (title, columns) older rows."""
            memory_id = self.add_memory()
            number = 1
            for old_title, old_columns in versions or []:
                self.add_version(
                    memory_id,
                    version_number=number,
                    title=old_title,
                    status="superseded",
                    **old_columns,
                )
                self.embed(old_title)
                number += 1
            self.add_version(memory_id, version_number=number, title=title, **columns)
            self.embed(title)

        memory(
            "alice-private",
            owner_user_id=self.alice,
            versions=[("alice-private-old", {"owner_user_id": self.alice})],
        )
        memory("bob-private", owner_user_id=self.bob)
        memory("carol-private", owner_user_id=self.carol)
        memory("p1-decision", scope="project", project_id=self.p1)
        memory("p2-decision", scope="project", project_id=self.p2)
        memory("g1-preference", scope="project_group", project_group_id=self.g1)
        memory("g2-preference", scope="project_group", project_group_id=self.g2)
        memory("r1-repo", scope="repo", repo_id=self.r1)
        memory("r2-repo", scope="repo", repo_id=self.r2)
        memory("r3-repo", scope="repo", repo_id=self.r3)
        memory(
            "shared-knowledge",
            scope="shared",
            versions=[("shared-history", {"scope": "shared"})],
        )
        # Widened from user to project: the private version stays private.
        memory(
            "widened-project",
            scope="project",
            project_id=self.p1,
            versions=[("widened-user", {"owner_user_id": self.alice})],
        )
        self.session.flush()

        self.principals = {
            "alice": Principal(self.alice, {self.p1}, {self.r1, self.r2}, {self.g1}),
            "bob": Principal(self.bob, {self.p1}, {self.r1}),  # barred from R2
            # Bob again, with a project group the caller resolved for him.
            "bob_in_group": Principal(self.bob, {self.p1}, {self.r1}, {self.g1}),
            "carol": Principal(self.carol, {self.p2}, {self.r3}, {self.g2}),
            "dave": Principal(self.dave),  # a member of nothing
            "member_without_repos": Principal(uuid4(), {self.p1}),
        }

    def embed(self, title: str) -> None:
        # A distinct vector per title; the exact values do not matter here.
        vector = [float(len(self.embeddings) + 1), 1.0, 0.0]
        self.embeddings[title] = vector
        version = self.session.execute(
            select(MemoryVersion.id).where(MemoryVersion.title == title)
        ).scalar_one_or_none()
        if version is not None:
            self.store_embedding(version, vector)

    def store_embedding(self, version: UUID, vector: list[float]) -> None:
        self.session.execute(
            insert(MemoryEmbedding).values(
                memory_version_id=version,
                embedding_model_id=MODEL,
                dimensions=len(vector),
                embedding=vector,
            )
        )

    def visible(self, name: str, *, only_active: bool = False) -> set[str]:
        query = select(MemoryVersion.title).where(
            readable_memory_versions(self.principals[name])
        )
        if only_active:
            query = query.where(MemoryVersion.status == "active")
        return set(self.session.execute(query).scalars())

    # -- what each principal sees -------------------------------------------

    def test_each_principal_sees_exactly_its_own_rows_in_every_scope(self):
        shared = {"shared-knowledge", "shared-history"}
        expected = {
            "alice": shared
            | {
                "alice-private",
                "alice-private-old",
                "widened-user",
                "widened-project",
                "p1-decision",
                "g1-preference",
                "r1-repo",
                "r2-repo",
            },
            "bob": shared
            | {"bob-private", "widened-project", "p1-decision", "r1-repo"},
            "bob_in_group": shared
            | {
                "bob-private",
                "widened-project",
                "p1-decision",
                "g1-preference",
                "r1-repo",
            },
            "carol": shared
            | {"carol-private", "p2-decision", "g2-preference", "r3-repo"},
            "dave": shared,
            "member_without_repos": shared | {"widened-project", "p1-decision"},
        }
        for name, titles in expected.items():
            with self.subTest(name):
                self.assertEqual(self.visible(name), titles)

    def test_the_active_filter_keeps_old_versions_out_of_normal_retrieval(self):
        self.assertEqual(
            self.visible("alice", only_active=True),
            {
                "alice-private",
                "widened-project",
                "p1-decision",
                "g1-preference",
                "r1-repo",
                "r2-repo",
                "shared-knowledge",
            },
        )

    def test_a_repo_acl_override_hides_that_repo_from_a_project_member(self):
        bob = self.visible("bob")

        self.assertIn("p1-decision", bob)  # Bob is a member of the project ...
        self.assertIn("r1-repo", bob)
        self.assertNotIn("r2-repo", bob)  # ... but not allowed into R2

    def test_widening_a_memory_does_not_expose_its_private_versions(self):
        self.assertIn("widened-project", self.visible("bob"))
        self.assertNotIn("widened-user", self.visible("bob"))
        self.assertIn("widened-user", self.visible("alice"))

    def test_nothing_is_visible_beyond_the_expected_rows(self):
        # Cross-check against a plain Python statement of the rules, row by row.
        rows = self.session.execute(
            select(
                MemoryVersion.title,
                MemoryVersion.scope,
                MemoryVersion.owner_user_id,
                MemoryVersion.project_id,
                MemoryVersion.project_group_id,
                MemoryVersion.repo_id,
            )
        ).all()
        self.assertEqual(len(rows), 15)

        def may_read(principal: Principal, row) -> bool:
            if row.scope == "user":
                return row.owner_user_id == principal.user_id
            if row.scope == "project":
                return row.project_id in principal.project_ids
            if row.scope == "project_group":
                return row.project_group_id in principal.project_group_ids
            if row.scope == "repo":
                return row.repo_id in principal.repo_ids
            return row.scope == "shared"

        for name, principal in self.principals.items():
            with self.subTest(name):
                self.assertEqual(
                    self.visible(name),
                    {row.title for row in rows if may_read(principal, row)},
                )

    def test_an_id_of_one_kind_never_grants_another_kind(self):
        # Alice's user id, used as a project, repo or group id, opens nothing of
        # hers; R2 used as a project or group id does not open R2's repo memory;
        # the project group G1 used as a project or repo id does not open the
        # group's memory, and P1 used as a group id does not open the project's.
        confused = Principal(
            self.dave,
            project_ids={self.alice, self.r2, self.g1},
            repo_ids={self.p1, self.alice, self.g1},
            project_group_ids={self.p1, self.r2, self.alice},
        )

        titles = set(
            self.session.execute(
                select(MemoryVersion.title).where(readable_memory_versions(confused))
            ).scalars()
        )

        self.assertEqual(titles, {"shared-knowledge", "shared-history"})

    def test_the_condition_can_filter_an_aliased_join(self):
        older, newer = aliased(MemoryVersion), aliased(MemoryVersion)
        self.session.execute(
            insert(MemoryRelation).values(
                from_version_id=select(MemoryVersion.id)
                .where(MemoryVersion.title == "p1-decision")
                .scalar_subquery(),
                to_version_id=select(MemoryVersion.id)
                .where(MemoryVersion.title == "alice-private")
                .scalar_subquery(),
                relation_type="confirmed_from",
            )
        )

        def graph(name: str) -> list[tuple[str, str]]:
            principal = self.principals[name]
            rows = self.session.execute(
                select(newer.title, older.title)
                .select_from(MemoryRelation)
                .join(newer, newer.id == MemoryRelation.from_version_id)
                .join(older, older.id == MemoryRelation.to_version_id)
                .where(
                    readable_memory_versions(principal, newer),
                    readable_memory_versions(principal, older),
                )
            )
            return [tuple(row) for row in rows]

        # A project memory confirmed from Alice's private preference: Bob sees
        # the project memory but not the edge to the private one.
        self.assertEqual(graph("alice"), [("p1-decision", "alice-private")])
        self.assertEqual(graph("bob"), [])

    # -- vector search ------------------------------------------------------

    def nearest(self, name: str, query: list[float], limit: int = 20) -> list[str]:
        principal = self.principals[name]
        distance = MemoryEmbedding.embedding.l2_distance(query)
        rows = self.session.execute(
            select(MemoryVersion.title)
            .join(
                MemoryEmbedding, MemoryEmbedding.memory_version_id == MemoryVersion.id
            )
            .where(
                MemoryEmbedding.embedding_model_id == MODEL,
                readable_memory_versions(principal),
                MemoryVersion.status == "active",
            )
            .order_by(distance, MemoryVersion.title)
            .limit(limit)
        )
        return list(rows.scalars())

    def test_vector_search_never_returns_a_row_the_principal_cannot_see(self):
        # The query is exactly Alice's private memory: the nearest row of all.
        query = self.embeddings["alice-private"]

        for name in self.principals:
            with self.subTest(name):
                found = set(self.nearest(name, query))
                self.assertEqual(found, self.visible(name, only_active=True))

        self.assertEqual(self.nearest("alice", query)[0], "alice-private")
        self.assertNotIn("alice-private", self.nearest("bob", query))
        self.assertNotIn("alice-private", self.nearest("carol", query))
        self.assertNotIn("alice-private", self.nearest("dave", query))

    def test_vector_search_ranks_within_what_the_principal_may_see(self):
        # Carol's private memory is the nearest for Carol; for Bob the same
        # query ranks only rows Bob can see, so the top hit changes.
        query = self.embeddings["carol-private"]

        self.assertEqual(self.nearest("carol", query, limit=1), ["carol-private"])
        bob_top = self.nearest("bob", query, limit=1)
        self.assertEqual(len(bob_top), 1)
        self.assertNotEqual(bob_top, ["carol-private"])
        self.assertIn(bob_top[0], self.visible("bob", only_active=True))

    # -- conversations ------------------------------------------------------

    def test_raw_conversations_are_private_to_their_owner(self):
        owned = self.add_conversation(owner_user_id=self.alice, project_id=self.p1)

        def readable_by(name: str) -> list[UUID]:
            principal = self.principals[name]
            return list(
                self.session.execute(
                    select(Conversation.id).where(readable_conversations(principal))
                ).scalars()
            )

        self.assertEqual(readable_by("alice"), [owned])
        # Bob shares the project and Carol may be an Admin elsewhere: neither
        # gets Alice's raw conversation from this condition.
        self.assertEqual(readable_by("bob"), [])
        self.assertEqual(readable_by("carol"), [])

    # -- indexes ------------------------------------------------------------

    def test_the_acl_condition_is_served_by_the_per_scope_indexes(self):
        # A realistic amount of data (the planner ignores indexes on tiny tables).
        self.connection.execute(
            text(
                "INSERT INTO memories (id)"
                " SELECT gen_random_uuid() FROM generate_series(1, 6000)"
            )
        )
        self.connection.execute(
            text(
                """
                WITH numbered AS (
                    SELECT id, row_number() OVER () AS n
                    FROM memories
                    WHERE id NOT IN (SELECT memory_id FROM memory_versions)
                ),
                owners AS (
                    SELECT array_agg(gen_random_uuid()) AS ids
                    FROM generate_series(1, 200)),
                projects AS (
                    SELECT array_agg(gen_random_uuid()) AS ids
                    FROM generate_series(1, 100)),
                groups AS (
                    SELECT array_agg(gen_random_uuid()) AS ids
                    FROM generate_series(1, 20)),
                repos AS (
                    SELECT array_agg(gen_random_uuid()) AS ids
                    FROM generate_series(1, 100))
                INSERT INTO memory_versions
                    (memory_id, version_number, scope, owner_user_id, project_id,
                     project_group_id, repo_id, memory_type, title, content,
                     status, confirmation_state, freshness_policy, actor_type)
                SELECT numbered.id, 1,
                    CASE numbered.n % 5 WHEN 0 THEN 'user' WHEN 1 THEN 'project'
                        WHEN 2 THEN 'project_group' WHEN 3 THEN 'repo'
                        ELSE 'shared' END,
                    CASE WHEN numbered.n % 5 = 0
                        THEN owners.ids[1 + numbered.n % 200] END,
                    CASE WHEN numbered.n % 5 = 1
                        THEN projects.ids[1 + numbered.n % 100] END,
                    CASE WHEN numbered.n % 5 = 2
                        THEN groups.ids[1 + numbered.n % 20] END,
                    CASE WHEN numbered.n % 5 = 3
                        THEN repos.ids[1 + numbered.n % 100] END,
                    'note', 'bulk', 'bulk',
                    CASE WHEN numbered.n % 7 = 0 THEN 'superseded' ELSE 'active' END,
                    'confirmed', 'permanent', 'system'
                FROM numbered, owners, projects, groups, repos
                """
            )
        )
        self.connection.execute(text("ANALYZE memory_versions"))
        principal = self.principals["alice"]
        query = select(MemoryVersion.title).where(
            readable_memory_versions(principal), MemoryVersion.status == "active"
        )
        compiled = query.compile(
            self.engine, compile_kwargs={"literal_binds": True}
        ).string

        plan = "\n".join(self.connection.execute(text("EXPLAIN " + compiled)).scalars())

        for index in (
            "ix_memory_versions_owner_user_id_status",
            "ix_memory_versions_project_id_status",
            "ix_memory_versions_project_group_id_status",
            "ix_memory_versions_repo_id_status",
            "ix_memory_versions_shared_status",
        ):
            with self.subTest(index):
                self.assertIn(f"Bitmap Index Scan on {index}", plan)


if __name__ == "__main__":
    unittest.main()
