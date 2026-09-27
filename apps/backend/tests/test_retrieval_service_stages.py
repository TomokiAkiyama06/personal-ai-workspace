"""Embedder, Reranker, System Policy and deadlines around a retrieval.

Real PostgreSQL. A foreign component that fails, is slow or answers nonsense must
never fail the permission step or leak into a response, and a quality stage that
fails degrades the answer instead of losing it.
"""

import asyncio
import math
from decimal import Decimal
from fractions import Fraction

from paw_backend.authz import Principal, SystemRole
from paw_backend.memory.retrieval import (
    Component,
    DegradedStage,
    RankingPolicy,
    RetrievalSourceError,
    RetrievalTimeoutError,
)
from paw_backend.memory.retrieval import limits as retrieval_limits
from paw_backend.memory.shared import StaticPolicySource, SystemPolicyItem

from .retrieval_pg_support import (
    CountingPolicies,
    FailingEmbedder,
    FixedEmbedder,
    PostgresRetrievalTestCase,
    RecordingReranker,
    requires_postgres,
    titles,
)


class ExplodingInt(int):
    def __float__(self):
        raise RuntimeError(SECRET)


class ExplodingFloat(float):
    def __float__(self):
        raise RuntimeError(SECRET)


# Everything a faulty component may put where a number belongs.
HOSTILE_NUMBERS = [
    10**400,
    -(10**400),
    10**309,
    True,
    False,
    math.nan,
    math.inf,
    -math.inf,
    Decimal("1e400"),
    Decimal("0.5"),
    Fraction(1, 2),
    "0.5",
    b"1",
    None,
    complex(1, 0),
    object(),
    [1],
    ExplodingInt(1),
    ExplodingFloat(1.0),
]

QUERY = "deploy backend friday"
TEXT = "deploy backend friday"
SECRET = "secret-connection-string-hunter2"
DIMENSIONS = 64
GOOD = [0.5] * DIMENSIONS


