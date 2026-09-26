"""History of the in-place metadata edits (pin, importance) of a memory version.

REQUIREMENTS.md "Manual Memory Editing": the low-risk metadata (Pin, Importance)
takes effect at once, but the change history is kept. The columns stay updatable
in place; a trigger records every change (old and new value, who) in the
append-only ``memory_metadata_changes``. The actor is named by the writer with
``paw_backend.memory.metadata.metadata_change_actor`` and the write is refused
without one. The same trigger records ``status`` and ``stale_since`` since
revision 0071: ``tests/test_memory_status_history.py``.

The first class needs no server; the others need PostgreSQL (skipped unless
``PAW_TEST_DATABASE_URL`` is set).
"""

import unittest
from functools import partial
from uuid import UUID, uuid4

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from paw_backend.memory.acl import Principal, readable_memory_versions
from paw_backend.memory.metadata import (
    ACTOR_TYPE_SETTING,
    ACTOR_USER_ID_SETTING,
    metadata_change_actor,
)
from paw_backend.memory.models import (
    ActorType,
    Memory,
    MemoryMetadataChange,
    MemoryVersion,
)

from .memory_support import MemoryDatabaseTestCase, requires_postgres

ACTOR_REQUIRED = "actor_type"  # the NOT NULL column the trigger cannot fill


class ActorStatementTest(unittest.TestCase):
    def sql(self, *arguments) -> str:
        statement = metadata_change_actor(*arguments)
        return str(statement.compile(compile_kwargs={"literal_binds": True}))

    def expected(self, actor_type: str, user_id: str) -> str:
        # ``true``: the setting is local to the transaction.
        return (
            f"SELECT set_config('{ACTOR_TYPE_SETTING}', '{actor_type}', true)"
            f" AS set_config_1, set_config('{ACTOR_USER_ID_SETTING}',"
            f" '{user_id}', true) AS set_config_2"
        )

    def test_a_user_is_named_with_the_user_id(self):
        user = uuid4()

        self.assertEqual(
            self.sql(ActorType.USER, user), self.expected("user", str(user))
        )

    def test_the_actor_type_may_be_given_as_a_string(self):
        user = uuid4()

        self.assertEqual(self.sql("user", user), self.expected("user", str(user)))

    def test_an_agent_or_the_system_needs_no_user_id(self):
        # The empty string clears a user id set earlier in the transaction.
        for actor_type in ("agent", "system"):
            with self.subTest(actor_type):
                self.assertEqual(self.sql(actor_type), self.expected(actor_type, ""))

    def test_a_user_without_an_id_is_refused(self):
        with self.assertRaises(ValueError):
            metadata_change_actor(ActorType.USER)

    def test_an_unknown_actor_type_is_refused_without_echoing_it(self):
        with self.assertRaises(ValueError) as caught:
            metadata_change_actor("robot-9000")

        self.assertNotIn("robot-9000", str(caught.exception))

    def test_the_user_id_must_be_a_uuid(self):
        for value in ("a3b1c2d4-0000-4000-8000-000000000000", 7, b"x"):
            with self.subTest(repr(value)):
                with self.assertRaises(TypeError):
                    metadata_change_actor("user", value)  # type: ignore[arg-type]


class MetadataHistoryBase(MemoryDatabaseTestCase):
    def act_as(self, actor_type: str = "system", user_id: UUID | None = None) -> None:
        self.session.execute(metadata_change_actor(actor_type, user_id))

    def edit(self, version: UUID, **values) -> None:
        self.session.execute(
            update(MemoryVersion).where(MemoryVersion.id == version).values(**values)
        )

    def history(self, version: UUID) -> list[tuple]:
        rows = self.session.execute(
            select(
                MemoryMetadataChange.old_pinned,
                MemoryMetadataChange.new_pinned,
                MemoryMetadataChange.old_importance,
                MemoryMetadataChange.new_importance,
                MemoryMetadataChange.actor_type,
                MemoryMetadataChange.actor_user_id,
            )
            .where(MemoryMetadataChange.memory_version_id == version)
            .order_by(MemoryMetadataChange.created_at)
        )
        return [tuple(row) for row in rows]

    def refused_column(self, action) -> str | None:
        """Run ``action``; the column a NOT NULL violation names, else None."""
        try:
            with self.session.begin_nested():
                action()
        except IntegrityError as error:
            return error.orig.diag.column_name
        return None


