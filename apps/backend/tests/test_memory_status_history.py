"""History of the status and stale-state changes of a memory version (issue #90).

``memory_versions.status`` (superseded / deprecated / history) and
``stale_since`` (a stale candidate is marked, later cleared) change in place, like
``pinned`` and ``importance``. They used to be overwritten without a trace, so the
history graph could not say when or by whom a version was deprecated or marked
stale. Revision 0071 makes the trigger of revision 0040 record them too, in the
same append-only ``memory_metadata_changes``, with the actor the writer names
(``metadata_change_actor``) and the database clock as the change time.

Every test needs PostgreSQL (skipped unless ``PAW_TEST_DATABASE_URL`` is set).
"""

import threading
import time
from datetime import datetime
from functools import partial
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from paw_backend.memory.metadata import metadata_change_actor
from paw_backend.memory.models import Memory, MemoryMetadataChange, MemoryVersion

from .memory_support import MemoryDatabaseTestCase, requires_postgres, utc

ACTOR_REQUIRED = "actor_type"  # the NOT NULL column the trigger cannot fill
FIRST, SECOND = utc(2026, 9, 1), utc(2026, 9, 2)
DEADLINE_SECONDS = 30  # generous: only a hang can reach it

COLUMNS = (
    MemoryMetadataChange.old_pinned,
    MemoryMetadataChange.new_pinned,
    MemoryMetadataChange.old_importance,
    MemoryMetadataChange.new_importance,
    MemoryMetadataChange.old_status,
    MemoryMetadataChange.new_status,
    MemoryMetadataChange.old_stale_since,
    MemoryMetadataChange.new_stale_since,
    MemoryMetadataChange.actor_type,
    MemoryMetadataChange.actor_user_id,
)


def change(
    *,
    status: tuple[str, str] = ("active", "active"),
    stale: tuple[datetime | None, datetime | None] = (None, None),
    pinned: tuple[bool, bool] = (False, False),
    importance: tuple[int, int] = (50, 50),
    actor: tuple[str, UUID | None] = ("system", None),
) -> tuple:
    """A history row as ``COLUMNS`` reads it; a column not named did not change."""
    return (*pinned, *importance, *status, *stale, *actor)


class StatusHistoryBase(MemoryDatabaseTestCase):
    def act_as(self, actor_type: str = "system", user_id: UUID | None = None) -> None:
        self.session.execute(metadata_change_actor(actor_type, user_id))

    def edit(self, version: UUID, **values: Any) -> None:
        self.session.execute(
            update(MemoryVersion).where(MemoryVersion.id == version).values(**values)
        )

    def history(self, version: UUID) -> list[tuple]:
        rows = self.session.execute(
            select(*COLUMNS)
            .where(MemoryMetadataChange.memory_version_id == version)
            .order_by(MemoryMetadataChange.created_at)
        )
        return [tuple(row) for row in rows]

    def stored(self, version: UUID, column: Any) -> Any:
        return self.session.execute(
            select(column).where(MemoryVersion.id == version)
        ).scalar_one()

    def refused_column(self, action) -> str | None:
        """Run ``action``; the column a NOT NULL violation names, else None."""
        try:
            with self.session.begin_nested():
                action()
        except IntegrityError as error:
            return error.orig.diag.column_name
        return None


