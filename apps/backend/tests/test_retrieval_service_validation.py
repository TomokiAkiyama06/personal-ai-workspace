"""Argument validation of ``HybridRetriever``: every bad value, no database.

The ``Database`` used here is not configured: any attempt to reach it would raise
``DatabaseNotConfiguredError``, so an ``InvalidRetrievalInputError`` proves the
value was refused before the database (or any component) was touched.
"""

import dataclasses
import math
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from paw_backend.authz import Authorizer, InMemoryAuditSink, Principal, SystemRole
from paw_backend.db import Database
from paw_backend.memory.retrieval import (
    HashingEmbedder,
    HybridRetriever,
    InvalidRetrievalInputError,
    RetrievalQuery,
)
from paw_backend.memory.retrieval import limits as retrieval_limits
from paw_backend.memory.shared import StaticPolicySource
from paw_backend.memory.shared.errors import InputProblem

from .support import make_settings


class Callable:
    """An object whose ``name`` attribute is callable."""

    def __init__(self, *names):
        for name in names:
            setattr(self, name, lambda *args, **kwargs: None)


def parts(**overrides):
    values = {
        "database": Database(make_settings()),
        "authorizer": Authorizer(InMemoryAuditSink()),
        "embedder": HashingEmbedder(),
        "policies": StaticPolicySource(()),
    }
    values.update(overrides)
    return values


def build(**overrides):
    values = parts()
    options = {}
    for name, value in overrides.items():
        (values if name in values else options)[name] = value
    return HybridRetriever(
        values["database"],
        values["authorizer"],
        values["embedder"],
        values["policies"],
        **options,
    )


class Embedder:
    def __init__(self, model_id="m", dimensions=8, embed=True):
        self.model_id = model_id
        self.dimensions = dimensions
        if embed:
            self.embed = lambda texts: None


class ConstructorTest(unittest.TestCase):
    def test_a_valid_retriever_builds_without_touching_the_database(self):
        self.assertIsInstance(build(), HybridRetriever)
        self.assertIsInstance(
            build(
                reranker=Callable("rerank"),
                repo_acls=Callable("repo_acls"),
                project_groups=Callable("project_group_ids"),
            ),
            HybridRetriever,
        )

    def test_every_bad_collaborator_or_option_is_refused_up_front(self):
        nan, inf = math.nan, math.inf
        cases = [
            ("database", {"database": None}),
            ("database", {"database": "postgresql://x"}),
            ("database", {"database": object()}),
            ("authorizer", {"authorizer": None}),
            ("authorizer", {"authorizer": object()}),
            ("authorizer", {"authorizer": Callable()}),
            ("embedder", {"embedder": None}),
            ("embedder", {"embedder": Embedder(embed=False)}),
            ("embedder", {"embedder": Embedder(model_id="")}),
            ("embedder", {"embedder": Embedder(model_id="  ")}),
            ("embedder", {"embedder": Embedder(model_id=None)}),
            ("embedder", {"embedder": Embedder(model_id=5)}),
            ("embedder", {"embedder": Embedder(model_id="x" * 201)}),
            ("embedder", {"embedder": Embedder(model_id="a\x00b")}),
            ("embedder", {"embedder": Embedder(dimensions=0)}),
            ("embedder", {"embedder": Embedder(dimensions=16001)}),
            ("embedder", {"embedder": Embedder(dimensions=True)}),
            ("embedder", {"embedder": Embedder(dimensions="8")}),
            ("embedder", {"embedder": Embedder(dimensions=8.0)}),
            ("embedder", {"embedder": Embedder(dimensions=None)}),
            ("policies", {"policies": None}),
            ("policies", {"policies": object()}),
            ("reranker", {"reranker": object()}),
            ("reranker", {"reranker": "rerank"}),
            ("repo_acls", {"repo_acls": object()}),
            ("project_groups", {"project_groups": object()}),
            ("ranking", {"ranking": {}}),
            ("ranking", {"ranking": "default"}),
            ("clock", {"clock": None}),
            ("clock", {"clock": 5}),
            ("keyword_candidates", {"keyword_candidates": 0}),
            (
                "keyword_candidates",
                {"keyword_candidates": retrieval_limits.MAX_CANDIDATES + 1},
            ),
            ("keyword_candidates", {"keyword_candidates": True}),
            ("keyword_candidates", {"keyword_candidates": "5"}),
            ("keyword_candidates", {"keyword_candidates": 5.0}),
            ("vector_candidates", {"vector_candidates": 0}),
            (
                "vector_candidates",
                {"vector_candidates": retrieval_limits.MAX_CANDIDATES + 1},
            ),
            ("vector_candidates", {"vector_candidates": None}),
            ("rerank_candidates", {"rerank_candidates": 0}),
            (
                "rerank_candidates",
                {"rerank_candidates": retrieval_limits.MAX_RERANK_CANDIDATES + 1},
            ),
            ("timeout_seconds", {"timeout_seconds": 0}),
            ("timeout_seconds", {"timeout_seconds": -1}),
            ("timeout_seconds", {"timeout_seconds": 60.5}),
            ("timeout_seconds", {"timeout_seconds": True}),
            ("timeout_seconds", {"timeout_seconds": "5"}),
            ("timeout_seconds", {"timeout_seconds": nan}),
            ("timeout_seconds", {"timeout_seconds": inf}),
            ("timeout_seconds", {"timeout_seconds": None}),
            ("stage_timeout_seconds", {"stage_timeout_seconds": 0}),
            ("stage_timeout_seconds", {"stage_timeout_seconds": 61}),
            ("stage_timeout_seconds", {"stage_timeout_seconds": nan}),
            ("stage_timeout_seconds", {"stage_timeout_seconds": False}),
        ]
        for field, options in cases:
            with self.subTest(
                field=field, option={k: repr(v)[:30] for k, v in options.items()}
            ):
                with self.assertRaises(InvalidRetrievalInputError) as caught:
                    build(**options)
                self.assertEqual(caught.exception.field, field)

    def test_the_boundaries_are_accepted(self):
        build(keyword_candidates=1, vector_candidates=retrieval_limits.MAX_CANDIDATES)
        build(rerank_candidates=retrieval_limits.MAX_RERANK_CANDIDATES)
        build(timeout_seconds=60, stage_timeout_seconds=0.001)
        build(embedder=Embedder(model_id="x" * 200, dimensions=16000))
        build(embedder=Embedder(dimensions=1))


class RetrieveArgumentsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.retriever = build()
        self.actor = Principal(uuid4(), SystemRole.USER)
        self.query = RetrievalQuery("deploy")

    async def test_a_wrong_actor_is_refused_before_anything_else(self):
        for actor in (None, "user", {"user_id": uuid4()}, uuid4(), object(), 5):
            with self.subTest(actor=repr(actor)[:30]):
                with self.assertRaises(InvalidRetrievalInputError) as caught:
                    await self.retriever.retrieve(actor, self.query)
                self.assertEqual(
                    (caught.exception.field, caught.exception.problem),
                    ("actor", InputProblem.WRONG_TYPE),
                )

    async def test_a_wrong_query_is_refused(self):
        for query in (None, "deploy", {"text": "deploy"}, object(), 5):
            with self.subTest(query=repr(query)[:30]):
                with self.assertRaises(InvalidRetrievalInputError) as caught:
                    await self.retriever.retrieve(self.actor, query)
                self.assertEqual(caught.exception.field, "query")

    async def test_a_query_changed_after_it_was_built_is_checked_again(self):
        with self.assertRaises(InvalidRetrievalInputError):
            dataclasses.replace(self.query, limit=0)
        with self.assertRaises(InvalidRetrievalInputError):
            dataclasses.replace(self.query, text="")

    async def test_a_bad_clock_is_refused_when_it_is_read(self):
        naive = datetime(2026, 9, 25, 12, 0, 0)
        for value, problem in (
            (naive, InputProblem.NAIVE_DATETIME),
            (None, InputProblem.WRONG_TYPE),
            ("2026-09-25", InputProblem.WRONG_TYPE),
            (5, InputProblem.WRONG_TYPE),
        ):
            with self.subTest(value=repr(value)):
                retriever = build(clock=lambda value=value: value)
                with self.assertRaises(InvalidRetrievalInputError) as caught:
                    await retriever.retrieve(self.actor, self.query)
                self.assertEqual(
                    (caught.exception.field, caught.exception.problem),
                    ("clock", problem),
                )

    async def test_a_good_call_on_an_unconfigured_database_reaches_the_database_last(
        self,
    ):
        # With everything valid the first thing that fails is the database.
        from paw_backend.db import DatabaseNotConfiguredError

        retriever = build(clock=lambda: datetime(2026, 9, 25, tzinfo=UTC))
        with self.assertRaises(DatabaseNotConfiguredError):
            await retriever.retrieve(self.actor, self.query)


if __name__ == "__main__":
    unittest.main()
