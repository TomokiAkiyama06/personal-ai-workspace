"""The statements of ``queries.py``, one function at a time (real PostgreSQL).

Each test runs a function inside a transaction it opens itself, seeds and checks
rows with SQL (``provenance_support``) and never uses the store, so a failure
points at the function under test.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from paw_backend.db import Database
from paw_backend.research.provenance import (
    Claim,
    EntityKind,
    ProvenanceConflictError,
    Reference,
    Relation,
    RelationKind,
    Source,
    SourceLink,
    Stance,
    queries,
)
from paw_backend.research.providers.contract import SourceType

from .memory_support import TEST_DATABASE_URL
from .provenance_support import (
    T0,
    PostgresProvenanceTestCase,
    content_hash,
    expected_fingerprint,
    requires_postgres,
    source_input,
)
from .support import make_settings

HOUR = timedelta(hours=1)


class ForbiddenSession:
    """A session that fails the test when a statement is executed."""

    async def execute(self, *args, **kwargs):
        raise AssertionError("the database must not be touched")


@requires_postgres
class QueryTestCase(PostgresProvenanceTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.database = Database(make_settings(database_url=TEST_DATABASE_URL))
        self.addAsyncCleanup(self.database.dispose)

    @asynccontextmanager
    async def transaction(self):
        """One transaction that commits when the block ends normally."""
        async with self.database.session() as session, session.begin():
            yield session


class EnsureSourceTest(QueryTestCase):
    async def ensure(self, source, *, project=None, at=T0):
        async with self.transaction() as session:
            return await queries.ensure_source(
                session, project or self.project_id, source, at
            )

    async def test_a_new_source_is_inserted_and_returned(self):
        source = source_input(
            "HTTPS://Example.com/Docs?utm_source=x",
            content="page",
            source_type=SourceType.OFFICIAL_GITHUB,
            fetched_at=T0 - HOUR,
            published_at=datetime(2026, 8, 1, tzinfo=UTC),
            title="The docs",
        )

        recorded, created = await self.ensure(source, at=T0 + HOUR)

        self.assertTrue(created)
        self.assertEqual(
            recorded,
            Source(
                id=recorded.id,
                project_id=self.project_id,
                locator="https://example.com/Docs",
                source_type=SourceType.OFFICIAL_GITHUB,
                title="The docs",
                content_hash=content_hash("page"),
                fetched_at=T0 - HOUR,
                published_at=datetime(2026, 8, 1, tzinfo=UTC),
                created_at=T0 + HOUR,
            ),
        )
        self.assertIsInstance(recorded.id, UUID)
        self.assertIsInstance(recorded.source_type, SourceType)
        self.assertEqual(self.source_from_sql(recorded.id), recorded)
        self.assertEqual(self.table_count("research_sources"), 1)

    async def test_an_unknown_publication_date_is_stored_as_null(self):
        recorded, _ = await self.ensure(source_input(published_at=None))

        self.assertIsNone(recorded.published_at)
        self.assertIsNone(self.source_rows()[0]["published_at"])

    async def test_the_same_source_again_returns_the_first_record_unchanged(self):
        first, created_first = await self.ensure(
            source_input(
                title="First",
                fetched_at=T0,
                published_at=T0 - HOUR,
                source_type=SourceType.PRIMARY,
            ),
            at=T0,
        )

        second, created_second = await self.ensure(
            source_input(
                title="Second",
                fetched_at=T0 + HOUR,
                published_at=None,
                source_type=SourceType.COMMUNITY,
            ),
            at=T0 + 2 * HOUR,
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(second, first)
        self.assertEqual(self.table_count("research_sources"), 1)
        self.assertEqual(self.source_from_sql(first.id), first)

    async def test_two_calls_in_one_transaction_share_the_row(self):
        async with self.transaction() as session:
            first = await queries.ensure_source(
                session, self.project_id, source_input(), T0
            )
            second = await queries.ensure_source(
                session, self.project_id, source_input(), T0
            )

        self.assertEqual((first[1], second[1]), (True, False))
        self.assertEqual(first[0].id, second[0].id)
        self.assertEqual(self.table_count("research_sources"), 1)

    async def test_the_same_locator_with_another_content_is_another_source(self):
        first, _ = await self.ensure(source_input(content="v1"))
        second, created = await self.ensure(source_input(content="v2"))

        self.assertTrue(created)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            sorted(row["content_hash"] for row in self.source_rows()),
            sorted([content_hash("v1"), content_hash("v2")]),
        )

    async def test_the_same_source_in_another_project_is_another_source(self):
        first, _ = await self.ensure(source_input())
        second, created = await self.ensure(
            source_input(), project=self.other_project_id
        )

        self.assertTrue(created)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.project_id, self.other_project_id)
        self.assertEqual(self.table_count("research_sources"), 2)
        # The first project's record is untouched.
        again, created_again = await self.ensure(source_input())
        self.assertEqual((again, created_again), (first, False))

    async def test_a_record_of_another_project_is_never_returned(self):
        self.seed_source(
            project_id=self.other_project_id, locator="https://example.com/a"
        )

        recorded, created = await self.ensure(source_input("https://example.com/a"))

        self.assertTrue(created)
        self.assertEqual(recorded.project_id, self.project_id)

    async def test_the_existing_record_is_found_in_this_project_not_in_another(self):
        # The other project's row is older: a lookup that ignores the project
        # would find it first.
        foreign = self.seed_source(
            project_id=self.other_project_id, locator="https://example.com/a"
        )

        first, created_first = await self.ensure(source_input("https://example.com/a"))
        second, created_second = await self.ensure(
            source_input("https://example.com/a")
        )

        self.assertEqual((created_first, created_second), (True, False))
        self.assertNotEqual(first.id, foreign)
        self.assertEqual(second, first)
        self.assertEqual(second.project_id, self.project_id)

    async def test_the_title_is_kept_exactly_as_given(self):
        recorded, _ = await self.ensure(source_input(title="  Padded  title  "))

        self.assertEqual(recorded.title, "  Padded  title  ")
        self.assertEqual(self.source_rows()[0]["title"], "  Padded  title  ")

    async def test_the_function_does_not_commit(self):
        with self.assertRaises(RuntimeError):
            async with self.transaction() as session:
                await queries.ensure_source(
                    session, self.project_id, source_input(), T0
                )
                raise RuntimeError("roll back")

        self.assertEqual(self.table_count("research_sources"), 0)

    async def test_an_existing_source_is_found_even_when_seeded_by_sql(self):
        seeded = self.seed_source(
            locator="https://example.com/a", content="v1", title="Seeded"
        )

        recorded, created = await self.ensure(source_input("https://example.com/a"))

        self.assertFalse(created)
        self.assertEqual(recorded.id, seeded)
        self.assertEqual(recorded.title, "Seeded")


class EnsureClaimTest(QueryTestCase):
    async def ensure(self, claim_text="The sky is blue.", *, project=None, **options):
        arguments = {
            "created_by": self.user_id,
            "task_id": None,
            "text": claim_text,
            "fingerprint": expected_fingerprint(claim_text),
            "created_at": T0,
        }
        arguments.update(options)
        async with self.transaction() as session:
            return await queries.ensure_claim(
                session, project or self.project_id, **arguments
            )

    async def test_a_new_claim_is_inserted_and_returned(self):
        task_id = self.seed_task()

        claim, created = await self.ensure(
            "  The Sky is Blue.  ", task_id=task_id, created_at=T0 + HOUR
        )

        self.assertTrue(created)
        self.assertEqual(
            claim,
            Claim(
                id=claim.id,
                project_id=self.project_id,
                text="  The Sky is Blue.  ",
                task_id=task_id,
                created_by=self.user_id,
                created_at=T0 + HOUR,
            ),
        )
        self.assertEqual(self.claim_from_sql(claim.id), claim)
        (row,) = self.claim_rows()
        self.assertEqual(
            row["text_fingerprint"], expected_fingerprint("The sky is blue.")
        )

    async def test_a_claim_without_a_task_has_no_task(self):
        claim, _ = await self.ensure(task_id=None)

        self.assertIsNone(claim.task_id)
        self.assertIsNone(self.claim_rows()[0]["task_id"])

    async def test_the_same_fingerprint_returns_the_first_claim_unchanged(self):
        first_task = self.seed_task()
        first_user = uuid4()
        first, created_first = await self.ensure(
            "The sky is blue.",
            created_by=first_user,
            task_id=first_task,
            created_at=T0,
        )

        second, created_second = await self.ensure(
            "THE SKY   IS BLUE.",
            created_by=uuid4(),
            task_id=self.seed_task(),
            created_at=T0 + HOUR,
            fingerprint=first_fingerprint(),
        )

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(second, first)
        self.assertEqual(second.text, "The sky is blue.")
        self.assertEqual(second.created_by, first_user)
        self.assertEqual(second.task_id, first_task)
        self.assertEqual(second.created_at, T0)
        self.assertEqual(self.table_count("research_claims"), 1)

    async def test_a_different_fingerprint_is_a_different_claim(self):
        first, _ = await self.ensure("The sky is blue.")
        second, created = await self.ensure("The sky is green.")

        self.assertTrue(created)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.table_count("research_claims"), 2)

    async def test_the_same_fingerprint_in_another_project_is_another_claim(self):
        first, _ = await self.ensure()
        second, created = await self.ensure(project=self.other_project_id)

        self.assertTrue(created)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.project_id, self.other_project_id)
        again, created_again = await self.ensure()
        self.assertEqual((again, created_again), (first, False))

    async def test_a_claim_of_another_project_is_never_returned(self):
        self.seed_claim("The sky is blue.", project_id=self.other_project_id)

        claim, created = await self.ensure()

        self.assertTrue(created)
        self.assertEqual(claim.project_id, self.project_id)

    async def test_two_calls_in_one_transaction_share_the_row(self):
        fingerprint = expected_fingerprint("x")
        async with self.transaction() as session:
            first = await queries.ensure_claim(
                session,
                self.project_id,
                created_by=self.user_id,
                task_id=None,
                text="x",
                fingerprint=fingerprint,
                created_at=T0,
            )
            second = await queries.ensure_claim(
                session,
                self.project_id,
                created_by=self.user_id,
                task_id=None,
                text="X",
                fingerprint=fingerprint,
                created_at=T0,
            )

        self.assertEqual((first[1], second[1]), (True, False))
        self.assertEqual(first[0], second[0])

    async def test_the_existing_claim_is_found_in_this_project_not_in_another(self):
        # The other project's row is older: a lookup that ignores the project
        # would find it first.
        foreign = self.seed_claim(project_id=self.other_project_id)

        first, created_first = await self.ensure()
        second, created_second = await self.ensure()

        self.assertEqual((created_first, created_second), (True, False))
        self.assertNotEqual(first.id, foreign)
        self.assertEqual(second, first)
        self.assertEqual(second.project_id, self.project_id)

    async def test_the_function_does_not_commit(self):
        with self.assertRaises(RuntimeError):
            async with self.transaction() as session:
                await queries.ensure_claim(
                    session,
                    self.project_id,
                    created_by=self.user_id,
                    task_id=None,
                    text="x",
                    fingerprint=expected_fingerprint("x"),
                    created_at=T0,
                )
                raise RuntimeError("roll back")

        self.assertEqual(self.table_count("research_claims"), 0)


def first_fingerprint() -> str:
    return expected_fingerprint("The sky is blue.")


class LinkClaimSourceTest(QueryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.claim_id = self.seed_claim()
        self.source_id = self.seed_source()
        self.source = self.source_from_sql(self.source_id)

    async def link(self, stance=Stance.SUPPORTS, *, at=T0, claim=None, source=None):
        async with self.transaction() as session:
            return await queries.link_claim_source(
                session,
                self.project_id,
                claim or self.claim_id,
                source or self.source,
                stance,
                at,
            )

    async def test_a_new_link_is_inserted_and_returned(self):
        link, created = await self.link(Stance.CONTRADICTS, at=T0 + HOUR)

        self.assertTrue(created)
        self.assertEqual(
            link,
            SourceLink(
                claim_id=self.claim_id,
                source=self.source,
                stance=Stance.CONTRADICTS,
                linked_at=T0 + HOUR,
            ),
        )
        self.assertIs(link.source, self.source)
        row = self.link_rows(self.claim_id)[self.source_id]
        self.assertEqual(
            (row["stance"], row["project_id"], row["created_at"]),
            ("contradicts", self.project_id, T0 + HOUR),
        )

    async def test_the_same_stance_again_changes_nothing_and_keeps_the_first_time(self):
        first, _ = await self.link(Stance.SUPPORTS, at=T0)

        again, created = await self.link(Stance.SUPPORTS, at=T0 + 3 * HOUR)

        self.assertFalse(created)
        self.assertEqual(again, first)
        self.assertEqual(again.linked_at, T0)
        self.assertEqual(len(self.link_rows(self.claim_id)), 1)

    async def test_the_other_stance_is_a_conflict_and_changes_nothing(self):
        for first, second in (
            (Stance.SUPPORTS, Stance.CONTRADICTS),
            (Stance.CONTRADICTS, Stance.SUPPORTS),
        ):
            with self.subTest(first):
                self.clean_tables()
                self.claim_id = self.seed_claim()
                self.source_id = self.seed_source()
                self.source = self.source_from_sql(self.source_id)
                await self.link(first)

                with self.assertRaises(ProvenanceConflictError):
                    await self.link(second, at=T0 + HOUR)

                row = self.link_rows(self.claim_id)[self.source_id]
                self.assertEqual((row["stance"], row["created_at"]), (first.value, T0))

    async def test_one_source_can_back_two_claims_and_a_claim_has_many_sources(self):
        other_claim = self.seed_claim("Another claim")
        other_source = self.source_from_sql(
            self.seed_source(locator="https://example.com/b")
        )

        await self.link()
        await self.link(claim=other_claim)
        await self.link(source=other_source)

        self.assertEqual(len(self.link_rows(self.claim_id)), 2)
        self.assertEqual(len(self.link_rows(other_claim)), 1)

    async def test_the_function_does_not_commit(self):
        with self.assertRaises(RuntimeError):
            async with self.transaction() as session:
                await queries.link_claim_source(
                    session,
                    self.project_id,
                    self.claim_id,
                    self.source,
                    Stance.SUPPORTS,
                    T0,
                )
                raise RuntimeError("roll back")

        self.assertEqual(self.link_rows(self.claim_id), {})


class AddClaimUseTest(QueryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.claim_id = self.seed_claim()

    async def use(self, reference, *, by=None, at=T0, claim=None):
        async with self.transaction() as session:
            return await queries.add_claim_use(
                session,
                self.project_id,
                claim or self.claim_id,
                reference,
                by or self.user_id,
                at,
            )

    async def test_a_use_is_recorded_once(self):
        answer = Reference.answer(uuid4())
        creator = uuid4()

        first = await self.use(answer, by=creator, at=T0)
        second = await self.use(answer, by=uuid4(), at=T0 + HOUR)

        self.assertEqual((first, second), (True, False))
        (row,) = self.use_rows(self.claim_id)
        self.assertEqual(
            (
                row["ref_kind"],
                row["ref_id"],
                row["project_id"],
                row["created_by"],
                row["created_at"],
            ),
            ("answer", answer.id, self.project_id, creator, T0),
        )

    async def test_an_answer_and_a_task_with_the_same_id_are_different_references(self):
        identifier = uuid4()

        self.assertTrue(await self.use(Reference.answer(identifier)))
        self.assertTrue(await self.use(Reference.task(identifier)))

        self.assertEqual(
            [row["ref_kind"] for row in self.use_rows(self.claim_id)],
            ["answer", "task"],
        )

    async def test_a_reference_may_use_many_claims(self):
        answer = Reference.answer(uuid4())
        other = self.seed_claim("Another claim")

        self.assertTrue(await self.use(answer))
        self.assertTrue(await self.use(answer, claim=other))

        self.assertEqual(len(self.use_rows(other)), 1)

    async def test_the_function_does_not_commit(self):
        with self.assertRaises(RuntimeError):
            async with self.transaction() as session:
                await queries.add_claim_use(
                    session,
                    self.project_id,
                    self.claim_id,
                    Reference.task(uuid4()),
                    self.user_id,
                    T0,
                )
                raise RuntimeError("roll back")

        self.assertEqual(self.use_rows(self.claim_id), [])


class InsertRelationTest(QueryTestCase):
    def setup_pair(self, entity: EntityKind):
        if entity is EntityKind.CLAIM:
            ids = (self.seed_claim("First"), self.seed_claim("Second"))
        else:
            ids = (
                self.seed_source(locator="https://example.com/1"),
                self.seed_source(locator="https://example.com/2"),
            )
        return tuple(sorted(ids, key=lambda value: value.int))

    async def insert(self, entity, kind, low, high, *, by=None, at=T0):
        async with self.transaction() as session:
            return await queries.insert_relation(
                session,
                self.project_id,
                entity,
                kind,
                low,
                high,
                by or self.user_id,
                at,
            )

    def table_of(self, entity):
        return (
            "research_claim_relations"
            if entity is EntityKind.CLAIM
            else "research_source_relations"
        )

    async def test_a_new_relation_is_inserted_and_returned(self):
        for entity in EntityKind:
            with self.subTest(entity):
                self.clean_tables()
                low, high = self.setup_pair(entity)
                creator = uuid4()

                relation, created = await self.insert(
                    entity,
                    RelationKind.CONTRADICTION,
                    low,
                    high,
                    by=creator,
                    at=T0 + HOUR,
                )

                self.assertTrue(created)
                self.assertEqual(
                    relation,
                    Relation(
                        entity=entity,
                        kind=RelationKind.CONTRADICTION,
                        project_id=self.project_id,
                        low_id=low,
                        high_id=high,
                        created_by=creator,
                        created_at=T0 + HOUR,
                    ),
                )
                (row,) = self.rows(f"SELECT * FROM {self.table_of(entity)}")
                self.assertEqual(
                    (row["low_id"], row["high_id"], row["kind"], row["created_by"]),
                    (low, high, "contradiction", creator),
                )

    async def test_the_relation_goes_to_the_table_of_its_entity_only(self):
        low, high = self.setup_pair(EntityKind.CLAIM)

        await self.insert(EntityKind.CLAIM, RelationKind.DUPLICATE, low, high)

        self.assertEqual(self.table_count("research_claim_relations"), 1)
        self.assertEqual(self.table_count("research_source_relations"), 0)

    async def test_the_same_relation_again_returns_the_first_one(self):
        for entity in EntityKind:
            with self.subTest(entity):
                self.clean_tables()
                low, high = self.setup_pair(entity)
                first_creator = uuid4()
                first, _ = await self.insert(
                    entity, RelationKind.DUPLICATE, low, high, by=first_creator, at=T0
                )

                again, created = await self.insert(
                    entity, RelationKind.DUPLICATE, low, high, by=uuid4(), at=T0 + HOUR
                )

                self.assertFalse(created)
                self.assertEqual(again, first)
                self.assertEqual(again.created_by, first_creator)
                self.assertEqual(again.created_at, T0)
                self.assertEqual(self.table_count(self.table_of(entity)), 1)

    async def test_the_other_kind_for_the_same_pair_is_a_conflict_and_changes_nothing(
        self,
    ):
        for entity in EntityKind:
            for first, second in (
                (RelationKind.DUPLICATE, RelationKind.CONTRADICTION),
                (RelationKind.CONTRADICTION, RelationKind.DUPLICATE),
            ):
                with self.subTest(entity=entity, first=first):
                    self.clean_tables()
                    low, high = self.setup_pair(entity)
                    await self.insert(entity, first, low, high)

                    with self.assertRaises(ProvenanceConflictError):
                        await self.insert(entity, second, low, high, at=T0 + HOUR)

                    (row,) = self.rows(f"SELECT * FROM {self.table_of(entity)}")
                    self.assertEqual(
                        (row["kind"], row["created_at"]), (first.value, T0)
                    )

    async def test_an_entity_can_have_several_relations(self):
        first, second = self.seed_claim("A"), self.seed_claim("B")
        third = self.seed_claim("C")
        low1, high1 = sorted((first, second), key=lambda value: value.int)
        low2, high2 = sorted((first, third), key=lambda value: value.int)

        await self.insert(EntityKind.CLAIM, RelationKind.DUPLICATE, low1, high1)
        await self.insert(EntityKind.CLAIM, RelationKind.CONTRADICTION, low2, high2)

        self.assertEqual(self.table_count("research_claim_relations"), 2)


class FetchClaimTest(QueryTestCase):
    async def fetch(self, claim_id, project=None):
        async with self.transaction() as session:
            return await queries.fetch_claim(
                session, project or self.project_id, claim_id
            )

    async def test_an_existing_claim_is_returned_with_every_field(self):
        task_id = self.seed_task()
        claim_id = self.seed_claim(
            "  Spaced text  ", task_id=task_id, created_at=T0 + HOUR
        )

        claim = await self.fetch(claim_id)

        self.assertEqual(
            claim,
            Claim(
                id=claim_id,
                project_id=self.project_id,
                text="  Spaced text  ",
                task_id=task_id,
                created_by=self.user_id,
                created_at=T0 + HOUR,
            ),
        )

    async def test_a_missing_claim_and_a_claim_of_another_project_are_none(self):
        claim_id = self.seed_claim(project_id=self.other_project_id)

        self.assertIsNone(await self.fetch(uuid4()))
        self.assertIsNone(await self.fetch(claim_id))
        self.assertIsNotNone(await self.fetch(claim_id, project=self.other_project_id))


class FetchReferenceClaimsTest(QueryTestCase):
    async def fetch(self, reference, limit=100, project=None):
        async with self.transaction() as session:
            return await queries.fetch_reference_claims(
                session, project or self.project_id, reference, limit
            )

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.answer = uuid4()
        # created_at order: c1 < c2 < c3 (c4 and c5 share an instant).
        self.c1 = self.seed_claim("one", created_at=T0)
        self.c2 = self.seed_claim("two", created_at=T0 + HOUR)
        self.c3 = self.seed_claim("three", created_at=T0 + 2 * HOUR)
        self.c4 = self.seed_claim("four", created_at=T0 + 3 * HOUR)
        self.c5 = self.seed_claim("five", created_at=T0 + 3 * HOUR)
        self.later_pair = sorted((self.c4, self.c5), key=lambda value: value.int)

    def use_all(self, *claims, kind="answer", ref=None, project=None):
        for claim in claims:
            self.seed_use(claim, kind, ref or self.answer, project_id=project)

    async def test_the_claims_of_a_reference_come_back_ordered_by_creation_then_id(
        self,
    ):
        # Seeded in scrambled order: the order of the result must not depend on it.
        self.use_all(self.c5, self.c3, self.c1, self.c4)

        claims, truncated = await self.fetch(Reference.answer(self.answer))

        self.assertEqual(
            [claim.id for claim in claims],
            [self.c1, self.c3, *self.later_pair[:2]],
        )
        self.assertFalse(truncated)

    async def test_the_claims_are_complete_records(self):
        self.use_all(self.c2)

        (claim,), _ = await self.fetch(Reference.answer(self.answer))

        self.assertEqual(claim, self.claim_from_sql(self.c2))
        self.assertEqual(claim.text, "two")

    async def test_the_limit_truncates_and_reports_it(self):
        self.use_all(self.c1, self.c2, self.c3)
        reference = Reference.answer(self.answer)

        two, truncated_two = await self.fetch(reference, limit=2)
        three, truncated_three = await self.fetch(reference, limit=3)
        many, truncated_many = await self.fetch(reference, limit=200)
        one, truncated_one = await self.fetch(reference, limit=1)

        self.assertEqual(
            ([c.id for c in two], truncated_two), ([self.c1, self.c2], True)
        )
        self.assertEqual(
            ([c.id for c in three], truncated_three),
            ([self.c1, self.c2, self.c3], False),
        )
        self.assertEqual(
            ([c.id for c in many], truncated_many), ([self.c1, self.c2, self.c3], False)
        )
        self.assertEqual(([c.id for c in one], truncated_one), ([self.c1], True))

    async def test_a_reference_that_nobody_used_gives_nothing(self):
        self.use_all(self.c1)

        self.assertEqual(await self.fetch(Reference.answer(uuid4())), ([], False))
        self.assertEqual(await self.fetch(Reference.task(uuid4())), ([], False))

    async def test_an_answer_and_a_task_are_told_apart(self):
        self.use_all(self.c1, kind="answer")
        self.use_all(self.c2, kind="task")

        answer, _ = await self.fetch(Reference.answer(self.answer))
        task, _ = await self.fetch(Reference.task(self.answer))

        self.assertEqual([c.id for c in answer], [self.c1])
        self.assertEqual([c.id for c in task], [self.c2])

    async def test_other_references_and_other_projects_are_invisible(self):
        self.use_all(self.c1)
        self.seed_use(self.c2, "answer", uuid4())
        foreign_claim = self.seed_claim("foreign", project_id=self.other_project_id)
        self.seed_use(
            foreign_claim, "answer", self.answer, project_id=self.other_project_id
        )

        mine, _ = await self.fetch(Reference.answer(self.answer))
        theirs, _ = await self.fetch(
            Reference.answer(self.answer), project=self.other_project_id
        )

        self.assertEqual([c.id for c in mine], [self.c1])
        self.assertEqual([c.id for c in theirs], [foreign_claim])


class FetchClaimLinksTest(QueryTestCase):
    async def fetch(self, claim_ids, project=None, session=None):
        if session is not None:
            return await queries.fetch_claim_links(
                session, project or self.project_id, claim_ids
            )
        async with self.transaction() as opened:
            return await queries.fetch_claim_links(
                opened, project or self.project_id, claim_ids
            )

    def keyed(self, links):
        return {(link.claim_id, link.source.id): link for link in links}

    async def test_every_link_comes_with_its_complete_source(self):
        claim = self.seed_claim()
        source = self.seed_source(
            locator="https://example.com/a",
            source_type="official_github",
            title="Release notes",
            fetched_at=T0 + HOUR,
            published_at=T0 - HOUR,
            created_at=T0 + 2 * HOUR,
        )
        self.seed_link(claim, source, "contradicts", created_at=T0 + 5 * HOUR)

        (link,) = await self.fetch([claim])

        self.assertEqual(
            link,
            SourceLink(
                claim_id=claim,
                source=self.source_from_sql(source),
                stance=Stance.CONTRADICTS,
                linked_at=T0 + 5 * HOUR,
            ),
        )
        self.assertEqual(link.source.source_type, SourceType.OFFICIAL_GITHUB)
        self.assertEqual(link.source.created_at, T0 + 2 * HOUR)

    async def test_the_links_of_several_claims_are_returned_together(self):
        c1, c2, c3 = self.seed_claim("1"), self.seed_claim("2"), self.seed_claim("3")
        s1 = self.seed_source(locator="https://example.com/1")
        s2 = self.seed_source(locator="https://example.com/2")
        self.seed_link(c1, s1)
        self.seed_link(c1, s2, "contradicts")
        self.seed_link(c2, s1)
        self.seed_link(c3, s2)

        links = await self.fetch([c1, c2])

        self.assertEqual(
            {(k[0], k[1]): v.stance for k, v in self.keyed(links).items()},
            {
                (c1, s1): Stance.SUPPORTS,
                (c1, s2): Stance.CONTRADICTS,
                (c2, s1): Stance.SUPPORTS,
            },
        )
        self.assertEqual(len(links), 3)

    async def test_no_claim_ids_return_nothing_without_touching_the_database(self):
        self.assertEqual(await self.fetch([], session=ForbiddenSession()), [])

    async def test_a_claim_without_links_and_an_unknown_claim_give_nothing(self):
        claim = self.seed_claim()

        self.assertEqual(await self.fetch([claim]), [])
        self.assertEqual(await self.fetch([uuid4()]), [])

    async def test_the_links_of_another_project_are_invisible(self):
        claim = self.seed_claim(project_id=self.other_project_id)
        source = self.seed_source(project_id=self.other_project_id)
        self.seed_link(claim, source, project_id=self.other_project_id)

        self.assertEqual(await self.fetch([claim]), [])
        self.assertEqual(
            len(await self.fetch([claim], project=self.other_project_id)), 1
        )


class FetchRelationsTest(QueryTestCase):
    async def fetch(self, entity, ids, project=None, session=None):
        if session is not None:
            return await queries.fetch_relations(
                session, project or self.project_id, entity, ids
            )
        async with self.transaction() as opened:
            return await queries.fetch_relations(
                opened, project or self.project_id, entity, ids
            )

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.a, self.b, self.c, self.d = (
            self.seed_claim(name) for name in ("a", "b", "c", "d")
        )

    def pairs(self, relations):
        return {(r.low_id, r.high_id): (r.kind, r.entity) for r in relations}

    def key(self, first, second):
        return tuple(sorted((first, second), key=lambda value: value.int))

    async def test_relations_are_found_from_either_endpoint(self):
        self.seed_relation("claim", self.a, self.b, "duplicate")
        self.seed_relation("claim", self.c, self.b, "contradiction")
        self.seed_relation("claim", self.c, self.d, "duplicate")

        relations = await self.fetch(EntityKind.CLAIM, [self.b])

        self.assertEqual(
            self.pairs(relations),
            {
                self.key(self.a, self.b): (RelationKind.DUPLICATE, EntityKind.CLAIM),
                self.key(self.b, self.c): (
                    RelationKind.CONTRADICTION,
                    EntityKind.CLAIM,
                ),
            },
        )
        self.assertEqual(len(relations), 2)

    async def test_a_relation_between_two_of_the_ids_is_listed_once(self):
        self.seed_relation("claim", self.a, self.b, "duplicate")
        self.seed_relation("claim", self.b, self.c, "duplicate")

        relations = await self.fetch(EntityKind.CLAIM, [self.a, self.b])

        self.assertEqual(len(relations), 2)
        self.assertEqual(
            set(self.pairs(relations)),
            {self.key(self.a, self.b), self.key(self.b, self.c)},
        )

    async def test_the_relations_carry_all_their_fields(self):
        creator = uuid4()
        self.seed_relation(
            "claim",
            self.a,
            self.b,
            "contradiction",
            created_by=creator,
            created_at=T0 + HOUR,
        )

        (relation,) = await self.fetch(EntityKind.CLAIM, [self.a])

        low, high = self.key(self.a, self.b)
        self.assertEqual(
            relation,
            Relation(
                entity=EntityKind.CLAIM,
                kind=RelationKind.CONTRADICTION,
                project_id=self.project_id,
                low_id=low,
                high_id=high,
                created_by=creator,
                created_at=T0 + HOUR,
            ),
        )

    async def test_the_entity_selects_the_table(self):
        s1 = self.seed_source(locator="https://example.com/1")
        s2 = self.seed_source(locator="https://example.com/2")
        self.seed_relation("claim", self.a, self.b, "duplicate")
        self.seed_relation("source", s1, s2, "contradiction")

        claims = await self.fetch(EntityKind.CLAIM, [self.a, s1])
        sources = await self.fetch(EntityKind.SOURCE, [self.a, s1])

        self.assertEqual({r.entity for r in claims}, {EntityKind.CLAIM})
        self.assertEqual(len(claims), 1)
        self.assertEqual({r.entity for r in sources}, {EntityKind.SOURCE})
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].kind, RelationKind.CONTRADICTION)

    async def test_no_ids_return_nothing_without_touching_the_database(self):
        self.assertEqual(
            await self.fetch(EntityKind.CLAIM, [], session=ForbiddenSession()), []
        )
        self.assertEqual(
            await self.fetch(EntityKind.SOURCE, [], session=ForbiddenSession()), []
        )

    async def test_an_entity_without_relations_gives_nothing(self):
        self.seed_relation("claim", self.a, self.b, "duplicate")

        self.assertEqual(await self.fetch(EntityKind.CLAIM, [self.d]), [])
        self.assertEqual(await self.fetch(EntityKind.CLAIM, [uuid4()]), [])

    async def test_the_relations_of_another_project_are_invisible(self):
        first = self.seed_claim("x", project_id=self.other_project_id)
        second = self.seed_claim("y", project_id=self.other_project_id)
        self.seed_relation(
            "claim", first, second, "duplicate", project_id=self.other_project_id
        )

        self.assertEqual(await self.fetch(EntityKind.CLAIM, [first]), [])
        self.assertEqual(
            len(
                await self.fetch(
                    EntityKind.CLAIM, [first], project=self.other_project_id
                )
            ),
            1,
        )