@requires_postgres
class StatusChangeHistoryTest(StatusHistoryBase):
    def test_deprecating_a_version_records_both_statuses_and_the_actor(self):
        user = uuid4()
        version = self.add_version(self.add_memory())
        self.act_as("user", user)

        self.edit(version, status="deprecated")

        self.assertEqual(
            self.history(version),
            [change(status=("active", "deprecated"), actor=("user", user))],
        )
        self.assertEqual(self.stored(version, MemoryVersion.status), "deprecated")

    def test_superseding_a_version_records_both_statuses_and_the_actor(self):
        agent_user = uuid4()
        version = self.add_version(self.add_memory())
        self.act_as("agent", agent_user)

        self.edit(version, status="superseded")

        self.assertEqual(
            self.history(version),
            [change(status=("active", "superseded"), actor=("agent", agent_user))],
        )

    def test_every_status_the_design_names_is_recorded(self):
        for target in ("superseded", "deprecated", "history"):
            with self.subTest(target):
                version = self.add_version(self.add_memory())
                self.act_as("system")

                self.edit(version, status=target)

                self.assertEqual(
                    self.history(version), [change(status=("active", target))]
                )

    def test_restoring_a_deprecated_version_is_a_change_too(self):
        version = self.add_version(self.add_memory(), status="deprecated")
        self.act_as("system")

        self.edit(version, status="active")

        self.assertEqual(
            self.history(version), [change(status=("deprecated", "active"))]
        )

    def test_marking_a_version_stale_records_null_and_the_new_time(self):
        user = uuid4()
        version = self.add_version(self.add_memory())
        self.act_as("user", user)

        self.edit(version, stale_since=FIRST)

        self.assertEqual(
            self.history(version),
            [change(stale=(None, FIRST), actor=("user", user))],
        )
        self.assertEqual(self.stored(version, MemoryVersion.stale_since), FIRST)

    def test_clearing_the_stale_state_records_the_old_time_and_null(self):
        version = self.add_version(self.add_memory(), stale_since=FIRST)
        self.act_as("system")

        self.edit(version, stale_since=None)

        self.assertEqual(self.history(version), [change(stale=(FIRST, None))])
        self.assertIsNone(self.stored(version, MemoryVersion.stale_since))

    def test_moving_the_stale_time_is_recorded_with_both_times(self):
        version = self.add_version(self.add_memory(), stale_since=FIRST)
        self.act_as("system")

        self.edit(version, stale_since=SECOND)

        self.assertEqual(self.history(version), [change(stale=(FIRST, SECOND))])

    def test_a_status_change_and_a_stale_change_in_one_update_are_one_row(self):
        version = self.add_version(self.add_memory())
        self.act_as("system")

        self.edit(version, status="superseded", stale_since=FIRST)

        self.assertEqual(
            self.history(version),
            [change(status=("active", "superseded"), stale=(None, FIRST))],
        )

    def test_one_update_of_all_four_columns_is_one_row(self):
        version = self.add_version(self.add_memory())
        self.act_as("system")

        self.edit(
            version,
            status="deprecated",
            stale_since=FIRST,
            pinned=True,
            importance=10,
        )

        self.assertEqual(
            self.history(version),
            [
                change(
                    status=("active", "deprecated"),
                    stale=(None, FIRST),
                    pinned=(False, True),
                    importance=(50, 10),
                )
            ],
        )

    def test_a_stale_change_keeps_the_status_as_it_is_in_the_row(self):
        version = self.add_version(self.add_memory(), status="history")
        self.act_as("system")

        self.edit(version, stale_since=FIRST)

        self.assertEqual(
            self.history(version),
            [change(status=("history", "history"), stale=(None, FIRST))],
        )

    def test_a_pin_change_records_the_status_and_stale_state_it_left_alone(self):
        version = self.add_version(self.add_memory(), stale_since=FIRST)
        self.act_as("system")

        self.edit(version, pinned=True)

        self.assertEqual(
            self.history(version),
            [change(pinned=(False, True), stale=(FIRST, FIRST))],
        )

    def test_a_life_cycle_is_told_in_order_and_each_row_starts_where_the_last_ended(
        self,
    ):
        marker, restorer = uuid4(), uuid4()
        version = self.add_version(self.add_memory())
        self.act_as("agent")
        self.edit(version, stale_since=FIRST)
        self.act_as("user", marker)
        self.edit(version, status="deprecated")
        self.act_as("user", restorer)
        self.edit(version, status="active", stale_since=None)

        rows = self.history(version)

        self.assertEqual(
            rows,
            [
                change(stale=(None, FIRST), actor=("agent", None)),
                change(
                    status=("active", "deprecated"),
                    stale=(FIRST, FIRST),
                    actor=("user", marker),
                ),
                change(
                    status=("deprecated", "active"),
                    stale=(FIRST, None),
                    actor=("user", restorer),
                ),
            ],
        )

    def test_each_version_has_its_own_history(self):
        memory = self.add_memory()
        old = self.add_version(memory, version_number=1)
        current = self.add_version(memory, version_number=2, status="superseded")
        self.act_as("system")

        self.edit(old, status="superseded")

        self.assertEqual(self.history(current), [])
        self.assertEqual(self.history(old), [change(status=("active", "superseded"))])


