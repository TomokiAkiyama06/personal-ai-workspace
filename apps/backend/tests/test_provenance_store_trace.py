"""``add_reference``, ``get_claim`` and ``trace`` on a real PostgreSQL.

The trace answers the question of the issue: from an answer or a task, which
claims were used and which sources (with their type, ``fetched_at`` and
``published_at``) stand behind them.
"""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import psycopg.errors
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.research.provenance import (
    ClaimNotFoundError,
    EntityKind,
    InputProblem,
    InvalidProvenanceInputError,
    Reference,
    Relation,
    RelationKind,
    SourceLink,
    Stance,
    Trace,
    TracedClaim,
    queries,
)
from paw_backend.research.providers.contract import SourceType

from .provenance_support import (
    T0,
    PostgresProvenanceTestCase,
    link,
    requires_postgres,
)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


@requires_postgres
class TraceTestCase(PostgresProvenanceTestCase):
    async def add(self, reference, claim_ids, **options):
        arguments = {"created_by": self.user_id}
        arguments.update(options)
        project = arguments.pop("project_id", self.project_id)
        return await self.store.add_reference(
            project, reference=reference, claim_ids=claim_ids, **arguments
        )

    def traced_link(self, claim, source, stance, linked_at=T0) -> SourceLink:
        return SourceLink(
            claim_id=claim,
            source=self.source_from_sql(source),
            stance=stance,
            linked_at=linked_at,
        )


class AddReferenceTest(TraceTestCase):
    async def test_an_answer_is_recorded_as_a_user_of_its_claims(self):
        answer = uuid4()
        first, second = self.seed_claim("one"), self.seed_claim("two")
        self.clock.advance(hours=2)

        added = await self.add(Reference.answer(answer), [first, second])

        self.assertEqual(added, 2)
        for claim in (first, second):
            (use,) = self.use_rows(claim)
            self.assertEqual(
                (
                    use["ref_kind"],
                    use["ref_id"],
                    use["project_id"],
                    use["created_by"],
                    use["created_at"],
                ),
                ("answer", answer, self.project_id, self.user_id, T0 + 2 * HOUR),
            )

    async def test_a_task_is_recorded_as_a_user_of_a_claim(self):
        task = self.seed_task()
        claim = self.seed_claim()

        added = await self.add(Reference.task(task), [claim])

        self.assertEqual(added, 1)
        self.assertEqual([use["ref_kind"] for use in self.use_rows(claim)], ["task"])

    async def test_a_use_that_exists_is_kept_and_not_counted(self):
        answer = Reference.answer(uuid4())
        first, second = self.seed_claim("one"), self.seed_claim("two")
        first_time = await self.add(answer, [first], created_by=self.user_id)
        self.clock.advance(hours=1)

        again = await self.add(answer, [first, second], created_by=uuid4())
        third = await self.add(answer, [first, second])

        self.assertEqual((first_time, again, third), (1, 1, 0))
        (use,) = self.use_rows(first)
        self.assertEqual((use["created_by"], use["created_at"]), (self.user_id, T0))
        self.assertEqual(len(self.use_rows(second)), 1)

    async def test_repeated_ids_in_one_call_count_once(self):
        claim = self.seed_claim()

        added = await self.add(Reference.answer(uuid4()), [claim, claim, claim])

        self.assertEqual(added, 1)
        self.assertEqual(len(self.use_rows(claim)), 1)

    async def test_an_answer_and_a_task_with_the_same_id_are_different_users(self):
        identifier = self.seed_task()
        claim = self.seed_claim()

        await self.add(Reference.answer(identifier), [claim])
        await self.add(Reference.task(identifier), [claim])

        self.assertEqual(
            [use["ref_kind"] for use in self.use_rows(claim)], ["answer", "task"]
        )

    async def test_a_missing_claim_is_not_found_and_nothing_is_recorded(self):
        real = self.seed_claim()

        with self.assertRaises(ClaimNotFoundError):
            await self.add(Reference.answer(uuid4()), [real, uuid4()])

        self.assertEqual(self.table_count("research_claim_uses"), 0)

    async def test_a_claim_of_another_project_is_not_found(self):
        mine = self.seed_claim("mine")
        theirs = self.seed_claim("theirs", project_id=self.other_project_id)

        with self.assertRaises(ClaimNotFoundError):
            await self.add(Reference.answer(uuid4()), [mine, theirs])

        self.assertEqual(self.table_count("research_claim_uses"), 0)

    async def test_a_task_of_this_project_is_needed_for_a_task_reference(self):
        claim = self.seed_claim()
        foreign = self.seed_task(project_id=self.other_project_id)
        for task in (uuid4(), foreign):
            with self.subTest(task):
                with self.assertRaises(InvalidProvenanceInputError) as raised:
                    await self.add(Reference.task(task), [claim])
                self.assertEqual(
                    (raised.exception.field, raised.exception.problem),
                    ("reference", InputProblem.UNKNOWN_REFERENCE),
                )
        self.assertEqual(self.table_count("research_claim_uses"), 0)

    async def test_an_answer_id_is_not_checked(self):
        claim = self.seed_claim()

        added = await self.add(Reference.answer(uuid4()), [claim])

        self.assertEqual(added, 1)

    async def test_50_claims_can_be_added_in_one_call(self):
        claims = [self.seed_claim(f"claim {n}") for n in range(50)]

        added = await self.add(Reference.answer(uuid4()), claims)

        self.assertEqual(added, 50)
        self.assertEqual(self.table_count("research_claim_uses"), 50)


