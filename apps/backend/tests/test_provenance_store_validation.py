"""Argument checks of the ``ProvenanceStore`` methods.

The store here runs on an unconfigured ``Database``: it raises
``DatabaseNotConfiguredError`` the moment it opens a session. So a test that
expects ``InvalidProvenanceInputError`` proves the check happened before the
database was touched, and a test that expects ``DatabaseNotConfiguredError``
proves that the arguments were accepted.
"""

import unittest
from uuid import uuid4

from paw_backend.db import Database
from paw_backend.research.provenance import (
    EntityKind,
    InputProblem,
    InvalidProvenanceInputError,
    ProvenanceStore,
    Reference,
    RelationKind,
    Stance,
)

from .provenance_support import (
    T0,
    FakeClock,
    StoreValidationTestCase,
    link,
    source_input,
)
from .support import make_settings


class ConstructorTest(unittest.TestCase):
    def test_the_collaborators_are_checked_up_front(self):
        database = Database(make_settings())

        for bad in (None, "database", object()):
            with self.subTest(repr(bad)):
                with self.assertRaises(TypeError):
                    ProvenanceStore(bad)
        with self.assertRaises(TypeError):
            ProvenanceStore(database, clock="now")
        with self.assertRaises(TypeError):
            ProvenanceStore(database, clock=lambda value: value)
        for bad in (True, "5000", 5000.0, None):
            with self.subTest(repr(bad)):
                with self.assertRaises(TypeError):
                    ProvenanceStore(database, lock_timeout_ms=bad)
        for bad in (49, 0, -1, 60_001):
            with self.subTest(bad):
                with self.assertRaises(ValueError):
                    ProvenanceStore(database, lock_timeout_ms=bad)

    def test_valid_options_are_accepted_and_nothing_connects(self):
        database = Database(make_settings())

        ProvenanceStore(database)
        ProvenanceStore(database, clock=FakeClock(), lock_timeout_ms=50)
        ProvenanceStore(database, clock=lambda: T0, lock_timeout_ms=60_000)
        self.assertFalse(database.configured)


