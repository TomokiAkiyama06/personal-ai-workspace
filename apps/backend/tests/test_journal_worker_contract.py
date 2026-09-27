"""``MemoryWorker`` and ``parse_worker_output``: the contract with the benchmark.

No database. The contract is the one the Memory Worker benchmark (PAW-018) judges:
``benchmarks/schemas/memory-worker-output-v1.schema.json``. The first class compares
this module with that file; the last runs both validators over the same documents
(the schema's side is a small interpreter in this file: the backend does not depend
on ``jsonschema``).
"""

import json
import re
import unittest
from pathlib import Path

from paw_backend.memory.journal import (
    OutputProblem,
    WorkerMemory,
    WorkerOutputError,
    WorkerScope,
    WorkerState,
    limits,
    parse_worker_output,
)
from paw_backend.memory.journal.worker import (
    _MEMORY_FIELDS,
    _REQUIRED_MEMORY_FIELDS,
    _TOP_FIELDS,
    MemoryWorker,
    check_worker,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / "schemas"
    / "memory-worker-output-v1.schema.json"
)


def doc(*memories, **top) -> str:
    return json.dumps({"memories": list(memories), **top})


def item(**fields) -> dict:
    values = {
        "key": "indent_style",
        "scope": "user",
        "state": "inferred",
        "supersedes": None,
        "content": "Use tabs.",
    }
    values.update(fields)
    return {name: value for name, value in values.items() if value is not ...}


class ContractMatchesTheBenchmarkSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.memory_schema = cls.schema["properties"]["memories"]["items"]

    def test_the_top_level_is_one_closed_member(self):
        self.assertEqual(set(self.schema["properties"]), set(_TOP_FIELDS))
        self.assertEqual(self.schema["required"], ["memories"])
        self.assertIs(self.schema["additionalProperties"], False)

    def test_the_memory_members_and_the_required_ones_are_the_same(self):
        self.assertEqual(set(self.memory_schema["properties"]), set(_MEMORY_FIELDS))
        self.assertEqual(
            set(self.memory_schema["required"]), set(_REQUIRED_MEMORY_FIELDS)
        )
        self.assertIs(self.memory_schema["additionalProperties"], False)

    def test_the_enums_are_the_same(self):
        properties = self.memory_schema["properties"]
        self.assertEqual(properties["scope"]["enum"], [s.value for s in WorkerScope])
        self.assertEqual(properties["state"]["enum"], [s.value for s in WorkerState])

    def test_the_non_blank_and_unique_rules_are_the_same(self):
        properties = self.memory_schema["properties"]
        for name in ("key", "content"):
            self.assertEqual(properties[name]["pattern"], "\\S", name)
        self.assertEqual(properties["supersedes"]["type"], ["string", "null"])
        self.assertEqual(properties["supersedes"]["pattern"], "\\S")
        self.assertIs(properties["conflicts_with"]["uniqueItems"], True)
        self.assertEqual(properties["conflicts_with"]["items"]["pattern"], "\\S")


class AcceptedOutputTest(unittest.TestCase):
    def test_a_complete_memory(self):
        (parsed,) = parse_worker_output(
            doc(item(conflicts_with=["other_key"], supersedes="old_key"))
        )
        self.assertEqual(
            (
                parsed.key,
                parsed.scope,
                parsed.state,
                parsed.supersedes,
                parsed.content,
                parsed.conflicts_with,
            ),
            (
                "indent_style",
                WorkerScope.USER,
                WorkerState.INFERRED,
                "old_key",
                "Use tabs.",
                ("other_key",),
            ),
        )

    def test_the_optional_members_may_be_absent(self):
        (parsed,) = parse_worker_output(doc(item(content=...)))
        self.assertEqual((parsed.content, parsed.conflicts_with), (None, ()))

    def test_no_memories_is_a_valid_answer(self):
        self.assertEqual(parse_worker_output(doc()), ())

    def test_every_scope_and_state_of_the_schema(self):
        for scope in ("user", "project", "repo", "shared"):
            for state in ("confirmed", "inferred"):
                with self.subTest(scope=scope, state=state):
                    (parsed,) = parse_worker_output(doc(item(scope=scope, state=state)))
                    self.assertEqual(
                        (parsed.scope.value, parsed.state.value), (scope, state)
                    )

    def test_text_is_kept_exactly_including_unicode_and_inner_whitespace(self):
        text = "  日本語のメモ\n二行目  "
        (parsed,) = parse_worker_output(doc(item(key=" キー ", content=text)))
        self.assertEqual((parsed.key, parsed.content), (" キー ", text))

    def test_the_bounds_are_inclusive(self):
        key = "k" * limits.MAX_KEY_CHARS
        content = "c" * limits.MAX_CONTENT_CHARS
        conflicts = [f"c{n}" for n in range(limits.MAX_CONFLICTS_PER_MEMORY)]
        many = [item(key=f"key{n}") for n in range(limits.MAX_MEMORIES_PER_OUTPUT)]
        self.assertEqual(
            len(
                parse_worker_output(
                    doc(item(key=key, content=content, conflicts_with=conflicts))
                )
            ),
            1,
        )
        self.assertEqual(
            len(parse_worker_output(doc(*many))), limits.MAX_MEMORIES_PER_OUTPUT
        )

    def test_surrounding_whitespace_of_the_document_is_fine(self):
        self.assertEqual(parse_worker_output("  \n" + doc() + "\n"), ())

    def test_the_parsed_memory_hides_its_text_from_repr(self):
        (parsed,) = parse_worker_output(
            doc(item(key="secret_key", content="secret text"))
        )
        self.assertNotIn("secret", repr(parsed))
        self.assertIn("scope", repr(parsed))
        self.assertIsInstance(parsed, WorkerMemory)


