"""Eligibility filters take part in candidate selection, before any limit.

A candidate list is cut to its limit (``keyword_candidates``, ``vector_candidates``,
``rerank_candidates``). If a filter ran AFTER the cut, rows that are not eligible (a
shared memory a System Policy covers, a stale memory when the caller excludes stale
ones, a version that a readable active version supersedes) would take places from the
eligible rows below them, and the answer would be empty or short although eligible
memories exist. So each filter is a condition of the candidate statements
themselves. The test of each is the same shape:

1. the world has only eligible memories: ``before`` is the answer;
2. ineligible memories that rank ABOVE all of them, more of them than the candidate
   limit, are added;
3. the answer is exactly ``before``: the same memories AND the same ranks and scores
   (as if the ineligible ones did not exist).

The differential tests then compare the SQL conditions with the Python rules they
mirror (``ranking.freshness_of`` and the Shared Memory rules of PAW-046), row by row.
"""

import os
import random
from datetime import timedelta
from unittest import mock

from paw_backend.authz import RepoAcl
from paw_backend.memory.retrieval import Freshness
from paw_backend.memory.shared import StaticPolicySource, SystemPolicyItem
from paw_backend.memory.shared.errors import SharedMemoryDataError
from paw_backend.memory.shared.precedence import overriding_policy_ids
from paw_backend.memory.shared.service import _subjects_of

from .retrieval_pg_support import (
    T0,
    FixedEmbedder,
    PostgresRetrievalTestCase,
    StaticRepoAcls,
    requires_postgres,
    titles,
)

QUERY = "deploy backend friday"
LIMIT = 3  # every candidate limit of the retrievers below
MODEL = "fixed-test-model"
NO_MATCH = "zzzq"


def policy(*subjects: str) -> StaticPolicySource:
    return StaticPolicySource(
        tuple(
            SystemPolicyItem(f"p{n}", subject, "rule")
            for n, subject in enumerate(subjects)
        )
    )


@requires_postgres
class CrowdingTestCase(PostgresRetrievalTestCase):
    """A retriever with short candidate lists, and the before / after check."""

    def small(self, **options):
        return self.new_retriever(
            keyword_candidates=LIMIT,
            vector_candidates=LIMIT,
            rerank_candidates=LIMIT,
            **options,
        )

    async def assert_ineligible_rows_do_not_crowd(
        self, actor, retriever, add_ineligible, *, expected_titles, **query
    ):
        text = query.pop("text", QUERY)
        before = await self.retrieve(actor, text, retriever=retriever, **query)
        self.assertEqual(
            sorted(titles(before)), sorted(expected_titles), "the eligible world"
        )
        add_ineligible()
        after = await self.retrieve(actor, text, retriever=retriever, **query)
        self.assertEqual(after, before)
        return after