class GetClaimTest(TraceTestCase):
    async def test_a_claim_comes_with_its_sources_and_relations(self):
        claim = self.seed_claim("Rust 1.75 is stable.", created_at=T0)
        old = self.seed_source(
            locator="https://a.example.com/",
            source_type="official_docs",
            fetched_at=T0 - DAY,
            published_at=T0 - 5 * DAY,
        )
        new = self.seed_source(
            locator="https://b.example.com/",
            source_type="official_github",
            fetched_at=T0,
            published_at=None,
        )
        against = self.seed_source(
            locator="https://c.example.com/",
            source_type="community",
            fetched_at=T0 + HOUR,
        )
        self.seed_link(claim, old, "supports", created_at=T0 + HOUR)
        self.seed_link(claim, new, "supports", created_at=T0 + 2 * HOUR)
        self.seed_link(claim, against, "contradicts", created_at=T0 + 3 * HOUR)
        twin, enemy = self.seed_claim("twin"), self.seed_claim("enemy")
        self.seed_relation("claim", claim, twin, "duplicate")
        self.seed_relation("claim", enemy, claim, "contradiction", created_at=T0 + HOUR)

        traced = await self.store.get_claim(self.project_id, claim)

        self.assertEqual(
            traced,
            TracedClaim(
                claim=self.claim_from_sql(claim),
                links=(
                    self.traced_link(claim, new, Stance.SUPPORTS, T0 + 2 * HOUR),
                    self.traced_link(claim, old, Stance.SUPPORTS, T0 + HOUR),
                    self.traced_link(claim, against, Stance.CONTRADICTS, T0 + 3 * HOUR),
                ),
                relations=(
                    Relation(
                        entity=EntityKind.CLAIM,
                        kind=RelationKind.CONTRADICTION,
                        project_id=self.project_id,
                        low_id=min(enemy, claim, key=lambda v: v.int),
                        high_id=max(enemy, claim, key=lambda v: v.int),
                        created_by=self.user_id,
                        created_at=T0 + HOUR,
                    ),
                    Relation(
                        entity=EntityKind.CLAIM,
                        kind=RelationKind.DUPLICATE,
                        project_id=self.project_id,
                        low_id=min(twin, claim, key=lambda v: v.int),
                        high_id=max(twin, claim, key=lambda v: v.int),
                        created_by=self.user_id,
                        created_at=T0,
                    ),
                ),
            ),
        )
        # The dates and the type of every source are the recorded ones.
        by_locator = {item.source.locator: item.source for item in traced.links}
        self.assertEqual(
            by_locator["https://a.example.com/"].published_at, T0 - 5 * DAY
        )
        self.assertIsNone(by_locator["https://b.example.com/"].published_at)
        self.assertEqual(
            by_locator["https://b.example.com/"].source_type, SourceType.OFFICIAL_GITHUB
        )
        self.assertEqual(by_locator["https://a.example.com/"].fetched_at, T0 - DAY)

    async def test_a_claim_without_sources_or_relations(self):
        claim = self.seed_claim()

        traced = await self.store.get_claim(self.project_id, claim)

        self.assertEqual(traced.links, ())
        self.assertEqual(traced.relations, ())
        self.assertEqual(traced.claim, self.claim_from_sql(claim))

    async def test_an_unknown_claim_and_a_claim_of_another_project_are_not_found(self):
        theirs = self.seed_claim("theirs", project_id=self.other_project_id)

        for claim in (uuid4(), theirs):
            with self.subTest(claim):
                with self.assertRaises(ClaimNotFoundError):
                    await self.store.get_claim(self.project_id, claim)
        self.assertEqual(
            (await self.store.get_claim(self.other_project_id, theirs)).claim.id, theirs
        )

    async def test_a_recorded_claim_can_be_read_back(self):
        recorded = await self.store.record_claim(
            self.project_id,
            created_by=self.user_id,
            text="Round trip",
            sources=[link("https://a.example.com/", Stance.CONTRADICTS)],
        )

        traced = await self.store.get_claim(self.project_id, recorded.claim.id)

        self.assertEqual(traced.claim, recorded.claim)
        self.assertEqual(traced.links, recorded.links)