P = OutputProblem
LONG_KEY = "k" * (limits.MAX_KEY_CHARS + 1)
LONG_CONTENT = "c" * (limits.MAX_CONTENT_CHARS + 1)
TOO_MANY_CONFLICTS = [f"c{n}" for n in range(limits.MAX_CONFLICTS_PER_MEMORY + 1)]
TOO_MANY_MEMORIES = [
    item(key=f"k{n}") for n in range(limits.MAX_MEMORIES_PER_OUTPUT + 1)
]
SURROGATE_KEY = (
    '{"memories": [{"key": "\\ud800", "scope": "user",'
    ' "state": "inferred", "supersedes": null}]}'
)
NESTED = "[" * 100_000 + "]" * 100_000

# name, raw output, expected problem
REJECTED = [
    ("empty text", "", P.NOT_JSON),
    ("plain text", "I remembered it.", P.NOT_JSON),
    ("truncated json", '{"memories": [', P.NOT_JSON),
    ("a byte order mark", "\ufeff" + doc(), P.NOT_JSON),
    ("a duplicate member name", '{"memories": [], "memories": []}', P.NOT_JSON),
    ("a duplicate member in a memory", '{"memories": [{"a": 1, "a": 2}]}', P.NOT_JSON),
    ("NaN", '{"memories": [], "x": NaN}', P.NOT_JSON),
    ("Infinity", '{"memories": [], "x": Infinity}', P.NOT_JSON),
    ("a deeply nested document", NESTED, P.NOT_JSON),
    ("a deeply nested memories", '{"memories": ' + NESTED + "}", P.NOT_JSON),
    ("a huge integer", '{"memories": [], "n": ' + "9" * 5000 + "}", P.NOT_JSON),
    ("a list at the top", "[]", P.NOT_AN_OBJECT),
    ("a string at the top", '"memories"', P.NOT_AN_OBJECT),
    ("null at the top", "null", P.NOT_AN_OBJECT),
    ("an unknown top member", doc(extra=1), P.UNKNOWN_FIELD),
    ("no memories member", "{}", P.MISSING_FIELD),
    ("memories is null", '{"memories": null}', P.WRONG_TYPE),
    ("memories is an object", '{"memories": {}}', P.WRONG_TYPE),
    ("a memory that is a string", doc("indent"), P.NOT_AN_OBJECT),
    ("a memory that is null", doc(None), P.NOT_AN_OBJECT),
    ("an unknown member of a memory", doc(item(color="blue")), P.UNKNOWN_FIELD),
    (
        "a misspelled member",
        doc(item(supercedes=None, supersedes=...)),
        P.UNKNOWN_FIELD,
    ),
    ("no key", doc(item(key=...)), P.MISSING_FIELD),
    ("no scope", doc(item(scope=...)), P.MISSING_FIELD),
    ("no state", doc(item(state=...)), P.MISSING_FIELD),
    ("no supersedes", doc(item(supersedes=...)), P.MISSING_FIELD),
    ("a numeric key", doc(item(key=5)), P.WRONG_TYPE),
    ("a null key", doc(item(key=None)), P.WRONG_TYPE),
    ("an empty key", doc(item(key="")), P.INVALID_VALUE),
    ("a blank key", doc(item(key=" \t\n")), P.INVALID_VALUE),
    ("a key with a newline", doc(item(key="a\nb")), P.INVALID_VALUE),
    ("a key with NUL", doc(item(key="a\u0000b")), P.INVALID_VALUE),
    ("a key with a lone surrogate", SURROGATE_KEY, P.INVALID_VALUE),
    ("a key that is too long", doc(item(key=LONG_KEY)), P.INVALID_VALUE),
    ("an unknown scope", doc(item(scope="team")), P.INVALID_VALUE),
    ("a scope in another case", doc(item(scope="User")), P.INVALID_VALUE),
    ("project_group scope", doc(item(scope="project_group")), P.INVALID_VALUE),
    ("a numeric scope", doc(item(scope=1)), P.WRONG_TYPE),
    ("observed state", doc(item(state="observed")), P.INVALID_VALUE),
    ("rejected state", doc(item(state="rejected")), P.INVALID_VALUE),
    ("an empty supersedes", doc(item(supersedes="")), P.INVALID_VALUE),
    ("a blank supersedes", doc(item(supersedes="  ")), P.INVALID_VALUE),
    ("a numeric supersedes", doc(item(supersedes=3)), P.WRONG_TYPE),
    ("a null content", doc(item(content=None)), P.WRONG_TYPE),
    ("a blank content", doc(item(content="   ")), P.INVALID_VALUE),
    ("an empty content", doc(item(content="")), P.INVALID_VALUE),
    ("a content that is too long", doc(item(content=LONG_CONTENT)), P.INVALID_VALUE),
    ("a content with NUL", doc(item(content="a\u0000b")), P.INVALID_VALUE),
    ("conflicts as a string", doc(item(conflicts_with="a")), P.WRONG_TYPE),
    ("conflicts as null", doc(item(conflicts_with=None)), P.WRONG_TYPE),
    ("a numeric conflict", doc(item(conflicts_with=[1])), P.WRONG_TYPE),
    ("a blank conflict", doc(item(conflicts_with=[" "])), P.INVALID_VALUE),
    ("a repeated conflict", doc(item(conflicts_with=["a", "a"])), P.DUPLICATE),
    (
        "too many conflicts",
        doc(item(conflicts_with=TOO_MANY_CONFLICTS)),
        P.TOO_MANY,
    ),
    ("too many memories", doc(*TOO_MANY_MEMORIES), P.TOO_MANY),
    (
        "a valid memory then an invalid one",
        doc(item(), item(scope="team")),
        P.INVALID_VALUE,
    ),
    (
        "an output that is too large",
        " " * (limits.MAX_RAW_OUTPUT_CHARS + 1),
        P.TOO_LARGE,
    ),
    ("None", None, P.NOT_TEXT),
    ("bytes", doc().encode(), P.NOT_TEXT),
    ("an already parsed dict", {"memories": []}, P.NOT_TEXT),
    ("an already parsed list", [], P.NOT_TEXT),
    ("a number", 5, P.NOT_TEXT),
]


