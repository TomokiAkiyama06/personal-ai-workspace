"""Keyword, vector and hybrid search, ordering and bounds (real PostgreSQL)."""

from paw_backend.memory.retrieval import (
    DegradedStage,
    MatchSource,
    RankingPolicy,
)

from .retrieval_pg_support import (
    FixedEmbedder,
    PostgresRetrievalTestCase,
    requires_postgres,
    titles,
)

MODEL = "fixed-test-model"


@requires_postgres
class KeywordSearchTest(PostgresRetrievalTestCase):
    async def test_a_memory_that_contains_the_words_is_found_and_others_are_not(self):
        me = self.user()
        self.seed(
            "Deploy schedule",
            "We deploy the backend every Friday",
            owner=me.user_id,
            embed=False,
        )
        self.seed(
            "Coffee",
            "The team prefers dark roast coffee",
            owner=me.user_id,
            embed=False,
        )

        result = await self.retrieve(me, "when do we deploy the backend")

        self.assertEqual(titles(result), ["Deploy schedule"])
        hit = result.hits[0]
        self.assertEqual(hit.sources, (MatchSource.KEYWORD,))
        self.assertEqual(hit.keyword_rank, 1)
        self.assertIsNone(hit.vector_rank)
        self.assertGreater(hit.keyword_score, 0.0)
        self.assertLessEqual(hit.keyword_score, 1.0)
        self.assertIsNone(hit.vector_similarity)

    async def test_a_memory_that_matches_more_of_the_query_ranks_higher(self):
        me = self.user()
        self.seed("Partial", "deploy notes", owner=me.user_id, embed=False)
        self.seed("Full", "deploy backend friday notes", owner=me.user_id, embed=False)
        result = await self.retrieve(me, "deploy backend friday")
        self.assertEqual(titles(result), ["Full", "Partial"])
        self.assertEqual([h.keyword_rank for h in result.hits], [1, 2])

    async def test_matching_ignores_case_and_full_width_letters(self):
        me = self.user()
        self.seed(
            "Naming", "Use snake_case for Python modules", owner=me.user_id, embed=False
        )
        for query in ("PYTHON", "ｐｙｔｈｏｎ", "python"):
            with self.subTest(query=query):
                self.assertEqual(titles(await self.retrieve(me, query)), ["Naming"])

    async def test_japanese_text_is_found_through_character_pairs(self):
        me = self.user()
        self.seed(
            "デプロイ手順",
            "本番環境へのデプロイはマージの後に金曜日に行う",
            owner=me.user_id,
            embed=False,
        )
        self.seed(
            "コーヒー", "チームは深煎りのコーヒーを好む", owner=me.user_id, embed=False
        )
        for query in ("デプロイの手順", "金曜日に行う", "本番環境"):
            with self.subTest(query=query):
                self.assertEqual(
                    titles(await self.retrieve(me, query)), ["デプロイ手順"]
                )

    async def test_half_width_katakana_matches_the_ordinary_form(self):
        me = self.user()
        self.seed("Note", "テストの結果を確認する", owner=me.user_id, embed=False)
        self.assertEqual(titles(await self.retrieve(me, "ﾃｽﾄ")), ["Note"])

    async def test_a_query_that_is_only_punctuation_finds_nothing_by_keyword(self):
        me = self.user()
        self.seed("Anything", "some text", owner=me.user_id, embed=False)
        self.assertEqual(titles(await self.retrieve(me, "!!! ...")), [])

    async def test_tsquery_syntax_in_the_query_is_only_text(self):
        me = self.user()
        self.seed("Ops", "restart the worker", owner=me.user_id, embed=False)
        for query in (
            "worker & !restart",
            "worker | (",
            "'worker' <-> :*",
            "\\ worker \\",
        ):
            with self.subTest(query=query):
                self.assertEqual(titles(await self.retrieve(me, query)), ["Ops"])

    async def test_the_keyword_candidate_limit_bounds_the_list(self):
        me = self.user()
        for n in range(6):
            self.seed(f"Note {n}", "deploy backend", owner=me.user_id, embed=False)
        retriever = self.new_retriever(keyword_candidates=3)
        result = await self.retrieve(me, "deploy", retriever=retriever, limit=50)
        self.assertEqual(len(result.hits), 3)
        self.assertEqual(sorted(h.keyword_rank for h in result.hits), [1, 2, 3])


