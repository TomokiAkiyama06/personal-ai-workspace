"""Status, freshness, confirmation, duplicates and conflicts in a retrieval.

Real PostgreSQL. ``T0`` (the fake clock) is the instant of every retrieval.
"""

from datetime import timedelta

from paw_backend.authz import RepoAcl
from paw_backend.memory.retrieval import (
    Freshness,
    MatchSource,
    StalePolicy,
    StaleReason,
)
from paw_backend.memory.retrieval import limits as retrieval_limits

from .retrieval_pg_support import (
    T0,
    PostgresRetrievalTestCase,
    StaticRepoAcls,
    requires_postgres,
    titles,
)

QUERY = "deploy backend friday"


@requires_postgres
class StatusTest(PostgresRetrievalTestCase):
    async def test_only_the_active_version_is_ever_returned(self):
        me = self.user()
        for status in ("superseded", "deprecated", "history"):
            self.seed(
                f"{status} best match",
                "deploy backend friday exactly",
                owner=me.user_id,
                status=status,
            )
        self.seed("active", "deploy backend", owner=me.user_id)
        result = await self.retrieve(me, QUERY)
        self.assertEqual(titles(result), ["active"])

    async def test_a_memory_shows_its_newest_version_not_the_old_one(self):
        me = self.user()
        first = self.seed(
            "old wording",
            "deploy backend friday old",
            owner=me.user_id,
            status="superseded",
        )
        self.seed(
            "new wording",
            "deploy backend friday new",
            owner=me.user_id,
            memory_id=first.memory_id,
            version_number=2,
        )
        result = await self.retrieve(me, QUERY)
        self.assertEqual(titles(result), ["new wording"])
        self.assertEqual(result.hits[0].version_number, 2)
        self.assertEqual(result.hits[0].memory_id, first.memory_id)

    async def test_an_active_version_with_an_active_successor_is_not_offered(self):
        # Inconsistent data (status not yet moved to superseded): the successor wins.
        me = self.user()
        old = self.seed("older", "deploy backend friday older", owner=me.user_id)
        new = self.seed("newer", "deploy backend friday newer", owner=me.user_id)
        self.seed_relation(new.version_id, old.version_id, "supersedes")
        result = await self.retrieve(me, QUERY)
        self.assertEqual(titles(result), ["newer"])

    async def test_a_supersedes_relation_from_an_inactive_version_changes_nothing(self):
        me = self.user()
        old = self.seed("older", "deploy backend friday older", owner=me.user_id)
        gone = self.seed(
            "gone", "deploy backend", owner=me.user_id, status="deprecated"
        )
        self.seed_relation(gone.version_id, old.version_id, "supersedes")
        self.assertEqual(titles(await self.retrieve(me, QUERY)), ["older"])


@requires_postgres
class FreshnessFilterTest(PostgresRetrievalTestCase):
    async def test_an_expiring_memory_is_gone_at_its_expiry_instant(self):
        me = self.user()
        self.seed(
            "before",
            "deploy backend friday before",
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0 + timedelta(microseconds=1),
        )
        self.seed(
            "at",
            "deploy backend friday at",
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0,
        )
        self.seed(
            "after",
            "deploy backend friday after",
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0 - timedelta(days=1),
        )
        self.assertEqual(titles(await self.retrieve(me, QUERY)), ["before"])

    async def test_the_expiry_follows_the_clock_of_the_retriever(self):
        me = self.user()
        self.seed(
            "soon",
            "deploy backend friday",
            owner=me.user_id,
            freshness="expiring",
            expires_at=T0 + timedelta(days=1),
        )
        self.assertEqual(titles(await self.retrieve(me, QUERY)), ["soon"])
        self.clock.advance(days=1)
        self.assertEqual(titles(await self.retrieve(me, QUERY)), [])

    async def test_a_session_only_memory_is_not_long_term_memory(self):
        me = self.user()
        self.seed(
            "scratch",
            "deploy backend friday",
            owner=me.user_id,
            freshness="session_only",
        )
        self.assertEqual(titles(await self.retrieve(me, QUERY)), [])