class RejectedOutputTest(unittest.TestCase):
    def test_every_violation_is_refused_with_its_closed_problem(self):
        for name, raw, problem in REJECTED:
            with self.subTest(name):
                with self.assertRaises(WorkerOutputError) as raised:
                    parse_worker_output(raw)
                self.assertEqual(raised.exception.problem, problem)

    def test_the_error_never_contains_the_output(self):
        secret = "SECRET-MARKER-" + "0d1e"
        for raw in (
            doc(item(scope=secret)),
            doc(item(key=secret, state="bogus")),
            f'{{"memories": [], "{secret}": 1}}',
            f"not json {secret}",
        ):
            with self.subTest(raw[:20]):
                with self.assertRaises(WorkerOutputError) as raised:
                    parse_worker_output(raw)
                self.assertNotIn(secret, str(raised.exception))
                self.assertNotIn(secret, repr(raised.exception))
                self.assertIsNone(raised.exception.__cause__)

    def test_the_whole_output_is_dropped_when_one_memory_is_bad(self):
        # Nothing is returned for the good memory: all-or-nothing, like the benchmark's
        # schema adherence.
        with self.assertRaises(WorkerOutputError):
            parse_worker_output(doc(item(key="good"), item(key="bad", state="x")))


class WorkerProtocolTest(unittest.TestCase):
    def test_an_async_extract_is_a_worker(self):
        class Good:
            async def extract(self, input_text: str) -> str:
                return doc()

        check_worker(Good())
        self.assertIsInstance(Good(), MemoryWorker)

    def test_a_synchronous_extract_is_refused_because_it_would_block_the_loop(self):
        class Blocking:
            def extract(self, input_text: str) -> str:
                return doc()

        with self.assertRaises(TypeError):
            check_worker(Blocking())

    def test_things_that_are_not_workers_are_refused(self):
        class NoExtract:
            pass

        for candidate in (None, 5, "worker", NoExtract(), object):
            with self.subTest(candidate):
                with self.assertRaises(TypeError):
                    check_worker(candidate)


