"""``RetrievalQuery`` and the validation helpers: every bad value, no database."""

import unittest
from types import MappingProxyType
from uuid import UUID, uuid4

from paw_backend.memory.models import MemoryScope
from paw_backend.memory.retrieval import limits
from paw_backend.memory.retrieval.errors import InvalidRetrievalInputError
from paw_backend.memory.retrieval.records import (
    ALL_SCOPES,
    RetrievalQuery,
    StalePolicy,
)
from paw_backend.memory.shared.errors import InputProblem

SHA = "0123456789abcdef" * 2 + "01234567"  # 40 characters
SHA256 = "0123456789abcdef" * 4


def query(**overrides):
    values = {"text": "how do we deploy"}
    values.update(overrides)
    return RetrievalQuery(**values)


class QueryDefaultsTest(unittest.TestCase):
    def test_a_query_with_only_text_asks_for_everything_with_the_default_limit(self):
        q = query()
        self.assertEqual(q.text, "how do we deploy")
        self.assertIsNone(q.scopes)
        self.assertEqual(q.wanted_scopes, ALL_SCOPES)
        self.assertIsNone(q.project_ids)
        self.assertIsNone(q.repo_ids)
        self.assertEqual(q.limit, limits.DEFAULT_LIMIT)
        self.assertIs(q.stale_policy, StalePolicy.FLAG)
        self.assertIsNone(q.repo_heads)

    def test_the_text_is_kept_exactly(self):
        self.assertEqual(query(text="  padded\ttext  ").text, "  padded\ttext  ")

    def test_enums_accept_the_member_or_the_exact_string_and_are_normalised(self):
        q = query(scopes=["project", MemoryScope.REPO], stale_policy="exclude")
        self.assertEqual(q.scopes, frozenset({MemoryScope.PROJECT, MemoryScope.REPO}))
        self.assertTrue(all(type(s) is MemoryScope for s in q.scopes))
        self.assertIs(q.stale_policy, StalePolicy.EXCLUDE)

    def test_caller_collections_are_copied_and_frozen(self):
        projects = {uuid4()}
        heads = {uuid4(): SHA}
        q = query(project_ids=projects, repo_ids=[uuid4()], repo_heads=heads)
        projects.add(uuid4())
        heads[uuid4()] = SHA
        self.assertEqual(len(q.project_ids), 1)
        self.assertIsInstance(q.project_ids, frozenset)
        self.assertEqual(len(q.repo_heads), 1)
        self.assertIsInstance(q.repo_heads, MappingProxyType)
        with self.assertRaises(TypeError):
            q.repo_heads[uuid4()] = SHA

    def test_an_empty_narrowing_is_kept_as_empty_not_as_everything(self):
        q = query(project_ids=set(), repo_ids=[], scopes=[])
        self.assertEqual(q.project_ids, frozenset())
        self.assertEqual(q.repo_ids, frozenset())
        self.assertEqual(q.scopes, frozenset())
        self.assertEqual(q.wanted_scopes, frozenset())

    def test_the_query_is_immutable(self):
        with self.assertRaises(AttributeError):
            query().limit = 5