@requires_postgres
class StaleTest(PostgresRetrievalTestCase):
    def seed_revalidate(self, title, *, verified_days_ago, owner):
        return self.seed(
            title,
            f"deploy backend friday {title}",
            owner=owner,
            freshness="revalidate",
            verified_at=T0 - timedelta(days=verified_days_ago),
            revalidate_after=timedelta(days=90),
        )

    async def test_a_stale_candidate_is_flagged_lowered_and_still_returned(self):
        me = self.user()
        self.seed_revalidate("fresh", verified_days_ago=10, owner=me.user_id)
        self.seed_revalidate("stale", verified_days_ago=200, owner=me.user_id)
        result = await self.retrieve(me, QUERY)
        hits = {h.title: h for h in result.hits}
        self.assertEqual(hits["fresh"].freshness, Freshness.FRESH)
        self.assertIsNone(hits["fresh"].stale_reason)
        self.assertEqual(hits["stale"].freshness, Freshness.STALE)
        self.assertEqual(hits["stale"].stale_reason, StaleReason.REVALIDATE_DUE)
        # ``on_stale: lower_priority``: the factor is 0.5 and nothing else differs.
        self.assertAlmostEqual(
            hits["stale"].score / hits["stale"].relevance,
            0.5 * hits["fresh"].score / hits["fresh"].relevance,
        )
        self.assertEqual(titles(result)[0], "fresh")

    async def test_revalidate_turns_stale_exactly_at_the_interval(self):
        me = self.user()
        self.seed(
            "boundary",
            "deploy backend friday",
            owner=me.user_id,
            freshness="revalidate",
            verified_at=T0 - timedelta(days=90),
            revalidate_after=timedelta(days=90),
        )
        (hit,) = (await self.retrieve(me, QUERY)).hits
        self.assertEqual(hit.stale_reason, StaleReason.REVALIDATE_DUE)
        self.clock.advance(seconds=-1)
        (hit,) = (await self.retrieve(me, QUERY)).hits
        self.assertEqual(hit.freshness, Freshness.FRESH)

    async def test_the_exclude_policy_leaves_stale_memories_out(self):
        me = self.user()
        self.seed_revalidate("fresh", verified_days_ago=10, owner=me.user_id)
        self.seed_revalidate("stale", verified_days_ago=200, owner=me.user_id)
        self.seed(
            "marked",
            "deploy backend friday marked",
            owner=me.user_id,
            stale_since=T0 - timedelta(days=1),
        )
        flagged = await self.retrieve(me, QUERY, stale_policy=StalePolicy.FLAG)
        self.assertEqual(sorted(titles(flagged)), ["fresh", "marked", "stale"])
        excluded = await self.retrieve(me, QUERY, stale_policy="exclude")
        self.assertEqual(titles(excluded), ["fresh"])

    async def test_a_memory_marked_stale_is_a_stale_candidate_whatever_its_policy(self):
        me = self.user()
        self.seed(
            "marked",
            "deploy backend friday",
            owner=me.user_id,
            stale_since=T0 - timedelta(days=1),
        )
        (hit,) = (await self.retrieve(me, QUERY)).hits
        self.assertEqual(
            (hit.freshness, hit.stale_reason),
            (Freshness.STALE, StaleReason.MARKED_STALE),
        )

    async def test_a_repo_memory_of_another_commit_is_stale_only_against_a_known_head(
        self,
    ):
        project = self.seed_project()
        me = self.member_of(project)
        repo_id = self.new_repo_id()
        self.seed(
            "at old commit",
            "deploy backend friday",
            scope="repo",
            repo=repo_id,
            freshness="repo_commit",
            commit_sha="a" * 40,
        )
        source = StaticRepoAcls([RepoAcl.inherit(repo_id, project)])
        retriever = self.new_retriever(repo_acls=source)

        unknown = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(unknown.hits[0].freshness, Freshness.FRESH)
        same = await self.retrieve(
            me, QUERY, retriever=retriever, repo_heads={repo_id: "a" * 40}
        )
        self.assertEqual(same.hits[0].freshness, Freshness.FRESH)
        moved = await self.retrieve(
            me, QUERY, retriever=retriever, repo_heads={repo_id: "b" * 40}
        )
        self.assertEqual(moved.hits[0].stale_reason, StaleReason.REPO_COMMIT_CHANGED)
        gone = await self.retrieve(
            me,
            QUERY,
            retriever=retriever,
            repo_heads={repo_id: "b" * 40},
            stale_policy="exclude",
        )
        self.assertEqual(gone.hits, ())