class TraceScenarioTest(TraceTestCase):
    """One answer that used two claims, one source backing both."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.answer = uuid4()
        self.c1 = self.seed_claim("First claim", created_at=T0)
        self.c2 = self.seed_claim("Second claim", created_at=T0 + HOUR)
        self.s_docs = self.seed_source(
            locator="https://docs.example.com/",
            source_type="official_docs",
            fetched_at=T0,
            published_at=T0 - 3 * DAY,
            title="Docs",
        )
        self.s_forum = self.seed_source(
            locator="https://forum.example.com/",
            source_type="community",
            fetched_at=T0 + HOUR,
            published_at=None,
        )
        self.s_repo = self.seed_source(
            locator="https://github.com/o/r",
            source_type="primary",
            fetched_at=T0 + 2 * HOUR,
            published_at=T0 - DAY,
        )
        self.seed_link(self.c1, self.s_docs, "supports", created_at=T0)
        self.seed_link(self.c1, self.s_forum, "contradicts", created_at=T0)
        self.seed_link(self.c2, self.s_docs, "supports", created_at=T0)
        self.seed_link(self.c2, self.s_repo, "supports", created_at=T0)
        self.seed_use(self.c1, "answer", self.answer)
        self.seed_use(self.c2, "answer", self.answer)

    async def test_an_answer_leads_to_its_claims_and_their_sources(self):
        trace = await self.store.trace(self.project_id, Reference.answer(self.answer))

        self.assertEqual(
            trace,
            Trace(
                reference=Reference.answer(self.answer),
                claims=(
                    TracedClaim(
                        claim=self.claim_from_sql(self.c1),
                        links=(
                            self.traced_link(self.c1, self.s_docs, Stance.SUPPORTS),
                            self.traced_link(self.c1, self.s_forum, Stance.CONTRADICTS),
                        ),
                        relations=(),
                    ),
                    TracedClaim(
                        claim=self.claim_from_sql(self.c2),
                        links=(
                            self.traced_link(self.c2, self.s_repo, Stance.SUPPORTS),
                            self.traced_link(self.c2, self.s_docs, Stance.SUPPORTS),
                        ),
                        relations=(),
                    ),
                ),
                truncated=False,
            ),
        )

    async def test_the_dates_and_types_of_the_sources_are_in_the_trace(self):
        trace = await self.store.trace(self.project_id, Reference.answer(self.answer))

        self.assertEqual(
            [
                (s.locator, s.source_type, s.fetched_at, s.published_at)
                for s in trace.sources
            ],
            [
                (
                    "https://docs.example.com/",
                    SourceType.OFFICIAL_DOCS,
                    T0,
                    T0 - 3 * DAY,
                ),
                ("https://forum.example.com/", SourceType.COMMUNITY, T0 + HOUR, None),
                ("https://github.com/o/r", SourceType.PRIMARY, T0 + 2 * HOUR, T0 - DAY),
            ],
        )

    async def test_a_task_that_recorded_a_claim_leads_to_it_and_its_sources(self):
        task = self.seed_task()
        recorded = await self.store.record_claim(
            self.project_id,
            created_by=self.user_id,
            text="A researched claim",
            sources=[
                link("https://a.example.com/", source_type=SourceType.OFFICIAL_DOCS),
                link("https://b.example.com/", Stance.CONTRADICTS),
            ],
            task_id=task,
        )

        trace = await self.store.trace(self.project_id, Reference.task(task))

        self.assertEqual(len(trace.claims), 1)
        self.assertEqual(trace.claims[0].claim, recorded.claim)
        self.assertEqual(
            sorted(item.source.locator for item in trace.claims[0].links),
            ["https://a.example.com/", "https://b.example.com/"],
        )
        self.assertEqual(trace.reference, Reference.task(task))

    async def test_a_claim_attached_to_an_answer_later_is_traced_from_it(self):
        self.clock.advance(hours=5)
        recorded = await self.store.record_claim(
            self.project_id,
            created_by=self.user_id,
            text="Attached later",
            sources=[link("https://a.example.com/")],
        )
        answer = uuid4()
        await self.add(Reference.answer(answer), [recorded.claim.id, self.c1])

        trace = await self.store.trace(self.project_id, Reference.answer(answer))

        self.assertEqual(
            [traced.claim.id for traced in trace.claims], [self.c1, recorded.claim.id]
        )
        self.assertEqual(trace.claims[1].links, recorded.links)

    async def test_an_answer_and_a_task_with_the_same_id_are_traced_separately(self):
        self.seed_use(self.c1, "task", self.answer)

        as_answer = await self.store.trace(
            self.project_id, Reference.answer(self.answer)
        )
        as_task = await self.store.trace(self.project_id, Reference.task(self.answer))

        self.assertEqual([t.claim.id for t in as_answer.claims], [self.c1, self.c2])
        self.assertEqual([t.claim.id for t in as_task.claims], [self.c1])

    async def test_a_claim_can_be_used_by_several_references(self):
        other_answer = uuid4()
        self.seed_use(self.c1, "answer", other_answer)

        mine = await self.store.trace(self.project_id, Reference.answer(self.answer))
        theirs = await self.store.trace(self.project_id, Reference.answer(other_answer))

        self.assertEqual(len(mine.claims), 2)
        self.assertEqual([t.claim.id for t in theirs.claims], [self.c1])
        self.assertEqual(theirs.claims[0].links, mine.claims[0].links)

    async def test_relations_of_a_used_claim_are_shown_even_to_claims_outside_the_trace(
        self,
    ):
        outsider = self.seed_claim("Not used by this answer")
        self.seed_relation("claim", self.c1, outsider, "contradiction")

        trace = await self.store.trace(self.project_id, Reference.answer(self.answer))

        first, second = trace.claims
        self.assertEqual(
            [r.kind for r in first.relations], [RelationKind.CONTRADICTION]
        )
        self.assertEqual(first.relations[0].other(self.c1), outsider)
        self.assertEqual(second.relations, ())
        self.assertNotIn(outsider, [t.claim.id for t in trace.claims])


class TraceEdgeCasesTest(TraceTestCase):
    async def test_a_reference_nobody_used_gives_an_empty_trace(self):
        self.seed_claim()
        for reference in (Reference.answer(uuid4()), Reference.task(uuid4())):
            with self.subTest(reference.kind):
                trace = await self.store.trace(self.project_id, reference)

                self.assertEqual(trace, Trace(reference, (), False))
                self.assertEqual(trace.sources, ())

    async def test_the_same_answer_id_in_another_project_is_invisible(self):
        answer = uuid4()
        mine = self.seed_claim("mine")
        theirs = self.seed_claim("theirs", project_id=self.other_project_id)
        self.seed_use(mine, "answer", answer)
        self.seed_use(theirs, "answer", answer, project_id=self.other_project_id)

        mine_trace = await self.store.trace(self.project_id, Reference.answer(answer))
        their_trace = await self.store.trace(
            self.other_project_id, Reference.answer(answer)
        )
        stranger = await self.store.trace(uuid4(), Reference.answer(answer))

        self.assertEqual([t.claim.id for t in mine_trace.claims], [mine])
        self.assertEqual([t.claim.id for t in their_trace.claims], [theirs])
        self.assertEqual(stranger.claims, ())

    async def test_claims_are_ordered_by_creation_time_then_id(self):
        answer = uuid4()
        early = self.seed_claim("early", created_at=T0)
        tied = sorted(
            (self.seed_claim(f"tied {n}", created_at=T0 + HOUR) for n in range(3)),
            key=lambda value: value.int,
        )
        late = self.seed_claim("late", created_at=T0 + 2 * HOUR)
        for claim in (late, tied[2], early, tied[0], tied[1]):
            self.seed_use(claim, "answer", answer)

        trace = await self.store.trace(self.project_id, Reference.answer(answer))

        self.assertEqual([t.claim.id for t in trace.claims], [early, *tied, late])

    async def test_the_limit_returns_the_first_claims_and_says_so(self):
        answer = uuid4()
        claims = [
            self.seed_claim(f"claim {n}", created_at=T0 + n * HOUR) for n in range(5)
        ]
        for claim in claims:
            self.seed_use(claim, "answer", answer)
        reference = Reference.answer(answer)

        two = await self.store.trace(self.project_id, reference, limit=2)
        five = await self.store.trace(self.project_id, reference, limit=5)
        many = await self.store.trace(self.project_id, reference, limit=200)
        default = await self.store.trace(self.project_id, reference)
        one = await self.store.trace(self.project_id, reference, limit=1)

        self.assertEqual(
            ([t.claim.id for t in two.claims], two.truncated), (claims[:2], True)
        )
        self.assertEqual(
            ([t.claim.id for t in five.claims], five.truncated), (claims, False)
        )
        self.assertEqual(
            ([t.claim.id for t in many.claims], many.truncated), (claims, False)
        )
        self.assertEqual(
            ([t.claim.id for t in default.claims], default.truncated), (claims, False)
        )
        self.assertEqual(
            ([t.claim.id for t in one.claims], one.truncated), (claims[:1], True)
        )

    async def test_a_truncated_trace_still_has_the_sources_of_the_claims_it_returns(
        self,
    ):
        answer = uuid4()
        first = self.seed_claim("first", created_at=T0)
        second = self.seed_claim("second", created_at=T0 + HOUR)
        s1 = self.seed_source(locator="https://one.example.com/")
        s2 = self.seed_source(locator="https://two.example.com/")
        self.seed_link(first, s1)
        self.seed_link(second, s2)
        self.seed_use(first, "answer", answer)
        self.seed_use(second, "answer", answer)

        trace = await self.store.trace(
            self.project_id, Reference.answer(answer), limit=1
        )

        self.assertTrue(trace.truncated)
        self.assertEqual([s.id for s in trace.sources], [s1])

    async def test_a_used_claim_without_sources_is_still_traced(self):
        answer = uuid4()
        claim = self.seed_claim()
        self.seed_use(claim, "answer", answer)

        trace = await self.store.trace(self.project_id, Reference.answer(answer))

        self.assertEqual(len(trace.claims), 1)
        self.assertEqual(trace.claims[0].links, ())

    async def test_the_sources_of_other_projects_never_appear(self):
        answer = uuid4()
        claim = self.seed_claim()
        own = self.seed_source(locator="https://own.example.com/")
        self.seed_link(claim, own)
        self.seed_use(claim, "answer", answer)
        # A second project reusing the same answer id and the same locator.
        other_claim = self.seed_claim("other", project_id=self.other_project_id)
        other_source = self.seed_source(
            project_id=self.other_project_id, locator="https://own.example.com/"
        )
        self.seed_link(other_claim, other_source, project_id=self.other_project_id)
        self.seed_use(other_claim, "answer", answer, project_id=self.other_project_id)

        trace = await self.store.trace(self.project_id, Reference.answer(answer))

        self.assertEqual([s.id for s in trace.sources], [own])
        self.assertEqual([s.project_id for s in trace.sources], [self.project_id])

    async def test_a_trace_is_one_consistent_snapshot_of_the_database(self):
        answer = uuid4()
        claim = self.seed_claim()
        first_source = self.seed_source(locator="https://one.example.com/")
        second_source = self.seed_source(locator="https://two.example.com/")
        self.seed_link(claim, first_source)
        self.seed_use(claim, "answer", answer)
        original = queries.fetch_claim_links

        async def links_after_a_concurrent_commit(session, project_id, claim_ids):
            # Another transaction commits a link after the trace has read the
            # claims and before it reads the links.
            self.seed_link(claim, second_source)
            return await original(session, project_id, claim_ids)

        with patch.object(
            queries, "fetch_claim_links", links_after_a_concurrent_commit
        ):
            trace = await self.store.trace(self.project_id, Reference.answer(answer))

        self.assertEqual(
            [item.source.id for item in trace.claims[0].links], [first_source]
        )
        later = await self.store.trace(self.project_id, Reference.answer(answer))
        self.assertEqual(len(later.claims[0].links), 2)

    async def test_a_read_cannot_write(self):
        answer = uuid4()
        claim = self.seed_claim()
        self.seed_use(claim, "answer", answer)
        original = queries.fetch_reference_claims
        outcome = {}

        async def try_to_write(session, project_id, reference, limit):
            try:
                async with session.begin_nested():
                    await session.execute(
                        text("DELETE FROM research_claim_uses WHERE claim_id = :id"),
                        {"id": claim},
                    )
            except DBAPIError as error:
                outcome["error"] = error.orig
            return await original(session, project_id, reference, limit)

        with patch.object(queries, "fetch_reference_claims", try_to_write):
            await self.store.get_claim(self.project_id, claim)
            await self.store.trace(self.project_id, Reference.answer(answer))

        self.assertIsInstance(outcome["error"], psycopg.errors.ReadOnlySqlTransaction)
        self.assertEqual(len(self.use_rows(claim)), 1)

    async def test_a_trace_is_read_only(self):
        answer = uuid4()
        claim = self.seed_claim()
        self.seed_use(claim, "answer", answer)
        before = self.counts()

        await self.store.trace(self.project_id, Reference.answer(answer))
        await self.store.get_claim(self.project_id, claim)

        self.assertEqual(self.counts(), before)