class RecordClaimValidationTest(StoreValidationTestCase):
    def arguments(self, **overrides):
        values = {
            "created_by": uuid4(),
            "text": "The sky is blue.",
            "sources": [link()],
        }
        values.update(overrides)
        return values

    def record(self, project_id=None, **overrides):
        return self.store.record_claim(
            project_id or uuid4(), **self.arguments(**overrides)
        )

    async def test_the_ids_are_checked_in_order(self):
        cases = [
            ("project_id", dict(project_id="not-a-uuid")),
            ("created_by", dict(created_by=str(uuid4()))),
            ("task_id", dict(task_id=str(uuid4()))),
        ]
        for field, options in cases:
            with self.subTest(field):
                self.assertEqual(
                    await self.rejected(self.record(**options)),
                    (field, InputProblem.WRONG_TYPE),
                )
        self.assertEqual(
            await self.rejected(self.store.record_claim(None, **self.arguments())),
            ("project_id", InputProblem.REQUIRED),
        )
        self.assertEqual(
            await self.rejected(self.record(created_by=None)),
            ("created_by", InputProblem.REQUIRED),
        )

    async def test_the_first_bad_argument_is_the_one_reported(self):
        everything_wrong = dict(
            project_id="x", created_by="x", task_id="x", text="", sources=[]
        )
        self.assertEqual(
            (await self.rejected(self.record(**everything_wrong)))[0], "project_id"
        )
        del everything_wrong["project_id"]
        self.assertEqual(
            (await self.rejected(self.record(**everything_wrong)))[0], "created_by"
        )
        del everything_wrong["created_by"]
        self.assertEqual(
            (await self.rejected(self.record(**everything_wrong)))[0], "task_id"
        )
        del everything_wrong["task_id"]
        self.assertEqual(
            (await self.rejected(self.record(**everything_wrong)))[0], "text"
        )

    async def test_the_text_is_checked(self):
        cases = {
            None: InputProblem.REQUIRED,
            b"bytes": InputProblem.WRONG_TYPE,
            5: InputProblem.WRONG_TYPE,
            "": InputProblem.BLANK,
            "  \t\n": InputProblem.BLANK,
            "　": InputProblem.BLANK,
            "a\x00b": InputProblem.INVALID_CHARACTERS,
            "\ud800": InputProblem.INVALID_CHARACTERS,
            "x" * 2001: InputProblem.TOO_LONG,
            "あ" * 2001: InputProblem.TOO_LONG,
        }
        for value, expected in cases.items():
            with self.subTest(repr(value)[:30]):
                self.assertEqual(
                    await self.rejected(self.record(text=value)), ("text", expected)
                )

    async def test_the_sources_are_checked(self):
        one = link()
        cases = {
            "none": (None, InputProblem.REQUIRED),
            "string": ("https://example.com/", InputProblem.WRONG_TYPE),
            "dict": ({"a": one}, InputProblem.WRONG_TYPE),
            "set": ({one}, InputProblem.WRONG_TYPE),
            "generator": ((x for x in [one]), InputProblem.WRONG_TYPE),
            "empty list": ([], InputProblem.EMPTY),
            "empty tuple": ((), InputProblem.EMPTY),
            "a source instead of a link": ([source_input()], InputProblem.WRONG_TYPE),
            "a string element": ([one, "x"], InputProblem.WRONG_TYPE),
            "a none element": ([one, None], InputProblem.WRONG_TYPE),
            "a late bad element": ([one] * 5 + [object()], InputProblem.WRONG_TYPE),
            "21 links": (
                [link(f"https://example.com/{n}") for n in range(21)],
                InputProblem.TOO_MANY,
            ),
            "21 repeats": ([one] * 21, InputProblem.TOO_MANY),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    await self.rejected(self.record(sources=value)),
                    ("sources", expected),
                )

    async def test_every_element_is_checked_before_sources_are_merged(self):
        conflicting = [link("https://a.io/"), link("https://a.io/", Stance.CONTRADICTS)]

        self.assertEqual(
            await self.rejected(self.record(sources=conflicting + ["bad"])),
            ("sources", InputProblem.WRONG_TYPE),
        )

    async def test_the_count_is_checked_before_sources_are_merged(self):
        many = [link("https://a.io/")] * 21

        self.assertEqual(
            await self.rejected(self.record(sources=many)),
            ("sources", InputProblem.TOO_MANY),
        )

    async def test_the_error_never_contains_the_text_or_the_locator(self):
        secret = "SECRET-TOKEN-4f9a1c"
        error_text = ""
        for options in (
            dict(text=secret * 200),
            dict(text=secret, sources=[secret]),
        ):
            with self.assertRaises(InvalidProvenanceInputError) as raised:
                await self.record(**options)
            error_text += str(raised.exception) + repr(raised.exception)

        self.assertNotIn("SECRET", error_text)


