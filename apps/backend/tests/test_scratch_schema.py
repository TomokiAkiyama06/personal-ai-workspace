"""Research Scratch schema: constraints and separation (real PostgreSQL).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. Every constraint is asserted by
name: a violation that fires on the wrong constraint would otherwise pass for
the wrong reason.
"""

from datetime import timedelta
from functools import partial
from uuid import uuid4

from sqlalchemy import delete, insert, text
from sqlalchemy.exc import IntegrityError

from paw_backend.research.scratch.limits import SCRATCH_TTL
from paw_backend.research.scratch.models import ScratchItemRow, ScratchLeaseRow

from .memory_support import MemoryDatabaseTestCase, requires_postgres, utc

CREATED = utc(2026, 9, 24)
ITEMS = "ck_research_scratch_items_"
LEASES = "ck_research_scratch_leases_"


def item_values(**overrides):
    values = {
        "project_id": uuid4(),
        "created_by": uuid4(),
        "summary": "A summary",
        "created_at": CREATED,
        "expires_at": CREATED + SCRATCH_TTL,
    }
    values.update(overrides)
    return values


@requires_postgres
class ScratchSchemaTestCase(MemoryDatabaseTestCase):
    def add_item(self, **overrides):
        return self.session.execute(
            insert(ScratchItemRow)
            .values(**item_values(**overrides))
            .returning(ScratchItemRow.id)
        ).scalar_one()

    def try_item(self, **overrides):
        return self.violation(partial(self.add_item, **overrides))

    def add_lease(self, item_id, **overrides):
        values = {
            "item_id": item_id,
            "holder_id": uuid4(),
            "leased_at": CREATED,
            "expires_at": CREATED + timedelta(minutes=5),
        }
        values.update(overrides)
        self.session.execute(insert(ScratchLeaseRow).values(**values))
        return values["holder_id"]

    def try_lease(self, item_id, **overrides):
        return self.violation(partial(self.add_lease, item_id, **overrides))

    def add_task(self, project_id=None):
        task_id = uuid4()
        self.session.execute(
            text(
                "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
                " attempt, retry_count, version, created_at, updated_at)"
                " VALUES (:id, :project, :user, 'T', CAST('{}' AS jsonb), 'queued',"
                " 1, 0, 1, :now, :now)"
            ),
            {
                "id": task_id,
                "project": project_id or uuid4(),
                "user": uuid4(),
                "now": CREATED,
            },
        )
        return task_id

    def null_violation(self, column):
        """The column a NOT NULL violation names, for an item without ``column``."""
        values = item_values()
        del values[column]
        with self.assertRaises(IntegrityError) as raised:
            with self.session.begin_nested():
                self.session.execute(insert(ScratchItemRow).values(**values))
        return raised.exception.orig.diag.column_name

    def item_row(self, item_id):
        return (
            self.session.execute(
                text("SELECT * FROM research_scratch_items WHERE id = :id"),
                {"id": item_id},
            )
            .mappings()
            .one()
        )


class TtlConstraintTest(ScratchSchemaTestCase):
    def test_expires_at_must_be_exactly_created_at_plus_24_hours(self):
        self.assertIsNone(self.try_item())
        self.assertEqual(
            self.try_item(expires_at=CREATED + SCRATCH_TTL + timedelta(microseconds=1)),
            ITEMS + "expires_at_matches_ttl",
        )
        self.assertEqual(
            self.try_item(expires_at=CREATED + SCRATCH_TTL - timedelta(microseconds=1)),
            ITEMS + "expires_at_matches_ttl",
        )
        self.assertEqual(
            self.try_item(expires_at=CREATED + timedelta(hours=23, minutes=59)),
            ITEMS + "expires_at_matches_ttl",
        )
        self.assertEqual(
            self.try_item(expires_at=CREATED), ITEMS + "expires_at_matches_ttl"
        )

    def test_the_ttl_is_24_hours_in_utc_whatever_the_session_time_zone(self):
        self.session.execute(text("SET LOCAL TIME ZONE 'Asia/Tokyo'"))

        self.assertIsNone(self.try_item())

    def test_both_times_are_required_and_have_no_default(self):
        for column in ("created_at", "expires_at"):
            with self.subTest(column):
                self.assertEqual(self.null_violation(column), column)


