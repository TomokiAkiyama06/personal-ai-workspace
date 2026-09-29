"""Checks for the synthetic gold datasets in ``benchmarks/gold``.

The datasets are loaded with the benchmark harness' own loaders, so a file that the
runners would reject fails here first. The sidecar ``*.meta.json`` files carry
labels the harness formats have no field for (case tags, graded relevance, hard
negative kinds); these tests keep them consistent with the datasets.
"""

import json
import re
import unittest
from collections import Counter
from pathlib import Path

from benchmarks.json_input import decode_json
from benchmarks.memory_worker_runner import load_cases
from benchmarks.memory_worker_runner import run_benchmark as run_memory_benchmark
from benchmarks.retrieval_runner import load_dataset, visible_memory_ids
from benchmarks.retrieval_runner import run_benchmark as run_retrieval_benchmark

DATASETS = Path(__file__).resolve().parents[1] / "gold"
MEMORY_CASES = DATASETS / "memory-worker-synthetic-v1.json"
MEMORY_META = DATASETS / "memory-worker-synthetic-v1.meta.json"
RETRIEVAL_DATASET = DATASETS / "retrieval-synthetic-v1.json"
RETRIEVAL_META = DATASETS / "retrieval-synthetic-v1.meta.json"

CJK = re.compile(r"[぀-ヿ一-鿿]")
# Shapes of real credentials and personal data that must never appear in the public
# synthetic data (SECURITY.md). A match is a dataset bug, not a false positive to
# silence: rewrite the text instead.
SECRET_SHAPES = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),
    re.compile(r"(?i)\b(?:password|passwd|token|secret)\s*[:=]\s*\S+"),
    re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)0\d{1,4}-\d{1,4}-\d{3,4}(?!\d)"),
)
EXISTING_KEY = re.compile(r"^- key=(\S+) scope=", re.MULTILINE)
ALLOWED_KEYS = re.compile(r"^\[Allowed keys\] (.*)$", re.MULTILINE)


def _read(path):
    return decode_json(path.read_text(encoding="utf-8"))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


class SecretShapeTest(unittest.TestCase):
    def test_no_dataset_contains_credential_or_personal_data_shapes(self):
        for path in (MEMORY_CASES, MEMORY_META, RETRIEVAL_DATASET, RETRIEVAL_META):
            for text in _strings(_read(path)):
                for pattern in SECRET_SHAPES:
                    with self.subTest(path=path.name, pattern=pattern.pattern):
                        self.assertIsNone(pattern.search(text))

    def test_detector_catches_the_shapes_it_is_meant_to(self):
        fake = "ghp_" + "a1B2" * 6
        self.assertTrue(any(pattern.search(fake) for pattern in SECRET_SHAPES))
        self.assertTrue(
            any(pattern.search("name@corp.test.jp") for pattern in SECRET_SHAPES)
        )


class GoldEchoWorker:
    """Returns each case's gold records, so a perfect Memory Worker is simulated."""

    def __init__(self, cases):
        self._outputs = {
            case.input_text: json.dumps(
                {
                    "memories": [
                        {
                            "key": record.key,
                            "scope": record.scope,
                            "state": record.state,
                            "supersedes": record.supersedes,
                            "content": record.content,
                            **(
                                {"conflicts_with": list(record.conflicts_with)}
                                if record.conflicts_with is not None
                                else {}
                            ),
                        }
                        for record in case.gold
                    ]
                },
                ensure_ascii=False,
            )
            for case in cases
        }

    def extract(self, input_text):
        return self._outputs[input_text]


class MemoryWorkerDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases(str(MEMORY_CASES))
        cls.meta = _read(MEMORY_META)
        cls.tags = {entry["id"]: set(entry["tags"]) for entry in cls.meta["cases"]}
        cls.records = [record for case in cls.cases for record in case.gold]

    def test_harness_loads_enough_cases(self):
        self.assertGreaterEqual(len(self.cases), 60)

    def test_meta_lists_every_case_once_in_order(self):
        self.assertTrue(self.meta["synthetic"])
        self.assertEqual(
            [entry["id"] for entry in self.meta["cases"]],
            [case.id for case in self.cases],
        )
        for entry in self.meta["cases"]:
            self.assertTrue(entry["tags"], entry["id"])

    def test_coverage_of_required_categories(self):
        tag_counts = Counter(tag for tags in self.tags.values() for tag in tags)
        self.assertGreaterEqual(tag_counts["extraction"], 10)
        self.assertGreaterEqual(tag_counts["schema-edge"], 10)
        self.assertGreaterEqual(tag_counts["supersedes"], 8)
        self.assertGreaterEqual(tag_counts["conflicts"], 5)
        # Unnecessary-memory traps: cases where storing anything is wrong.
        empty = [case.id for case in self.cases if not case.gold]
        self.assertGreaterEqual(len(empty), 15)
        for case_id in empty:
            self.assertTrue(self.tags[case_id] & {"trap", "schema-edge"}, case_id)
        scopes = Counter(record.scope for record in self.records)
        for scope in ("user", "project", "repo", "shared"):
            self.assertGreaterEqual(scopes[scope], 5, scope)
        states = Counter(record.state for record in self.records)
        self.assertGreaterEqual(states["confirmed"], 10)
        self.assertGreaterEqual(states["inferred"], 8)
        self.assertGreaterEqual(
            sum(record.supersedes is not None for record in self.records), 8
        )
        self.assertGreaterEqual(
            sum(bool(record.conflicts_with) for record in self.records), 5
        )
        # Records labelled "conflicts with nothing" are scored too.
        self.assertGreaterEqual(
            sum(record.conflicts_with == () for record in self.records), 5
        )

    def test_languages_are_mixed(self):
        japanese = [case for case in self.cases if CJK.search(case.input_text)]
        self.assertGreaterEqual(len(japanese), 30)
        self.assertGreaterEqual(len(self.cases) - len(japanese), 15)

    def test_gold_keys_are_offered_and_relations_point_at_existing_memories(self):
        for case in self.cases:
            with self.subTest(case=case.id):
                allowed_line = ALLOWED_KEYS.findall(case.input_text)
                self.assertEqual(len(allowed_line), 1)
                allowed = set(allowed_line[0].split(", "))
                existing = set(EXISTING_KEY.findall(case.input_text))
                # Decoys: the allowed list never gives away which keys are gold.
                self.assertGreater(len(allowed), len(case.gold))
                for record in case.gold:
                    self.assertIn(record.key, allowed)
                    if record.supersedes is not None:
                        self.assertIn(record.supersedes, existing)
                        self.assertNotEqual(record.supersedes, record.key)
                    for key in record.conflicts_with or ():
                        self.assertIn(key, existing)
                        self.assertNotEqual(key, record.key)

    def test_a_worker_echoing_the_gold_is_schema_valid_and_scores_perfectly(self):
        report = run_memory_benchmark(
            GoldEchoWorker(self.cases), self.cases, timeout_seconds=30
        )
        metrics = report.metrics
        self.assertEqual(metrics["schema_adherence_rate"], 1.0)
        self.assertEqual(metrics["extraction_recall"], 1.0)
        self.assertEqual(metrics["exact_recall"], 1.0)
        self.assertEqual(metrics["unneeded_rate"], 0.0)
        for name in (
            "scope_accuracy",
            "state_accuracy",
            "supersedes_accuracy",
            "content_accuracy",
            "conflict_accuracy",
        ):
            self.assertEqual(metrics[name], 1.0, name)


class RetrievalDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(str(RETRIEVAL_DATASET))
        cls.memories = {memory.id: memory for memory in cls.dataset.memories}
        cls.meta = _read(RETRIEVAL_META)
        cls.labels = {entry["id"]: entry for entry in cls.meta["queries"]}

    def test_harness_loads_enough_memories_and_queries(self):
        self.assertGreaterEqual(len(self.dataset.memories), 200)
        self.assertGreaterEqual(len(self.dataset.queries), 80)

    def test_every_memory_has_an_acl(self):
        for memory in self.dataset.memories:
            self.assertTrue(memory.acl, memory.id)

    def test_meta_matches_queries_and_grades_are_the_relevant_ids(self):
        self.assertTrue(self.meta["synthetic"])
        self.assertEqual(
            [entry["id"] for entry in self.meta["queries"]],
            [query.id for query in self.dataset.queries],
        )
        for query in self.dataset.queries:
            with self.subTest(query=query.id):
                grades = self.labels[query.id]["grades"]
                self.assertEqual(set(grades), set(query.relevant_ids))
                self.assertTrue(set(grades.values()) <= {1, 2, 3})
                self.assertEqual(max(grades.values()), 3)

    def test_hard_negatives_are_what_their_kind_says(self):
        for query in self.dataset.queries:
            visible = visible_memory_ids(self.dataset, query)
            negatives = self.labels[query.id]["hard_negatives"]
            for memory_id, kind in negatives.items():
                with self.subTest(query=query.id, memory=memory_id, kind=kind):
                    memory = self.memories[memory_id]
                    self.assertNotIn(memory_id, query.relevant_ids)
                    if kind == "leakage":
                        self.assertNotIn(memory_id, visible)
                    elif kind in {"superseded", "deprecated", "history"}:
                        self.assertEqual(memory.status, kind)
                        self.assertIn(memory_id, visible)
                    elif kind == "stale":
                        self.assertFalse(memory.fresh)
                        self.assertIn(memory_id, visible)
                    elif kind == "wrong-scope":
                        self.assertIn(memory_id, visible)
                        self.assertNotIn(memory.scope, query.effective_scopes)
                        self.assertEqual(memory.status, "active")
                    elif kind == "near-miss":
                        self.assertIn(memory_id, visible)
                        self.assertEqual(memory.status, "active")
                        self.assertTrue(memory.fresh)
                    else:
                        self.fail(f"unknown hard negative kind {kind}")

    def test_coverage_of_hard_cases(self):
        kinds = Counter()
        queries_with = Counter()
        for entry in self.meta["queries"]:
            present = set(entry["hard_negatives"].values())
            kinds.update(entry["hard_negatives"].values())
            queries_with.update(present)
        # REQUIREMENTS.md: Permission Leakage must be 0, so there must be traps.
        self.assertGreaterEqual(queries_with["leakage"], 30)
        self.assertGreaterEqual(queries_with["superseded"], 25)
        self.assertGreaterEqual(queries_with["wrong-scope"], 15)
        self.assertGreaterEqual(kinds["stale"], 4)
        self.assertGreaterEqual(kinds["deprecated"], 3)
        self.assertGreaterEqual(kinds["history"], 4)
        graded = Counter(
            grade
            for entry in self.meta["queries"]
            for grade in entry["grades"].values()
        )
        self.assertGreaterEqual(graded[1], 10)
        self.assertGreaterEqual(graded[2], 8)
        # Leakage traps inside the query's own scope, not only in other projects.
        same_scope_leaks = [
            query.id
            for query in self.dataset.queries
            for memory_id, kind in self.labels[query.id]["hard_negatives"].items()
            if kind == "leakage"
            and self.memories[memory_id].scope in query.effective_scopes
        ]
        self.assertGreaterEqual(len(same_scope_leaks), 5)

    def test_languages_are_mixed(self):
        japanese = [query for query in self.dataset.queries if CJK.search(query.text)]
        self.assertGreaterEqual(len(japanese), 40)
        self.assertGreaterEqual(len(self.dataset.queries) - len(japanese), 25)

    def _oracle(self):
        by_input = {}
        for query in self.dataset.queries:
            grades = self.labels[query.id]["grades"]
            scopes = (query.scope, *sorted(query.allowed_scopes - {query.scope}))
            key = (query.text, tuple(sorted(query.requester_principals)), scopes)
            self.assertNotIn(
                key, by_input, "two queries give the retriever the same input"
            )
            by_input[key] = sorted(grades, key=lambda memory_id: -grades[memory_id])
        return by_input

    def test_an_oracle_retriever_is_perfect_and_leaks_nothing(self):
        answers = self._oracle()

        class Oracle:
            def retrieve(self, query_text, requester_principals, k, scopes):
                return answers[(query_text, tuple(requester_principals), tuple(scopes))]

        metrics = run_retrieval_benchmark(Oracle(), self.dataset, k=10).metrics
        self.assertEqual(metrics["recall_at_k"], 1.0)
        self.assertEqual(metrics["mrr"], 1.0)
        self.assertEqual(metrics["ndcg_at_k"], 1.0)
        self.assertEqual(metrics["permission_leakage_total"], 0)
        self.assertEqual(metrics["stale_rate"], 0.0)
        self.assertEqual(metrics["superseded_rate"], 0.0)
        self.assertEqual(metrics["scope_mismatch_rate"], 0.0)

    def test_hard_negatives_are_counted_by_the_harness(self):
        answers = self._oracle()
        negatives = {}
        for query in self.dataset.queries:
            scopes = (query.scope, *sorted(query.allowed_scopes - {query.scope}))
            key = (query.text, tuple(sorted(query.requester_principals)), scopes)
            negatives[key] = list(self.labels[query.id]["hard_negatives"])

        class AclBlind:
            """Ranks every hard negative first, as a retriever ignoring ACL/status would."""

            def retrieve(self, query_text, requester_principals, k, scopes):
                key = (query_text, tuple(requester_principals), tuple(scopes))
                return negatives[key] + answers[key]

        metrics = run_retrieval_benchmark(AclBlind(), self.dataset, k=10).metrics
        leak_traps = sum(
            kind == "leakage"
            for entry in self.meta["queries"]
            for kind in entry["hard_negatives"].values()
        )
        self.assertEqual(metrics["permission_leakage_total"], leak_traps)
        self.assertGreater(metrics["superseded_rate"], 0.0)
        self.assertGreater(metrics["stale_rate"], 0.0)
        self.assertGreater(metrics["scope_mismatch_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