class AddReferenceValidationTest(StoreValidationTestCase):
    def add(self, project_id=None, **overrides):
        values = {
            "reference": Reference.answer(uuid4()),
            "claim_ids": [uuid4()],
            "created_by": uuid4(),
        }
        values.update(overrides)
        return self.store.add_reference(project_id or uuid4(), **values)

    async def test_valid_arguments_reach_the_database(self):
        await self.reaches_the_database(self.add())
        await self.reaches_the_database(self.add(claim_ids=(uuid4(),)))
        await self.reaches_the_database(self.add(reference=Reference.task(uuid4())))

    async def test_each_argument_is_checked_in_order(self):
        self.assertEqual(
            await self.rejected(self.add(project_id="x", reference="x")),
            ("project_id", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(self.add(reference="answer", claim_ids=[])),
            ("reference", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(self.add(reference=None)),
            ("reference", InputProblem.REQUIRED),
        )
        self.assertEqual(
            await self.rejected(self.add(claim_ids=[], created_by="x")),
            ("claim_ids", InputProblem.EMPTY),
        )
        self.assertEqual(
            await self.rejected(self.add(created_by="x")),
            ("created_by", InputProblem.WRONG_TYPE),
        )

    async def test_the_claim_ids_are_checked(self):
        one = uuid4()
        cases = {
            "none": (None, InputProblem.REQUIRED),
            "string": (str(one), InputProblem.WRONG_TYPE),
            "set": ({one}, InputProblem.WRONG_TYPE),
            "generator": ((x for x in [one]), InputProblem.WRONG_TYPE),
            "empty": ([], InputProblem.EMPTY),
            "string element": ([one, str(uuid4())], InputProblem.WRONG_TYPE),
            "none element": ([None], InputProblem.WRONG_TYPE),
            "51 ids": ([uuid4() for _ in range(51)], InputProblem.TOO_MANY),
            "51 repeats": ([one] * 51, InputProblem.TOO_MANY),
            "late bad element": ([one] * 10 + [5], InputProblem.WRONG_TYPE),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    await self.rejected(self.add(claim_ids=value)),
                    ("claim_ids", expected),
                )

    async def test_50_claim_ids_are_allowed(self):
        await self.reaches_the_database(
            self.add(claim_ids=[uuid4() for _ in range(50)])
        )
        await self.reaches_the_database(self.add(claim_ids=[uuid4()] * 50))


class MarkRelatedValidationTest(StoreValidationTestCase):
    def mark(self, project_id=None, **overrides):
        values = {
            "entity": EntityKind.CLAIM,
            "kind": RelationKind.DUPLICATE,
            "first_id": uuid4(),
            "second_id": uuid4(),
            "created_by": uuid4(),
        }
        values.update(overrides)
        return self.store.mark_related(project_id or uuid4(), **values)

    async def test_each_argument_is_checked_in_order(self):
        cases = [
            ("project_id", dict(project_id="x", entity="claim")),
            ("entity", dict(entity="claim", kind="duplicate")),
            ("kind", dict(kind="duplicate", first_id="x")),
            ("first_id", dict(first_id="x", second_id="x")),
            ("second_id", dict(second_id="x", created_by="x")),
            ("created_by", dict(created_by="x")),
        ]
        for field, options in cases:
            with self.subTest(field):
                self.assertEqual((await self.rejected(self.mark(**options)))[0], field)

    async def test_wrong_types_are_not_coerced(self):
        for options in (
            dict(entity="claim"),
            dict(entity=None),
            dict(kind="duplicate"),
            dict(kind=None),
        ):
            with self.subTest(options):
                field, expected = await self.rejected(self.mark(**options))
                self.assertIn(field, ("entity", "kind"))
                self.assertIn(
                    expected, (InputProblem.WRONG_TYPE, InputProblem.REQUIRED)
                )

    async def test_a_thing_is_not_related_to_itself(self):
        same = uuid4()

        self.assertEqual(
            await self.rejected(self.mark(first_id=same, second_id=same)),
            ("second_id", InputProblem.SELF_REFERENCE),
        )


class ReadValidationTest(StoreValidationTestCase):
    async def test_get_claim(self):
        await self.reaches_the_database(self.store.get_claim(uuid4(), uuid4()))
        self.assertEqual(
            await self.rejected(self.store.get_claim("x", "x")),
            ("project_id", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(self.store.get_claim(uuid4(), str(uuid4()))),
            ("claim_id", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(self.store.get_claim(uuid4(), None)),
            ("claim_id", InputProblem.REQUIRED),
        )

    async def test_trace(self):
        reference = Reference.answer(uuid4())
        await self.reaches_the_database(self.store.trace(uuid4(), reference))
        await self.reaches_the_database(self.store.trace(uuid4(), reference, limit=1))
        await self.reaches_the_database(self.store.trace(uuid4(), reference, limit=200))
        self.assertEqual(
            await self.rejected(self.store.trace("x", "x", limit=0)),
            ("project_id", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(self.store.trace(uuid4(), "answer", limit=0)),
            ("reference", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(self.store.trace(uuid4(), None)),
            ("reference", InputProblem.REQUIRED),
        )

    async def test_the_trace_limit(self):
        reference = Reference.task(uuid4())
        cases = {
            0: InputProblem.OUT_OF_RANGE,
            201: InputProblem.OUT_OF_RANGE,
            -1: InputProblem.OUT_OF_RANGE,
            True: InputProblem.WRONG_TYPE,
            "5": InputProblem.WRONG_TYPE,
            5.0: InputProblem.WRONG_TYPE,
            None: InputProblem.REQUIRED,
        }
        for value, expected in cases.items():
            with self.subTest(repr(value)):
                self.assertEqual(
                    await self.rejected(
                        self.store.trace(uuid4(), reference, limit=value)
                    ),
                    ("limit", expected),
                )

    async def test_list_relations(self):
        await self.reaches_the_database(
            self.store.list_relations(uuid4(), EntityKind.SOURCE, uuid4())
        )
        self.assertEqual(
            await self.rejected(self.store.list_relations("x", "claim", "x")),
            ("project_id", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(self.store.list_relations(uuid4(), "claim", "x")),
            ("entity", InputProblem.WRONG_TYPE),
        )
        self.assertEqual(
            await self.rejected(
                self.store.list_relations(uuid4(), EntityKind.CLAIM, "x")
            ),
            ("entity_id", InputProblem.WRONG_TYPE),
        )


if __name__ == "__main__":
    unittest.main()
