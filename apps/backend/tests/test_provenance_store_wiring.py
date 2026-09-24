"""What the store does with VALID arguments, on an unconfigured ``Database``.

The store raises ``DatabaseNotConfiguredError`` when it opens its first session,
so a test that expects it proves that the arguments passed every check and that
the pure rules (``rules.py``) were called on the way. They need the rules to be
implemented, so they fail against the stubs (with ``NotImplementedError``); the
checks that reject bad arguments are in ``test_provenance_store_validation.py``.
"""

from datetime import datetime
from uuid import uuid4

from paw_backend.research.provenance import (
    EntityKind,
    InputProblem,
    InvalidProvenanceInputError,
    RelationKind,
    Stance,
)

from .provenance_support import (
    StoreValidationTestCase,
    link,
    unconfigured_store,
)


class RecordClaimReachesTheDatabaseTest(StoreValidationTestCase):
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

    async def test_valid_arguments_reach_the_database(self):
        await self.reaches_the_database(self.record())
        await self.reaches_the_database(self.record(task_id=uuid4()))
        await self.reaches_the_database(self.record(sources=(link(),)))

    async def test_the_text_limit_is_2000_characters(self):
        await self.reaches_the_database(self.record(text="x" * 2000))
        await self.reaches_the_database(self.record(text="あ" * 2000))
        await self.reaches_the_database(self.record(text="  padded  "))

    async def test_20_sources_are_allowed_as_a_list_or_a_tuple(self):
        twenty = [link(f"https://example.com/{n}") for n in range(20)]

        await self.reaches_the_database(self.record(sources=twenty))
        await self.reaches_the_database(self.record(sources=tuple(twenty)))

    async def test_the_same_source_with_two_stances_is_a_conflict(self):
        conflicting = [link("https://a.io/"), link("https://a.io/", Stance.CONTRADICTS)]

        self.assertEqual(
            await self.rejected(self.record(sources=conflicting)),
            ("sources", InputProblem.CONFLICT),
        )

    async def test_a_clock_that_returns_a_naive_time_is_reported(self):
        store = unconfigured_store(clock=lambda: datetime(2026, 9, 24))

        with self.assertRaises(InvalidProvenanceInputError) as raised:
            await store.record_claim(uuid4(), **self.arguments())

        self.assertEqual(
            (raised.exception.field, raised.exception.problem),
            ("clock", InputProblem.NAIVE_DATETIME),
        )


class MarkRelatedReachesTheDatabaseTest(StoreValidationTestCase):
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

    async def test_valid_arguments_reach_the_database(self):
        for entity in EntityKind:
            for kind in RelationKind:
                with self.subTest(entity=entity, kind=kind):
                    await self.reaches_the_database(self.mark(entity=entity, kind=kind))