@requires_postgres
class VectorSearchTest(PostgresRetrievalTestCase):
    def setUp(self):
        super().setUp()
        self.fixed = FixedEmbedder({"the query": [1.0, 0.0, 0.0]}, model_id=MODEL)

    async def test_the_nearest_memories_come_first_by_cosine_similarity(self):
        me = self.user()
        self.seed(
            "near", "alpha", owner=me.user_id, embedding=[0.9, 0.1, 0.0], model_id=MODEL
        )
        self.seed(
            "middle",
            "beta",
            owner=me.user_id,
            embedding=[0.7, 0.7, 0.0],
            model_id=MODEL,
        )
        self.seed(
            "far", "gamma", owner=me.user_id, embedding=[0.0, 1.0, 0.0], model_id=MODEL
        )
        retriever = self.new_retriever(embedder=self.fixed)

        result = await self.retrieve(me, "the query", retriever=retriever)

        self.assertEqual(titles(result), ["near", "middle", "far"])
        self.assertEqual([h.vector_rank for h in result.hits], [1, 2, 3])
        self.assertAlmostEqual(
            result.hits[0].vector_similarity, 0.9 / (0.9**2 + 0.1**2) ** 0.5
        )
        self.assertAlmostEqual(result.hits[1].vector_similarity, 0.5**0.5)
        self.assertAlmostEqual(result.hits[2].vector_similarity, 0.0)
        self.assertTrue(all(h.sources == (MatchSource.VECTOR,) for h in result.hits))
        self.assertTrue(all(h.keyword_rank is None for h in result.hits))
        self.assertEqual(self.fixed.texts, ["the query"])

    async def test_a_similarity_floor_removes_far_memories(self):
        me = self.user()
        self.seed(
            "near", "alpha", owner=me.user_id, embedding=[0.9, 0.1, 0.0], model_id=MODEL
        )
        self.seed(
            "middle",
            "beta",
            owner=me.user_id,
            embedding=[0.7, 0.7, 0.0],
            model_id=MODEL,
        )
        self.seed(
            "far", "gamma", owner=me.user_id, embedding=[0.0, 1.0, 0.0], model_id=MODEL
        )
        for floor, expected in (
            (0.99, ["near"]),
            (0.7, ["near", "middle"]),
            (0.0, ["near", "middle", "far"]),
            (-1.0, ["near", "middle", "far"]),
        ):
            with self.subTest(floor=floor):
                retriever = self.new_retriever(
                    embedder=self.fixed,
                    ranking=RankingPolicy(min_vector_similarity=floor),
                )
                result = await self.retrieve(me, "the query", retriever=retriever)
                self.assertEqual(titles(result), expected)

    async def test_the_candidate_limit_counts_only_memories_above_the_floor(self):
        me = self.user()
        self.seed(
            "far", "gamma", owner=me.user_id, embedding=[0.0, 1.0, 0.0], model_id=MODEL
        )
        self.seed(
            "near", "alpha", owner=me.user_id, embedding=[1.0, 0.1, 0.0], model_id=MODEL
        )
        retriever = self.new_retriever(
            embedder=self.fixed,
            vector_candidates=1,
            ranking=RankingPolicy(min_vector_similarity=0.5),
        )
        self.assertEqual(
            titles(await self.retrieve(me, "the query", retriever=retriever)), ["near"]
        )

    async def test_only_vectors_of_the_embedders_own_model_are_compared(self):
        me = self.user()
        self.seed(
            "mine", "a", owner=me.user_id, embedding=[1.0, 0.0, 0.0], model_id=MODEL
        )
        # Another model with another dimension: never compared, never an error.
        self.seed(
            "other model", "b", owner=me.user_id, embedding=[1.0, 0.0], model_id="other"
        )
        retriever = self.new_retriever(embedder=self.fixed)
        self.assertEqual(
            titles(await self.retrieve(me, "the query", retriever=retriever)), ["mine"]
        )

    async def test_a_memory_without_an_embedding_is_reachable_by_keyword_only(self):
        me = self.user()
        self.seed("unembedded deploy", "deploy notes", owner=me.user_id, embed=False)
        self.seed(
            "embedded",
            "other",
            owner=me.user_id,
            embedding=[1.0, 0.0, 0.0],
            model_id=MODEL,
        )
        retriever = self.new_retriever(
            embedder=FixedEmbedder({"deploy": [1.0, 0.0, 0.0]}, model_id=MODEL)
        )
        result = await self.retrieve(me, "deploy", retriever=retriever)
        by_title = {h.title: h for h in result.hits}
        self.assertEqual(by_title["unembedded deploy"].sources, (MatchSource.KEYWORD,))
        self.assertEqual(by_title["embedded"].sources, (MatchSource.VECTOR,))

    async def test_the_vector_candidate_limit_bounds_the_list(self):
        me = self.user()
        for n in range(5):
            self.seed(
                f"v{n}",
                "x",
                owner=me.user_id,
                embedding=[1.0, n / 10, 0.0],
                model_id=MODEL,
            )
        retriever = self.new_retriever(embedder=self.fixed, vector_candidates=2)
        result = await self.retrieve(me, "the query", retriever=retriever, limit=50)
        self.assertEqual(titles(result), ["v0", "v1"])