@requires_postgres
class EmbedderDegradationTest(PostgresRetrievalTestCase):
    def failing(self, behaviour):
        return self.new_retriever(embedder=FailingEmbedder(behaviour))

    async def test_a_failing_embedder_leaves_a_keyword_answer_and_says_so(self):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        retriever = self.failing(RuntimeError(SECRET))
        with self.assertLogs("paw_backend.memory.retrieval.stages", "WARNING") as logs:
            result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(titles(result), ["mine"])
        self.assertEqual(result.degraded, (DegradedStage.VECTOR,))
        self.assertIsNone(result.hits[0].vector_rank)
        self.assertEqual(
            logs.output,
            [
                "WARNING:paw_backend.memory.retrieval.stages:retrieval component"
                " failed: component=embedder exception_type=RuntimeError"
            ],
        )

    async def test_a_foreign_exception_class_is_logged_by_a_fixed_word(self):
        class Boom(Exception):
            pass

        Boom.__name__ = f"Boom{SECRET}"
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        with self.assertLogs("paw_backend.memory.retrieval.stages", "WARNING") as logs:
            await self.retrieve(me, QUERY, retriever=self.failing(Boom(SECRET)))
        self.assertIn("exception_type=component_error", logs.output[0])
        self.assertNotIn("hunter2", logs.output[0])

    async def test_every_answer_that_is_not_one_good_vector_degrades(self):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        nan, inf = float("nan"), float("inf")
        wrong = [
            None,
            [],
            "string",
            b"bytes",
            [GOOD, GOOD],
            [GOOD[:-1]],
            [GOOD + [0.5]],
            [[nan] + GOOD[1:]],
            [[inf] + GOOD[1:]],
            [[True] * DIMENSIONS],
            [["0.5"] * DIMENSIONS],
            [[None] * DIMENSIONS],
            [[0.0] * DIMENSIONS],  # no direction
            ["x" * DIMENSIONS],
            [5],
            42,
        ]
        for answer in wrong:
            with self.subTest(answer=repr(answer)[:30]):
                result = await self.retrieve(me, QUERY, retriever=self.failing(answer))
                self.assertEqual(result.degraded, (DegradedStage.VECTOR,))
                self.assertEqual(titles(result), ["mine"])

    async def test_hostile_numbers_in_a_vector_degrade_instead_of_failing_the_call(
        self,
    ):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        for value in HOSTILE_NUMBERS:
            with self.subTest(value=repr(value)[:30]):
                vector = [value] + [0.5] * (DIMENSIONS - 1)
                result = await self.retrieve(
                    me, QUERY, retriever=self.failing([vector])
                )
                self.assertEqual(result.degraded, (DegradedStage.VECTOR,))
                self.assertEqual(titles(result), ["mine"])

    async def test_a_hostile_sequence_degrades_instead_of_failing_the_call(self):
        class Refuses(list):
            def __len__(self):
                raise RuntimeError(SECRET)

        class RefusesToBeRead(list):
            def __getitem__(self, index):
                raise RuntimeError(SECRET)

            def __iter__(self):
                raise RuntimeError(SECRET)

        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        for answer in (
            Refuses([GOOD]),
            [Refuses(GOOD)],
            RefusesToBeRead([GOOD]),
            [RefusesToBeRead(GOOD)],
        ):
            with self.subTest(answer=type(answer).__name__):
                result = await self.retrieve(me, QUERY, retriever=self.failing(answer))
                self.assertEqual(result.degraded, (DegradedStage.VECTOR,))

    async def test_huge_but_finite_values_are_a_direction_not_an_overflow(self):
        # pgvector refuses 1e39 (float4) and its distance overflows at 1e30: the query
        # vector is scaled to length one first (the direction is all a cosine uses).
        me = self.user()
        for scale in (1e-300, 1.0, 1e30, 1e39, 1e300, 10**39):
            with self.subTest(scale=scale):
                self.clean_tables()
                embedder = FixedEmbedder(
                    {QUERY: [3 * scale, 4 * scale, 0.0]}, model_id="fixed-test-model"
                )
                self.seed(
                    "aligned",
                    "x",
                    owner=me.user_id,
                    embedding=[0.3, 0.4, 0.0],
                    model_id="fixed-test-model",
                )
                self.seed(
                    "orthogonal",
                    "y",
                    owner=me.user_id,
                    embedding=[0.0, 0.0, 1.0],
                    model_id="fixed-test-model",
                )
                retriever = self.new_retriever(embedder=embedder)
                result = await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(result.degraded, ())
                self.assertEqual(titles(result)[0], "aligned")
                self.assertAlmostEqual(result.hits[0].vector_similarity, 1.0, places=5)
                self.assertAlmostEqual(result.hits[1].vector_similarity, 0.0, places=5)

    async def test_a_good_answer_of_ints_and_floats_is_accepted(self):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id)
        vector = [1, 0.5] + [0] * (DIMENSIONS - 2)
        result = await self.retrieve(me, QUERY, retriever=self.failing([vector]))
        self.assertEqual(result.degraded, ())

    async def test_a_slow_embedder_is_cut_at_the_stage_timeout(self):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)

        async def forever(texts):
            await asyncio.sleep(3600)

        retriever = self.new_retriever(
            embedder=FailingEmbedder(forever), stage_timeout_seconds=0.2
        )
        async with asyncio.timeout(30):
            result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(result.degraded, (DegradedStage.VECTOR,))
        self.assertEqual(titles(result), ["mine"])

    async def test_no_keywords_and_a_dead_embedder_answer_empty_but_degraded(
        self,
    ):
        me = self.user()
        self.seed("mine", TEXT, owner=me.user_id, embed=False)
        result = await self.retrieve(
            me, "!!!", retriever=self.failing(RuntimeError("x"))
        )
        self.assertEqual(result.hits, ())
        self.assertEqual(result.degraded, (DegradedStage.VECTOR,))

    async def test_the_embedder_is_never_asked_when_the_user_may_read_nothing(self):
        embedder = FailingEmbedder([GOOD])
        system = self.user()
        outsider = type(system)(system.user_id, "system")
        retriever = self.new_retriever(embedder=embedder)
        result = await self.retrieve(outsider, QUERY, retriever=retriever)
        self.assertEqual(result.hits, ())
        self.assertEqual(embedder.calls, 0)

    async def test_the_embedder_sees_only_the_query_text(self):
        me = self.user()
        seen = []

        async def record(texts):
            seen.extend(texts)
            return [GOOD]

        self.seed("mine", "private words inside", owner=me.user_id)
        await self.retrieve(me, QUERY, retriever=self.failing(record))
        self.assertEqual(seen, [QUERY])


