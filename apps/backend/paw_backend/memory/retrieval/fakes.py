"""Deterministic stand-ins for the Embedder and the Reranker (tests, development).

They need no model and no GPU. They are NOT a benchmark candidate: the hashing
Embedder finds shared words and Japanese character pairs, never meaning, and the
Reranker counts overlapping terms. What they are for is a retrieval whose result
depends only on its inputs, so that permission, ordering and fusion behaviour can
be tested exactly.
"""

import hashlib
import math
from collections.abc import Sequence

from paw_backend.memory.fulltext import features
from paw_backend.memory.retrieval.protocols import RerankCandidate

DEFAULT_MODEL_ID = "fake-hashing-embedder-v1"
DEFAULT_DIMENSIONS = 64


class HashingEmbedder:
    """A bag of words and character pairs, hashed into ``dimensions`` and normalised.

    Each feature (:func:`~paw_backend.memory.fulltext.features`) adds ``+1``
    or ``-1`` (the sign is a hash bit) to one of the dimensions; the result is
    scaled to length 1. A text with no feature gets one fixed feature, so the
    vector is never the zero vector (whose cosine distance is undefined).
    Identical texts give identical vectors, and texts that share features are
    closer than texts that do not.
    """

    def __init__(
        self,
        dimensions: int = DEFAULT_DIMENSIONS,
        model_id: str = DEFAULT_MODEL_ID,
    ) -> None:
        self._dimensions = dimensions
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def vector(self, text: str) -> list[float]:
        values = [0.0] * self._dimensions
        for feature in features(text) or ("<empty>",):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "big") % self._dimensions
            values[slot] += 1.0 if digest[4] & 1 else -1.0
        length = math.sqrt(sum(value * value for value in values))
        if length == 0.0:  # features that cancelled out: fall back to one axis
            values[0] = 1.0
            return values
        return [value / length for value in values]

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.vector(text) for text in texts]


class OverlapReranker:
    """Scores a candidate by the share of the query's terms its text contains."""

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate]
    ) -> list[float]:
        wanted = set(features(query))
        scores: list[float] = []
        for candidate in candidates:
            if not wanted:
                scores.append(0.0)
                continue
            present = set(features(f"{candidate.title} {candidate.content}"))
            scores.append(len(wanted & present) / len(wanted))
        return scores
