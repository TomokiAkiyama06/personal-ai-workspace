"""The SQL statements of a retrieval, one by one (real PostgreSQL).

Each statement is run directly with a chosen ``ResolvedScopes``, so that the
permission prefilter of every one of them is proved on its own (the service tests
would not notice a statement that leaned on another one's filter).
"""

import random
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import text

from paw_backend.authz import ProjectRole
from paw_backend.memory.fulltext import keyword_terms, tsquery_text
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.retrieval import queries
from paw_backend.memory.retrieval.scopes import ResolvedScopes
from paw_backend.projects import MemberStatus, ProjectStatus

from .retrieval_pg_support import (
    T0,
    PostgresRetrievalTestCase,
    requires_postgres,
)

ALL = frozenset(MemoryScope)


@requires_postgres
class PrefilterTest(PostgresRetrievalTestCase):
    """A fixed world; ``visible`` is checked with different ``ResolvedScopes``."""

    def setUp(self):
        super().setUp()
        self.me, self.other = uuid4(), uuid4()
        self.p1, self.p2, self.r1, self.r2, self.g1, self.g2 = (
            uuid4() for _ in range(6)
        )

    def world(self):
        s = {}
        s["own"] = self.seed("own", "alpha", owner=self.me)
        s["other"] = self.seed("other", "alpha", owner=self.other)
        s["p1"] = self.seed("p1", "alpha", scope="project", project=self.p1)
        s["p2"] = self.seed("p2", "alpha", scope="project", project=self.p2)
        s["r1"] = self.seed("r1", "alpha", scope="repo", repo=self.r1)
        s["r2"] = self.seed("r2", "alpha", scope="repo", repo=self.r2)
        s["g1"] = self.seed("g1", "alpha", scope="project_group", group=self.g1)
        s["g2"] = self.seed("g2", "alpha", scope="project_group", group=self.g2)
        s["shared"] = self.seed("shared", "alpha", scope="shared")
        s["superseded"] = self.seed(
            "superseded", "alpha", owner=self.me, status="superseded"
        )
        s["session"] = self.seed(
            "session", "alpha", owner=self.me, freshness="session_only"
        )
        s["expired"] = self.seed(
            "expired", "alpha", owner=self.me, freshness="expiring", expires_at=T0
        )
        s["future"] = self.seed(
            "future",
            "alpha",
            owner=self.me,
            freshness="expiring",
            expires_at=T0 + timedelta(seconds=1),
        )
        return s

    def readable(self, scopes: ResolvedScopes) -> set[str]:
        statement = queries.keyword_statement(scopes, T0, "'alpha'", 100)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        return {row.title for row in rows}

    def scopes(self, **overrides) -> ResolvedScopes:
        values = {
            "user_id": self.me,
            "scopes": ALL,
            "project_ids": frozenset({self.p1}),
            "repo_ids": frozenset({self.r1}),
            "project_group_ids": frozenset({self.g1}),
        }
        values.update(overrides)
        return ResolvedScopes(**values)

    def test_the_prefilter_returns_exactly_the_readable_active_fresh_rows(self):
        self.world()
        cases = {
            "everything the ids allow": (
                self.scopes(),
                {"own", "p1", "r1", "g1", "shared", "future"},
            ),
            "no ids: only user and shared": (
                self.scopes(
                    project_ids=frozenset(),
                    repo_ids=frozenset(),
                    project_group_ids=frozenset(),
                ),
                {"own", "shared", "future"},
            ),
            "the user scope was not authorised": (
                self.scopes(scopes=ALL - {MemoryScope.USER}),
                {"p1", "r1", "g1", "shared"},
            ),
            "the shared scope was not authorised": (
                self.scopes(scopes=ALL - {MemoryScope.SHARED}),
                {"own", "p1", "r1", "g1", "future"},
            ),
            "scopes narrow ids that are present": (
                self.scopes(scopes=frozenset({MemoryScope.SHARED})),
                {"shared"},
            ),
            "two projects": (
                self.scopes(project_ids=frozenset({self.p1, self.p2})),
                {"own", "p1", "p2", "r1", "g1", "shared", "future"},
            ),
            "no scope at all": (self.scopes(scopes=frozenset()), set()),
        }
        for label, (scopes, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.readable(scopes), expected)

    def test_a_scope_that_is_not_in_the_set_hides_its_rows_even_with_ids(self):
        self.world()
        only_project = self.scopes(scopes=frozenset({MemoryScope.PROJECT}))
        self.assertEqual(self.readable(only_project), {"p1"})
        only_repo = self.scopes(scopes=frozenset({MemoryScope.REPO}))
        self.assertEqual(self.readable(only_repo), {"r1"})
        only_group = self.scopes(scopes=frozenset({MemoryScope.PROJECT_GROUP}))
        self.assertEqual(self.readable(only_group), {"g1"})

    def test_versions_statement_applies_the_same_filter(self):
        world = self.world()
        ids = [seeded.version_id for seeded in world.values()]
        with self.engine.connect() as connection:
            rows = connection.execute(
                queries.versions_statement(self.scopes(), T0, ids)
            ).all()
        self.assertEqual(
            {row.title for row in rows}, {"own", "p1", "r1", "g1", "shared", "future"}
        )

    def test_the_vector_statement_ranks_readable_rows_only(self):
        world = self.world()
        # Every row has the query's own vector; the hidden ones tie with the readable.
        vector = [1.0, 0.0, 0.0]
        with self.engine.connect() as connection:
            for seeded in world.values():
                connection.execute(
                    text("DELETE FROM memory_embeddings WHERE memory_version_id = :v"),
                    {"v": seeded.version_id},
                )
            connection.commit()
        for seeded in world.values():
            self.seed_embedding(seeded.version_id, vector, model_id="m3")
        statement = queries.vector_statement(self.scopes(), T0, "m3", 3, vector, 100)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        self.assertEqual(
            {row.title for row in rows}, {"own", "p1", "r1", "g1", "shared", "future"}
        )
        limited = queries.vector_statement(self.scopes(), T0, "m3", 3, vector, 2)
        with self.engine.connect() as connection:
            self.assertEqual(len(connection.execute(limited).all()), 2)

    def test_the_keyword_limit_counts_readable_rows_only(self):
        for n in range(5):
            self.seed(f"hidden {n}", "alpha alpha alpha alpha", owner=self.other)
        self.seed("mine", "alpha", owner=self.me)
        scopes = self.scopes()
        statement = queries.keyword_statement(scopes, T0, "'alpha'", 1)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        self.assertEqual([row.title for row in rows], ["mine"])

    def test_a_relation_is_returned_only_when_both_ends_are_readable(self):
        mine = self.seed("mine", "alpha", owner=self.me)
        mine2 = self.seed("mine2", "alpha", owner=self.me)
        hidden = self.seed("hidden", "alpha", owner=self.other)
        gone = self.seed("gone", "alpha", owner=self.me, status="superseded")
        self.seed_relation(mine.version_id, mine2.version_id)  # both readable
        self.seed_relation(mine.version_id, hidden.version_id)  # hidden older end
        self.seed_relation(hidden.version_id, mine.version_id)  # hidden newer end
        self.seed_relation(mine.version_id, gone.version_id)  # inactive end
        self.seed_relation(mine.version_id, mine2.version_id, "extends")  # wrong type
        statement = queries.relations_statement(
            self.scopes(), T0, [mine.version_id, mine2.version_id], 100
        )
        with self.engine.connect() as connection:
            rows = connection.execute(statement).all()
        self.assertEqual(
            [(r.from_version_id, r.to_version_id, r.relation_type) for r in rows],
            [(mine.version_id, mine2.version_id, "conflicts_with")],
        )

    def test_a_relation_with_a_readable_end_only_is_never_the_rows_side_channel(self):
        mine = self.seed("mine", "alpha", owner=self.me)
        hidden = [self.seed(f"h{n}", "alpha", owner=self.other) for n in range(3)]
        for seeded in hidden:
            self.seed_relation(seeded.version_id, mine.version_id)
        statement = queries.relations_statement(
            self.scopes(), T0, [mine.version_id], 100
        )
        with self.engine.connect() as connection:
            self.assertEqual(connection.execute(statement).all(), [])