@requires_postgres
class RerankerTest(PostgresRetrievalTestCase):
    async def test_the_reranker_sees_only_readable_texts_in_fused_order_without_ids(
        self,
    ):
        me = self.user()
        other = self.user()
        self.seed("first", "deploy backend friday first", owner=me.user_id, embed=False)
        self.seed("second", "deploy backend", owner=me.user_id, embed=False)
        self.seed(
            "hidden", "deploy backend friday hidden", owner=other.user_id, embed=False
        )
        reranker = RecordingReranker()
        retriever = self.new_retriever(reranker=reranker)
        await self.retrieve(me, QUERY, retriever=retriever)
        ((query, shown),) = reranker.calls
        self.assertEqual(query, QUERY)
        self.assertEqual([c.title for c in shown], ["first", "second"])
        self.assertEqual([c.index for c in shown], [0, 1])
        self.assertNotIn("hidden", reranker.seen_texts)
        self.assertEqual({type(c).__name__ for c in shown}, {"RerankCandidate"})
        self.assertEqual(
            set(type(shown[0]).__dataclass_fields__), {"index", "title", "content"}
        )

    async def test_long_content_is_cut_for_the_reranker_but_not_in_the_answer(self):
        me = self.user()
        content = "deploy " + "x" * (retrieval_limits.MAX_RERANK_CONTENT_CHARS + 500)
        self.seed("long", content, owner=me.user_id, embed=False)
        reranker = RecordingReranker()
        retriever = self.new_retriever(reranker=reranker)
        result = await self.retrieve(me, QUERY, retriever=retriever)
        ((_, shown),) = reranker.calls
        self.assertEqual(
            len(shown[0].content), retrieval_limits.MAX_RERANK_CONTENT_CHARS
        )
        self.assertEqual(result.hits[0].content, content)

    async def test_the_reranker_is_not_called_when_there_is_nothing_to_rerank(self):
        me = self.user()
        reranker = RecordingReranker()
        retriever = self.new_retriever(reranker=reranker)
        result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual((result.hits, result.degraded, reranker.calls), ((), (), []))

    async def test_the_number_of_reranked_candidates_is_bounded(self):
        me = self.user()
        for n in range(6):
            self.seed(f"n{n}", f"deploy backend {n}", owner=me.user_id, embed=False)
        reranker = RecordingReranker()
        retriever = self.new_retriever(reranker=reranker, rerank_candidates=4)
        result = await self.retrieve(me, QUERY, retriever=retriever, limit=50)
        ((_, shown),) = reranker.calls
        self.assertEqual(len(shown), 4)
        self.assertEqual(len(result.hits), 4)

    async def test_a_failing_reranker_keeps_the_fused_order_and_says_so(self):
        me = self.user()
        self.seed("full", "deploy backend friday full", owner=me.user_id, embed=False)
        self.seed("part", "deploy notes", owner=me.user_id, embed=False)
        reranker = RecordingReranker(RuntimeError(SECRET))
        retriever = self.new_retriever(reranker=reranker)
        with self.assertLogs("paw_backend.memory.retrieval.stages", "WARNING") as logs:
            result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(titles(result), ["full", "part"])
        self.assertEqual(result.degraded, (DegradedStage.RERANK,))
        self.assertTrue(all(h.rerank_score is None for h in result.hits))
        self.assertNotIn("hunter2", "".join(logs.output))

    async def test_every_answer_that_is_not_one_score_per_candidate_in_range_degrades(
        self,
    ):
        me = self.user()
        self.seed("a", "deploy backend friday a", owner=me.user_id, embed=False)
        self.seed("b", "deploy backend friday b", owner=me.user_id, embed=False)

        async def nothing(query, candidates):
            return None

        wrong = [
            nothing,
            [],
            [0.5],
            [0.5, 0.5, 0.5],
            "ab",
            [1.5, 0.5],
            [-0.1, 0.5],
            [math.nan, 0.5],
            [math.inf, 0.5],
            [True, 0.5],
            ["0.5", 0.5],
            [None, None],
        ]
        for answer in wrong:
            with self.subTest(answer=repr(answer)[:30]):
                retriever = self.new_retriever(reranker=RecordingReranker(answer))
                result = await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(result.degraded, (DegradedStage.RERANK,))
                self.assertEqual(len(result.hits), 2)

    async def test_hostile_numbers_as_scores_degrade_instead_of_failing_the_call(self):
        me = self.user()
        self.seed("a", "deploy backend friday a", owner=me.user_id, embed=False)
        self.seed("b", "deploy backend friday b", owner=me.user_id, embed=False)
        for value in HOSTILE_NUMBERS:
            with self.subTest(value=repr(value)[:30]):
                retriever = self.new_retriever(reranker=RecordingReranker([value, 0.5]))
                result = await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(result.degraded, (DegradedStage.RERANK,))
                self.assertEqual(len(result.hits), 2)

    async def test_a_hostile_sequence_of_scores_degrades_instead_of_failing_the_call(
        self,
    ):
        class Refuses(list):
            def __len__(self):
                raise RuntimeError(SECRET)

        class RefusesToBeRead(list):
            def __iter__(self):
                raise RuntimeError(SECRET)

        me = self.user()
        self.seed("a", "deploy backend friday a", owner=me.user_id, embed=False)
        for answer in (Refuses([0.5]), RefusesToBeRead([0.5])):
            with self.subTest(answer=type(answer).__name__):
                retriever = self.new_retriever(reranker=RecordingReranker(answer))
                result = await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(result.degraded, (DegradedStage.RERANK,))
                self.assertEqual(titles(result), ["a"])

    async def test_the_boundary_scores_zero_and_one_are_accepted(self):
        me = self.user()
        self.seed("a", "deploy backend friday a", owner=me.user_id, embed=False)
        self.seed("b", "deploy backend friday b", owner=me.user_id, embed=False)
        retriever = self.new_retriever(reranker=RecordingReranker([0, 1.0]))
        result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(result.degraded, ())
        self.assertEqual(sorted(h.rerank_score for h in result.hits), [0.0, 1.0])

    async def test_a_slow_reranker_is_cut_at_the_stage_timeout(self):
        me = self.user()
        self.seed("a", TEXT, owner=me.user_id, embed=False)

        async def forever(query, candidates):
            await asyncio.sleep(3600)

        retriever = self.new_retriever(
            reranker=RecordingReranker(forever), stage_timeout_seconds=0.2
        )
        async with asyncio.timeout(30):
            result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(result.degraded, (DegradedStage.RERANK,))

    async def test_the_rerank_weight_blends_the_score_into_the_relevance(self):
        me = self.user()
        self.seed("only", TEXT, owner=me.user_id, embed=False)
        for weight, expected in ((0.0, 0.5), (0.5, 0.5 * 0.5 + 0.5 * 0.2), (1.0, 0.2)):
            with self.subTest(weight=weight):
                retriever = self.new_retriever(
                    reranker=RecordingReranker([0.2]),
                    ranking=RankingPolicy(rerank_weight=weight),
                )
                (hit,) = (await self.retrieve(me, QUERY, retriever=retriever)).hits
                self.assertAlmostEqual(hit.relevance, expected)
                self.assertEqual(hit.rerank_score, 0.2)