@requires_postgres
class HybridSearchTest(PostgresRetrievalTestCase):
    async def test_a_memory_in_both_lists_beats_one_in_a_single_list(self):
        me = self.user()
        embedder = FixedEmbedder({"zebra": [1.0, 0.0, 0.0]}, model_id=MODEL)
        # "both" is first by keyword (three occurrences), second by vector.
        both = self.seed(
            "both",
            "zebra zebra zebra crossing",
            owner=me.user_id,
            embedding=[0.9, 0.1, 0.0],
            model_id=MODEL,
        )
        keyword_only = self.seed(
            "keyword only",
            "zebra",
            owner=me.user_id,
            embedding=[0.0, 1.0, 0.0],
            model_id=MODEL,
        )
        vector_only = self.seed(
            "vector only",
            "giraffe",
            owner=me.user_id,
            embedding=[1.0, 0.0, 0.0],
            model_id=MODEL,
        )
        retriever = self.new_retriever(embedder=embedder)

        result = await self.retrieve(me, "zebra", retriever=retriever)

        self.assertEqual(result.hits[0].version_id, both.version_id)
        hits = {h.version_id: h for h in result.hits}
        self.assertEqual(
            hits[both.version_id].sources, (MatchSource.KEYWORD, MatchSource.VECTOR)
        )
        # With no similarity floor the vector list holds every embedded memory.
        self.assertEqual(
            hits[keyword_only.version_id].sources,
            (MatchSource.KEYWORD, MatchSource.VECTOR),
        )
        self.assertEqual(
            [hits[v.version_id].vector_rank for v in (vector_only, both, keyword_only)],
            [1, 2, 3],
        )
        self.assertEqual(hits[vector_only.version_id].sources, (MatchSource.VECTOR,))
        self.assertGreater(
            hits[both.version_id].fused, hits[keyword_only.version_id].fused
        )
        self.assertGreater(
            hits[both.version_id].fused, hits[vector_only.version_id].fused
        )

    async def test_the_default_hashing_embedder_finds_a_paraphrase_by_shared_words(
        self,
    ):
        me = self.user()
        self.seed(
            "Backend deploys",
            "The backend deploys every Friday afternoon",
            owner=me.user_id,
        )
        self.seed("Recipe", "Simmer the tomato sauce for an hour", owner=me.user_id)
        result = await self.retrieve(me, "backend deploys Friday")
        self.assertEqual(result.hits[0].title, "Backend deploys")
        self.assertEqual(result.degraded, ())

    async def test_the_weights_of_the_legs_change_the_order(self):
        me = self.user()
        embedder = FixedEmbedder({"zebra": [1.0, 0.0, 0.0]}, model_id=MODEL)
        self.seed(
            "keyword",
            "zebra",
            owner=me.user_id,
            embedding=[0.0, 1.0, 0.0],
            model_id=MODEL,
        )
        self.seed(
            "vector",
            "giraffe",
            owner=me.user_id,
            embedding=[1.0, 0.0, 0.0],
            model_id=MODEL,
        )
        for weights, expected in (((3.0, 1.0), "keyword"), ((1.0, 3.0), "vector")):
            with self.subTest(weights=weights):
                retriever = self.new_retriever(
                    embedder=embedder,
                    ranking=RankingPolicy(
                        keyword_weight=weights[0],
                        vector_weight=weights[1],
                        # "keyword" is then in the keyword list only.
                        min_vector_similarity=0.5,
                    ),
                )
                result = await self.retrieve(me, "zebra", retriever=retriever)
                self.assertEqual(result.hits[0].title, expected)

    async def test_the_limit_cuts_the_result_and_the_order_is_stable(self):
        me = self.user()
        for n in range(8):
            self.seed(f"note {n}", f"deploy backend {n}", owner=me.user_id, embed=False)
        first = await self.retrieve(me, "deploy backend", limit=5)
        second = await self.retrieve(me, "deploy backend", limit=5)
        self.assertEqual(len(first.hits), 5)
        self.assertEqual(
            [h.version_id for h in first.hits], [h.version_id for h in second.hits]
        )
        wider = await self.retrieve(me, "deploy backend", limit=50)
        self.assertEqual(
            [h.version_id for h in wider.hits][:5], [h.version_id for h in first.hits]
        )

    async def test_no_memory_at_all_gives_an_empty_result(self):
        result = await self.retrieve(self.user(), "anything at all")
        self.assertEqual(result.hits, ())
        self.assertEqual(result.conflicts, ())
        self.assertEqual(result.degraded, ())

    async def test_a_reranker_reorders_and_its_score_is_reported(self):
        me = self.user()
        self.seed("Alpha", "deploy backend notes alpha", owner=me.user_id, embed=False)
        self.seed("Beta", "deploy backend notes beta", owner=me.user_id, embed=False)
        seen = []

        async def prefer_beta(query, candidates):
            seen.append([c.title for c in candidates])
            return [1.0 if c.title == "Beta" else 0.0 for c in candidates]

        from .retrieval_pg_support import RecordingReranker

        reranker = RecordingReranker(prefer_beta)
        retriever = self.new_retriever(
            reranker=reranker, ranking=RankingPolicy(rerank_weight=1.0)
        )
        result = await self.retrieve(me, "deploy backend", retriever=retriever)
        self.assertEqual(titles(result), ["Beta", "Alpha"])
        self.assertEqual([h.rerank_score for h in result.hits], [1.0, 0.0])
        self.assertEqual(reranker.calls[0][0], "deploy backend")
        self.assertEqual(sorted(seen[0]), ["Alpha", "Beta"])
        self.assertEqual(result.degraded, ())
        self.assertNotIn(DegradedStage.RERANK, result.degraded)


if __name__ == "__main__":
    import unittest

    unittest.main()