@requires_postgres
class SystemPolicyCrowdingTest(CrowdingTestCase):
    def covered(self, count, *, embedding=None, model_id=None, content=None):
        def add():
            for n in range(count):
                self.seed(
                    f"covered {n}",
                    content or "deploy backend friday " * 3 + f"covered {n}",
                    scope="shared",
                    subjects=["merge.permission"],
                    embed=embedding is not None,
                    embedding=embedding,
                    model_id=model_id,
                )

        return add

    async def test_covered_shared_memories_above_the_keyword_limit_do_not_hide_the_rest(
        self,
    ):
        me = self.user()
        self.seed("eligible a", "deploy backend friday", scope="shared", embed=False)
        self.seed("eligible b", "deploy backend", owner=me.user_id, embed=False)
        retriever = self.small(policies=policy("merge"))
        result = await self.assert_ineligible_rows_do_not_crowd(
            me,
            retriever,
            self.covered(LIMIT + 2),
            expected_titles=["eligible a", "eligible b"],
        )
        self.assertEqual(sorted(h.keyword_rank for h in result.hits), [1, 2])

    async def test_covered_shared_memories_above_the_vector_limit_do_not_hide_the_rest(
        self,
    ):
        me = self.user()
        embedder = FixedEmbedder({NO_MATCH: [1.0, 0.0, 0.0]}, model_id=MODEL)
        self.seed(
            "eligible a",
            "alpha",
            scope="shared",
            embedding=[0.6, 0.8, 0.0],
            model_id=MODEL,
        )
        self.seed(
            "eligible b",
            "beta",
            owner=me.user_id,
            embedding=[0.0, 1.0, 0.0],
            model_id=MODEL,
        )
        retriever = self.small(policies=policy("merge"), embedder=embedder)
        result = await self.assert_ineligible_rows_do_not_crowd(
            me,
            retriever,
            self.covered(
                LIMIT + 2, embedding=[1.0, 0.01, 0.0], model_id=MODEL, content="gamma"
            ),
            expected_titles=["eligible a", "eligible b"],
            text=NO_MATCH,
        )
        self.assertEqual(sorted(h.vector_rank for h in result.hits), [1, 2])

    async def test_covered_shared_memories_above_the_rerank_limit_do_not_hide_the_rest(
        self,
    ):
        # Both lists are short, so the fused list is longer than the rerank limit only
        # through the two legs: the covered rows are the best in both.
        me = self.user()
        for n in range(LIMIT):
            self.seed(f"eligible {n}", f"deploy backend {n}", owner=me.user_id)
        retriever = self.new_retriever(
            policies=policy("merge"),
            keyword_candidates=50,
            vector_candidates=50,
            rerank_candidates=LIMIT,
        )
        await self.assert_ineligible_rows_do_not_crowd(
            me,
            retriever,
            lambda: [
                self.seed(
                    f"covered {n}",
                    "deploy backend friday " * 3,
                    scope="shared",
                    subjects=["merge"],
                )
                for n in range(LIMIT + 2)
            ],
            expected_titles=[f"eligible {n}" for n in range(LIMIT)],
        )

    async def test_covered_conflict_partners_are_not_pulled_in(self):
        me = self.user()
        keep = self.seed("keep", "deploy backend friday", owner=me.user_id, embed=False)
        hidden = self.seed(
            "covered", "other", scope="shared", subjects=["merge"], embed=False
        )
        self.seed_relation(hidden.version_id, keep.version_id)
        result = await self.retrieve(
            me, QUERY, retriever=self.small(policies=policy("merge"))
        )
        self.assertEqual(titles(result), ["keep"])
        self.assertEqual(result.conflicts, ())


@requires_postgres
class StaleCrowdingTest(CrowdingTestCase):
    async def test_stale_memories_above_the_limit_do_not_hide_fresh_ones_when_excluded(
        self,
    ):
        project = self.seed_project()
        me = self.member_of(project)
        repo = self.new_repo_id()
        self.seed("fresh a", "deploy backend friday", owner=me.user_id, embed=False)
        self.seed("fresh b", "deploy backend", owner=me.user_id, embed=False)
        retriever = self.small(
            repo_acls=StaticRepoAcls([RepoAcl.inherit(repo, project)])
        )
        strong = "deploy backend friday " * 3

        def add():
            for n in range(LIMIT + 2):
                self.seed(
                    f"due {n}",
                    strong,
                    owner=me.user_id,
                    embed=False,
                    freshness="revalidate",
                    verified_at=T0 - timedelta(days=200),
                    revalidate_after=timedelta(days=90),
                )
                self.seed(
                    f"marked {n}",
                    strong,
                    owner=me.user_id,
                    embed=False,
                    stale_since=T0 - timedelta(days=1),
                )
                self.seed(
                    f"moved {n}",
                    strong,
                    scope="repo",
                    repo=repo,
                    embed=False,
                    freshness="repo_commit",
                    commit_sha="a" * 40,
                )

        result = await self.assert_ineligible_rows_do_not_crowd(
            me,
            retriever,
            add,
            expected_titles=["fresh a", "fresh b"],
            stale_policy="exclude",
            repo_heads={repo: "b" * 40},
        )
        self.assertEqual(sorted(h.keyword_rank for h in result.hits), [1, 2])

    async def test_stale_memories_above_the_vector_limit_do_not_hide_fresh_ones(self):
        me = self.user()
        embedder = FixedEmbedder({NO_MATCH: [1.0, 0.0, 0.0]}, model_id=MODEL)
        self.seed(
            "fresh",
            "alpha",
            owner=me.user_id,
            embedding=[0.6, 0.8, 0.0],
            model_id=MODEL,
        )
        retriever = self.small(embedder=embedder)

        def add():
            for n in range(LIMIT + 2):
                self.seed(
                    f"marked {n}",
                    "beta",
                    owner=me.user_id,
                    embedding=[1.0, 0.01, 0.0],
                    model_id=MODEL,
                    stale_since=T0 - timedelta(days=1),
                )

        result = await self.assert_ineligible_rows_do_not_crowd(
            me,
            retriever,
            add,
            expected_titles=["fresh"],
            text=NO_MATCH,
            stale_policy="exclude",
        )
        self.assertEqual(result.hits[0].vector_rank, 1)

    async def test_the_flag_policy_still_returns_stale_memories_ranked_by_their_match(
        self,
    ):
        me = self.user()
        self.seed("fresh", "deploy backend", owner=me.user_id, embed=False)
        for n in range(LIMIT + 2):
            self.seed(
                f"marked {n}",
                "deploy backend friday " * 3,
                owner=me.user_id,
                embed=False,
                stale_since=T0 - timedelta(days=1),
            )
        result = await self.retrieve(
            me, QUERY, retriever=self.small(), stale_policy="flag"
        )
        # The default keeps stale candidates (lowered, flagged): they may fill the list.
        self.assertEqual(len(result.hits), LIMIT)
        self.assertTrue(all(h.freshness is Freshness.STALE for h in result.hits))