class ItemColumnConstraintTest(ScratchSchemaTestCase):
    def test_defaults_of_a_new_item(self):
        item_id = self.add_item()

        row = self.item_row(item_id)

        self.assertFalse(row["pinned"])
        self.assertFalse(row["saved"])
        self.assertEqual(row["promotion_state"], "none")
        self.assertIsNone(row["promotion_requested_at"])
        self.assertEqual(row["source_metadata"], {})
        self.assertIsNone(row["task_id"])
        self.assertIsNotNone(row["id"])

    def test_pinned_and_saved_are_separate_required_markers(self):
        item_id = self.add_item(pinned=True)
        saved_id = self.add_item(saved=True)

        pinned_row, saved_row = self.item_row(item_id), self.item_row(saved_id)

        self.assertEqual((pinned_row["pinned"], pinned_row["saved"]), (True, False))
        self.assertEqual((saved_row["pinned"], saved_row["saved"]), (False, True))
        for column in ("pinned", "saved"):
            with self.subTest(column):
                with self.assertRaises(IntegrityError) as raised:
                    with self.session.begin_nested():
                        self.add_item(**{column: None})
                self.assertEqual(raised.exception.orig.diag.column_name, column)

    def test_an_item_needs_a_summary_or_a_content(self):
        self.assertIsNone(self.try_item(summary="s", content=None))
        self.assertIsNone(self.try_item(summary=None, content="c"))
        self.assertEqual(
            self.try_item(summary=None, content=None), ITEMS + "has_content"
        )

    def test_text_lengths_are_bounded_in_characters(self):
        cases = [
            ("query", 1000, "query_length"),
            ("title", 500, "title_length"),
            ("summary", 8000, "summary_length"),
            ("content", 100_000, "content_length"),
        ]
        for column, limit, name in cases:
            with self.subTest(column):
                self.assertIsNone(self.try_item(**{column: "あ" * limit}))
                self.assertEqual(
                    self.try_item(**{column: "あ" * (limit + 1)}), ITEMS + name
                )
                self.assertEqual(self.try_item(**{column: ""}), ITEMS + name)

    def test_promotion_state_takes_only_the_four_values(self):
        for state in ("none", "promoted", "rejected"):
            with self.subTest(state):
                self.assertIsNone(self.try_item(promotion_state=state))
        self.assertIsNone(
            self.try_item(promotion_state="pending", promotion_requested_at=CREATED)
        )
        self.assertEqual(
            self.try_item(promotion_state="Pending"), ITEMS + "promotion_state_valid"
        )
        self.assertEqual(
            self.try_item(promotion_state="done"), ITEMS + "promotion_state_valid"
        )

    def test_a_request_time_exists_exactly_while_a_promotion_is_pending(self):
        self.assertEqual(
            self.try_item(promotion_state="pending"),
            ITEMS + "promotion_requested_matches_state",
        )
        for state in ("none", "promoted", "rejected"):
            with self.subTest(state):
                self.assertEqual(
                    self.try_item(
                        promotion_state=state, promotion_requested_at=CREATED
                    ),
                    ITEMS + "promotion_requested_matches_state",
                )

    def test_source_metadata_is_a_bounded_json_object(self):
        self.assertIsNone(self.try_item(source_metadata={"url": "https://example.org"}))
        for bad in ([], [1], "text", 1, None):
            with self.subTest(bad):
                self.assertEqual(
                    self.try_item(source_metadata=bad),
                    ITEMS + "source_metadata_object",
                )

    def test_the_source_metadata_size_backstop_is_65536_bytes_of_text(self):
        # The text form of {"k": "<n x a>"} is n + 9 characters.
        self.assertIsNone(self.try_item(source_metadata={"k": "a" * (65536 - 9)}))
        self.assertEqual(
            self.try_item(source_metadata={"k": "a" * (65536 - 8)}),
            ITEMS + "source_metadata_size",
        )

    def test_required_columns_are_not_null(self):
        for column in ("project_id", "created_by"):
            with self.subTest(column):
                self.assertEqual(self.null_violation(column), column)


class TaskRelationTest(ScratchSchemaTestCase):
    def test_project_and_task_relations_are_stored(self):
        project_id = uuid4()
        task_id = self.add_task(project_id)

        item_id = self.add_item(project_id=project_id, task_id=task_id)

        row = self.item_row(item_id)
        self.assertEqual(row["project_id"], project_id)
        self.assertEqual(row["task_id"], task_id)

    def test_the_task_id_is_a_plain_uuid_that_the_database_does_not_check(self):
        # Decision 0013: there is no foreign key. That a task exists (and belongs
        # to the project) is checked by ``ScratchStore.add`` under a row lock
        # (``test_scratch_store_items``); a row written by other means keeps the
        # id as it was written.
        unknown = uuid4()

        item_id = self.add_item(task_id=unknown)

        self.assertEqual(self.item_row(item_id)["task_id"], unknown)

    def test_deleting_a_task_is_not_blocked_and_keeps_the_item_relation(self):
        project_id = uuid4()
        task_id = self.add_task(project_id)
        item_id = self.add_item(project_id=project_id, task_id=task_id)
        pinned_id = self.add_item(project_id=project_id, task_id=task_id, pinned=True)

        result = self.session.execute(
            text("DELETE FROM tasks WHERE id = :id"), {"id": task_id}
        )

        self.assertEqual(result.rowcount, 1)
        for kept in (item_id, pinned_id):
            with self.subTest(item=kept):
                row = self.item_row(kept)
                self.assertEqual(row["task_id"], task_id)
                self.assertEqual(row["project_id"], project_id)
        self.assertTrue(self.item_row(pinned_id)["pinned"])

    def test_the_items_table_has_no_foreign_key_at_all(self):
        columns = self.connection.execute(
            text(
                "SELECT DISTINCT a.attname FROM pg_constraint con"
                " JOIN pg_attribute a ON a.attrelid = con.conrelid"
                "   AND a.attnum = ANY (con.conkey)"
                " WHERE con.contype = 'f'"
                "   AND con.conrelid = 'research_scratch_items'::regclass"
            )
        ).scalars()
        self.assertEqual(set(columns), set())