@requires_postgres
class NothingChangedTest(StatusHistoryBase):
    def test_writing_the_status_that_is_already_there_records_nothing(self):
        version = self.add_version(self.add_memory())
        self.act_as("system")

        self.edit(version, status="active")

        self.assertEqual(self.history(version), [])

    def test_writing_the_stale_state_that_is_already_there_records_nothing(self):
        marked = self.add_version(self.add_memory(), stale_since=FIRST)
        unmarked = self.add_version(self.add_memory())
        self.act_as("system")

        self.edit(marked, stale_since=FIRST)
        self.edit(unmarked, stale_since=None)

        self.assertEqual(self.history(marked), [])
        self.assertEqual(self.history(unmarked), [])

    def test_writing_every_tracked_column_unchanged_records_nothing(self):
        version = self.add_version(self.add_memory(), stale_since=FIRST)
        self.act_as("system")

        self.edit(
            version,
            status="active",
            stale_since=FIRST,
            pinned=False,
            importance=50,
        )

        self.assertEqual(self.history(version), [])

    def test_a_second_write_of_the_new_value_records_nothing_more(self):
        version = self.add_version(self.add_memory())
        self.act_as("system")
        self.edit(version, status="deprecated")

        self.edit(version, status="deprecated")

        self.assertEqual(
            self.history(version), [change(status=("active", "deprecated"))]
        )

    def test_an_update_that_changes_nothing_needs_no_actor(self):
        version = self.add_version(self.add_memory(), stale_since=FIRST)

        column = self.refused_column(
            lambda: self.edit(version, status="active", stale_since=FIRST)
        )

        self.assertIsNone(column)
        self.assertEqual(self.history(version), [])

    def test_an_update_of_an_untracked_column_records_nothing_and_needs_no_actor(
        self,
    ):
        # The application role may not update it at all (see the grants tests);
        # the owner may, and it is not the trigger's business.
        version = self.add_version(self.add_memory())

        self.edit(version, title="Renamed by the owner")

        self.assertEqual(self.history(version), [])
        self.assertEqual(
            self.stored(version, MemoryVersion.title), "Renamed by the owner"
        )


@requires_postgres
class ActorRequiredTest(StatusHistoryBase):
    """The rule of the pin / importance trigger holds for the new columns too.

    A change without a named actor is refused by the database and not applied
    (the history row cannot be written), instead of being attributed to somebody.
    """

    def test_deprecating_without_an_actor_is_refused_and_not_applied(self):
        version = self.add_version(self.add_memory())

        column = self.refused_column(lambda: self.edit(version, status="deprecated"))

        self.assertEqual(column, ACTOR_REQUIRED)
        self.assertEqual(self.history(version), [])
        self.assertEqual(self.stored(version, MemoryVersion.status), "active")

    def test_marking_stale_without_an_actor_is_refused_and_not_applied(self):
        version = self.add_version(self.add_memory())

        column = self.refused_column(lambda: self.edit(version, stale_since=FIRST))

        self.assertEqual(column, ACTOR_REQUIRED)
        self.assertEqual(self.history(version), [])
        self.assertIsNone(self.stored(version, MemoryVersion.stale_since))

    def test_clearing_stale_without_an_actor_is_refused_and_not_applied(self):
        version = self.add_version(self.add_memory(), stale_since=FIRST)

        column = self.refused_column(lambda: self.edit(version, stale_since=None))

        self.assertEqual(column, ACTOR_REQUIRED)
        self.assertEqual(self.history(version), [])
        self.assertEqual(self.stored(version, MemoryVersion.stale_since), FIRST)

    def test_an_actor_named_for_an_earlier_change_is_not_kept_for_later_ones(self):
        # The naming is per transaction: a second transaction has to name its own
        # actor (real commits are covered by ``CommittedActorTest``); within one
        # transaction the last naming wins, with its user id.
        first, second = uuid4(), uuid4()
        version = self.add_version(self.add_memory())
        self.act_as("user", first)
        self.edit(version, stale_since=FIRST)
        self.act_as("user", second)

        self.edit(version, status="deprecated")

        self.assertEqual(
            [row[-2:] for row in self.history(version)],
            [("user", first), ("user", second)],
        )