@requires_postgres
class MetadataHistoryTest(MetadataHistoryBase):
    def test_a_pin_change_records_the_old_and_new_value_and_the_actor(self):
        user = uuid4()
        version = self.add_version(self.add_memory())
        self.act_as("user", user)

        self.edit(version, pinned=True)

        self.assertEqual(self.history(version), [(False, True, 50, 50, "user", user)])
        stored = self.session.execute(
            select(MemoryVersion.pinned).where(MemoryVersion.id == version)
        ).scalar_one()
        self.assertTrue(stored)

    def test_an_importance_change_records_the_old_and_new_value(self):
        version = self.add_version(self.add_memory())
        self.act_as("agent")

        self.edit(version, importance=80)

        self.assertEqual(self.history(version), [(False, False, 50, 80, "agent", None)])

    def test_one_update_of_both_columns_is_one_change(self):
        version = self.add_version(self.add_memory())
        self.act_as("system")

        self.edit(version, pinned=True, importance=10)

        self.assertEqual(self.history(version), [(False, True, 50, 10, "system", None)])

    def test_every_change_of_a_version_is_kept_in_order(self):
        first, second = uuid4(), uuid4()
        version = self.add_version(self.add_memory())
        self.act_as("user", first)
        self.edit(version, pinned=True)
        self.act_as("user", second)
        self.edit(version, importance=70)
        self.edit(version, pinned=False)

        self.assertEqual(
            self.history(version),
            [
                (False, True, 50, 50, "user", first),
                (True, True, 50, 70, "user", second),
                (True, False, 70, 70, "user", second),
            ],
        )

    def test_writing_the_value_that_is_already_there_records_nothing(self):
        version = self.add_version(self.add_memory())
        self.act_as("system")

        self.edit(version, pinned=False, importance=50)

        self.assertEqual(self.history(version), [])

    def test_status_and_stale_since_need_an_actor_like_the_pin(self):
        # Before revision 0071 these two columns were overwritten without a
        # trace (and without an actor); ``tests/test_memory_status_history.py``
        # covers what is recorded now, this is the rule they share with the pin.
        version = self.add_version(self.add_memory())

        for values in ({"status": "superseded"}, {"stale_since": text("now()")}):
            with self.subTest(list(values)):
                column = self.refused_column(lambda v=values: self.edit(version, **v))
                self.assertEqual(column, ACTOR_REQUIRED)
        self.assertEqual(self.history(version), [])

    def test_each_version_has_its_own_history(self):
        memory = self.add_memory()
        old = self.add_version(memory, version_number=1, status="superseded")
        current = self.add_version(memory, version_number=2)
        self.act_as("system")

        self.edit(current, importance=90)

        self.assertEqual(self.history(old), [])
        self.assertEqual(
            self.history(current), [(False, False, 50, 90, "system", None)]
        )

    def test_a_change_without_a_named_actor_is_refused_and_not_applied(self):
        version = self.add_version(self.add_memory())

        column = self.refused_column(lambda: self.edit(version, pinned=True))

        self.assertEqual(column, ACTOR_REQUIRED)
        self.assertEqual(self.history(version), [])
        pinned = self.session.execute(
            select(MemoryVersion.pinned).where(MemoryVersion.id == version)
        ).scalar_one()
        self.assertFalse(pinned)

    def test_the_actor_can_be_replaced_within_a_transaction(self):
        version = self.add_version(self.add_memory())
        user = uuid4()
        self.act_as("user", user)
        self.edit(version, pinned=True)
        self.act_as("system")  # no user id: the earlier one must not stay

        self.edit(version, importance=60)

        self.assertEqual(
            [row[4:] for row in self.history(version)],
            [("user", user), ("system", None)],
        )

    def test_the_database_refuses_an_actor_that_does_not_fit(self):
        version = self.add_version(self.add_memory())
        cases = {
            # A user without an id (the helper cannot build this).
            "ck_memory_metadata_changes_user_actor_has_id": ("user", ""),
            "ck_memory_metadata_changes_actor_type_valid": ("robot", ""),
        }
        for expected, (actor_type, user_id) in cases.items():
            with self.subTest(expected):
                self.session.execute(
                    text(
                        "SELECT set_config(:type_key, :type, true),"
                        " set_config(:user_key, :user, true)"
                    ),
                    {
                        "type_key": ACTOR_TYPE_SETTING,
                        "type": actor_type,
                        "user_key": ACTOR_USER_ID_SETTING,
                        "user": user_id,
                    },
                )

                self.assertEqual(
                    self.violation(lambda: self.edit(version, pinned=True)), expected
                )
        self.assertEqual(self.history(version), [])

    def test_the_history_goes_with_its_memory(self):
        memory = self.add_memory()
        version = self.add_version(memory)
        self.act_as("system")
        self.edit(version, pinned=True)

        self.session.execute(delete(Memory).where(Memory.id == memory))

        self.assertEqual(self.history(version), [])

    def test_the_history_of_a_version_is_filtered_by_its_acl(self):
        alice, bob = uuid4(), uuid4()
        mine = self.add_version(self.add_memory(), owner_user_id=alice)
        theirs = self.add_version(self.add_memory(), owner_user_id=bob)
        self.act_as("system")
        self.edit(mine, importance=70)
        self.edit(theirs, importance=20)

        def visible_to(principal: Principal) -> list[tuple[int, int]]:
            rows = self.session.execute(
                select(
                    MemoryMetadataChange.old_importance,
                    MemoryMetadataChange.new_importance,
                )
                .join(
                    MemoryVersion,
                    MemoryVersion.id == MemoryMetadataChange.memory_version_id,
                )
                .where(readable_memory_versions(principal))
            )
            return [tuple(row) for row in rows]

        self.assertEqual(visible_to(Principal(alice)), [(50, 70)])
        self.assertEqual(visible_to(Principal(bob)), [(50, 20)])
        self.assertEqual(visible_to(Principal(uuid4())), [])


