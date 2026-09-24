"""``ProvenanceStore.record_claim`` on a real PostgreSQL.

Rows are seeded and checked with SQL (``provenance_support``); the clock is
injected. Every scenario states the rows it expects, so a method that "works" by
writing the wrong thing fails.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from paw_backend.research.provenance import (
    Claim,
    InputProblem,
    InvalidProvenanceInputError,
    ProvenanceConflictError,
    ProvenanceLimitError,
    RecordedClaim,
    Source,
    SourceInput,
    SourceLink,
    SourceLinkInput,
    Stance,
)
from paw_backend.research.providers import ProviderKind, SourceMetadata
from paw_backend.research.providers.contract import SourceType

from .provenance_support import (
    T0,
    PostgresProvenanceTestCase,
    content_hash,
    expected_fingerprint,
    link,
    requires_postgres,
)

HOUR = timedelta(hours=1)
SKY = "The sky is blue."
SECRET = "SECRET-TOKEN-4f9a1c"


@requires_postgres
class RecordClaimTestCase(PostgresProvenanceTestCase):
    async def record(self, text=SKY, sources=None, **options):
        arguments = {"created_by": self.user_id, "sources": sources or [link()]}
        arguments.update(options)
        project = arguments.pop("project_id", self.project_id)
        return await self.store.record_claim(project, text=text, **arguments)


class RecordClaimTest(RecordClaimTestCase):
    async def test_a_claim_with_a_source_is_recorded_and_returned(self):
        published = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
        recorded = await self.record(
            sources=[
                link(
                    "https://example.com/sky",
                    source_type=SourceType.OFFICIAL_DOCS,
                    fetched_at=T0 - HOUR,
                    published_at=published,
                    title="About the sky",
                    content="sky page",
                )
            ]
        )

        claim = recorded.claim
        source = recorded.links[0].source
        self.assertEqual(
            claim,
            Claim(
                id=claim.id,
                project_id=self.project_id,
                text=SKY,
                task_id=None,
                created_by=self.user_id,
                created_at=T0,
            ),
        )
        self.assertEqual(
            source,
            Source(
                id=source.id,
                project_id=self.project_id,
                locator="https://example.com/sky",
                source_type=SourceType.OFFICIAL_DOCS,
                title="About the sky",
                content_hash=content_hash("sky page"),
                fetched_at=T0 - HOUR,
                published_at=published,
                created_at=T0,
            ),
        )
        self.assertEqual(
            recorded,
            RecordedClaim(
                claim=claim,
                created=True,
                links=(
                    SourceLink(
                        claim_id=claim.id,
                        source=source,
                        stance=Stance.SUPPORTS,
                        linked_at=T0,
                    ),
                ),
                new_links=1,
            ),
        )
        # And the database holds exactly that.
        self.assertEqual(self.claim_from_sql(claim.id), claim)
        self.assertEqual(self.source_from_sql(source.id), source)
        (claim_row,) = self.claim_rows()
        self.assertEqual(claim_row["text_fingerprint"], expected_fingerprint(SKY))
        self.assertEqual(
            {
                k: (v["stance"], v["project_id"], v["created_at"])
                for k, v in self.link_rows(claim.id).items()
            },
            {source.id: ("supports", self.project_id, T0)},
        )
        self.assertEqual(
            self.counts(),
            {
                "research_sources": 1,
                "research_claims": 1,
                "research_claim_sources": 1,
                "research_claim_uses": 0,
                "research_claim_relations": 0,
                "research_source_relations": 0,
            },
        )

    async def test_an_unknown_publication_date_stays_unknown(self):
        recorded = await self.record(sources=[link(published_at=None)])

        self.assertIsNone(recorded.links[0].source.published_at)
        self.assertIsNone(self.source_rows()[0]["published_at"])

    async def test_the_text_is_stored_exactly_as_given(self):
        text = "  The Sky   IS Blue.\n"

        recorded = await self.record(text)

        self.assertEqual(recorded.claim.text, text)
        self.assertEqual(self.claim_rows()[0]["claim_text"], text)

    async def test_every_written_row_carries_the_clock_time_of_the_call(self):
        self.clock.advance(hours=5)

        recorded = await self.record(
            sources=[link(fetched_at=T0, published_at=T0 - HOUR)]
        )

        later = T0 + 5 * HOUR
        self.assertEqual(recorded.claim.created_at, later)
        self.assertEqual(recorded.links[0].source.created_at, later)
        self.assertEqual(recorded.links[0].linked_at, later)
        # ... while the dates of the source are the caller's.
        self.assertEqual(recorded.links[0].source.fetched_at, T0)
        self.assertEqual(recorded.links[0].source.published_at, T0 - HOUR)

    async def test_the_sources_come_back_in_the_order_they_were_given(self):
        sources = [
            link("https://c.example.com/"),
            link("https://a.example.com/"),
            link("https://b.example.com/", Stance.CONTRADICTS),
        ]

        recorded = await self.record(sources=sources)

        self.assertEqual(
            [(item.source.locator, item.stance) for item in recorded.links],
            [
                ("https://c.example.com/", Stance.SUPPORTS),
                ("https://a.example.com/", Stance.SUPPORTS),
                ("https://b.example.com/", Stance.CONTRADICTS),
            ],
        )
        self.assertEqual(recorded.new_links, 3)
        self.assertEqual(
            sorted(row["stance"] for row in self.link_rows(recorded.claim.id).values()),
            ["contradicts", "supports", "supports"],
        )

    async def test_a_tuple_of_sources_is_accepted(self):
        recorded = await self.record(sources=(link("https://a.example.com/"),))

        self.assertEqual(len(recorded.links), 1)

    async def test_the_locator_is_stored_in_its_canonical_form(self):
        recorded = await self.record(
            sources=[link("HTTPS://Example.COM:443/a?utm_source=x&b=2&a=1#top")]
        )

        self.assertEqual(
            recorded.links[0].source.locator, "https://example.com/a?a=1&b=2"
        )
        self.assertEqual(
            self.source_rows()[0]["locator"], "https://example.com/a?a=1&b=2"
        )

    async def test_a_credential_in_the_query_is_never_stored(self):
        await self.record(
            sources=[link(f"https://example.com/a?token={SECRET}&x=1", title="t")]
        )

        (row,) = self.source_rows()
        self.assertEqual(row["locator"], "https://example.com/a?x=1")
        self.assertNotIn(SECRET, " ".join(str(value) for value in row.values()))

    async def test_every_source_type_is_recorded(self):
        for source_type in SourceType:
            await self.record(
                f"Claim of {source_type.value}",
                [
                    link(
                        f"https://example.com/{source_type.value}",
                        source_type=source_type,
                    )
                ],
            )

        self.assertEqual(
            {row["source_type"] for row in self.source_rows()},
            {member.value for member in SourceType},
        )

    async def test_the_metadata_of_a_provider_can_be_recorded(self):
        metadata = SourceMetadata(
            provider_kind=ProviderKind.GITHUB,
            provider_id="github-a",
            locator="https://github.com/o/r/releases/tag/v1",
            title="v1",
            retrieved_at=T0,
            content_hash=content_hash("release"),
            source_type=SourceType.OFFICIAL_GITHUB,
            published_at=T0 - 24 * HOUR,
            private_source=False,
        )

        recorded = await self.record(
            sources=[SourceLinkInput(source=SourceInput.from_metadata(metadata))]
        )

        source = recorded.links[0].source
        self.assertEqual(
            (
                source.locator,
                source.source_type,
                source.fetched_at,
                source.published_at,
            ),
            (metadata.locator, SourceType.OFFICIAL_GITHUB, T0, T0 - 24 * HOUR),
        )
        self.assertEqual(source.content_hash, content_hash("release"))

    async def test_the_creator_is_recorded(self):
        other_user = uuid4()

        recorded = await self.record(created_by=other_user)

        self.assertEqual(recorded.claim.created_by, other_user)
        self.assertEqual(self.claim_rows()[0]["created_by"], other_user)


class RecordClaimDeduplicationTest(RecordClaimTestCase):
    async def test_the_same_call_twice_changes_nothing(self):
        first = await self.record()
        self.clock.advance(hours=3)

        second = await self.record()

        self.assertFalse(second.created)
        self.assertEqual(second.new_links, 0)
        self.assertEqual(second.claim, first.claim)
        self.assertEqual(second.links, first.links)
        self.assertEqual(second.links[0].linked_at, T0)
        self.assertEqual(self.table_count("research_claims"), 1)
        self.assertEqual(self.table_count("research_sources"), 1)
        self.assertEqual(self.table_count("research_claim_sources"), 1)

    async def test_the_same_claim_in_another_spelling_is_the_same_claim(self):
        first = await self.record("The sky is blue.")

        for spelling in (
            "  THE SKY   IS BLUE.  ",
            "the\tsky\nis blue.",
            "Ｔｈｅ sky is blue.",
        ):
            with self.subTest(spelling):
                again = await self.record(spelling)
                self.assertFalse(again.created)
                self.assertEqual(again.claim, first.claim)

        self.assertEqual(self.table_count("research_claims"), 1)
        # The first text stays.
        self.assertEqual(self.claim_rows()[0]["claim_text"], "The sky is blue.")

    async def test_another_claim_is_not_merged(self):
        await self.record("The sky is blue.")

        other = await self.record("The sky is not blue.")

        self.assertTrue(other.created)
        self.assertEqual(self.table_count("research_claims"), 2)

    async def test_a_second_call_adds_only_the_new_sources(self):
        first = await self.record(sources=[link("https://a.example.com/")])
        self.clock.advance(hours=1)

        second = await self.record(
            sources=[link("https://a.example.com/"), link("https://b.example.com/")]
        )

        self.assertFalse(second.created)
        self.assertEqual(second.new_links, 1)
        self.assertEqual(
            [item.source.locator for item in second.links],
            ["https://a.example.com/", "https://b.example.com/"],
        )
        self.assertEqual(second.links[0], first.links[0])
        self.assertEqual(second.links[1].linked_at, T0 + HOUR)
        self.assertEqual(len(self.link_rows(first.claim.id)), 2)

    async def test_the_first_record_of_a_claim_keeps_its_creator_and_time(self):
        first = await self.record(created_by=self.user_id)
        self.clock.advance(hours=2)

        second = await self.record(created_by=uuid4())

        self.assertEqual(second.claim.created_by, self.user_id)
        self.assertEqual(second.claim.created_at, T0)
        self.assertEqual(second.claim, first.claim)

    async def test_a_repeated_source_in_one_call_is_recorded_once(self):
        a, b = link("https://a.example.com/"), link("https://b.example.com/")

        recorded = await self.record(sources=[a, b, a, a])

        self.assertEqual(
            [item.source.locator for item in recorded.links],
            ["https://a.example.com/", "https://b.example.com/"],
        )
        self.assertEqual(recorded.new_links, 2)
        self.assertEqual(self.table_count("research_sources"), 2)
        self.assertEqual(self.table_count("research_claim_sources"), 2)

    async def test_two_spellings_of_one_page_in_one_call_are_one_source(self):
        recorded = await self.record(
            sources=[
                link("https://EXAMPLE.com:443/a?utm_source=x"),
                link("https://example.com/a"),
            ]
        )

        self.assertEqual(len(recorded.links), 1)
        self.assertEqual(self.table_count("research_sources"), 1)

    async def test_the_same_source_with_two_stances_in_one_call_is_rejected_first(self):
        with self.assertRaises(InvalidProvenanceInputError) as raised:
            await self.record(
                sources=[
                    link("https://a.example.com/", Stance.SUPPORTS),
                    link("https://a.example.com/", Stance.CONTRADICTS),
                ]
            )

        self.assertEqual(
            (raised.exception.field, raised.exception.problem),
            ("sources", InputProblem.CONFLICT),
        )
        self.assertTrue(all(count == 0 for count in self.counts().values()))


class RecordClaimSourceIdentityTest(RecordClaimTestCase):
    async def test_one_source_can_back_two_claims(self):
        first = await self.record("Claim one", [link("https://shared.example.com/")])
        second = await self.record("Claim two", [link("https://shared.example.com/")])

        self.assertEqual(first.links[0].source, second.links[0].source)
        self.assertEqual(self.table_count("research_sources"), 1)
        self.assertEqual(self.table_count("research_claim_sources"), 2)

    async def test_the_first_record_of_a_source_wins(self):
        first = await self.record(
            "Claim one",
            [
                link(
                    "https://shared.example.com/",
                    title="First",
                    fetched_at=T0,
                    published_at=T0 - HOUR,
                    source_type=SourceType.PRIMARY,
                )
            ],
        )
        self.clock.advance(hours=4)

        second = await self.record(
            "Claim two",
            [
                link(
                    "https://shared.example.com/",
                    title="Second",
                    fetched_at=T0 + 3 * HOUR,
                    published_at=None,
                    source_type=SourceType.COMMUNITY,
                )
            ],
        )

        self.assertEqual(second.links[0].source, first.links[0].source)
        self.assertEqual(second.links[0].source.title, "First")
        self.assertEqual(second.links[0].source.fetched_at, T0)
        self.assertEqual(second.links[0].source.source_type, SourceType.PRIMARY)
        self.assertEqual(
            self.source_from_sql(first.links[0].source.id), first.links[0].source
        )

    async def test_the_same_locator_with_new_content_is_a_new_source(self):
        first = await self.record(
            "Claim one", [link("https://a.example.com/", content="v1")]
        )
        second = await self.record(
            "Claim one", [link("https://a.example.com/", content="v2")]
        )

        self.assertNotEqual(first.links[0].source.id, second.links[0].source.id)
        self.assertEqual(second.new_links, 1)
        self.assertEqual(self.table_count("research_sources"), 2)
        self.assertEqual(len(self.link_rows(first.claim.id)), 2)

    async def test_a_stance_can_differ_between_claims_for_the_same_source(self):
        await self.record(
            "Claim one", [link("https://a.example.com/", Stance.SUPPORTS)]
        )
        second = await self.record(
            "Claim two", [link("https://a.example.com/", Stance.CONTRADICTS)]
        )

        self.assertEqual(second.links[0].stance, Stance.CONTRADICTS)
        self.assertEqual(self.table_count("research_sources"), 1)


class RecordClaimConflictTest(RecordClaimTestCase):
    async def test_an_existing_link_with_the_other_stance_is_a_conflict(self):
        for first, second in (
            (Stance.SUPPORTS, Stance.CONTRADICTS),
            (Stance.CONTRADICTS, Stance.SUPPORTS),
        ):
            with self.subTest(first):
                self.clean_tables()
                await self.record(sources=[link("https://a.example.com/", first)])

                with self.assertRaises(ProvenanceConflictError):
                    await self.record(sources=[link("https://a.example.com/", second)])

                (row,) = self.rows("SELECT stance FROM research_claim_sources")
                self.assertEqual(row["stance"], first.value)

    async def test_a_conflict_rolls_back_everything_of_the_call(self):
        task = self.seed_task()
        await self.record(sources=[link("https://a.example.com/", Stance.SUPPORTS)])
        before = self.counts()

        with self.assertRaises(ProvenanceConflictError):
            await self.record(
                task_id=task,
                sources=[
                    link("https://a.example.com/", Stance.CONTRADICTS),
                    link("https://b.example.com/"),
                    link("https://c.example.com/"),
                ],
            )

        self.assertEqual(self.counts(), before)

    async def test_the_conflict_error_is_a_fixed_message(self):
        await self.record(
            SKY + SECRET, [link("https://a.example.com/", Stance.SUPPORTS)]
        )

        with self.assertRaises(ProvenanceConflictError) as raised:
            await self.record(
                SKY + SECRET, [link("https://a.example.com/", Stance.CONTRADICTS)]
            )

        self.assertNotIn(SECRET, str(raised.exception) + repr(raised.exception))
        self.assertNotIn("example.com", str(raised.exception))
        self.assertEqual(raised.exception.code, "provenance_conflict")


class RecordClaimTaskTest(RecordClaimTestCase):
    async def test_the_task_that_recorded_a_claim_is_recorded_and_uses_it(self):
        task = self.seed_task()

        recorded = await self.record(task_id=task)

        self.assertEqual(recorded.claim.task_id, task)
        self.assertEqual(self.claim_rows()[0]["task_id"], task)
        (use,) = self.use_rows(recorded.claim.id)
        self.assertEqual(
            (
                use["ref_kind"],
                use["ref_id"],
                use["project_id"],
                use["created_by"],
                use["created_at"],
            ),
            ("task", task, self.project_id, self.user_id, T0),
        )

    async def test_a_claim_without_a_task_has_no_uses(self):
        recorded = await self.record()

        self.assertIsNone(recorded.claim.task_id)
        self.assertEqual(self.use_rows(recorded.claim.id), [])

    async def test_an_unknown_task_is_rejected_and_nothing_is_written(self):
        with self.assertRaises(InvalidProvenanceInputError) as raised:
            await self.record(task_id=uuid4())

        self.assertEqual(
            (raised.exception.field, raised.exception.problem),
            ("task_id", InputProblem.UNKNOWN_REFERENCE),
        )
        self.assertTrue(all(count == 0 for count in self.counts().values()))

    async def test_the_task_of_another_project_is_rejected_like_an_unknown_one(self):
        foreign = self.seed_task(project_id=self.other_project_id)

        with self.assertRaises(InvalidProvenanceInputError) as raised:
            await self.record(task_id=foreign)

        self.assertEqual(
            (raised.exception.field, raised.exception.problem),
            ("task_id", InputProblem.UNKNOWN_REFERENCE),
        )
        self.assertTrue(all(count == 0 for count in self.counts().values()))

    async def test_a_second_task_uses_the_existing_claim_without_taking_it_over(self):
        first_task, second_task = self.seed_task(), self.seed_task()
        first = await self.record(task_id=first_task)

        second = await self.record(task_id=second_task)
        third = await self.record(task_id=second_task)

        self.assertFalse(second.created)
        self.assertEqual(second.claim.task_id, first_task)
        self.assertEqual(third.claim, first.claim)
        self.assertEqual(
            sorted(use["ref_id"] for use in self.use_rows(first.claim.id)),
            sorted([first_task, second_task]),
        )
        self.assertEqual(self.claim_rows()[0]["task_id"], first_task)

    async def test_a_task_that_records_an_existing_claim_gets_a_use_of_it(
        self,
    ):
        task = self.seed_task()
        first = await self.record()

        second = await self.record(task_id=task)

        self.assertIsNone(second.claim.task_id)
        self.assertEqual(
            [use["ref_id"] for use in self.use_rows(first.claim.id)], [task]
        )

    async def test_a_task_use_is_recorded_once_per_task_and_claim(self):
        task = self.seed_task()
        await self.record(task_id=task)
        self.clock.advance(hours=1)

        await self.record(task_id=task)

        (use,) = self.use_rows(self.claim_rows()[0]["id"])
        self.assertEqual(use["created_at"], T0)


class RecordClaimProjectTest(RecordClaimTestCase):
    async def test_the_same_claim_and_source_in_two_projects_are_separate_records(self):
        mine = await self.record()
        theirs = await self.record(project_id=self.other_project_id)

        self.assertTrue(theirs.created)
        self.assertNotEqual(mine.claim.id, theirs.claim.id)
        self.assertNotEqual(mine.links[0].source.id, theirs.links[0].source.id)
        self.assertEqual(theirs.claim.project_id, self.other_project_id)
        self.assertEqual(theirs.links[0].source.project_id, self.other_project_id)
        self.assertEqual(self.table_count("research_claims"), 2)
        self.assertEqual(self.table_count("research_sources"), 2)
        for row in self.rows("SELECT project_id FROM research_claim_sources"):
            self.assertIn(row["project_id"], {self.project_id, self.other_project_id})
        self.assertEqual(
            sorted(
                str(r["project_id"])
                for r in self.rows("SELECT project_id FROM research_claim_sources")
            ),
            sorted([str(self.project_id), str(self.other_project_id)]),
        )

    async def test_a_claim_of_another_project_is_not_reused(self):
        foreign = self.seed_claim(SKY, project_id=self.other_project_id)

        recorded = await self.record()

        self.assertTrue(recorded.created)
        self.assertNotEqual(recorded.claim.id, foreign)
        self.assertEqual(recorded.claim.project_id, self.project_id)
        self.assertEqual(self.link_rows(foreign), {})

    async def test_a_source_of_another_project_is_not_reused(self):
        foreign = self.seed_source(
            project_id=self.other_project_id,
            locator="https://a.example.com/",
            content="v1",
        )

        recorded = await self.record(
            sources=[link("https://a.example.com/", content="v1")]
        )

        self.assertNotEqual(recorded.links[0].source.id, foreign)
        self.assertEqual(recorded.links[0].source.project_id, self.project_id)


class RecordClaimLimitTest(RecordClaimTestCase):
    def seed_claim_with_sources(self, count: int):
        claim = self.seed_claim(SKY)
        for number in range(count):
            source = self.seed_source(locator=f"https://seed.example.com/{number}")
            self.seed_link(claim, source)
        return claim

    async def test_a_claim_may_have_exactly_50_sources(self):
        claim = self.seed_claim_with_sources(49)

        recorded = await self.record(sources=[link("https://new.example.com/")])

        self.assertEqual(recorded.claim.id, claim)
        self.assertEqual(recorded.new_links, 1)
        self.assertEqual(len(self.link_rows(claim)), 50)

    async def test_the_51st_source_is_refused_and_nothing_is_written(self):
        claim = self.seed_claim_with_sources(50)
        task = self.seed_task()
        before = self.counts()

        with self.assertRaises(ProvenanceLimitError):
            await self.record(task_id=task, sources=[link("https://new.example.com/")])

        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.link_rows(claim)), 50)

    async def test_two_new_sources_that_would_pass_the_limit_are_refused_together(self):
        claim = self.seed_claim_with_sources(49)
        before = self.counts()

        with self.assertRaises(ProvenanceLimitError):
            await self.record(
                sources=[
                    link("https://new1.example.com/"),
                    link("https://new2.example.com/"),
                ]
            )

        self.assertEqual(self.counts(), before)
        self.assertEqual(len(self.link_rows(claim)), 49)

    async def test_sources_that_are_already_linked_do_not_count_again(self):
        claim = self.seed_claim_with_sources(50)

        recorded = await self.record(
            sources=[
                link("https://seed.example.com/0"),
                link("https://seed.example.com/1"),
            ]
        )

        self.assertEqual(recorded.new_links, 0)
        self.assertEqual(len(self.link_rows(claim)), 50)

    async def test_the_limit_error_is_a_fixed_message(self):
        self.seed_claim_with_sources(50)

        with self.assertRaises(ProvenanceLimitError) as raised:
            await self.record(sources=[link("https://new.example.com/")])

        self.assertEqual(raised.exception.code, "provenance_limit")
        self.assertNotIn("new.example.com", str(raised.exception))

    async def test_a_new_claim_takes_20_sources_in_one_call(self):
        sources = [link(f"https://example.com/{n}") for n in range(20)]

        recorded = await self.record(sources=sources)

        self.assertEqual(recorded.new_links, 20)
        self.assertEqual(len(recorded.links), 20)
        self.assertEqual(len(self.link_rows(recorded.claim.id)), 20)


class RecordClaimNoContentTest(RecordClaimTestCase):
    async def test_only_the_hash_of_the_content_is_kept(self):
        recorded = await self.record(
            sources=[
                link("https://a.example.com/", content="THE FULL PAGE TEXT " * 100)
            ]
        )

        (row,) = self.source_rows()
        self.assertEqual(row["content_hash"], content_hash("THE FULL PAGE TEXT " * 100))
        self.assertNotIn("FULL PAGE", " ".join(str(v) for v in row.values()))
        self.assertEqual(recorded.links[0].source.content_hash, row["content_hash"])