@requires_postgres
class SystemPolicyTest(PostgresRetrievalTestCase):
    def use(self, *items):
        self.counting = CountingPolicies(StaticPolicySource(items))
        return self.new_retriever(policies=self.counting)

    async def test_a_shared_memory_covered_by_a_system_policy_is_not_returned(self):
        me = self.user()
        self.seed(
            "covered", TEXT, scope="shared", subjects=["merge.permission"], embed=False
        )
        self.seed(
            "free", TEXT + " free", scope="shared", subjects=["docs"], embed=False
        )
        self.seed("none", TEXT + " none", scope="shared", embed=False)
        retriever = self.use(
            SystemPolicyItem("no-merge", "merge", "Never merge alone.")
        )
        result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(sorted(titles(result)), ["free", "none"])

    async def test_the_policy_covers_the_subject_and_everything_below_it_only(self):
        me = self.user()
        for subject in ("merge", "merge.permission", "mergeable", "merge_x"):
            self.seed(
                subject,
                TEXT + f" {subject}",
                scope="shared",
                subjects=[subject],
                embed=False,
            )
        retriever = self.use(SystemPolicyItem("p", "merge", "rule"))
        result = await self.retrieve(me, QUERY, retriever=retriever, limit=50)
        self.assertEqual(sorted(titles(result)), ["merge_x", "mergeable"])

    async def test_a_policy_never_touches_private_or_project_memory(self):
        project = self.seed_project()
        me = self.member_of(project)
        self.seed("mine", TEXT, owner=me.user_id, subjects=["merge"], embed=False)
        self.seed(
            "project",
            TEXT,
            scope="project",
            project=project,
            subjects=["merge"],
            embed=False,
        )
        retriever = self.use(SystemPolicyItem("p", "merge", "rule"))
        # Whether or not the policy is consulted, it never touches these scopes.
        for scopes in (["user", "project"], None):
            with self.subTest(scopes=scopes):
                result = await self.retrieve(
                    me, QUERY, retriever=retriever, scopes=scopes
                )
                self.assertEqual(sorted(titles(result)), ["mine", "project"])

    async def test_the_policy_is_loaded_only_when_the_shared_scope_is_searched(self):
        # Loaded BEFORE the candidates (the covered memories are left out by the
        # candidate statements, ahead of their limits), so it depends on the scope,
        # not on whether a shared memory happens to be a candidate.
        me = self.user()
        retriever = self.use()
        for scopes in (["user"], ["user", "project"], []):
            await self.retrieve(me, QUERY, retriever=retriever, scopes=scopes)
        self.assertEqual(self.counting.calls, 0)
        await self.retrieve(me, QUERY, retriever=retriever)
        await self.retrieve(me, QUERY, retriever=retriever, scopes=["shared"])
        self.assertEqual(self.counting.calls, 2)  # once per call, never cached

    async def test_the_policy_is_not_loaded_for_a_caller_who_may_not_read_shared_memory(
        self,
    ):
        system = Principal(self.seed_user(), SystemRole.SYSTEM)
        retriever = self.use()
        await self.retrieve(system, QUERY, retriever=retriever)
        self.assertEqual(self.counting.calls, 0)

    async def test_a_policy_that_cannot_be_loaded_fails_the_call_without_any_memory(
        self,
    ):
        me = self.user()
        self.seed("shared", TEXT, scope="shared", embed=False)
        self.seed("mine", TEXT, owner=me.user_id, embed=False)

        class Hangs:
            async def items(self):
                await asyncio.sleep(3600)

        class Bad:
            async def items(self):
                return "not a list"

        sources = [
            CountingPolicies(RuntimeError(SECRET)),
            Hangs(),
            Bad(),
        ]
        for source in sources:
            with self.subTest(source=type(source).__name__):
                retriever = self.new_retriever(
                    policies=source, stage_timeout_seconds=0.2
                )
                async with asyncio.timeout(30):
                    with self.assertRaises(RetrievalSourceError) as caught:
                        await self.retrieve(me, QUERY, retriever=retriever)
                self.assertEqual(caught.exception.component, Component.POLICY_SOURCE)
                self.assertNotIn("hunter2", str(caught.exception))

    async def test_a_shared_memory_with_malformed_subjects_is_left_out(
        self,
    ):
        me = self.user()
        for index, attributes in enumerate(
            [
                {"policy_subjects": "merge"},
                {"policy_subjects": [1]},
                {"policy_subjects": ["Merge"]},
                {"policy_subjects": {"merge": 1}},
                {"policy_subjects": [f"s{n}" for n in range(21)]},
            ]
        ):
            self.seed(
                f"bad{index}",
                TEXT + f" {index}",
                scope="shared",
                attributes=attributes,
                embed=False,
            )
        self.seed(
            "good",
            TEXT,
            scope="shared",
            attributes={"policy_subjects": ["docs"]},
            embed=False,
        )
        result = await self.retrieve(me, QUERY, retriever=self.use(), limit=50)
        self.assertEqual(titles(result), ["good"])


