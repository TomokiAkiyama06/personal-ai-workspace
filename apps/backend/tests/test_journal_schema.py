"""Every constraint of the three journal tables, tried on the migrated schema.

Real PostgreSQL (skipped unless ``PAW_TEST_DATABASE_URL`` is set). One rolled-back
transaction per test; a violation is asserted by the name of the constraint the
server reports.
"""

import unittest
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from paw_backend.memory.journal import limits
from paw_backend.memory.journal.models import TABLE_NAMES

from .memory_support import (
    FOREIGN_KEYS_WITHOUT_INDEX,
    MemoryDatabaseTestCase,
    requires_postgres,
)

ENTRIES = "memory_journal_entries"
JOBS = "memory_consolidation_queue"
KEYS = "memory_consolidation_keys"


@requires_postgres
class JournalSchemaTestCase(MemoryDatabaseTestCase):
    def sql(self, statement: str, **params: Any):
        return self.session.execute(text(statement), params)

    def add_entry(self, conversation=None, sequence=0, message=None, **overrides: Any):
        """A message and its journal entry; returns (entry id, conversation id)."""
        conversation = conversation or self.add_conversation()
        message = message or self.add_message(conversation, sequence)
        values: dict[str, Any] = {
            "conversation_id": conversation,
            "message_id": message,
            "turn_id": uuid4(),
            "event_sequence": sequence,
            "owner_user_id": uuid4(),
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        entry = self.sql(
            f"INSERT INTO {ENTRIES} ({columns}) VALUES ({placeholders}) RETURNING id",
            **values,
        ).scalar_one()
        return entry, conversation

    def add_job(self, entry_id, **overrides: Any):
        values: dict[str, Any] = {
            "entry_id": entry_id,
            "priority": "normal",
            "priority_rank": 1,
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        return self.sql(
            f"INSERT INTO {JOBS} ({columns}) VALUES ({placeholders}) RETURNING id",
            **values,
        ).scalar_one()

    def name(self, table: str, short: str) -> str:
        return f"ck_{table}_{short}"


class SchemaShapeTest(JournalSchemaTestCase):
    def test_the_three_tables_exist(self):
        found = set(
            self.sql(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).scalars()
        )
        self.assertTrue(set(TABLE_NAMES) <= found)
        self.assertEqual(set(TABLE_NAMES), {ENTRIES, JOBS, KEYS})

    def test_every_foreign_key_has_an_index_that_leads_with_its_column(self):
        rows = self.sql(FOREIGN_KEYS_WITHOUT_INDEX, tables=list(TABLE_NAMES)).scalars()
        self.assertEqual(list(rows), [])


class JournalEntryConstraintTest(JournalSchemaTestCase):
    def test_a_new_entry_is_pending_with_no_outcome(self):
        entry, _ = self.add_entry()
        row = (
            self.sql(f"SELECT * FROM {ENTRIES} WHERE id = :i", i=entry).mappings().one()
        )
        self.assertEqual(
            (row["state"], row["consolidated_at"], row["outcome"], row["project_id"]),
            ("pending", None, None, None),
        )
        self.assertIsNotNone(row["recorded_at"])

    def test_the_event_sequence_is_not_negative_and_unique_per_conversation(self):
        conversation = self.add_conversation()
        message = self.add_message(conversation, 3)
        common = dict(
            conversation_id=conversation,
            message_id=message,
            turn_id=uuid4(),
            owner_user_id=uuid4(),
        )
        self.assertEqual(
            self.violation(
                lambda: self.sql(
                    f"INSERT INTO {ENTRIES} (conversation_id, message_id, turn_id,"
                    " event_sequence, owner_user_id) VALUES (:conversation_id,"
                    " :message_id, :turn_id, -1, :owner_user_id)",
                    **common,
                )
            ),
            self.name(ENTRIES, "event_sequence_not_negative"),
        )
        self.add_entry(conversation, 3, message=message)
        other_message = self.add_message(conversation, 4)
        self.assertEqual(
            self.violation(
                lambda: self.sql(
                    f"INSERT INTO {ENTRIES} (conversation_id, message_id, turn_id,"
                    " event_sequence, owner_user_id) VALUES (:c, :m, :t, 3, :o)",
                    c=conversation,
                    m=other_message,
                    t=uuid4(),
                    o=uuid4(),
                )
            ),
            f"uq_{ENTRIES}_conversation_id",
        )

    def test_one_entry_per_message(self):
        entry, conversation = self.add_entry()
        message = self.sql(f"SELECT message_id FROM {ENTRIES}").scalar_one()
        self.assertEqual(
            self.violation(
                lambda: self.sql(
                    f"INSERT INTO {ENTRIES} (conversation_id, message_id, turn_id,"
                    " event_sequence, owner_user_id) VALUES (:c, :m, :t, 9, :o)",
                    c=conversation,
                    m=message,
                    t=uuid4(),
                    o=uuid4(),
                )
            ),
            f"uq_{ENTRIES}_message_id",
        )
        self.assertIsNotNone(entry)

    def test_the_message_must_belong_to_the_conversation(self):
        first, second = self.add_conversation(), self.add_conversation()
        message_of_second = self.add_message(second, 0)
        self.assertEqual(
            self.violation(
                lambda: self.sql(
                    f"INSERT INTO {ENTRIES} (conversation_id, message_id, turn_id,"
                    " event_sequence, owner_user_id) VALUES (:c, :m, :t, 0, :o)",
                    c=first,
                    m=message_of_second,
                    t=uuid4(),
                    o=uuid4(),
                )
            ),
            f"fk_{ENTRIES}_conversation_id_messages",
        )

    def test_the_states_are_a_closed_set_and_agree_with_their_columns(self):
        entry, _ = self.add_entry()

        def update(**values):
            sets = ", ".join(f"{name} = :{name}" for name in values)
            return lambda: self.sql(
                f"UPDATE {ENTRIES} SET {sets} WHERE id = :i", i=entry, **values
            )

        self.assertEqual(
            self.violation(update(state="bogus")), self.name(ENTRIES, "state_valid")
        )
        # consolidated needs its time and its outcome ...
        self.assertIn(
            self.violation(update(state="consolidated")),
            {
                self.name(ENTRIES, "consolidated_has_time"),
                self.name(ENTRIES, "consolidated_has_outcome"),
            },
        )
        # ... and a pending entry has neither.
        self.assertEqual(
            self.violation(update(consolidated_at="2030-01-01T00:00:00+00:00")),
            self.name(ENTRIES, "consolidated_has_time"),
        )
        self.assertEqual(
            self.violation(update(outcome='{"items": []}')),
            self.name(ENTRIES, "consolidated_has_outcome"),
        )
        self.assertIsNone(
            self.violation(
                update(
                    state="consolidated",
                    consolidated_at="2030-01-01T00:00:00+00:00",
                    outcome='{"items": []}',
                )
            )
        )

    def test_the_outcome_is_a_json_object(self):
        entry, _ = self.add_entry()
        for outcome in ("[]", '"text"', "1", "null"):
            with self.subTest(outcome):
                self.assertIsNotNone(
                    self.violation(
                        lambda outcome=outcome: self.sql(
                            f"UPDATE {ENTRIES} SET state = 'consolidated',"
                            " consolidated_at = now(),"
                            " outcome = CAST(:o AS jsonb) WHERE id = :i",
                            o=outcome,
                            i=entry,
                        )
                    )
                )

    def test_deleting_the_conversation_deletes_its_entries_and_jobs(self):
        entry, conversation = self.add_entry()
        self.add_job(entry)
        self.sql("DELETE FROM conversations WHERE id = :c", c=conversation)
        self.assertEqual(self.sql(f"SELECT count(*) FROM {ENTRIES}").scalar_one(), 0)
        self.assertEqual(self.sql(f"SELECT count(*) FROM {JOBS}").scalar_one(), 0)


class QueueConstraintTest(JournalSchemaTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.entry, _ = self.add_entry()

    def violated(self, **overrides: Any) -> str | None:
        entry, _ = self.add_entry()
        return self.violation(lambda: self.add_job(entry, **overrides))

    def test_a_new_job_is_queued_with_zero_counters(self):
        job = self.add_job(self.entry)
        row = self.sql(f"SELECT * FROM {JOBS} WHERE id = :i", i=job).mappings().one()
        self.assertEqual(
            (
                row["status"],
                row["attempts"],
                row["deferrals"],
                row["claim_count"],
                row["claimed_by"],
                row["lease_expires_at"],
                row["last_failure"],
                row["finished_at"],
            ),
            ("queued", 0, 0, 0, None, None, None, None),
        )
        self.assertIsNotNone(row["enqueued_at"])
        self.assertIsNotNone(row["available_at"])

    def test_the_status_and_the_priority_are_closed_sets(self):
        self.assertEqual(self.violated(status="bogus"), self.name(JOBS, "status_valid"))
        # A priority that is not one of the three fails its own check and, having no
        # rank that fits it, the rank check as well: the server names one of them.
        self.assertIn(
            self.violated(priority="urgent", priority_rank=0),
            {
                self.name(JOBS, "priority_valid"),
                self.name(JOBS, "priority_rank_matches_priority"),
            },
        )
        finished = {"finished_at": "2030-01-01T00:00:00+00:00"}
        self.assertIsNone(self.violated(status="queued"))
        self.assertIsNone(self.violated(status="completed", **finished))

    def test_the_rank_follows_the_priority(self):
        for priority, rank in (("high", 0), ("normal", 1), ("low", 2)):
            self.assertIsNone(self.violated(priority=priority, priority_rank=rank))
            for wrong in {0, 1, 2, 3} - {rank}:
                with self.subTest(priority=priority, wrong=wrong):
                    self.assertEqual(
                        self.violated(priority=priority, priority_rank=wrong),
                        self.name(JOBS, "priority_rank_matches_priority"),
                    )

    def test_the_last_failure_is_a_closed_set(self):
        for failure in (
            "worker_unavailable",
            "worker_timeout",
            "worker_error",
            "worker_output_invalid",
            "apply_failed",
        ):
            self.assertIsNone(self.violated(last_failure=failure))
        self.assertEqual(
            self.violated(last_failure="the message text"),
            self.name(JOBS, "last_failure_valid"),
        )

    def test_the_counters_are_not_negative(self):
        for column in ("attempts", "deferrals", "claim_count"):
            with self.subTest(column):
                self.assertEqual(
                    self.violated(**{column: -1}),
                    self.name(JOBS, f"{column}_not_negative"),
                )

    def test_a_lease_exists_exactly_while_the_job_is_claimed(self):
        claimed = dict(
            status="claimed",
            claimed_by="worker-1",
            claimed_at="2030-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            self.violated(**claimed), self.name(JOBS, "lease_matches_status")
        )
        self.assertIsNone(
            self.violated(**claimed, lease_expires_at="2030-01-01T00:05:00+00:00")
        )
        self.assertEqual(
            self.violated(lease_expires_at="2030-01-01T00:05:00+00:00"),
            self.name(JOBS, "lease_matches_status"),
        )

    def test_a_claimed_job_names_its_worker_and_a_queued_one_has_none(self):
        lease = "2030-01-01T00:05:00+00:00"
        self.assertEqual(
            self.violated(status="claimed", lease_expires_at=lease),
            self.name(JOBS, "claimed_has_worker"),
        )
        self.assertEqual(
            self.violated(
                claimed_by="worker-1", claimed_at="2030-01-01T00:00:00+00:00"
            ),
            self.name(JOBS, "queued_has_no_worker"),
        )

    def test_the_lease_ends_after_the_claim(self):
        self.assertEqual(
            self.violated(
                status="claimed",
                claimed_by="w",
                claimed_at="2030-01-01T00:05:00+00:00",
                lease_expires_at="2030-01-01T00:05:00+00:00",
            ),
            self.name(JOBS, "lease_after_claim"),
        )

    def test_finished_exactly_when_completed_or_dead(self):
        finished = "2030-01-01T00:00:00+00:00"
        self.assertEqual(
            self.violated(status="completed"),
            self.name(JOBS, "finished_matches_status"),
        )
        self.assertEqual(
            self.violated(finished_at=finished),
            self.name(JOBS, "finished_matches_status"),
        )
        self.assertIsNone(
            self.violated(status="dead", attempts=1, finished_at=finished)
        )

    def test_a_dead_job_has_failed_at_least_once(self):
        self.assertEqual(
            self.violated(status="dead", finished_at="2030-01-01T00:00:00+00:00"),
            self.name(JOBS, "dead_has_failed_attempts"),
        )

    def test_an_entry_has_at_most_one_active_job(self):
        self.add_job(self.entry)
        claimed = {
            "status": "claimed",
            "claimed_by": "w",
            "claimed_at": "2030-01-01T00:00:00+00:00",
            "lease_expires_at": "2030-01-01T00:05:00+00:00",
        }
        for kind, extra in (("queued", {}), ("claimed", claimed)):
            with self.subTest(kind):
                self.assertEqual(
                    self.violation(
                        lambda extra=extra: self.add_job(self.entry, **extra)
                    ),
                    "uq_memory_consolidation_queue_one_active_per_entry",
                )

    def test_a_finished_job_does_not_block_a_new_one(self):
        first = self.add_job(self.entry)
        self.sql(
            f"UPDATE {JOBS} SET status = 'dead', attempts = 1, finished_at = now()"
            " WHERE id = :i",
            i=first,
        )
        second = self.add_job(self.entry)
        self.sql(
            f"UPDATE {JOBS} SET status = 'completed', finished_at = now()"
            " WHERE id = :i",
            i=second,
        )
        self.assertIsNotNone(self.add_job(self.entry))

    def test_the_entry_must_exist(self):
        self.assertEqual(
            self.violation(lambda: self.add_job(uuid4())),
            f"fk_{JOBS}_entry_id_{ENTRIES}",
        )

    def test_the_claim_order_index_serves_the_claim_query(self):
        for _ in range(3):
            entry, _ = self.add_entry(self.add_conversation())
            self.add_job(entry)
        plan = "\n".join(
            self.session.execute(
                text(
                    f"EXPLAIN SELECT id FROM {JOBS}"
                    " WHERE status IN ('queued', 'claimed')"
                    " ORDER BY priority_rank, enqueued_at, id LIMIT 1"
                )
            ).scalars()
        )
        self.assertIn(JOBS, plan)


class KeyConstraintTest(JournalSchemaTestCase):
    def add_key(self, memory_id=None, owner=None, key="indent_style", **overrides):
        values: dict[str, Any] = {
            "owner_user_id": owner or uuid4(),
            "key": key,
            "memory_id": memory_id or self.add_memory(),
            "applied_conversation_id": uuid4(),
            "applied_event_sequence": 0,
            "applied_recorded_at": "2030-01-01T00:00:00+00:00",
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        self.sql(f"INSERT INTO {KEYS} ({columns}) VALUES ({placeholders})", **values)
        return values

    def test_the_key_length_boundaries(self):
        cases = (
            (1, None),
            (limits.MAX_KEY_CHARS, None),
            (limits.MAX_KEY_CHARS + 1, self.name(KEYS, "key_length")),
            (0, self.name(KEYS, "key_length")),
        )
        for length, expected in cases:
            with self.subTest(length=length):
                violated = self.violation(
                    lambda length=length: self.add_key(key="k" * length)
                )
                self.assertEqual(violated, expected)

    def test_the_guard_sequence_is_not_negative(self):
        self.assertEqual(
            self.violation(lambda: self.add_key(applied_event_sequence=-1)),
            self.name(KEYS, "applied_sequence_not_negative"),
        )

    def test_a_key_names_one_memory_per_owner_and_a_memory_has_one_key(self):
        owner, memory = uuid4(), self.add_memory()
        self.add_key(memory, owner, "a")
        self.assertEqual(
            self.violation(lambda: self.add_key(self.add_memory(), owner, "a")),
            f"pk_{KEYS}",
        )
        self.assertEqual(
            self.violation(lambda: self.add_key(memory, uuid4(), "b")),
            f"uq_{KEYS}_memory_id",
        )
        # The same key of another owner is another memory's key: fine.
        self.assertIsNone(self.violation(lambda: self.add_key(None, uuid4(), "a")))

    def test_the_memory_must_exist_and_the_key_goes_with_it(self):
        self.assertEqual(
            self.violation(lambda: self.add_key(uuid4())),
            f"fk_{KEYS}_memory_id_memories",
        )
        memory = self.add_memory()
        self.add_key(memory)
        self.sql("DELETE FROM memories WHERE id = :m", m=memory)
        self.assertEqual(self.sql(f"SELECT count(*) FROM {KEYS}").scalar_one(), 0)


if __name__ == "__main__":
    unittest.main()
