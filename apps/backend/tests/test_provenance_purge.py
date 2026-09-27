"""``ProvenanceStore.purge_projects`` (Decision 0028, real PostgreSQL).

Rows are seeded and checked with SQL (``provenance_support``), like every other
provenance store test.
"""

from uuid import uuid4

from paw_backend.projects.records import ProjectStatus
from paw_backend.research.provenance import (
    EntityKind,
    InputProblem,
    InvalidProvenanceInputError,
    ProjectUnavailableError,
    Reference,
    RelationKind,
)

from .provenance_support import PostgresProvenanceTestCase, link, requires_postgres


@requires_postgres
class PurgeProjectsTest(PostgresProvenanceTestCase):
    def seed_everything(self, project_id):
        """One row in each of the six tables, all in ``project_id``."""
        claim = self.seed_claim("Claim", project_id=project_id)
        other_claim = self.seed_claim("Other claim", project_id=project_id)
        source = self.seed_source(project_id=project_id)
        other_source = self.seed_source(
            project_id=project_id, locator="https://example.com/other"
        )
        self.seed_link(claim, source, project_id=project_id)
        self.seed_use(claim, "answer", uuid4(), project_id=project_id)
        self.seed_relation("claim", claim, other_claim, project_id=project_id)
        self.seed_relation("source", source, other_source, project_id=project_id)
        return {
            "sources": {source, other_source},
            "claims": {claim, other_claim},
        }

    async def test_an_empty_collection_deletes_nothing_and_opens_no_transaction(self):
        self.assertEqual(await self.store.purge_projects([]), ())
        self.assertEqual(await self.store.purge_projects(()), ())

    async def test_unknown_ids_are_fine_and_purge_nothing(self):
        self.assertEqual(await self.store.purge_projects([uuid4()]), ())

    async def test_bad_input_is_rejected_before_the_database_is_touched(self):
        claim = self.seed_claim("Claim")
        for bad, problem in (
            ("not-a-list", InputProblem.NOT_A_COLLECTION),
            (b"not-a-list", InputProblem.NOT_A_COLLECTION),
            (None, InputProblem.NOT_A_COLLECTION),
            (["not-a-uuid"], InputProblem.WRONG_TYPE),
            ([None], InputProblem.REQUIRED),  # validate_uuid's own rule
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidProvenanceInputError) as raised:
                    await self.store.purge_projects(bad)
                self.assertEqual(
                    (raised.exception.field, raised.exception.problem),
                    ("project_ids", problem),
                )
        self.assertEqual(self.claim_from_sql(claim).id, claim)

    async def test_a_project_that_is_not_yet_a_tombstone_keeps_its_provenance(self):
        for status in (
            ProjectStatus.ACTIVE,
            ProjectStatus.ARCHIVED,
            ProjectStatus.PENDING_DELETION,
        ):
            with self.subTest(status=status):
                project_id = self.seed_project(status)
                self.seed_everything(project_id)
                before = self.counts()

                purged = await self.store.purge_projects([project_id])

                self.assertEqual(purged, ())
                self.assertEqual(self.counts(), before)

    async def test_every_table_of_a_deleted_project_is_emptied(self):
        project_id = self.seed_project(ProjectStatus.DELETED)
        self.seed_everything(project_id)

        purged = await self.store.purge_projects([project_id])

        self.assertEqual(purged, (project_id,))
        self.assertEqual(self.counts(), dict.fromkeys(self.counts(), 0))

    async def test_other_projects_are_left_alone(self):
        gone = self.seed_project(ProjectStatus.DELETED)
        kept_project = self.seed_project(ProjectStatus.ACTIVE)
        self.seed_everything(gone)
        kept = self.seed_everything(kept_project)
        before = self.counts()

        purged = await self.store.purge_projects([gone, kept_project])

        self.assertEqual(purged, (gone,))
        self.assertEqual({row["id"] for row in self.claim_rows()}, kept["claims"])
        self.assertEqual({row["id"] for row in self.source_rows()}, kept["sources"])
        # Nothing of the kept project's rows was touched or recreated.
        self.assertEqual(
            self.counts()["research_claims"], before["research_claims"] - 2
        )

    async def test_only_projects_with_at_least_one_row_are_returned(self):
        with_rows = self.seed_project(ProjectStatus.DELETED)
        without_rows = self.seed_project(ProjectStatus.DELETED)
        self.seed_everything(with_rows)

        purged = await self.store.purge_projects([with_rows, without_rows])

        self.assertEqual(purged, (with_rows,))

    async def test_a_project_with_only_a_source_and_no_claim_is_still_found(self):
        # ``record_claim`` never records a source without a claim, but a project
        # is still found through whichever table it happens to have rows in.
        project_id = self.seed_project(ProjectStatus.DELETED)
        self.seed_source(project_id=project_id)

        purged = await self.store.purge_projects([project_id])

        self.assertEqual(purged, (project_id,))

    async def test_the_result_is_sorted_and_duplicates_collapse(self):
        first = self.seed_project(ProjectStatus.DELETED)
        second = self.seed_project(ProjectStatus.DELETED)
        self.seed_source(project_id=first)
        self.seed_source(project_id=second)

        purged = await self.store.purge_projects([second, first, first, second])

        self.assertEqual(purged, tuple(sorted((first, second))))

    async def test_a_second_call_finds_nothing_left(self):
        project_id = self.seed_project(ProjectStatus.DELETED)
        self.seed_everything(project_id)

        first = await self.store.purge_projects([project_id])
        second = await self.store.purge_projects([project_id])

        self.assertEqual(first, (project_id,))
        self.assertEqual(second, ())

    async def test_a_deleted_project_with_purged_rows_is_logged(self):
        project_id = self.seed_project(ProjectStatus.DELETED)
        self.seed_everything(project_id)

        with self.assertLogs("paw_backend.research.provenance.store", level="INFO"):
            await self.store.purge_projects([project_id])

    async def test_nothing_purged_is_not_logged(self):
        with self.assertRaises(AssertionError):
            with self.assertLogs("paw_backend.research.provenance.store", level="INFO"):
                await self.store.purge_projects([uuid4()])