@requires_postgres
class EligibilityStatementTest(PostgresRetrievalTestCase):
    """Each statement applies the eligibility conditions on its own."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.me = uuid4()
        self.scopes = ResolvedScopes(self.me, frozenset(MemoryScope))
        self.eligible = queries.Eligibility(
            exclude_stale=True, policy_subjects=("merge",)
        )
        self.keep = self.seed("keep", "alpha", owner=self.me)
        self.stale = self.seed(
            "stale", "alpha", owner=self.me, stale_since=T0 - timedelta(days=1)
        )
        self.covered = self.seed(
            "covered", "alpha", scope="shared", subjects=["merge.permission"]
        )
        self.old = self.seed("old", "alpha", owner=self.me)
        self.new = self.seed("new", "alpha", owner=self.me)
        self.seed_relation(self.new.version_id, self.old.version_id, "supersedes")
        for other in (self.stale, self.covered, self.old):
            self.seed_relation(self.keep.version_id, other.version_id)
        # The ineligible memory is the NEWER end of these two.
        self.keep_too = self.seed("keep too", "alpha", owner=self.me)
        self.seed_relation(self.stale.version_id, self.keep_too.version_id)
        self.seed_relation(self.covered.version_id, self.keep_too.version_id)

    def run_statement(self, statement):
        with self.engine.connect() as connection:
            return connection.execute(statement).all()

    def test_versions_statement_leaves_out_what_is_not_eligible(self):
        ids = [s.version_id for s in (self.keep, self.stale, self.covered, self.old)]
        everything = self.run_statement(
            queries.versions_statement(self.scopes, T0, ids)
        )
        self.assertEqual(
            {r.title for r in everything}, {"keep", "stale", "covered"}
        )  # ``old`` is superseded whatever the caller asks for
        strict = self.run_statement(
            queries.versions_statement(self.scopes, T0, ids, self.eligible)
        )
        self.assertEqual({r.title for r in strict}, {"keep"})

    def test_relations_statement_needs_both_ends_to_be_eligible(self):
        ids = [self.keep.version_id]
        lenient = self.run_statement(
            queries.relations_statement(self.scopes, T0, ids, 100)
        )
        self.assertEqual(
            {r.to_version_id for r in lenient},
            {self.stale.version_id, self.covered.version_id},
        )  # the superseded ``old`` is never an end
        strict = self.run_statement(
            queries.relations_statement(self.scopes, T0, ids, 100, self.eligible)
        )
        self.assertEqual(strict, [])
        # The other direction: the covered memory is the one asked about.
        strict = self.run_statement(
            queries.relations_statement(
                self.scopes, T0, [self.covered.version_id], 100, self.eligible
            )
        )
        self.assertEqual(strict, [])

    def test_the_newer_end_of_a_relation_must_be_eligible_as_well(self):
        ids = [self.keep_too.version_id]
        lenient = self.run_statement(
            queries.relations_statement(self.scopes, T0, ids, 100)
        )
        self.assertEqual(
            {r.from_version_id for r in lenient},
            {self.stale.version_id, self.covered.version_id},
        )
        strict = self.run_statement(
            queries.relations_statement(self.scopes, T0, ids, 100, self.eligible)
        )
        self.assertEqual(strict, [])

    def test_keyword_and_vector_statements_carry_them_too(self):
        keyword = self.run_statement(
            queries.keyword_statement(self.scopes, T0, "'alpha'", 100, self.eligible)
        )
        self.assertEqual({r.title for r in keyword}, {"keep", "keep too", "new"})
        for seeded in (
            self.keep,
            self.keep_too,
            self.stale,
            self.covered,
            self.old,
            self.new,
        ):
            self.seed_embedding(seeded.version_id, [1.0, 0.0, 0.0], model_id="m3")
        vector = self.run_statement(
            queries.vector_statement(
                self.scopes, T0, "m3", 3, [1.0, 0.0, 0.0], 100, None, self.eligible
            )
        )
        self.assertEqual({r.title for r in vector}, {"keep", "keep too", "new"})


@requires_postgres
class MembershipStatementTest(PostgresRetrievalTestCase):
    def rows(self, user_id, project_ids=None):
        with self.engine.connect() as connection:
            return connection.execute(
                queries.memberships_statement(user_id, project_ids, 100)
            ).all()

    def test_only_accepted_memberships_of_readable_projects_are_returned(self):
        me = self.seed_user()
        wanted = {}
        for status in ProjectStatus:
            project = self.seed_project(status)
            self.seed_member(project, me, role=ProjectRole.VIEWER)
            wanted[status] = project
        invited = self.seed_project()
        self.seed_member(invited, me, status=MemberStatus.INVITED)
        someone_else = self.seed_project()
        self.seed_member(someone_else, self.seed_user())
        rows = self.rows(me)
        self.assertEqual(
            sorted((r.project_id, r.role, r.status) for r in rows),
            sorted(
                (wanted[s], "viewer", s.value)
                for s in (ProjectStatus.ACTIVE, ProjectStatus.ARCHIVED)
            ),
        )

    def test_the_narrowing_keeps_only_the_named_projects(self):
        me = self.seed_user()
        a, b = self.seed_project(), self.seed_project()
        self.seed_member(a, me)
        self.seed_member(b, me)
        self.assertEqual([r.project_id for r in self.rows(me, [a])], [a])
        self.assertEqual(self.rows(me, []), [])
        self.assertEqual(self.rows(me, [uuid4()]), [])
        self.assertEqual(len(self.rows(me, None)), 2)

    def test_the_limit_bounds_the_rows(self):
        me = self.seed_user()
        for _ in range(3):
            self.seed_member(self.seed_project(), me)
        with self.engine.connect() as connection:
            rows = connection.execute(queries.memberships_statement(me, None, 2)).all()
        self.assertEqual(len(rows), 2)


ALPHABET = (
    "abcXYZ019_ -.,;:'\"\\|&!()<>*%$#@[]{}?/"  # ASCII and tsquery syntax
    "検索日本語のひらがなカタカナー"  # Japanese
    "ＡＢＣ１２３ｶﾀｶﾅ"  # full width and half width
    "한국어العربيةעברית"  # other scripts
    "\u0301\u200d\u200b\u00a0\u3000"  # combining, joiners, spaces
    "\U0001f600\U0001f1ef\U00010400"  # outside the basic plane
)


@requires_postgres
class TsqueryTest(PostgresRetrievalTestCase):
    """The query text is a bind value, never SQL (real PostgreSQL)."""

    def test_random_text_never_makes_the_keyword_query_fail(self):
        rng = random.Random(7)
        scopes = ResolvedScopes(uuid4(), frozenset({MemoryScope.SHARED}))
        self.seed("anchor", "検索 alpha", scope="shared", embed=False)
        tried = 0
        with self.engine.connect() as connection:
            for _ in range(400):
                text_ = "".join(rng.choice(ALPHABET) for _ in range(rng.randint(1, 60)))
                query = tsquery_text(keyword_terms(text_))
                if query is None:
                    continue
                tried += 1
                statement = queries.keyword_statement(scopes, T0, query, 5)
                connection.execute(statement).all()  # must not raise
        self.assertGreater(tried, 300)

    def test_hostile_text_is_matched_as_words(self):
        hostile = "x'); DROP TABLE memories; -- \\ ' | & ! ( ) :*"
        terms = keyword_terms(hostile)
        query = tsquery_text(terms)
        self.assertNotIn(";", query)
        self.assertNotIn("--", query)
        scopes = ResolvedScopes(uuid4(), frozenset({MemoryScope.SHARED}))
        self.seed("safe", "drop table memories", scope="shared", embed=False)
        statement = queries.keyword_statement(scopes, T0, query, 10)
        with self.engine.connect() as connection:
            self.assertEqual([r.title for r in connection.execute(statement)], ["safe"])
            count = connection.execute(text("SELECT count(*) FROM memories")).scalar()
        self.assertEqual(count, 1)