@requires_postgres
class ChangeTimeTest(StatusHistoryBase):
    def created_at(self, version: UUID) -> list[datetime]:
        return list(
            self.session.execute(
                select(MemoryMetadataChange.created_at)
                .where(MemoryMetadataChange.memory_version_id == version)
                .order_by(MemoryMetadataChange.created_at)
            ).scalars()
        )

    def clock(self) -> datetime:
        return self.session.execute(text("SELECT clock_timestamp()")).scalar_one()

    def test_the_change_time_is_the_database_clock_and_not_a_time_the_writer_gave(
        self,
    ):
        version = self.add_version(self.add_memory())
        started = self.session.execute(text("SELECT now()")).scalar_one()
        self.act_as("system")
        before = self.clock()

        # The writer's own stale time is data of the version, not the change time.
        self.edit(version, status="deprecated", stale_since=utc(2020, 1, 1))

        after = self.clock()
        (created,) = self.created_at(version)
        self.assertLess(started, before)
        self.assertLessEqual(before, created)
        self.assertLessEqual(created, after)

    def test_two_changes_of_one_transaction_sort_in_the_order_they_were_made(self):
        version = self.add_version(self.add_memory())
        self.act_as("system")
        self.edit(version, stale_since=FIRST)
        self.edit(version, stale_since=None)
        self.edit(version, status="deprecated")

        first, second, third = self.created_at(version)

        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertEqual(
            [(row[6], row[7]) for row in self.history(version)],
            [(None, FIRST), (FIRST, None), (None, None)],
        )


@requires_postgres
class HistoryRowRulesTest(StatusHistoryBase):
    """The history table's own rules for the status columns (rows inserted directly)."""

    def add_change(self, version: UUID, **overrides: Any):
        values: dict[str, Any] = {
            "memory_version_id": version,
            "old_pinned": False,
            "new_pinned": False,
            "old_importance": 50,
            "new_importance": 50,
            "old_status": "active",
            "new_status": "deprecated",
            "actor_type": "system",
        }
        values.update(overrides)
        return self.session.execute(insert(MemoryMetadataChange).values(**values))

    def test_a_row_must_describe_a_valid_change(self):
        version = self.add_version(self.add_memory())
        cases = {
            "ck_memory_metadata_changes_something_changed": {
                "new_status": "active"  # nothing differs any more
            },
            "ck_memory_metadata_changes_status_valid": {"new_status": "gone"},
            "ck_memory_metadata_changes_status_pair": {"old_status": None},
            "ck_memory_metadata_changes_stale_since_needs_status": {
                # The columns of a row from before revision 0071 (no status)
                # cannot carry a stale time.
                "old_status": None,
                "new_status": None,
                "new_pinned": True,
                "new_stale_since": FIRST,
            },
        }
        for expected, overrides in cases.items():
            with self.subTest(expected):
                action = partial(self.add_change, version, **overrides)
                self.assertEqual(self.violation(action), expected)

    def test_an_old_status_that_is_not_a_status_is_refused_too(self):
        version = self.add_version(self.add_memory())

        action = partial(self.add_change, version, old_status="gone")

        self.assertEqual(
            self.violation(action), "ck_memory_metadata_changes_status_valid"
        )

    def test_the_row_shapes_that_are_meant_are_accepted(self):
        version = self.add_version(self.add_memory())
        shapes = {
            "a status change": {},
            "a stale time set": {
                "old_status": "active",
                "new_status": "active",
                "new_stale_since": FIRST,
            },
            "a stale time cleared": {
                "old_status": "active",
                "new_status": "active",
                "old_stale_since": FIRST,
            },
            # What revision 0040 wrote: pin / importance only, no status columns.
            "a row from before revision 0071": {
                "old_status": None,
                "new_status": None,
                "new_pinned": True,
            },
        }
        for name, overrides in shapes.items():
            with self.subTest(name):
                action = partial(self.add_change, version, **overrides)
                self.assertIsNone(self.violation(action))

    def test_a_status_only_change_of_a_row_without_a_pin_change_is_valid(self):
        version = self.add_version(self.add_memory())

        self.assertIsNone(self.violation(partial(self.add_change, version)))
        self.assertEqual(
            self.history(version),
            [change(status=("active", "deprecated"))],
        )


