"""Tests for the retrieval dataset loader and benchmark runner."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.metrics_collector import MetricsCollector
from benchmarks.retrieval_runner import (
    RetrievalDataset,
    RetrievalMemory,
    RetrievalQuery,
    load_dataset,
    run_benchmark,
    validate_retriever,
    visible_memory_ids,
)


class FakeRetriever:
    """A fake retriever for testing purposes."""

    def __init__(self, results):
        self.results = results  # Map of query_id -> list of memory_ids

    def retrieve(self, query_text, requester_principals, k):
        return self.results.get(query_text, [])


class PerfectRetriever:
    """A retriever that returns perfect results for testing."""

    def __init__(self, dataset):
        self.dataset = dataset

    def retrieve(self, query_text, requester_principals, k):
        # Find the query by text
        for query in self.dataset.queries:
            if query.text == query_text:
                return list(query.relevant_ids)[:k]
        return []


class LeakingRetriever:
    """A retriever that leaks memory it shouldn't be able to see."""

    def __init__(self, dataset, leaked_ids):
        self.dataset = dataset
        self.leaked_ids = leaked_ids

    def retrieve(self, query_text, requester_principals, k):
        # Find the query by text
        for query in self.dataset.queries:
            if query.text == query_text:
                # Return relevant ids + leaked ids
                result = list(query.relevant_ids)
                result.extend(self.leaked_ids)
                return result[:k]
        return []


class StaleRetriever:
    """A retriever that returns stale (non-fresh) results."""

    def __init__(self, dataset, stale_ids):
        self.dataset = dataset
        self.stale_ids = stale_ids

    def retrieve(self, query_text, requester_principals, k):
        # Find the query by text
        for query in self.dataset.queries:
            if query.text == query_text:
                # Return relevant ids + stale ids
                result = list(query.relevant_ids)
                result.extend(self.stale_ids)
                return result[:k]
        return []


class SupersededRetriever:
    """A retriever that returns superseded (non-active) results."""

    def __init__(self, dataset, superseded_ids):
        self.dataset = dataset
        self.superseded_ids = superseded_ids

    def retrieve(self, query_text, requester_principals, k):
        # Find the query by text
        for query in self.dataset.queries:
            if query.text == query_text:
                # Return relevant ids + superseded ids
                result = list(query.relevant_ids)
                result.extend(self.superseded_ids)
                return result[:k]
        return []


class WrongScopeRetriever:
    """A retriever that returns wrong-scope results."""

    def __init__(self, dataset, wrong_scope_ids):
        self.dataset = dataset
        self.wrong_scope_ids = wrong_scope_ids

    def retrieve(self, query_text, requester_principals, k):
        # Find the query by text
        for query in self.dataset.queries:
            if query.text == query_text:
                # Return relevant ids + wrong scope ids
                result = list(query.relevant_ids)
                result.extend(self.wrong_scope_ids)
                return result[:k]
        return []


class ErroringRetriever:
    """A retriever that raises an error for one query."""

    def __init__(self, error_query_id):
        self.error_query_id = error_query_id

    def retrieve(self, query_text, requester_principals, k):
        if query_text == self.error_query_id:
            raise RuntimeError("Simulated error")
        return []