@requires_postgres
class WriteAfterDeleteTest(PostgresProvenanceTestCase):
    """``record_claim`` / ``add_reference`` / ``mark_related`` refuse a project
    ``purge_projects`` already deleted (``_guard_project``, Decision 0028)."""

    async def test_record_claim_refuses_a_deleted_project(self):
        project_id = self.seed_project(ProjectStatus.DELETED)

        with self.assertRaises(ProjectUnavailableError):
            await self.store.record_claim(
                project_id,
                created_by=self.user_id,
                text="A new claim",
                sources=[link()],
            )

        self.assertEqual(self.table_count("research_claims"), 0)
        self.assertEqual(self.table_count("research_sources"), 0)

    async def test_add_reference_refuses_a_deleted_project(self):
        project_id = self.seed_project(ProjectStatus.DELETED)
        claim = self.seed_claim("Claim", project_id=project_id)

        with self.assertRaises(ProjectUnavailableError):
            await self.store.add_reference(
                project_id,
                reference=Reference.answer(uuid4()),
                claim_ids=[claim],
                created_by=self.user_id,
            )

        self.assertEqual(self.table_count("research_claim_uses"), 0)

    async def test_mark_related_refuses_a_deleted_project(self):
        project_id = self.seed_project(ProjectStatus.DELETED)
        first = self.seed_claim("First", project_id=project_id)
        second = self.seed_claim("Second", project_id=project_id)

        with self.assertRaises(ProjectUnavailableError):
            await self.store.mark_related(
                project_id,
                entity=EntityKind.CLAIM,
                kind=RelationKind.DUPLICATE,
                first_id=first,
                second_id=second,
                created_by=self.user_id,
            )

        self.assertEqual(self.table_count("research_claim_relations"), 0)

    async def test_a_project_this_store_has_no_row_for_is_unaffected(self):
        # Most tests never seed a ``projects`` row (module docstring: no
        # foreign key); the guard must stay a no-op for them.
        unknown_project = uuid4()

        recorded = await self.store.record_claim(
            unknown_project, created_by=self.user_id, text="Fine", sources=[link()]
        )

        self.assertTrue(recorded.created)
