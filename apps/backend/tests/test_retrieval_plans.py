"""Query plans of a retrieval: the permission filter is in the scan (real PostgreSQL).

The claim "ACL / metadata filters are applied BEFORE vector search" is checked in
the plan, not only in the results: every node above the ordering by distance has no
permission condition, and the scan of ``memory_versions`` below it carries the scope
and status conditions in its own filter or index condition. Nothing depends on the
statistics of the table: the structure holds for every join order the planner may
pick, and the index checks turn sequential scans off.
"""

from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import aliased

from paw_backend.memory.acl import readable_memory_versions
from paw_backend.memory.fulltext import keyword_terms, tsquery_text
from paw_backend.memory.models import MemoryScope, MemoryVersion
from paw_backend.memory.retrieval import queries
from paw_backend.memory.retrieval.scopes import ResolvedScopes

from .retrieval_pg_support import T0, PostgresRetrievalTestCase, requires_postgres
from .task_support import PostgresTaskTestCase

plan_nodes = PostgresTaskTestCase.plan_nodes

CONDITION_KEYS = ("Filter", "Index Cond", "Recheck Cond", "Join Filter", "Hash Cond")
PERMISSION_COLUMNS = (
    "scope",
    "owner_user_id",
    "project_id",
    "repo_id",
    "project_group_id",
)


BULK_VERSIONS = """
WITH numbered AS (
    SELECT id, row_number() OVER () AS n FROM memories),
owners AS (
    SELECT array_agg(gen_random_uuid()) AS ids FROM generate_series(1, 50)),
projects AS (
    SELECT array_agg(gen_random_uuid()) AS ids FROM generate_series(1, 30)),
repos AS (
    SELECT array_agg(gen_random_uuid()) AS ids FROM generate_series(1, 30))
INSERT INTO memory_versions
    (memory_id, version_number, scope, owner_user_id, project_id, repo_id,
     memory_type, title, content, status, confirmation_state, freshness_policy,
     actor_type)
SELECT numbered.id, 1,
    CASE numbered.n % 4 WHEN 0 THEN 'user' WHEN 1 THEN 'project'
        WHEN 2 THEN 'repo' ELSE 'shared' END,
    CASE WHEN numbered.n % 4 = 0 THEN owners.ids[1 + numbered.n % 50] END,
    CASE WHEN numbered.n % 4 = 1 THEN projects.ids[1 + numbered.n % 30] END,
    CASE WHEN numbered.n % 4 = 2 THEN repos.ids[1 + numbered.n % 30] END,
    'note', 'title ' || numbered.n, 'alpha beta ' || numbered.n,
    CASE WHEN numbered.n % 7 = 0 THEN 'superseded' ELSE 'active' END,
    'confirmed', 'permanent', 'system'
FROM numbered, owners, projects, repos
"""
BULK_EMBEDDINGS = """
INSERT INTO memory_embeddings
    (memory_version_id, embedding_model_id, dimensions, embedding)
SELECT id, 'm3', 3,
    ('[' || random() || ',' || random() || ',' || random() || ']')::vector
FROM memory_versions
"""
SOME_IDS = """
SELECT (SELECT project_id FROM memory_versions WHERE project_id IS NOT NULL LIMIT 1),
       (SELECT repo_id FROM memory_versions WHERE repo_id IS NOT NULL LIMIT 1)
"""


def conditions(node) -> str:
    return " ".join(str(node[key]) for key in CONDITION_KEYS if key in node)


def subtree_ids(node) -> set[int]:
    return {id(n) for n in plan_nodes(node)}