class RetrievalRunnerTest(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures."""
        # Create a simple valid dataset for testing
        self.valid_dataset = RetrievalDataset(
            memories=(
                RetrievalMemory(
                    id="mem1",
                    text="Memory 1 content",
                    acl=frozenset(["user:alice", "user:bob"]),
                    status="active",
                    fresh=True,
                    scope="project:p1",
                ),
                RetrievalMemory(
                    id="mem2",
                    text="Memory 2 content",
                    acl=frozenset(["user:alice"]),
                    status="active",
                    fresh=True,
                    scope="project:p1",
                ),
                RetrievalMemory(
                    id="mem3",
                    text="Memory 3 content",
                    acl=frozenset(["user:charlie"]),
                    status="superseded",
                    fresh=False,
                    scope="project:p2",
                ),
            ),
            queries=(
                RetrievalQuery(
                    id="q1",
                    text="What is project p1?",
                    requester_principals=frozenset(["user:alice"]),
                    scope="project:p1",
                    relevant_ids=frozenset(["mem1"]),
                ),
            ),
        )

    def test_visible_memory_ids(self):
        """Test visibility rule."""
        # mem1 is visible to user:alice (both have common ACL)
        # mem2 is visible to user:alice (has matching ACL)
        # mem3 is NOT visible to user:alice (different ACL)
        visible = visible_memory_ids(self.valid_dataset, self.valid_dataset.queries[0])
        self.assertIn("mem1", visible)
        self.assertIn("mem2", visible)
        self.assertNotIn("mem3", visible)
        self.assertEqual(len(visible), 2)

    def test_load_dataset_valid_fixture(self):
        """Test loading a valid dataset fixture."""
        dataset = load_dataset("benchmarks/tests/fixtures/retrieval/valid_dataset.json")
        self.assertEqual(len(dataset.memories), 4)
        self.assertEqual(len(dataset.queries), 2)

        # Check memory properties
        mem1 = next(m for m in dataset.memories if m.id == "mem1")
        self.assertEqual(mem1.text, "Memory 1 content")
        self.assertEqual(mem1.status, "active")
        self.assertTrue(mem1.fresh)
        self.assertEqual(mem1.scope, "project:p1")
        mem4 = next(m for m in dataset.memories if m.id == "mem4")
        self.assertEqual((mem4.status, mem4.fresh), ("superseded", False))

        # Check query properties
        q1 = next(q for q in dataset.queries if q.id == "q1")
        self.assertEqual(q1.text, "What is project p1?")
        self.assertEqual(q1.scope, "project:p1")
        self.assertEqual(q1.relevant_ids, frozenset(["mem1", "mem2"]))

    def test_load_dataset_empty_memories(self):
        """Test loading dataset with empty memories."""
        with self.assertRaises(ValueError) as context:
            load_dataset("benchmarks/tests/fixtures/retrieval/empty_memories.json")
        self.assertIn(
            "Dataset must contain at least one memory", str(context.exception)
        )

    def test_load_dataset_empty_queries(self):
        """Test loading dataset with empty queries."""
        with self.assertRaises(ValueError) as context:
            load_dataset("benchmarks/tests/fixtures/retrieval/empty_queries.json")
        self.assertIn("Dataset must contain at least one query", str(context.exception))

    def test_load_dataset_invalid_status(self):
        """Test loading dataset with invalid memory status."""
        with self.assertRaises(ValueError) as context:
            load_dataset("benchmarks/tests/fixtures/retrieval/invalid_status.json")
        self.assertIn("Invalid status for memory mem1", str(context.exception))

    def test_load_dataset_invisible_relevant(self):
        """Test loading dataset where a relevant ID is not visible to requester."""
        with self.assertRaises(ValueError) as context:
            load_dataset("benchmarks/tests/fixtures/retrieval/invisible_relevant.json")
        self.assertIn("not visible to its requester", str(context.exception))

    def test_perfect_retriever(self):
        """Test with a perfect retriever."""
        retriever = PerfectRetriever(self.valid_dataset)
        report = run_benchmark(retriever, self.valid_dataset, k=2)

        # Should have perfect scores
        self.assertEqual(report.metrics["recall_at_k"], 1.0)
        self.assertEqual(report.metrics["mrr"], 1.0)
        self.assertEqual(report.metrics["ndcg_at_k"], 1.0)
        self.assertEqual(report.metrics["permission_leakage_total"], 0)

    def test_leaking_retriever(self):
        """Test with a retriever that leaks memory."""
        leaking_retriever = LeakingRetriever(
            self.valid_dataset, ["mem3"]
        )  # mem3 is not visible
        report = run_benchmark(leaking_retriever, self.valid_dataset, k=2)

        # Should have leakage
        self.assertGreater(report.metrics["permission_leakage_total"], 0)

    def test_stale_retriever(self):
        """Test with a retriever that returns stale results."""
        dataset_with_stale = RetrievalDataset(
            memories=(
                RetrievalMemory(
                    id="mem1",
                    text="Memory 1 content",
                    acl=frozenset(["user:alice"]),
                    status="active",
                    fresh=True,
                    scope="project:p1",
                ),
                RetrievalMemory(
                    id="mem2",
                    text="Memory 2 content",
                    acl=frozenset(["user:alice"]),
                    status="active",
                    fresh=False,  # Stale
                    scope="project:p1",
                ),
            ),
            queries=(
                RetrievalQuery(
                    id="q1",
                    text="What is project p1?",
                    requester_principals=frozenset(["user:alice"]),
                    scope="project:p1",
                    relevant_ids=frozenset(["mem1"]),
                ),
            ),
        )

        stale_retriever = StaleRetriever(dataset_with_stale, ["mem2"])  # mem2 is stale
        report = run_benchmark(stale_retriever, dataset_with_stale, k=2)

        # Should have stale rate > 0
        self.assertGreater(report.metrics["stale_rate"], 0.0)

    def test_superseded_retriever(self):
        """Test with a retriever that returns superseded results."""
        dataset_with_superseded = RetrievalDataset(
            memories=(
                RetrievalMemory(
                    id="mem1",
                    text="Memory 1 content",
                    acl=frozenset(["user:alice"]),
                    status="active",
                    fresh=True,
                    scope="project:p1",
                ),
                RetrievalMemory(
                    id="mem2",
                    text="Memory 2 content",
                    acl=frozenset(["user:alice"]),
                    status="superseded",  # Superseded
                    fresh=False,
                    scope="project:p1",
                ),
            ),
            queries=(
                RetrievalQuery(
                    id="q1",
                    text="What is project p1?",
                    requester_principals=frozenset(["user:alice"]),
                    scope="project:p1",
                    relevant_ids=frozenset(["mem1"]),
                ),
            ),
        )

        superseded_retriever = SupersededRetriever(
            dataset_with_superseded, ["mem2"]
        )  # mem2 is superseded
        report = run_benchmark(superseded_retriever, dataset_with_superseded, k=2)

        # Should have superseded rate > 0
        self.assertGreater(report.metrics["superseded_rate"], 0.0)

    def test_wrong_scope_retriever(self):
        """Test with a retriever that returns wrong-scope results."""
        dataset_with_scopes = RetrievalDataset(
            memories=(
                RetrievalMemory(
                    id="mem1",
                    text="Memory 1 content",
                    acl=frozenset(["user:alice"]),
                    status="active",
                    fresh=True,
                    scope="project:p1",
                ),
                RetrievalMemory(
                    id="mem2",
                    text="Memory 2 content",
                    acl=frozenset(["user:alice"]),
                    status="active",
                    fresh=True,
                    scope="project:p2",  # Different scope
                ),
            ),
            queries=(
                RetrievalQuery(
                    id="q1",
                    text="What is project p1?",
                    requester_principals=frozenset(["user:alice"]),
                    scope="project:p1",
                    relevant_ids=frozenset(["mem1"]),
                ),
            ),
        )

        wrong_scope_retriever = WrongScopeRetriever(
            dataset_with_scopes, ["mem2"]
        )  # mem2 is wrong scope
        report = run_benchmark(wrong_scope_retriever, dataset_with_scopes, k=2)

        # Should have scope mismatch rate > 0
        self.assertGreater(report.metrics["scope_mismatch_rate"], 0.0)

    def test_duplicate_ids_dropped(self):
        """Test that duplicate IDs are dropped."""

        class DuplicateRetriever:
            def retrieve(self, query_text, requester_principals, k):
                return ["mem1", "mem1", "mem2"]  # Duplicates

        retriever = DuplicateRetriever()
        report = run_benchmark(retriever, self.valid_dataset, k=2)

        # Should still process correctly (duplicates dropped)
        self.assertEqual(
            report.metrics["recall_at_k"], 1.0
        )  # If mem1 and mem2 are relevant

    def test_unknown_id_counted_as_leakage(self):
        """Test that unknown IDs are counted as leakage."""

        class UnknownIdRetriever:
            def retrieve(self, query_text, requester_principals, k):
                return ["mem1", "unknown_id"]  # One known, one unknown

        retriever = UnknownIdRetriever()
        report = run_benchmark(retriever, self.valid_dataset, k=2)

        # Should have leakage count of 1
        self.assertEqual(report.metrics["permission_leakage_total"], 1)

    def test_erroring_retriever(self):
        """Test that a retriever raising an error doesn't break the whole benchmark."""
        erroring_retriever = ErroringRetriever("What is project p1?")
        report = run_benchmark(erroring_retriever, self.valid_dataset, k=2)

        # One query should fail (have error_type)
        failed_query = None
        for query in report.queries:
            if query["error_type"] is not None:
                failed_query = query
                break

        self.assertIsNotNone(failed_query)
        self.assertEqual(failed_query["error_type"], "RuntimeError")

    @staticmethod
    def _load_from_text(text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            path.write_text(text, encoding="utf-8")
            return load_dataset(str(path))

    @staticmethod
    def _document():
        return {
            "memories": [
                {
                    "id": "m1",
                    "text": "SECRET-TEXT",
                    "acl": ["user:a"],
                    "status": "active",
                    "fresh": True,
                    "scope": "project",
                }
            ],
            "queries": [
                {
                    "id": "q1",
                    "text": "QUERY-TEXT",
                    "requester_principals": ["user:a"],
                    "scope": "project",
                    "relevant_ids": ["m1"],
                }
            ],
        }

    def test_the_minimal_document_used_by_the_malformed_cases_is_valid(self):
        dataset = self._load_from_text(json.dumps(self._document()))
        self.assertEqual([m.id for m in dataset.memories], ["m1"])

    def test_malformed_datasets_raise_value_error_naming_the_field(self):
        def memory(**changes):
            document = self._document()
            document["memories"][0].update(changes)
            return document

        def query(**changes):
            document = self._document()
            document["queries"][0].update(changes)
            return document

        def without(section, field):
            document = self._document()
            del document[section][0][field]
            return document

        cases = (
            (without("memories", "id"), "'id' must be a non-empty string"),
            (without("memories", "acl"), "'acl' must be a list of non-empty strings"),
            (memory(acl="user:a"), "'acl' must be a list of non-empty strings"),
            (memory(acl=["user:a", "user:a"]), "'acl' must not contain duplicates"),
            (memory(fresh="false"), "'fresh' must be a boolean"),
            (memory(fresh=1), "'fresh' must be a boolean"),
            (without("memories", "fresh"), "'fresh' must be a boolean"),
            (memory(status="stale"), "Invalid status for memory m1"),
            (memory(text=""), "'text' must be a non-empty string"),
            (without("queries", "id"), "'id' must be a non-empty string"),
            (
                query(requester_principals=[]),
                "'requester_principals' must not be empty",
            ),
            (
                query(relevant_ids=["m1", "m1"]),
                "'relevant_ids' must not contain duplicates",
            ),
            (query(relevant_ids=[]), "'relevant_ids' must not be empty"),
            (query(relevant_ids=["nope"]), "references non-existent memory nope"),
            (query(scope=5), "'scope' must be a non-empty string"),
            ({"memories": ["x"], "queries": []}, "memory at index 0 must be an object"),
            ([], "dataset must be an object"),
            ({"memories": {}, "queries": []}, "at least one memory"),
        )
        for document, message in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as context:
                    self._load_from_text(json.dumps(document))
                self.assertIn(message, str(context.exception))
                self.assertNotIn("SECRET-TEXT", str(context.exception))
                self.assertNotIn("user:a", str(context.exception))

    def test_duplicate_or_nonstandard_json_in_a_dataset_is_rejected(self):
        document = json.dumps(self._document())
        texts = (
            document[:-1] + ', "memories": []}',
            document.replace('"fresh": true', '"fresh": true, "fresh": false', 1),
            document.replace('"fresh": true', '"fresh": NaN', 1),
        )
        for text in texts:
            with self.subTest(text=text[-30:]), self.assertRaises(ValueError):
                self._load_from_text(text)

    def test_duplicate_ids_are_rejected(self):
        document = self._document()
        document["memories"].append(dict(document["memories"][0]))
        with self.assertRaises(ValueError) as context:
            self._load_from_text(json.dumps(document))
        self.assertIn("Duplicate memory ID: m1", str(context.exception))

        document = self._document()
        document["queries"].append(dict(document["queries"][0]))
        with self.assertRaises(ValueError) as context:
            self._load_from_text(json.dumps(document))
        self.assertIn("Duplicate query ID: q1", str(context.exception))

    def test_the_valid_fixture_is_consistent_with_its_own_metrics(self):
        """Returning exactly the relevant ids must be a uniformly perfect result."""
        dataset = load_dataset("benchmarks/tests/fixtures/retrieval/valid_dataset.json")
        relevant = {q.text: sorted(q.relevant_ids) for q in dataset.queries}

        class Oracle:
            def retrieve(self, query_text, requester_principals, k):
                return relevant[query_text]

        metrics = run_benchmark(Oracle(), dataset, k=5).metrics

        for name in ("recall_at_k", "mrr", "ndcg_at_k"):
            self.assertEqual(metrics[name], 1.0, name)
        for name in ("stale_rate", "superseded_rate", "scope_mismatch_rate"):
            self.assertEqual(metrics[name], 0.0, name)
        self.assertEqual(metrics["permission_leakage_total"], 0)

    def test_relevance_labels_must_be_consistent_with_the_misselection_metrics(self):
        for change, label in (
            ({"status": "superseded"}, "superseded"),
            ({"status": "deprecated"}, "deprecated"),
            ({"fresh": False}, "stale"),
            ({"scope": "another-scope"}, "wrong scope"),
        ):
            with self.subTest(label=label):
                document = self._document()
                document["memories"][0].update(change)
                with self.assertRaises(ValueError) as context:
                    self._load_from_text(json.dumps(document))
                self.assertIn(
                    "relevant memory m1 must be active, fresh and in the query's scope",
                    str(context.exception),
                )
                self.assertNotIn("SECRET-TEXT", str(context.exception))

    def test_retriever_results_must_be_a_sequence_of_ids(self):
        for result in ("m1", b"m1", {"m1"}, iter(["m1"]), None, 5):
            with self.subTest(result=repr(result)), self.assertRaises(TypeError):
                run_benchmark(FixedRetriever(result), self._dataset(1), k=1)

    def test_an_invalid_id_beyond_the_cutoff_is_still_rejected(self):
        for result in (["m1", 5], ["m1", None], ["m1", "x", 5.0]):
            with self.subTest(result=result), self.assertRaises(TypeError):
                run_benchmark(FixedRetriever(result), self._dataset(1), k=1)

    def test_misspelled_or_unknown_dataset_fields_are_rejected_not_ignored(self):
        def with_memory(**extra):
            document = self._document()
            document["memories"][0].update(extra)
            return document

        def with_query(**extra):
            document = self._document()
            document["queries"][0].update(extra)
            return document

        documents = (
            (with_memory(fresh_=True), "fresh_"),
            (with_memory(statuss="active"), "statuss"),
            (with_query(relevant_id=["m1"]), "relevant_id"),
            (with_query(principals=["user:a"]), "principals"),
            (dict(self._document(), memory=[]), "memory"),
        )
        for document, name in documents:
            with self.subTest(field=name), self.assertRaises(ValueError) as context:
                self._load_from_text(json.dumps(document))
            self.assertIn("unknown field(s)", str(context.exception))
            self.assertIn(name, str(context.exception))
            self.assertNotIn("SECRET-TEXT", str(context.exception))

    def test_only_the_first_k_ids_are_scored(self):
        dataset = self._dataset(1)

        at_one = run_benchmark(FixedRetriever(["x", "m1"]), dataset, k=1).metrics
        at_two = run_benchmark(FixedRetriever(["x", "m1"]), dataset, k=2).metrics

        self.assertEqual((at_one["recall_at_k"], at_one["mrr"]), (0.0, 0.0))
        self.assertEqual((at_two["recall_at_k"], at_two["mrr"]), (1.0, 0.5))

    def test_cpu_time_is_reported_per_query_and_aggregated(self):
        readings = iter([0.0, 0.002, 0.0, 0.004])

        report = run_benchmark(
            FixedRetriever(["m1"]),
            self._dataset(2),
            k=1,
            cpu_clock=lambda: next(readings),
        )

        self.assertAlmostEqual(report.queries[0]["cpu_ms"], 2.0)
        self.assertAlmostEqual(report.queries[1]["cpu_ms"], 4.0)
        self.assertAlmostEqual(report.metrics["cpu_ms_mean"], 3.0)
        self.assertAlmostEqual(report.metrics["cpu_ms_total"], 6.0)

    def test_failed_queries_still_report_cpu_time(self):
        class Raising:
            def retrieve(self, query_text, requester_principals, k):
                raise RuntimeError("boom")

        readings = iter([1.0, 1.003])

        report = run_benchmark(
            Raising(), self._dataset(1), k=1, cpu_clock=lambda: next(readings)
        )

        self.assertAlmostEqual(report.queries[0]["cpu_ms"], 3.0)

    def test_history_memories_are_valid_hard_negatives_scored_as_non_active(self):
        document = self._document()
        document["memories"].append(
            {
                "id": "h1",
                "text": "AUDIT-ONLY",
                "acl": ["user:a"],
                "status": "history",
                "fresh": True,
                "scope": "project",
            }
        )
        dataset = self._load_from_text(json.dumps(document))

        metrics = run_benchmark(FixedRetriever(["m1", "h1"]), dataset, k=2).metrics

        self.assertEqual(
            next(m.status for m in dataset.memories if m.id == "h1"), "history"
        )
        self.assertEqual(metrics["superseded_rate"], 0.5)
        self.assertEqual(metrics["recall_at_k"], 1.0)

    def test_a_history_memory_cannot_be_a_relevance_label(self):
        document = self._document()
        document["memories"][0]["status"] = "history"
        with self.assertRaises(ValueError) as context:
            self._load_from_text(json.dumps(document))
        self.assertIn("must be active", str(context.exception))

    def test_retrieve_with_an_uninspectable_signature_is_rejected(self):
        class Retriever:
            def retrieve(self, query_text, requester_principals, k):
                return []

        with (
            patch(
                "benchmarks.retrieval_runner.inspect.signature", side_effect=ValueError
            ),
            self.assertRaises(TypeError) as context,
        ):
            validate_retriever(Retriever())

        self.assertIn("no inspectable signature", str(context.exception))

    def test_retrievers_without_the_required_interface_are_rejected(self):
        class NoMethod:
            pass

        class WrongSignature:
            def retrieve(self, only_one):
                return []

        class NotCallable:
            retrieve = 5

        for retriever in (NoMethod(), WrongSignature(), NotCallable()):
            with self.subTest(retriever=type(retriever).__name__):
                with self.assertRaises(TypeError):
                    validate_retriever(retriever)
                with self.assertRaises(TypeError):
                    run_benchmark(retriever, self._dataset(1), k=1)

    def test_metrics_collector_is_stopped_when_scoring_fails(self):
        calls = []

        class RecordingCollector:
            def start(self):
                calls.append("start")

            def stop(self):
                calls.append("stop")

            def metrics(self):
                calls.append("metrics")
                return {}

        with self.assertRaises(TypeError):
            run_benchmark(
                FixedRetriever([5]),
                self._dataset(1),
                k=1,
                metrics_collector=RecordingCollector(),
            )

        self.assertEqual(calls, ["start", "stop"])

    def test_failed_queries_are_counted(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def retrieve(self, query_text, requester_principals, k):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("boom")
                return ["m1"]

        report = run_benchmark(Flaky(), self._dataset(3), k=1)

        self.assertEqual(report.metrics["failed_queries"], 1)
        self.assertEqual(
            [q["error_type"] for q in report.queries], [None, "RuntimeError", None]
        )

    @staticmethod
    def _dataset(query_count):
        memory = RetrievalMemory(
            id="m1",
            text="TEXT-m1",
            acl=frozenset({"user:a"}),
            status="active",
            fresh=True,
            scope="project",
        )
        queries = tuple(
            RetrievalQuery(
                id=f"q{index}",
                text=f"QTEXT-{index}",
                requester_principals=frozenset({"user:a"}),
                scope="project",
                relevant_ids=frozenset({"m1"}),
            )
            for index in range(query_count)
        )
        return RetrievalDataset(memories=(memory,), queries=queries)

    def test_latency_statistics_use_nearest_rank_percentiles(self):
        dataset = self._dataset(10)
        # The runner reads the clock twice per query: before and after retrieve().
        readings = iter(
            value
            for milliseconds in range(1, 11)
            for value in (0.0, milliseconds / 1000)
        )
        report = run_benchmark(
            FixedRetriever(["m1"]), dataset, k=1, clock=lambda: next(readings)
        )

        self.assertAlmostEqual(report.queries[2]["latency_ms"], 3.0)
        self.assertAlmostEqual(report.metrics["latency_ms_mean"], 5.5)
        self.assertAlmostEqual(report.metrics["latency_ms_p50"], 5.0)
        self.assertAlmostEqual(report.metrics["latency_ms_p95"], 10.0)

    def test_latency_excludes_scoring_time(self):
        dataset = self._dataset(1)
        readings = iter([0.0, 0.004])
        report = run_benchmark(
            FixedRetriever(["m1"]), dataset, k=1, clock=lambda: next(readings)
        )
        self.assertAlmostEqual(report.queries[0]["latency_ms"], 4.0)

    def test_report_to_dict_is_json_serializable_without_text(self):
        dataset = self._dataset(2)
        report = run_benchmark(FixedRetriever(["m1"]), dataset, k=1)

        data = report.to_dict()
        serialized = json.dumps(data)

        for text in ("TEXT-m1", "QTEXT-0", "QTEXT-1", "user:a"):
            self.assertNotIn(text, serialized)
        self.assertEqual(json.loads(serialized)["metrics"]["k"], 1)
        self.assertEqual(len(data["queries"]), 2)
        self.assertNotIn("resources", data)

    def test_metrics_collector_resources_are_reported(self):
        dataset = self._dataset(2)
        collector = MetricsCollector(gpu_poll_interval_s=None)

        class CountingRetriever(FixedRetriever):
            def retrieve(self, query_text, requester_principals, k):
                collector.record_step()
                return super().retrieve(query_text, requester_principals, k)

        report = run_benchmark(
            CountingRetriever(["m1"]), dataset, k=1, metrics_collector=collector
        )

        self.assertEqual(report.resources["agent_steps"], 2)
        self.assertEqual(report.to_dict()["resources"]["agent_steps"], 2)

    def test_malformed_retriever_output_is_not_hidden_as_a_retriever_failure(self):
        dataset = self._dataset(1)
        with self.assertRaises(TypeError):
            run_benchmark(FixedRetriever([5]), dataset, k=1)


class FixedRetriever:
    def __init__(self, ids):
        self.ids = ids

    def retrieve(self, query_text, requester_principals, k):
        return self.ids


if __name__ == "__main__":
    unittest.main()
