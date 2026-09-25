"""The deterministic Embedder and Reranker (no database)."""

import asyncio
import math
import unittest

from paw_backend.memory.retrieval.fakes import HashingEmbedder, OverlapReranker
from paw_backend.memory.retrieval.protocols import RerankCandidate


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


class HashingEmbedderTest(unittest.TestCase):
    def setUp(self):
        self.embedder = HashingEmbedder()

    def test_it_reports_its_model_and_dimension(self):
        self.assertEqual(self.embedder.model_id, "fake-hashing-embedder-v1")
        self.assertEqual(self.embedder.dimensions, 64)
        self.assertEqual(HashingEmbedder(8, "m").dimensions, 8)
        self.assertEqual(HashingEmbedder(8, "m").model_id, "m")

    def test_vectors_have_the_dimension_and_unit_length(self):
        for text in ("deploy on friday", "デプロイは金曜日", "x", ""):
            with self.subTest(text=text):
                vector = self.embedder.vector(text)
                self.assertEqual(len(vector), 64)
                self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0)

    def test_it_is_deterministic(self):
        self.assertEqual(
            self.embedder.vector("Same Text"), HashingEmbedder().vector("Same Text")
        )

    def test_text_that_shares_features_is_closer_than_text_that_does_not(self):
        base = self.embedder.vector("deploy the backend service on friday")
        near = self.embedder.vector("deploy the backend service on monday")
        far = self.embedder.vector("completely unrelated cooking recipe")
        self.assertGreater(cosine(base, near), cosine(base, far))
        self.assertAlmostEqual(cosine(base, base), 1.0)

    def test_case_and_width_do_not_matter(self):
        self.assertEqual(
            self.embedder.vector("ＡＢＣ deploy"), self.embedder.vector("abc DEPLOY")
        )

    def test_a_text_without_features_still_has_a_nonzero_vector(self):
        vector = self.embedder.vector("!!!")
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in vector)), 1.0)
        self.assertEqual(vector, self.embedder.vector(""))

    def test_embed_returns_one_vector_per_text_in_order(self):
        result = asyncio.run(self.embedder.embed(["a b c", "d e f"]))
        self.assertEqual(
            result, [self.embedder.vector("a b c"), self.embedder.vector("d e f")]
        )
        self.assertEqual(asyncio.run(self.embedder.embed([])), [])


class OverlapRerankerTest(unittest.TestCase):
    def rerank(self, query, *texts):
        candidates = [RerankCandidate(i, t, "") for i, t in enumerate(texts)]
        return asyncio.run(OverlapReranker().rerank(query, candidates))

    def test_the_score_is_the_share_of_query_terms_the_text_contains(self):
        scores = self.rerank(
            "deploy backend friday", "deploy backend friday", "deploy", "cooking"
        )
        self.assertEqual(scores, [1.0, 1 / 3, 0.0])

    def test_content_counts_as_well_as_the_title(self):
        candidate = RerankCandidate(0, "title", "deploy notes")
        self.assertEqual(
            asyncio.run(OverlapReranker().rerank("deploy", [candidate])), [1.0]
        )

    def test_a_query_without_terms_scores_zero(self):
        self.assertEqual(self.rerank("!!!", "anything"), [0.0])

    def test_no_candidates_give_no_scores(self):
        self.assertEqual(self.rerank("query"), [])


if __name__ == "__main__":
    unittest.main()