@requires_postgres
class MetadataHistoryConstraintTest(MetadataHistoryBase):
    """The history table's own rules, with rows inserted directly."""

    def add_change(self, version: UUID, **overrides):
        values = {
            "memory_version_id": version,
            "old_pinned": False,
            "new_pinned": True,
            "old_importance": 50,
            "new_importance": 50,
            "actor_type": "system",
        }
        values.update(overrides)
        return self.session.execute(insert(MemoryMetadataChange).values(**values))

    def test_a_row_must_describe_a_valid_change(self):
        version = self.add_version(self.add_memory())
        cases = {
            "ck_memory_metadata_changes_importance_range": {"new_importance": 101},
            "ck_memory_metadata_changes_something_changed": {
                "new_pinned": False,
                "new_importance": 50,
            },
            "ck_memory_metadata_changes_actor_type_valid": {"actor_type": "robot"},
            "ck_memory_metadata_changes_user_actor_has_id": {"actor_type": "user"},
            "fk_memory_metadata_changes_memory_version_id_memory_versions": {
                "memory_version_id": uuid4()
            },
        }
        for expected, overrides in cases.items():
            with self.subTest(expected):
                action = partial(self.add_change, version, **overrides)
                self.assertEqual(self.violation(action), expected)
        self.assertIsNone(self.violation(lambda: self.add_change(version)))

    def test_the_history_of_a_version_is_read_by_an_index(self):
        version = self.add_version(self.add_memory())
        self.session.execute(text("SET LOCAL enable_seqscan = off"))

        plan = "\n".join(
            self.session.execute(
                text(
                    "EXPLAIN SELECT 1 FROM memory_metadata_changes"
                    " WHERE memory_version_id = :id"
                ),
                {"id": version},
            ).scalars()
        )

        self.assertIn("ix_memory_metadata_changes_memory_version_id_created_at", plan)


@requires_postgres
class CommittedMetadataTest(MemoryDatabaseTestCase):
    """The actor of one transaction must not leak into the next (real commits).

    ``set_config(..., is_local => true)`` lasts until the end of the
    transaction, so a pooled connection never carries an actor over to the next
    request. The other tests run in one rolled-back transaction and cannot show
    that.
    """

    def setUp(self) -> None:
        self.memory = uuid4()
        self.version = uuid4()
        with self.engine.begin() as connection:
            connection.execute(insert(Memory).values(id=self.memory))
            connection.execute(
                insert(MemoryVersion).values(
                    id=self.version,
                    memory_id=self.memory,
                    version_number=1,
                    scope="user",
                    owner_user_id=uuid4(),
                    memory_type="preference",
                    title="Prefers tabs",
                    content="Use tabs.",
                    status="active",
                    confirmation_state="confirmed",
                    freshness_policy="permanent",
                    actor_type="system",
                )
            )
        self.addCleanup(self.remove_rows)

    def remove_rows(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(delete(Memory).where(Memory.id == self.memory))

    def edit(self, connection, **values) -> None:
        connection.execute(
            update(MemoryVersion)
            .where(MemoryVersion.id == self.version)
            .values(**values)
        )

    def test_the_actor_does_not_outlive_its_transaction(self):
        user = uuid4()
        with self.engine.begin() as connection:
            connection.execute(metadata_change_actor("user", user))
            self.edit(connection, pinned=True)

        with self.assertRaises(IntegrityError) as caught:
            with self.engine.begin() as connection:  # same pooled connection
                self.edit(connection, importance=70)

        self.assertEqual(caught.exception.orig.diag.column_name, ACTOR_REQUIRED)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    MemoryMetadataChange.new_pinned,
                    MemoryMetadataChange.new_importance,
                    MemoryMetadataChange.actor_user_id,
                ).where(MemoryMetadataChange.memory_version_id == self.version)
            ).all()
            importance = connection.execute(
                select(MemoryVersion.importance).where(MemoryVersion.id == self.version)
            ).scalar_one()
        self.assertEqual([tuple(row) for row in rows], [(True, 50, user)])
        self.assertEqual(importance, 50)


if __name__ == "__main__":
    unittest.main()