class _Writer(threading.Thread):
    """One transaction on its own connection: name an actor, update, commit.

    ``ready`` is set once the backend pid is known (so that the test can watch
    for the lock wait) and again when the thread is done.
    """

    def __init__(
        self,
        engine,
        version: UUID,
        user: UUID,
        values: dict[str, Any],
        *,
        only_if_status: str | None = None,
        rollback: bool = False,
    ) -> None:
        super().__init__(daemon=True)
        self.engine = engine
        self.version = version
        self.user = user
        self.values = values
        self.only_if_status = only_if_status
        self.rollback = rollback
        self.ready = threading.Event()
        self.backend_pid: int | None = None
        self.rowcount: int | None = None
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            with self.engine.connect() as connection:
                self.backend_pid = connection.execute(
                    text("SELECT pg_backend_pid()")
                ).scalar_one()
                self.ready.set()
                connection.execute(metadata_change_actor("user", self.user))
                statement = update(MemoryVersion).where(
                    MemoryVersion.id == self.version
                )
                if self.only_if_status is not None:
                    statement = statement.where(
                        MemoryVersion.status == self.only_if_status
                    )
                self.rowcount = connection.execute(
                    statement.values(**self.values)
                ).rowcount
                if self.rollback:
                    connection.rollback()
                else:
                    connection.commit()
        except BaseException as error:  # reported by the test, on its own thread
            self.error = error
        finally:
            self.ready.set()