@requires_postgres
class SuccessorCrowdingTest(CrowdingTestCase):
    async def test_superseded_but_still_active_rows_above_the_limit_do_not_hide_others(
        self,
    ):
        me = self.user()
        self.seed("keep a", "deploy backend friday", owner=me.user_id, embed=False)
        self.seed("keep b", "deploy backend", owner=me.user_id, embed=False)
        retriever = self.small()

        def add():
            for n in range(LIMIT + 2):
                old = self.seed(
                    f"old {n}",
                    "deploy backend friday " * 3,
                    owner=me.user_id,
                    embed=False,
                )
                new = self.seed(
                    f"new {n}", "unrelated words", owner=me.user_id, embed=False
                )
                self.seed_relation(new.version_id, old.version_id, "supersedes")

        result = await self.assert_ineligible_rows_do_not_crowd(
            me, retriever, add, expected_titles=["keep a", "keep b"]
        )
        self.assertEqual(sorted(h.keyword_rank for h in result.hits), [1, 2])

    async def test_the_same_holds_for_the_vector_leg(self):
        me = self.user()
        embedder = FixedEmbedder({NO_MATCH: [1.0, 0.0, 0.0]}, model_id=MODEL)
        self.seed(
            "keep", "alpha", owner=me.user_id, embedding=[0.6, 0.8, 0.0], model_id=MODEL
        )
        retriever = self.small(embedder=embedder)

        def add():
            for n in range(LIMIT + 2):
                old = self.seed(
                    f"old {n}",
                    "beta",
                    owner=me.user_id,
                    embedding=[1.0, 0.01, 0.0],
                    model_id=MODEL,
                )
                # No embedding and no matching word: the successors are not candidates.
                new = self.seed(f"new {n}", "gamma", owner=me.user_id, embed=False)
                self.seed_relation(new.version_id, old.version_id, "supersedes")

        result = await self.assert_ineligible_rows_do_not_crowd(
            me,
            retriever,
            add,
            expected_titles=["keep", *[]],
            text=NO_MATCH,
        )
        self.assertEqual(result.hits[0].vector_rank, 1)

    async def test_a_successor_the_caller_cannot_read_does_not_prune_the_readable_row(
        self,
    ):
        me, other = self.user(), self.user()
        old = self.seed("old", "deploy backend friday", owner=me.user_id, embed=False)
        new = self.seed(
            "new", "deploy backend friday", owner=other.user_id, embed=False
        )
        self.seed_relation(new.version_id, old.version_id, "supersedes")
        result = await self.retrieve(me, QUERY, retriever=self.small())
        self.assertEqual(titles(result), ["old"])

    async def test_a_successor_that_is_not_active_does_not_prune_the_row(self):
        me = self.user()
        old = self.seed("old", "deploy backend friday", owner=me.user_id, embed=False)
        for n, status in enumerate(("deprecated", "history", "superseded")):
            new = self.seed(
                f"new {n}", "x", owner=me.user_id, status=status, embed=False
            )
            if n == 0:
                self.seed_relation(new.version_id, old.version_id, "supersedes")
        self.assertEqual(
            titles(await self.retrieve(me, QUERY, retriever=self.small())), ["old"]
        )

    async def test_the_successor_relation_only_counts_when_it_is_a_supersedes_relation(
        self,
    ):
        me = self.user()
        old = self.seed("old", "deploy backend friday", owner=me.user_id, embed=False)
        for kind in ("extends", "conflicts_with", "merged_from"):
            new = self.seed(f"new {kind}", "unrelated", owner=me.user_id, embed=False)
            self.seed_relation(new.version_id, old.version_id, kind)
        result = await self.retrieve(me, QUERY, retriever=self.small(), limit=50)
        self.assertIn("old", titles(result))