@requires_postgres
class StructuredScoreTest(PostgresRetrievalTestCase):
    def factor(self, hit):
        return hit.score / hit.relevance

    async def test_confirmed_outranks_inferred_and_observed_of_the_same_match(self):
        me = self.user()
        for state in ("observed", "inferred", "confirmed"):
            self.seed(
                state,
                f"deploy backend friday {state}",
                owner=me.user_id,
                confirmation=state,
                embed=False,
            )
        result = await self.retrieve(me, QUERY)
        self.assertEqual(titles(result), ["confirmed", "inferred", "observed"])
        factors = [self.factor(h) for h in result.hits]
        self.assertAlmostEqual(factors[0], 1.02)
        self.assertAlmostEqual(factors[1], 0.85 * 1.02)
        self.assertAlmostEqual(factors[2], 0.7 * 1.02)

    async def test_importance_and_pin_move_the_score_by_their_factors(self):
        me = self.user()
        self.seed("plain", "deploy backend friday plain", owner=me.user_id, embed=False)
        self.seed(
            "important",
            "deploy backend friday important",
            owner=me.user_id,
            importance=100,
            embed=False,
        )
        self.seed(
            "pinned",
            "deploy backend friday pinned",
            owner=me.user_id,
            pinned=True,
            embed=False,
        )
        hits = {h.title: h for h in (await self.retrieve(me, QUERY)).hits}
        self.assertAlmostEqual(self.factor(hits["plain"]), 1.02)
        self.assertAlmostEqual(self.factor(hits["important"]), 1.2 * 1.02)
        self.assertAlmostEqual(self.factor(hits["pinned"]), 1.1 * 1.02)
        self.assertEqual(hits["important"].importance, 100)
        self.assertTrue(hits["pinned"].pinned)

    async def test_a_more_specific_scope_scores_higher_for_the_same_match(self):
        project = self.seed_project()
        me = self.member_of(project)
        repo_id = self.new_repo_id()
        self.seed("shared", "deploy backend friday shared", scope="shared", embed=False)
        self.seed("user", "deploy backend friday user", owner=me.user_id, embed=False)
        self.seed(
            "project",
            "deploy backend friday project",
            scope="project",
            project=project,
            embed=False,
        )
        self.seed(
            "repo",
            "deploy backend friday repo",
            scope="repo",
            repo=repo_id,
            embed=False,
        )
        retriever = self.new_retriever(
            repo_acls=StaticRepoAcls([RepoAcl.inherit(repo_id, project)])
        )
        result = await self.retrieve(me, QUERY, retriever=retriever)
        hits = {h.title: h for h in result.hits}
        expected = {"shared": 1.0, "user": 1.02, "project": 1.06, "repo": 1.08}
        for title, factor in expected.items():
            self.assertAlmostEqual(self.factor(hits[title]), factor, msg=title)

    async def test_the_result_carries_the_ids_of_its_scope(self):
        project = self.seed_project()
        me = self.member_of(project)
        seeded = self.seed(
            "project note",
            "deploy backend friday",
            scope="project",
            project=project,
            embed=False,
        )
        (hit,) = (await self.retrieve(me, QUERY)).hits
        self.assertEqual(hit.scope.value, "project")
        self.assertEqual(
            (hit.project_id, hit.repo_id, hit.project_group_id), (project, None, None)
        )
        self.assertEqual((hit.memory_id, hit.version_id), seeded)