@requires_postgres
class DeadlineTest(PostgresRetrievalTestCase):
    async def test_the_whole_call_is_bounded(self):
        me = self.user()
        self.seed("a", TEXT, owner=me.user_id, embed=False)

        async def forever(query, candidates):
            await asyncio.sleep(3600)

        retriever = self.new_retriever(
            reranker=RecordingReranker(forever),
            timeout_seconds=0.3,
            stage_timeout_seconds=60,
        )
        async with asyncio.timeout(30):
            with self.assertRaises(RetrievalTimeoutError):
                await self.retrieve(me, QUERY, retriever=retriever)

    async def test_a_timeout_error_raised_by_a_stage_is_not_the_deadline(self):
        me = self.user()
        self.seed("a", TEXT, owner=me.user_id, embed=False)
        retriever = self.new_retriever(embedder=FailingEmbedder(TimeoutError()))
        result = await self.retrieve(me, QUERY, retriever=retriever)
        self.assertEqual(result.degraded, (DegradedStage.VECTOR,))

    async def test_the_clock_is_read_once_per_call(self):
        me = self.user()
        reads = []

        def clock():
            reads.append(1)
            return self.clock()

        self.seed("a", TEXT, owner=me.user_id, embed=False)
        await self.retrieve(me, QUERY, retriever=self.new_retriever(clock=clock))
        self.assertEqual(len(reads), 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