@requires_postgres
class ConcurrentChangeTest(MemoryDatabaseTestCase):
    """Concurrent changes are serialised by the row lock, each recorded once.

    Real commits on separate connections: an ``UPDATE`` waits for the row lock
    of an earlier, uncommitted one, then runs against the row as the earlier one
    left it, so its trigger sees that state as OLD.
    """

    def setUp(self) -> None:
        super().setUp()
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
        self.writers: list[_Writer] = []
        self.addCleanup(self.stop_writers)

    def remove_rows(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(delete(Memory).where(Memory.id == self.memory))

    def stop_writers(self) -> None:
        for writer in self.writers:
            writer.join(DEADLINE_SECONDS)

    def hold(self, user: UUID, **values: Any):
        """Open a transaction that has updated the version and not committed yet."""
        connection = self.engine.connect()
        self.addCleanup(connection.close)  # a still open transaction is rolled back
        transaction = connection.begin()
        connection.execute(metadata_change_actor("user", user))
        connection.execute(
            update(MemoryVersion)
            .where(MemoryVersion.id == self.version)
            .values(**values)
        )
        return transaction

    def start_blocked(self, user: UUID, values: dict[str, Any], **options: Any):
        """Start a writer and return it once it waits for the row lock."""
        writer = _Writer(self.engine, self.version, user, values, **options)
        self.writers.append(writer)
        writer.start()
        self.assertTrue(writer.ready.wait(DEADLINE_SECONDS))
        deadline = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            with self.engine.connect() as connection:
                waiting = connection.execute(
                    text(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"
                    ),
                    {"pid": writer.backend_pid},
                ).scalar_one_or_none()
            if waiting == "Lock":
                return writer
            time.sleep(0.02)
        self.fail("the second writer never waited for the row lock")

    def finish(self, writer: _Writer) -> None:
        writer.join(DEADLINE_SECONDS)
        self.assertFalse(writer.is_alive())
        self.assertIsNone(writer.error)

    def recorded(self) -> list[tuple]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(*COLUMNS)
                .where(MemoryMetadataChange.memory_version_id == self.version)
                .order_by(MemoryMetadataChange.created_at)
            ).all()
            return [tuple(row) for row in rows]

    def stored(self) -> tuple:
        with self.engine.connect() as connection:
            return tuple(
                connection.execute(
                    select(MemoryVersion.status, MemoryVersion.stale_since).where(
                        MemoryVersion.id == self.version
                    )
                ).one()
            )

    def test_two_different_transitions_are_both_recorded_in_lock_order(self):
        first, second = uuid4(), uuid4()
        holder = self.hold(first, status="deprecated")

        writer = self.start_blocked(second, {"status": "history"})
        holder.commit()
        self.finish(writer)

        self.assertEqual(
            self.recorded(),
            [
                change(status=("active", "deprecated"), actor=("user", first)),
                # The second update started from the state the first one left.
                change(status=("deprecated", "history"), actor=("user", second)),
            ],
        )
        self.assertEqual(self.stored(), ("history", None))

    def test_a_status_change_and_a_stale_change_do_not_lose_each_other(self):
        first, second = uuid4(), uuid4()
        holder = self.hold(first, stale_since=FIRST)

        writer = self.start_blocked(second, {"status": "superseded"})
        holder.commit()
        self.finish(writer)

        self.assertEqual(
            self.recorded(),
            [
                change(stale=(None, FIRST), actor=("user", first)),
                change(
                    status=("active", "superseded"),
                    stale=(FIRST, FIRST),
                    actor=("user", second),
                ),
            ],
        )
        self.assertEqual(self.stored(), ("superseded", FIRST))

    def test_the_same_transition_twice_is_recorded_exactly_once(self):
        first, second = uuid4(), uuid4()
        holder = self.hold(first, status="deprecated")

        writer = self.start_blocked(second, {"status": "deprecated"})
        holder.commit()
        self.finish(writer)

        # The second update found the row already deprecated: nothing to record.
        self.assertEqual(writer.rowcount, 1)
        self.assertEqual(
            self.recorded(),
            [change(status=("active", "deprecated"), actor=("user", first))],
        )

    def test_a_guarded_transition_that_lost_the_race_changes_and_records_nothing(self):
        first, second = uuid4(), uuid4()
        holder = self.hold(first, status="superseded")

        writer = self.start_blocked(
            second, {"status": "deprecated"}, only_if_status="active"
        )
        holder.commit()
        self.finish(writer)

        self.assertEqual(writer.rowcount, 0)
        self.assertEqual(
            self.recorded(),
            [change(status=("active", "superseded"), actor=("user", first))],
        )
        self.assertEqual(self.stored(), ("superseded", None))

    def test_a_rolled_back_change_leaves_no_record_and_the_next_one_starts_at_active(
        self,
    ):
        first, second = uuid4(), uuid4()
        holder = self.hold(first, status="deprecated")

        writer = self.start_blocked(second, {"status": "history"})
        holder.rollback()
        self.finish(writer)

        self.assertEqual(
            self.recorded(),
            [change(status=("active", "history"), actor=("user", second))],
        )

    def test_a_change_that_rolls_back_after_waiting_leaves_no_record(self):
        first, second = uuid4(), uuid4()
        holder = self.hold(first, stale_since=FIRST)

        writer = self.start_blocked(second, {"stale_since": None}, rollback=True)
        holder.commit()
        self.finish(writer)

        self.assertEqual(
            self.recorded(), [change(stale=(None, FIRST), actor=("user", first))]
        )
        self.assertEqual(self.stored(), ("active", FIRST))


@requires_postgres
class CommittedActorTest(MemoryDatabaseTestCase):
    """The actor of one transaction does not leak into the next one (real commits)."""

    def setUp(self) -> None:
        super().setUp()
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

    def test_a_status_change_needs_its_own_actor_in_each_transaction(self):
        user = uuid4()
        with self.engine.begin() as connection:
            connection.execute(metadata_change_actor("user", user))
            connection.execute(
                update(MemoryVersion)
                .where(MemoryVersion.id == self.version)
                .values(status="deprecated")
            )

        with self.assertRaises(IntegrityError) as caught:
            with self.engine.begin() as connection:  # the same pooled connection
                connection.execute(
                    update(MemoryVersion)
                    .where(MemoryVersion.id == self.version)
                    .values(status="history")
                )

        self.assertEqual(caught.exception.orig.diag.column_name, ACTOR_REQUIRED)
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    MemoryMetadataChange.old_status,
                    MemoryMetadataChange.new_status,
                    MemoryMetadataChange.actor_user_id,
                ).where(MemoryMetadataChange.memory_version_id == self.version)
            ).all()
            status = connection.execute(
                select(MemoryVersion.status).where(MemoryVersion.id == self.version)
            ).scalar_one()
        self.assertEqual([tuple(row) for row in rows], [("active", "deprecated", user)])
        self.assertEqual(status, "deprecated")