@requires_postgres
class DuplicateTest(PostgresRetrievalTestCase):
    async def test_the_same_memory_in_two_scopes_is_returned_once_with_the_better_claim(
        self,
    ):
        project = self.seed_project()
        me = self.member_of(project)
        text = "deploy the backend every friday after the merge is green"
        private = self.seed("Deploy rule", text, owner=me.user_id)
        shared_with_project = self.seed(
            "Deploy rule", text, scope="project", project=project
        )
        result = await self.retrieve(me, QUERY)
        (hit,) = result.hits
        self.assertEqual(hit.version_id, shared_with_project.version_id)
        self.assertEqual(hit.duplicates, (private.version_id,))

    async def test_memories_with_different_wording_are_both_kept(self):
        me = self.user()
        self.seed("A", "deploy backend friday morning", owner=me.user_id)
        self.seed("B", "deploy backend friday evening rollback", owner=me.user_id)
        self.assertEqual(sorted(titles(await self.retrieve(me, QUERY))), ["A", "B"])

    async def test_a_confirmed_copy_replaces_an_inferred_one(self):
        me = self.user()
        text = "deploy the backend every friday after the merge is green"
        self.seed(
            "Rule", text, owner=me.user_id, confirmation="inferred", importance=100
        )
        confirmed = self.seed("Rule", text, owner=me.user_id, importance=0)
        (hit,) = (await self.retrieve(me, QUERY)).hits
        self.assertEqual(hit.version_id, confirmed.version_id)