# -- SQL against the Python rules ----------------------------------------------------


@requires_postgres
class StaleConditionMatchesThePythonRuleTest(PostgresRetrievalTestCase):
    async def seed_cases(self, me, project, repo, other_repo):
        base = timedelta(days=90)
        cases = {
            "permanent": {},
            "marked": {"stale_since": T0},
            "due_exact": {
                "freshness": "revalidate",
                "verified_at": T0 - base,
                "revalidate_after": base,
            },
            "due_before": {
                "freshness": "revalidate",
                "verified_at": T0 - base + timedelta(microseconds=1),
                "revalidate_after": base,
            },
            "due_long_ago": {
                "freshness": "revalidate",
                "verified_at": T0 - 5 * base,
                "revalidate_after": base,
            },
            "hours_due": {
                "freshness": "revalidate",
                "verified_at": T0 - timedelta(hours=5),
                "revalidate_after": timedelta(hours=5),
            },
            "hours_fresh": {
                "freshness": "revalidate",
                "verified_at": T0 - timedelta(hours=5),
                "revalidate_after": timedelta(hours=6),
            },
            "marked_revalidate": {
                "freshness": "revalidate",
                "verified_at": T0,
                "revalidate_after": base,
                "stale_since": T0,
            },
            "expiring_ok": {
                "freshness": "expiring",
                "expires_at": T0 + timedelta(days=1),
            },
            "commit_same": {
                "scope": "repo",
                "repo": repo,
                "freshness": "repo_commit",
                "commit_sha": "a" * 40,
            },
            "commit_moved": {
                "scope": "repo",
                "repo": repo,
                "freshness": "repo_commit",
                "commit_sha": "b" * 40,
            },
            "commit_other_repo": {
                "scope": "repo",
                "repo": other_repo,
                "freshness": "repo_commit",
                "commit_sha": "b" * 40,
            },
            "commit_marked": {
                "scope": "repo",
                "repo": repo,
                "freshness": "repo_commit",
                "commit_sha": "a" * 40,
                "stale_since": T0,
            },
        }
        for title, values in cases.items():
            if "scope" not in values:
                values["owner"] = me.user_id
            self.seed(title, "widget notes", embed=False, **values)
        # A project-scoped repo_commit memory has no repo: never judged by commit.
        self.seed(
            "commit_without_repo",
            "widget notes",
            scope="project",
            project=project,
            freshness="repo_commit",
            commit_sha="c" * 40,
            embed=False,
        )
        return list(cases) + ["commit_without_repo"]

    async def test_excluding_stale_returns_exactly_what_the_python_rule_calls_fresh(
        self,
    ):
        project = self.seed_project()
        me = self.member_of(project)
        repo, other_repo = self.new_repo_id(), self.new_repo_id()
        names = await self.seed_cases(me, project, repo, other_repo)
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls(
                [RepoAcl.inherit(repo, project), RepoAcl.inherit(other_repo, project)]
            )
        )
        heads = {repo: "a" * 40}
        flagged = await self.retrieve(
            me, "widget", retriever=retriever, limit=50, repo_heads=heads
        )
        self.assertEqual(sorted(titles(flagged)), sorted(names))
        excluded = await self.retrieve(
            me,
            "widget",
            retriever=retriever,
            limit=50,
            repo_heads=heads,
            stale_policy="exclude",
        )
        fresh = sorted(h.title for h in flagged.hits if h.freshness is Freshness.FRESH)
        self.assertEqual(sorted(titles(excluded)), fresh)
        # The comparison is not vacuous: both kinds exist.
        self.assertIn("marked", set(titles(flagged)) - set(fresh))
        self.assertIn("commit_moved", set(titles(flagged)) - set(fresh))
        self.assertIn("commit_same", fresh)
        self.assertIn("due_before", fresh)
        self.assertIn("due_exact", set(titles(flagged)) - set(fresh))

    async def test_without_repo_heads_no_repo_commit_memory_is_judged_stale(self):
        project = self.seed_project()
        me = self.member_of(project)
        repo, other_repo = self.new_repo_id(), self.new_repo_id()
        await self.seed_cases(me, project, repo, other_repo)
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls(
                [RepoAcl.inherit(repo, project), RepoAcl.inherit(other_repo, project)]
            )
        )
        excluded = await self.retrieve(
            me, "widget", retriever=retriever, limit=50, stale_policy="exclude"
        )
        for name in (
            "commit_same",
            "commit_moved",
            "commit_other_repo",
            "commit_without_repo",
        ):
            self.assertIn(name, titles(excluded))

    async def test_a_session_time_zone_with_daylight_saving_changes_nothing(self):
        # ``verified_at + 20 days`` is a calendar step in a zone with daylight
        # saving and 480 hours in UTC; the driver's ``timedelta`` (the Python rule)
        # is always 480 hours.
        me = self.user()
        verified = T0.replace(month=3, day=1, hour=0)
        self.seed(
            "boundary",
            "widget notes",
            owner=me.user_id,
            embed=False,
            freshness="revalidate",
            verified_at=verified,
            revalidate_after=timedelta(days=20),
        )
        for offset in (timedelta(minutes=-30), timedelta(minutes=30)):
            self.clock.now = verified + timedelta(days=20) + offset
            flagged = await self.retrieve(me, "widget")
            excluded = await self.retrieve(me, "widget", stale_policy="exclude")
            fresh = flagged.hits[0].freshness is Freshness.FRESH
            self.assertEqual(fresh, offset < timedelta(0))
            self.assertEqual(len(excluded.hits), 1 if fresh else 0)
            with mock.patch.dict(os.environ, {"PGTZ": "America/New_York"}):
                retriever = self.new_retriever()
                flagged_ny = await self.retrieve(me, "widget", retriever=retriever)
                excluded_ny = await self.retrieve(
                    me, "widget", retriever=retriever, stale_policy="exclude"
                )
            self.assertEqual(flagged_ny.hits[0].freshness, flagged.hits[0].freshness)
            self.assertEqual(len(excluded_ny.hits), len(excluded.hits))