class QueryRejectsTest(unittest.TestCase):
    """(field, value, problem): each is refused before anything else happens."""

    CASES = [
        ("text", None, InputProblem.REQUIRED),
        ("text", 5, InputProblem.WRONG_TYPE),
        ("text", b"bytes", InputProblem.WRONG_TYPE),
        ("text", "", InputProblem.BLANK),
        ("text", " \t\n", InputProblem.BLANK),
        ("text", "a\x00b", InputProblem.INVALID_CHARACTERS),
        ("text", "lone \ud800 surrogate", InputProblem.INVALID_CHARACTERS),
        ("text", "x" * (limits.MAX_QUERY_CHARS + 1), InputProblem.TOO_LONG),
        ("scopes", "user", InputProblem.WRONG_TYPE),
        ("scopes", {"user": 1}, InputProblem.WRONG_TYPE),
        ("scopes", ["everything"], InputProblem.INVALID_FORMAT),
        ("scopes", ["User"], InputProblem.INVALID_FORMAT),
        ("scopes", [" user"], InputProblem.INVALID_FORMAT),
        ("scopes", [1], InputProblem.WRONG_TYPE),
        ("scopes", [StalePolicy.FLAG], InputProblem.WRONG_TYPE),
        ("scopes", [None], InputProblem.REQUIRED),
        ("scopes", ["user"] * 6, InputProblem.TOO_MANY),
        ("project_ids", uuid4(), InputProblem.WRONG_TYPE),
        ("project_ids", str(uuid4()), InputProblem.WRONG_TYPE),
        ("project_ids", [str(uuid4())], InputProblem.WRONG_TYPE),
        ("project_ids", [None], InputProblem.REQUIRED),
        ("project_ids", [1], InputProblem.WRONG_TYPE),
        ("repo_ids", [str(uuid4())], InputProblem.WRONG_TYPE),
        ("limit", None, InputProblem.REQUIRED),
        ("limit", 0, InputProblem.OUT_OF_RANGE),
        ("limit", -1, InputProblem.OUT_OF_RANGE),
        ("limit", limits.MAX_LIMIT + 1, InputProblem.OUT_OF_RANGE),
        ("limit", True, InputProblem.WRONG_TYPE),
        ("limit", 5.0, InputProblem.WRONG_TYPE),
        ("limit", "5", InputProblem.WRONG_TYPE),
        ("stale_policy", None, InputProblem.REQUIRED),
        ("stale_policy", "Flag", InputProblem.INVALID_FORMAT),
        ("stale_policy", "drop", InputProblem.INVALID_FORMAT),
        ("stale_policy", 1, InputProblem.WRONG_TYPE),
        ("repo_heads", [(uuid4(), SHA)], InputProblem.WRONG_TYPE),
        ("repo_heads", {str(uuid4()): SHA}, InputProblem.WRONG_TYPE),
        ("repo_heads", {uuid4(): 5}, InputProblem.WRONG_TYPE),
        ("repo_heads", {uuid4(): SHA.upper()}, InputProblem.INVALID_FORMAT),
        ("repo_heads", {uuid4(): SHA[:39]}, InputProblem.INVALID_FORMAT),
        ("repo_heads", {uuid4(): SHA + "0"}, InputProblem.INVALID_FORMAT),
        ("repo_heads", {uuid4(): "g" * 40}, InputProblem.INVALID_FORMAT),
        ("repo_heads", {uuid4(): ""}, InputProblem.INVALID_FORMAT),
    ]

    def test_every_bad_value_is_refused_with_its_field_and_problem(self):
        for field, value, problem in self.CASES:
            with self.subTest(field=field, value=repr(value)[:40]):
                with self.assertRaises(InvalidRetrievalInputError) as caught:
                    query(**{field: value})
                self.assertEqual(caught.exception.field, field)
                self.assertEqual(caught.exception.problem, problem)

    def test_the_error_never_contains_the_rejected_value(self):
        secret = "secret-query-text-" + "x" * limits.MAX_QUERY_CHARS
        with self.assertRaises(InvalidRetrievalInputError) as caught:
            query(text=secret)
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(InvalidRetrievalInputError) as caught:
            query(text="ok\x00secret")
        self.assertNotIn("secret", str(caught.exception))

    def test_too_many_ids_are_refused_before_any_element_is_looked_at(self):
        too_many = [object()] * (limits.MAX_REQUESTED_PROJECTS + 1)
        with self.assertRaises(InvalidRetrievalInputError) as caught:
            query(project_ids=too_many)
        self.assertEqual(caught.exception.problem, InputProblem.TOO_MANY)
        with self.assertRaises(InvalidRetrievalInputError) as caught:
            query(repo_ids=[uuid4() for _ in range(limits.MAX_REQUESTED_REPOS + 1)])
        self.assertEqual(caught.exception.problem, InputProblem.TOO_MANY)
        heads = {uuid4(): SHA for _ in range(limits.MAX_REPO_HEADS + 1)}
        with self.assertRaises(InvalidRetrievalInputError) as caught:
            query(repo_heads=heads)
        self.assertEqual(caught.exception.problem, InputProblem.TOO_MANY)

    def test_every_element_is_checked_before_duplicates_are_removed(self):
        same = uuid4()
        with self.assertRaises(InvalidRetrievalInputError):
            query(project_ids=[same, same, "not-a-uuid"])


class QueryBoundariesTest(unittest.TestCase):
    def test_the_limits_themselves_are_accepted(self):
        self.assertEqual(query(text="x" * limits.MAX_QUERY_CHARS).text[-1], "x")
        self.assertEqual(query(limit=1).limit, 1)
        self.assertEqual(query(limit=limits.MAX_LIMIT).limit, limits.MAX_LIMIT)
        ids = [uuid4() for _ in range(limits.MAX_REQUESTED_PROJECTS)]
        self.assertEqual(len(query(project_ids=ids).project_ids), len(ids))

    def test_both_commit_id_lengths_are_accepted(self):
        repo = uuid4()
        self.assertEqual(query(repo_heads={repo: SHA}).repo_heads[repo], SHA)
        self.assertEqual(query(repo_heads={repo: SHA256}).repo_heads[repo], SHA256)

    def test_ids_must_be_uuid_objects(self):
        self.assertIsInstance(next(iter(query(repo_ids=[uuid4()]).repo_ids)), UUID)


if __name__ == "__main__":
    unittest.main()
