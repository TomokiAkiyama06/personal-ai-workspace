"""Concurrency and failure handling of the provenance store.

Real PostgreSQL. The tests that need a lock to be held do it from a separate
connection and wait on state (a task that is still running), never on the speed
of the machine. Every wait has a generous deadline.
"""

import asyncio
import unittest
from uuid import uuid4

import psycopg.errors
from sqlalchemy.exc import DBAPIError, OperationalError

from paw_backend.db import Database
from paw_backend.research.provenance import (
    EntityKind,
    ProvenanceBusyError,
    ProvenanceConflictError,
    ProvenanceLimitError,
    ProvenanceStore,
    RecordedClaim,
    Reference,
    Relation,
    RelationKind,
    Stance,
)

from .provenance_support import (
    GUARD_SECONDS,
    FakeClock,
    PostgresProvenanceTestCase,
    link,
    requires_postgres,
    together,
)
from .support import make_settings

SKY = "The sky is blue."
SECRET = "SECRET-TOKEN-4f9a1c"


async def guarded(awaitable):
    async with asyncio.timeout(GUARD_SECONDS):
        return await awaitable


class BrokenSession:
    """A session whose first statement fails with ``error``."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exception):
        return False

    def begin(self):
        return self

    async def execute(self, *args, **kwargs):
        raise self._error


class BrokenDatabase(Database):
    def __init__(self, error: Exception) -> None:
        super().__init__(make_settings())
        self._error = error

    def session(self):
        return BrokenSession(self._error)


def driver_error(orig: Exception) -> DBAPIError:
    """What SQLAlchemy raises for a failed statement (its text quotes SQL data)."""
    return DBAPIError(f"SELECT '{SECRET}'", {"secret": SECRET}, orig)


class ErrorMappingTest(unittest.IsolatedAsyncioTestCase):
    """Lock timeouts and deadlocks become ``ProvenanceBusyError``; nothing else does."""

    async def add(self, error: Exception):
        store = ProvenanceStore(BrokenDatabase(error), clock=FakeClock())
        return await store.add_reference(
            uuid4(),
            reference=Reference.answer(uuid4()),
            claim_ids=[uuid4()],
            created_by=uuid4(),
        )

    async def test_a_lock_timeout_and_a_deadlock_are_busy(self):
        for orig in (
            psycopg.errors.LockNotAvailable("canceling statement due to lock timeout"),
            psycopg.errors.DeadlockDetected("deadlock detected"),
        ):
            with self.subTest(type(orig).__name__):
                with self.assertRaises(ProvenanceBusyError) as raised:
                    await self.add(driver_error(orig))

                self.assertEqual(raised.exception.code, "provenance_busy")
                self.assertIsNone(raised.exception.__cause__)
                self.assertTrue(raised.exception.__suppress_context__)
                text = str(raised.exception) + repr(raised.exception)
                self.assertNotIn(SECRET, text)

    async def test_every_other_database_error_passes_through_unchanged(self):
        for orig in (
            psycopg.errors.UndefinedTable("no table"),
            psycopg.errors.OperationalError("server closed the connection"),
            psycopg.errors.QueryCanceled("statement timeout"),
            psycopg.errors.UniqueViolation("duplicate"),
        ):
            with self.subTest(type(orig).__name__):
                error = driver_error(orig)

                with self.assertRaises(DBAPIError) as raised:
                    await self.add(error)

                self.assertIs(raised.exception, error)

    async def test_an_error_that_is_not_a_database_error_passes_through(self):
        with self.assertRaises(ConnectionResetError):
            await self.add(ConnectionResetError("gone"))

    async def test_an_operational_error_that_is_not_a_lock_problem_is_not_hidden(self):
        error = OperationalError("SELECT 1", {}, psycopg.errors.AdminShutdown("bye"))

        with self.assertRaises(OperationalError) as raised:
            await self.add(error)

        self.assertIs(raised.exception, error)


@requires_postgres
class ConcurrentRecordTest(PostgresProvenanceTestCase):
    async def record(self, store, text=SKY, sources=None, **options):
        return await store.record_claim(
            self.project_id,
            created_by=self.user_id,
            text=text,
            sources=sources or [link()],
            **options,
        )

    async def test_the_same_claim_recorded_at_once_is_one_claim_with_all_sources(self):
        for round_number in range(3):
            with self.subTest(round_number):
                self.clean_tables()
                stores = [self.new_store() for _ in range(8)]

                results = await together(
                    *(
                        self.record(
                            store,
                            sources=[link(f"https://example.com/{number}")],
                        )
                        for number, store in enumerate(stores)
                    )
                )

                self.assertEqual(sum(result.created for result in results), 1)
                self.assertEqual(
                    {result.claim.id for result in results}, {results[0].claim.id}
                )
                self.assertEqual(self.table_count("research_claims"), 1)
                self.assertEqual(self.table_count("research_sources"), 8)
                self.assertEqual(len(self.link_rows(results[0].claim.id)), 8)
                self.assertEqual(sum(result.new_links for result in results), 8)

    async def test_the_same_source_recorded_at_once_is_one_source(self):
        stores = [self.new_store() for _ in range(6)]

        results = await together(
            *(
                self.record(
                    store,
                    text=f"Claim {number}",
                    sources=[link("https://shared.example.com/")],
                )
                for number, store in enumerate(stores)
            )
        )

        self.assertTrue(all(result.created for result in results))
        self.assertEqual(self.table_count("research_sources"), 1)
        self.assertEqual(self.table_count("research_claims"), 6)
        self.assertEqual(self.table_count("research_claim_sources"), 6)
        self.assertEqual(
            {result.links[0].source.id for result in results},
            {results[0].links[0].source.id},
        )

    async def test_calls_with_overlapping_sources_in_opposite_orders_do_not_deadlock(
        self,
    ):
        locators = [f"https://example.com/{number}" for number in range(6)]
        for round_number in range(6):
            with self.subTest(round_number):
                self.clean_tables()
                forward = [link(locator) for locator in locators]
                backward = list(reversed(forward))

                first, second = await together(
                    self.record(self.new_store(), text="Claim A", sources=forward),
                    self.record(self.new_store(), text="Claim B", sources=backward),
                )

                self.assertEqual(self.table_count("research_sources"), 6)
                self.assertEqual(len(self.link_rows(first.claim.id)), 6)
                self.assertEqual(len(self.link_rows(second.claim.id)), 6)
                self.assertEqual(
                    [item.source.locator for item in second.links],
                    list(reversed(locators)),
                )


@requires_postgres
class LockingTest(PostgresProvenanceTestCase):
    async def record(self, store, sources, text=SKY):
        return await store.record_claim(
            self.project_id, created_by=self.user_id, text=text, sources=sources
        )

    async def test_a_call_that_waits_too_long_for_the_claim_is_busy_and_writes_nothing(
        self,
    ):
        claim = self.seed_claim(SKY)
        self.lock_claim(claim)
        before = self.counts()
        store = self.new_store(lock_timeout_ms=200)

        with self.assertRaises(ProvenanceBusyError) as raised:
            await guarded(self.record(store, [link("https://new.example.com/")]))

        self.assertEqual(raised.exception.code, "provenance_busy")
        self.assertIsNone(raised.exception.__cause__)
        self.assertEqual(self.counts(), before)

    async def test_the_other_writers_wait_for_a_locked_claim_too(self):
        first, second = self.seed_claim("first"), self.seed_claim("second")
        self.lock_claim(first)
        store = self.new_store(lock_timeout_ms=200)

        with self.assertRaises(ProvenanceBusyError):
            await guarded(
                store.add_reference(
                    self.project_id,
                    reference=Reference.answer(uuid4()),
                    claim_ids=[first],
                    created_by=self.user_id,
                )
            )
        with self.assertRaises(ProvenanceBusyError):
            await guarded(
                store.mark_related(
                    self.project_id,
                    entity=EntityKind.CLAIM,
                    kind=RelationKind.DUPLICATE,
                    first_id=first,
                    second_id=second,
                    created_by=self.user_id,
                )
            )

        self.assertEqual(self.table_count("research_claim_uses"), 0)
        self.assertEqual(self.table_count("research_claim_relations"), 0)

    async def test_a_call_waits_for_the_lock_and_then_completes(self):
        claim = self.seed_claim(SKY)
        _, transaction = self.lock_claim(claim)
        task = self.spawn(
            self.record(self.new_store(), [link("https://new.example.com/")])
        )
        await asyncio.sleep(0.5)
        self.assertWaiting(task)

        transaction.rollback()
        recorded = await guarded(task)

        self.assertFalse(recorded.created)
        self.assertEqual(recorded.new_links, 1)
        self.assertEqual(len(self.link_rows(claim)), 1)

    async def test_the_source_limit_holds_under_concurrent_calls(self):
        claim = self.seed_claim(SKY)
        for number in range(49):
            self.seed_link(
                claim, self.seed_source(locator=f"https://seed.example.com/{number}")
            )
        _, transaction = self.lock_claim(claim)
        tasks = [
            self.spawn(
                self.record(self.new_store(), [link(f"https://new.example.com/{n}")])
            )
            for n in range(2)
        ]
        await asyncio.sleep(0.5)
        for task in tasks:
            self.assertWaiting(task)

        transaction.rollback()
        results = await together(*tasks, return_exceptions=True)

        recorded = [r for r in results if isinstance(r, RecordedClaim)]
        refused = [r for r in results if isinstance(r, ProvenanceLimitError)]
        self.assertEqual((len(recorded), len(refused)), (1, 1))
        self.assertEqual(len(self.link_rows(claim)), 50)
        self.assertEqual(self.table_count("research_sources"), 50)

    async def test_reads_do_not_wait_for_a_locked_claim(self):
        answer = uuid4()
        claim = self.seed_claim(SKY)
        self.seed_use(claim, "answer", answer)
        self.lock_claim(claim)

        traced = await guarded(self.store.get_claim(self.project_id, claim))
        trace = await guarded(
            self.store.trace(self.project_id, Reference.answer(answer))
        )
        relations = await guarded(
            self.store.list_relations(self.project_id, EntityKind.CLAIM, claim)
        )

        self.assertEqual(traced.claim.id, claim)
        self.assertEqual([t.claim.id for t in trace.claims], [claim])
        self.assertEqual(relations, ())


@requires_postgres
class ConcurrentRelationTest(PostgresProvenanceTestCase):
    async def mark(self, store, kind, first, second):
        return await store.mark_related(
            self.project_id,
            entity=EntityKind.CLAIM,
            kind=kind,
            first_id=first,
            second_id=second,
            created_by=self.user_id,
        )

    async def test_the_same_pair_marked_at_once_in_both_orders_is_one_relation(self):
        first, second = self.seed_claim("a"), self.seed_claim("b")
        stores = [self.new_store() for _ in range(6)]

        results = await together(
            *(
                self.mark(
                    store,
                    RelationKind.DUPLICATE,
                    *((first, second) if number % 2 else (second, first)),
                )
                for number, store in enumerate(stores)
            )
        )

        self.assertEqual(len({(r.low_id, r.high_id, r.created_at) for r in results}), 1)
        self.assertTrue(all(isinstance(r, Relation) for r in results))
        self.assertEqual(self.table_count("research_claim_relations"), 1)

    async def test_two_kinds_marked_at_once_leave_exactly_one_relation(self):
        first, second = self.seed_claim("a"), self.seed_claim("b")

        results = await together(
            self.mark(self.new_store(), RelationKind.DUPLICATE, first, second),
            self.mark(self.new_store(), RelationKind.CONTRADICTION, second, first),
            return_exceptions=True,
        )

        self.assertEqual(
            sorted(type(result).__name__ for result in results),
            ["ProvenanceConflictError", "Relation"],
        )
        (row,) = self.rows("SELECT kind FROM research_claim_relations")
        winner = next(r for r in results if isinstance(r, Relation))
        self.assertEqual(row["kind"], winner.kind.value)
        self.assertTrue(any(isinstance(r, ProvenanceConflictError) for r in results))

    async def test_a_stance_conflict_between_concurrent_calls_leaves_one_stance(self):
        store_a, store_b = self.new_store(), self.new_store()

        async def record(store, stance):
            return await store.record_claim(
                self.project_id,
                created_by=self.user_id,
                text=SKY,
                sources=[link("https://a.example.com/", stance)],
            )

        results = await together(
            record(store_a, Stance.SUPPORTS),
            record(store_b, Stance.CONTRADICTS),
            return_exceptions=True,
        )

        self.assertEqual(
            sorted(type(result).__name__ for result in results),
            ["ProvenanceConflictError", "RecordedClaim"],
        )
        self.assertEqual(self.table_count("research_claim_sources"), 1)