class PlanTestCase(PostgresRetrievalTestCase):
    def bulk_seed(self, rows: int = 3000) -> ResolvedScopes:
        """A table big enough that the planner has real choices."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO memories (id)"
                    " SELECT gen_random_uuid() FROM generate_series(1, :n)"
                ),
                {"n": rows},
            )
            connection.execute(text(BULK_VERSIONS))
            connection.execute(
                text("INSERT INTO embedding_models (id, dimensions) VALUES ('m3', 3)")
            )
            connection.execute(text(BULK_EMBEDDINGS))
            connection.execute(text("ANALYZE memory_versions"))
            connection.execute(text("ANALYZE memory_embeddings"))
        with self.engine.connect() as connection:
            project, repo = connection.execute(text(SOME_IDS)).one()
        return ResolvedScopes(
            uuid4(),
            frozenset(MemoryScope),
            frozenset({project}),
            frozenset({repo}),
            frozenset({uuid4()}),
        )

    def vector(self, scopes: ResolvedScopes, limit: int = 10):
        return queries.vector_statement(scopes, T0, "m3", 3, [0.1, 0.2, 0.3], limit)

    def assert_filter_below_the_distance_sort(self, root) -> None:
        nodes = list(plan_nodes(root))
        sorts = [n for n in nodes if n["Node Type"] in ("Sort", "Incremental Sort")]
        self.assertEqual(len(sorts), 1, [n["Node Type"] for n in nodes])
        (sort,) = sorts
        self.assertIn("<=>", " ".join(sort["Sort Key"]))
        below = subtree_ids(sort)
        version_scans = [
            n
            for n in nodes
            if id(n) in below
            and n.get("Relation Name") == "memory_versions"
            and "Scan" in n["Node Type"]
        ]
        self.assertTrue(version_scans, "memory_versions is not scanned under the sort")
        below_text = " ".join(conditions(n) for n in version_scans)
        for column in ("scope", "status"):
            self.assertIn(column, below_text)
        # Nothing above the sort (the limit, a projection) filters by permission.
        for node in nodes:
            if id(node) not in below:
                for column in PERMISSION_COLUMNS:
                    self.assertNotIn(column, conditions(node), node["Node Type"])


@requires_postgres
class VectorPlanTest(PlanTestCase):
    def test_the_permission_filter_is_in_the_scan_below_the_distance_ordering(self):
        scopes = self.bulk_seed()
        for label, kwargs in (
            ("custom plan", {"generic": False}),
            ("generic plan", {"generic": True}),
            ("custom, no seqscan", {"generic": False, "seqscan": False}),
            ("generic, no bitmap", {"generic": True, "bitmap": False}),
        ):
            with self.subTest(label):
                root = self.explain(self.vector(scopes), **kwargs)
                self.assertEqual(root["Node Type"], "Limit")
                self.assert_filter_below_the_distance_sort(root)

    def test_the_conditions_name_the_scope_columns_and_the_caller_ids(self):
        scopes = self.bulk_seed()
        root = self.explain(self.vector(scopes))
        below = " ".join(conditions(n) for n in plan_nodes(root))
        for expected in ("scope", "owner_user_id", "status", "embedding_model_id"):
            self.assertIn(expected, below)

    def test_the_scope_names_are_written_into_the_sql_not_bound(self):
        scopes = self.bulk_seed()
        compiled = self.vector(scopes).compile(dialect=self.engine.dialect)
        self.assertIn("mv.scope = 'shared'", compiled.string)
        self.assertIn("mv.status = 'active'", compiled.string)
        self.assertEqual(
            sorted(k for k in compiled.params if "scope" in k or "status" in k), []
        )


@requires_postgres
class KeywordPlanTest(PlanTestCase):
    def statement(self, scopes):
        query = tsquery_text(keyword_terms("alpha beta"))
        return queries.keyword_statement(scopes, T0, query, 10)

    def test_the_full_text_index_serves_the_keyword_query(self):
        scopes = self.bulk_seed()
        for generic in (False, True):
            with self.subTest(generic=generic):
                root = self.explain(
                    self.statement(scopes),
                    generic=generic,
                    seqscan=False,
                    only_index="ix_memory_versions_search",
                )
                names = {n.get("Index Name") for n in plan_nodes(root)}
                self.assertIn("ix_memory_versions_search", names)

    def test_the_permission_condition_sits_in_the_same_scan_as_the_text_match(self):
        scopes = self.bulk_seed()
        root = self.explain(self.statement(scopes), seqscan=False)
        scans = [
            n
            for n in plan_nodes(root)
            if n.get("Relation Name") == "memory_versions" and "Scan" in n["Node Type"]
        ]
        self.assertTrue(scans)
        text_ = " ".join(conditions(n) for n in scans)
        for expected in ("scope", "status", "to_tsvector"):
            self.assertIn(expected, text_)
        # The ordering by rank has no permission filter above it.
        for node in plan_nodes(root):
            if node["Node Type"] in ("Limit", "Sort"):
                for column in PERMISSION_COLUMNS:
                    self.assertNotIn(column, conditions(node))


@requires_postgres
class AclIndexTest(PlanTestCase):
    def acl_statement(self, scopes, *, inline):
        version = aliased(MemoryVersion, name="mv")
        condition = (
            queries.visible(version, scopes, T0)
            if inline
            else readable_memory_versions(scopes.acl_principal(), version)
        )
        return select(version.id).where(condition)

    def test_a_generic_plan_can_use_the_per_scope_partial_indexes(self):
        scopes = self.bulk_seed()
        root = self.explain(
            self.acl_statement(scopes, inline=True), generic=True, seqscan=False
        )
        names = {n.get("Index Name") for n in plan_nodes(root)}
        for index in (
            "ix_memory_versions_owner_user_id_status",
            "ix_memory_versions_project_id_status",
            "ix_memory_versions_repo_id_status",
            "ix_memory_versions_shared_status",
        ):
            with self.subTest(index):
                self.assertIn(index, names)

    def test_the_control_a_bound_scope_name_cannot_use_the_shared_partial_index(self):
        # Why the scope names are inlined: with a bound ``scope = $n`` a generic
        # plan cannot prove the partial index's ``scope = 'shared'`` predicate.
        scopes = self.bulk_seed()
        root = self.explain(
            self.acl_statement(scopes, inline=False), generic=True, seqscan=False
        )
        names = {n.get("Index Name") for n in plan_nodes(root)}
        self.assertNotIn("ix_memory_versions_shared_status", names)

    def test_the_relation_and_membership_statements_use_their_indexes(self):
        scopes = self.bulk_seed()
        many = [uuid4() for _ in range(5)]
        root = self.explain(
            queries.relations_statement(scopes, T0, many, 10), seqscan=False
        )
        names = {n.get("Index Name") for n in plan_nodes(root)}
        self.assertTrue(
            names
            & {
                "ix_memory_relations_to_version_id",
                "uq_memory_relations_from_version_id",
            },
            names,
        )
        root = self.explain(
            queries.memberships_statement(uuid4(), None, 10), seqscan=False
        )
        names = {n.get("Index Name") for n in plan_nodes(root)}
        self.assertIn("ix_project_members_user_id", names)


if __name__ == "__main__":
    import unittest

    unittest.main()