class LeaseConstraintTest(ScratchSchemaTestCase):
    def test_a_lease_lasts_more_than_zero_and_at_most_one_hour(self):
        item_id = self.add_item()

        self.assertIsNone(
            self.try_lease(item_id, expires_at=CREATED + timedelta(microseconds=1))
        )
        self.assertIsNone(
            self.try_lease(item_id, expires_at=CREATED + timedelta(hours=1))
        )
        self.assertEqual(
            self.try_lease(item_id, expires_at=CREATED),
            LEASES + "lease_window",
        )
        self.assertEqual(
            self.try_lease(item_id, expires_at=CREATED - timedelta(seconds=1)),
            LEASES + "lease_window",
        )
        self.assertEqual(
            self.try_lease(
                item_id, expires_at=CREATED + timedelta(hours=1, microseconds=1)
            ),
            LEASES + "lease_window",
        )

    def test_a_holder_has_one_lease_per_item(self):
        item_id = self.add_item()
        holder = self.add_lease(item_id)

        self.assertEqual(
            self.try_lease(item_id, holder_id=holder), "pk_research_scratch_leases"
        )
        other_item = self.add_item()
        self.assertIsNone(self.try_lease(other_item, holder_id=holder))

    def test_a_lease_needs_an_existing_item(self):
        self.assertEqual(
            self.try_lease(uuid4()),
            "fk_research_scratch_leases_item_id_research_scratch_items",
        )

    def test_deleting_an_item_deletes_its_leases(self):
        item_id = self.add_item()
        self.add_lease(item_id)
        self.add_lease(item_id)
        keep = self.add_item()
        self.add_lease(keep)

        self.session.execute(delete(ScratchItemRow).where(ScratchItemRow.id == item_id))

        remaining = (
            self.session.execute(text("SELECT item_id FROM research_scratch_leases"))
            .scalars()
            .all()
        )
        self.assertEqual(remaining, [keep])


class SeparationFromLongTermMemoryTest(ScratchSchemaTestCase):
    """Research Scratch is separate from Long-term Memory (the acceptance criterion)."""

    SCRATCH = {"research_scratch_items", "research_scratch_leases"}

    def foreign_keys(self):
        return self.connection.execute(
            text(
                "SELECT child.relname, parent.relname, con.confdeltype"
                " FROM pg_constraint con"
                " JOIN pg_class child ON child.oid = con.conrelid"
                " JOIN pg_class parent ON parent.oid = con.confrelid"
                " WHERE con.contype = 'f'"
                "   AND child.relnamespace = 'public'::regnamespace"
            )
        ).all()

    def test_the_only_foreign_key_of_the_scratch_tables_is_lease_to_item(self):
        edges = {
            (child, parent): action
            for child, parent, action in self.foreign_keys()
            if child in self.SCRATCH or parent in self.SCRATCH
        }

        # 'c' = ON DELETE CASCADE. No foreign key leads to ``tasks`` (0013).
        self.assertEqual(
            edges, {("research_scratch_leases", "research_scratch_items"): "c"}
        )

    def test_no_foreign_key_connects_scratch_and_any_memory_or_conversation_table(self):
        memory_side = {
            "conversations",
            "messages",
            "session_states",
            "memories",
            "memory_versions",
            "memory_relations",
            "memory_sources",
            "memory_embeddings",
        }

        for child, parent, _ in self.foreign_keys():
            with self.subTest(child=child, parent=parent):
                self.assertFalse(
                    (child in self.SCRATCH and parent in memory_side)
                    or (parent in self.SCRATCH and child in memory_side)
                )

    def test_deleting_scratch_rows_leaves_long_term_memory_untouched(self):
        memory_id = self.add_memory()
        self.add_version(memory_id)
        item_id = self.add_item()
        self.add_lease(item_id)

        self.session.execute(delete(ScratchItemRow))

        counts = {
            table: self.session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            for table in ("memories", "memory_versions")
        }
        self.assertEqual(counts, {"memories": 1, "memory_versions": 1})

    def test_the_scratch_tables_hold_no_memory_reference_column(self):
        columns = set(
            self.connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = 'public'"
                    "   AND table_name IN ('research_scratch_items',"
                    "                      'research_scratch_leases')"
                )
            ).scalars()
        )

        self.assertFalse(
            {name for name in columns if "memory" in name or "memories" in name}
        )