@requires_postgres
class ConflictTest(PostgresRetrievalTestCase):
    async def test_conflicting_memories_form_a_group_and_none_is_chosen(self):
        me = self.user()
        old = self.seed(
            "Friday",
            "deploy backend friday always",
            owner=me.user_id,
            confirmation="inferred",
            embed=False,
        )
        new = self.seed(
            "Monday",
            "deploy backend never friday monday instead",
            owner=me.user_id,
            embed=False,
        )
        self.seed_relation(new.version_id, old.version_id, "conflicts_with")
        other = self.seed(
            "Other",
            "deploy backend friday unrelated topic notes",
            owner=me.user_id,
            embed=False,
        )

        result = await self.retrieve(me, QUERY)

        self.assertEqual(len(result.conflicts), 1)
        group = result.conflicts[0]
        self.assertEqual(group.group_id, 0)
        # Confirmed before inferred; both stay results.
        self.assertEqual(group.version_ids, (new.version_id, old.version_id))
        by_version = {h.version_id: h for h in result.hits}
        self.assertEqual(by_version[new.version_id].conflict_group, 0)
        self.assertEqual(by_version[old.version_id].conflict_group, 0)
        self.assertIsNone(by_version[other.version_id].conflict_group)
        order = [h.version_id for h in result.hits]
        self.assertEqual(
            abs(order.index(new.version_id) - order.index(old.version_id)), 1
        )

    async def test_the_conflicting_partner_is_pulled_in_even_if_the_query_misses_it(
        self,
    ):
        me = self.user()
        hit = self.seed(
            "Friday", "deploy backend friday always", owner=me.user_id, embed=False
        )
        partner = self.seed(
            "Opposite", "never release on that day", owner=me.user_id, embed=False
        )
        self.seed_relation(partner.version_id, hit.version_id)

        result = await self.retrieve(me, QUERY)

        self.assertEqual(
            {h.version_id for h in result.hits}, {hit.version_id, partner.version_id}
        )
        pulled = next(h for h in result.hits if h.version_id == partner.version_id)
        self.assertEqual(pulled.sources, (MatchSource.CONFLICT,))
        self.assertEqual(
            (pulled.keyword_rank, pulled.vector_rank, pulled.fused), (None, None, 0.0)
        )
        self.assertEqual(pulled.score, 0.0)
        self.assertEqual(len(result.conflicts), 1)

    async def test_a_conflict_with_an_inactive_memory_is_not_a_group(self):
        me = self.user()
        hit = self.seed(
            "Friday", "deploy backend friday", owner=me.user_id, embed=False
        )
        history = self.seed(
            "Old",
            "deploy backend friday old",
            owner=me.user_id,
            status="superseded",
            embed=False,
        )
        self.seed_relation(hit.version_id, history.version_id)
        result = await self.retrieve(me, QUERY)
        self.assertEqual(titles(result), ["Friday"])
        self.assertEqual(result.conflicts, ())

    async def test_a_group_that_does_not_fit_the_limit_is_dropped_whole_and_counted(
        self,
    ):
        me = self.user()
        a = self.seed("A", "deploy backend friday a", owner=me.user_id, embed=False)
        b = self.seed("B", "deploy backend friday b", owner=me.user_id, embed=False)
        self.seed_relation(a.version_id, b.version_id)
        single = self.seed("Single", "deploy notes", owner=me.user_id, embed=False)
        result = await self.retrieve(me, QUERY, limit=1)
        self.assertEqual([h.version_id for h in result.hits], [single.version_id])
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.dropped_conflict_groups, 1)
        fits = await self.retrieve(me, QUERY, limit=2)
        self.assertEqual(len(fits.conflicts), 1)
        self.assertEqual(fits.dropped_conflict_groups, 0)

    async def test_too_many_conflict_partners_are_reported_as_incomplete(self):
        me = self.user()
        hit = self.seed(
            "Hub", "deploy backend friday hub", owner=me.user_id, embed=False
        )
        for n in range(retrieval_limits.MAX_CONFLICT_PARTNERS + 1):
            partner = self.seed(
                f"P{n}", f"unrelated {n}", owner=me.user_id, embed=False
            )
            self.seed_relation(partner.version_id, hit.version_id)
        result = await self.retrieve(me, QUERY, limit=50)
        self.assertTrue(result.conflicts_incomplete)
        self.assertEqual(len(result.hits), 1 + retrieval_limits.MAX_CONFLICT_PARTNERS)
        few = self.new_user_with_conflict()
        self.assertFalse((await self.retrieve(few, QUERY)).conflicts_incomplete)

    def new_user_with_conflict(self):
        me = self.user()
        a = self.seed("X", "deploy backend friday x", owner=me.user_id, embed=False)
        b = self.seed("Y", "deploy backend friday y", owner=me.user_id, embed=False)
        self.seed_relation(a.version_id, b.version_id)
        return me

    async def test_conflicting_copies_are_not_merged_as_duplicates(self):
        me = self.user()
        text = "deploy the backend every friday after the merge is green"
        a = self.seed("Rule", text, owner=me.user_id)
        b = self.seed("Rule", text, owner=me.user_id)
        self.seed_relation(a.version_id, b.version_id)
        result = await self.retrieve(me, QUERY)
        self.assertEqual(len(result.hits), 2)
        self.assertEqual(len(result.conflicts), 1)
        self.assertTrue(all(h.duplicates == () for h in result.hits))

    async def test_a_copy_of_both_ends_of_a_conflict_does_not_hide_the_conflict(self):
        # ``best`` is a confirmed copy of ``a`` and of ``b``; ``a`` and ``b`` conflict.
        # Merging both into ``best`` would leave one memory and no trace of the
        # disagreement: the second copy has to stay a memory of its own.
        me = self.user()
        text = "deploy the backend every friday after the merge is green"
        best = self.seed("Rule", text, owner=me.user_id, importance=100)
        first = self.seed(
            "Rule", text, owner=me.user_id, confirmation="inferred", importance=60
        )
        second = self.seed(
            "Rule", text, owner=me.user_id, confirmation="inferred", importance=50
        )
        self.seed_relation(first.version_id, second.version_id)

        result = await self.retrieve(me, QUERY)

        self.assertEqual(len(result.hits), 2)
        ids = {h.version_id: h for h in result.hits}
        self.assertIn(best.version_id, ids)
        # The conflict is a group of the two survivors, and nobody chose.
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(set(result.conflicts[0].version_ids), set(ids))
        self.assertTrue(all(h.conflict_group == 0 for h in result.hits))
        # One of the two ends was merged into ``best`` and is listed there.
        merged = {d for h in result.hits for d in h.duplicates}
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged <= {first.version_id, second.version_id})
        self.assertEqual(ids[best.version_id].duplicates, tuple(merged))

    async def test_other_relation_types_do_not_make_a_conflict(self):
        me = self.user()
        a = self.seed("A", "deploy backend friday a", owner=me.user_id, embed=False)
        b = self.seed("B", "deploy backend friday b", owner=me.user_id, embed=False)
        self.seed_relation(a.version_id, b.version_id, "extends")
        self.assertEqual((await self.retrieve(me, QUERY)).conflicts, ())


if __name__ == "__main__":
    import unittest

    unittest.main()