KNOWN_KEYWORDS = {
    "$schema",
    "description",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "pattern",
    "uniqueItems",
}
JSON_TYPES = {
    "string": str,
    "array": list,
    "object": dict,
    "null": type(None),
}


def schema_accepts(schema: dict, value) -> bool:
    """A validator for exactly the keywords ``memory-worker-output-v1`` uses.

    The backend does not depend on ``jsonschema`` (the benchmark's checks do), so
    this small interpreter reads the SAME schema file. A keyword it does not know
    raises ``NotImplementedError``: a schema that grows a rule fails this test
    instead of being ignored.
    """
    unknown = set(schema) - KNOWN_KEYWORDS
    if unknown:
        raise NotImplementedError(sorted(unknown))
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(isinstance(value, JSON_TYPES[name]) for name in names):
            return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if "pattern" in schema and isinstance(value, str):
        if re.search(schema["pattern"], value) is None:
            return False
    if isinstance(value, dict):
        if any(name not in value for name in schema.get("required", [])):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties", True) is False and set(value) - set(
            properties
        ):
            return False
        for name, sub_schema in properties.items():
            if name in value and not schema_accepts(sub_schema, value[name]):
                return False
    if isinstance(value, list):
        if "items" in schema and not all(
            schema_accepts(schema["items"], element) for element in value
        ):
            return False
        if schema.get("uniqueItems") and len({json.dumps(e) for e in value}) != len(
            value
        ):
            return False
    return True


class AgreesWithTheBenchmarkSchemaTest(unittest.TestCase):
    """Both validators, the same documents: nothing the schema forbids is accepted.

    Where this module is stricter than the schema (it stores the memories, so the
    text has bounds and a key has no control character), the document is listed in
    ``STRICTER`` and the test asserts that the schema does allow it.
    """

    STRICTER = {
        "a key with a newline",
        "a key with NUL",
        "a key that is too long",
        "a content that is too long",
        "a content with NUL",
        "too many conflicts",
        "too many memories",
        "an output that is too large",
        "a key with a lone surrogate",
    }
    # ``json.loads`` keeps the last of two members with one name; the contract
    # refuses the document, which a schema cannot express.
    DUPLICATE_MEMBERS = {"a duplicate member name", "a duplicate member in a memory"}

    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_the_two_agree_on_every_document_of_the_table(self):
        compared = stricter = 0
        for name, raw, _problem in REJECTED:
            if not isinstance(raw, str):
                continue
            try:
                document = json.loads(raw)
            except (ValueError, RecursionError):
                continue  # not JSON at all: nothing to compare
            with self.subTest(name):
                accepted = schema_accepts(self.schema, document)
                if name in self.DUPLICATE_MEMBERS:
                    continue
                if name in self.STRICTER:
                    self.assertTrue(
                        accepted, "listed as stricter, but the schema refuses"
                    )
                    stricter += 1
                else:
                    self.assertFalse(
                        accepted, "the schema accepts what the contract refuses"
                    )
                    compared += 1
        self.assertGreater(compared, 30)
        self.assertGreater(stricter, 5)

    def test_what_the_contract_accepts_the_schema_accepts(self):
        accepted = [
            doc(),
            doc(item()),
            doc(item(content=...)),
            doc(item(conflicts_with=["a", "b"], supersedes="x")),
            doc(item(scope="shared", state="confirmed")),
            doc(item(key="日本語", content="  x ")),
        ]
        for raw in accepted:
            with self.subTest(raw[:40]):
                parse_worker_output(raw)
                self.assertTrue(schema_accepts(self.schema, json.loads(raw)))

    def test_the_interpreter_itself_tells_valid_from_invalid(self):
        # A validator that accepted everything would make the two tests above vacuous.
        for document, expected in (
            ({"memories": []}, True),
            ({}, False),
            ({"memories": [], "x": 1}, False),
            ({"memories": [{"key": "a"}]}, False),
            ([], False),
        ):
            with self.subTest(document):
                self.assertIs(schema_accepts(self.schema, document), expected)


if __name__ == "__main__":
    unittest.main()