SUBJECT_POOL = [
    "merge",
    "merge.permission",
    "merge.permission.admin",
    "mergeable",
    "merge_x",
    "docs",
    "docs.api",
    "a.b.c.d.e",
    "a.b.c.d.e.f",
    "Merge",
    "1merge",
    "merge-x",
    "merge.",
    ".merge",
    "merge..x",
    "",
    " merge",
    "x" * 32,
    "x" * 33,
    "a" * 20 + "." + "b" * 20 + "." + "c" * 20 + "." + "d" * 20,
]
POLICY_POOL = [
    "merge",
    "merge.permission",
    "docs",
    "a.b",
    "a.b.c.d",
    "merge_x",
    "deploy",
]
ATTRIBUTE_POOL = [
    None,  # absent
    "a string",
    5,
    True,
    {"x": 1},
    [],
    [1],
    [None],
    ["merge", 5],
    [["merge"]],
]


@requires_postgres
class PolicyConditionMatchesTheSharedMemoryRulesTest(PostgresRetrievalTestCase):
    def expected_survivors(self, rows, policy_subjects):
        items = tuple(
            SystemPolicyItem(f"p{n}", subject, "rule")
            for n, subject in enumerate(policy_subjects)
        )
        survivors = []
        for title, attributes in rows:
            try:
                subjects = _subjects_of(attributes)
            except SharedMemoryDataError:
                continue
            if not overriding_policy_ids(subjects, items):
                survivors.append(title)
        return sorted(survivors)

    async def check(self, seed, policy_subjects, rows):
        me = self.user()
        for title, attributes in rows:
            self.seed(
                title,
                "widget notes",
                scope="shared",
                attributes=attributes,
                embed=False,
            )
        retriever = self.new_retriever(policies=policy(*policy_subjects))
        result = await self.retrieve(me, "widget", retriever=retriever, limit=50)
        self.assertEqual(
            sorted(titles(result)),
            self.expected_survivors(rows, policy_subjects),
            f"seed {seed}, policy {policy_subjects}",
        )
        return result

    async def test_random_declarations_are_judged_like_the_shared_memory_rules(self):
        survivors_seen = dropped_seen = 0
        for seed in range(6):
            with self.subTest(seed=seed):
                self.clean_tables()
                rng = random.Random(seed)
                policy_subjects = rng.sample(POLICY_POOL, rng.randint(0, 3))
                rows = []
                for n in range(30):
                    if rng.random() < 0.25:
                        raw = rng.choice(ATTRIBUTE_POOL)
                        attributes = {} if raw is None else {"policy_subjects": raw}
                    else:
                        count = rng.randint(0, 4)
                        attributes = {
                            "policy_subjects": [
                                rng.choice(SUBJECT_POOL) for _ in range(count)
                            ]
                        }
                        if rng.random() < 0.05:
                            attributes["policy_subjects"] = [
                                f"s{k}" for k in range(rng.choice([20, 21]))
                            ]
                    rows.append((f"row {n:02d}", attributes))
                result = await self.check(seed, policy_subjects, rows)
                survivors_seen += len(result.hits)
                dropped_seen += len(rows) - len(result.hits)
        self.assertGreater(survivors_seen, 30)
        self.assertGreater(dropped_seen, 30)

    async def test_the_explicit_boundary_cases(self):
        rows = [
            ("equal", {"policy_subjects": ["merge"]}),
            ("below", {"policy_subjects": ["merge.permission"]}),
            ("deep below", {"policy_subjects": ["merge.permission.admin"]}),
            ("prefix only", {"policy_subjects": ["mergeable"]}),
            ("underscore twin", {"policy_subjects": ["mergeaxb"]}),
            ("above", {"policy_subjects": ["me"]}),
            ("none", {}),
            ("empty", {"policy_subjects": []}),
            ("null", {"policy_subjects": None}),
            ("scalar", {"policy_subjects": "merge"}),
            ("upper", {"policy_subjects": ["Merge"]}),
            ("twenty", {"policy_subjects": [f"s{k}" for k in range(20)]}),
            ("twenty one", {"policy_subjects": [f"s{k}" for k in range(21)]}),
            ("mixed", {"policy_subjects": ["docs", "merge.permission"]}),
        ]
        result = await self.check("explicit", ["merge", "merge_x"], rows)
        self.assertEqual(
            sorted(titles(result)),
            ["above", "empty", "none", "prefix only", "twenty", "underscore twin"],
        )
        # ``merge_x`` covers ``merge_x`` and below, not ``mergeaxb`` (no wildcard).
        self.assertNotIn("mixed", titles(result))

    async def test_the_subject_pattern_of_the_sql_is_the_one_of_the_shared_validation(
        self,
    ):
        from paw_backend.memory.retrieval import queries
        from paw_backend.memory.shared import limits as shared_limits
        from paw_backend.memory.shared.validation import _SUBJECT

        self.assertEqual(queries.SUBJECT_PATTERN, f"^(?:{_SUBJECT.pattern})$")
        self.assertEqual(queries.MAX_SUBJECT_CHARS, shared_limits.MAX_SUBJECT_CHARS)
        self.assertEqual(queries.MAX_POLICY_SUBJECTS, shared_limits.MAX_POLICY_SUBJECTS)


if __name__ == "__main__":
    import unittest

    unittest.main()
