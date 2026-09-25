"""Optimistic concurrency, restoring from the database, schema and migration.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set (see test_postgres_integration).
"""

import asyncio
import io
import unittest
import uuid

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from paw_backend.db import Base
from paw_backend.tasks import (
    EvaluationResult,
    IllegalTransitionError,
    LogLevel,
    PullRequestInfo,
    PullRequestState,
    ReviewState,
    ReviewStatus,
    StepStatus,
    TaskCommand,
    TaskConflictError,
    TaskService,
    TaskState,
    WaitReason,
    WorktreeState,
)

from .support import paw_environment
from .task_support import (
    FIRST_RUN,
    PostgresTaskTestCase,
    migrate,
    new_database,
    requires_postgres,
)
from .test_migrations import offline_config

C = TaskCommand
S = TaskState
TASK_TABLES = (
    "tasks",
    "task_attempts",
    "task_steps",
    "task_tool_invocations",
    "task_logs",
    "task_events",
)


@requires_postgres
class ConcurrencyTest(PostgresTaskTestCase):
    async def test_a_stale_expected_version_is_rejected_and_changes_nothing(self):
        task_id = await self.create_task()
        other_process = TaskService(self.new_database())

        await self.service.execute(
            task_id, C.START, actor=self.system, expected_version=1
        )
        with self.assertRaises(TaskConflictError) as caught:
            await other_process.execute(
                task_id, C.CANCEL, actor=self.user, expected_version=1
            )
        self.assertEqual(caught.exception.code, "task_conflict")

        snapshot = await self.service.restore(task_id)
        self.assertEqual((snapshot.state, snapshot.version), (S.RUNNING, 2))
        self.assertEqual(
            [event.command for event in await self.service.history(task_id)],
            [C.CREATE, C.START],
        )

    async def test_the_current_version_is_accepted(self):
        task_id = await self.create_task()
        event = await self.service.execute(
            task_id, C.START, actor=self.system, expected_version=1
        )
        self.assertEqual(event.task_version, 2)
        event = await self.service.execute(
            task_id, C.PAUSE, actor=self.user, expected_version=2
        )
        self.assertEqual(event.task_version, 3)

    async def test_a_stale_version_is_reported_before_an_illegal_transition(self):
        task_id = await self.task_in_state(S.COMPLETED)
        with self.assertRaises(TaskConflictError):
            await self.service.execute(
                task_id, C.PAUSE, actor=self.user, expected_version=1
            )
        with self.assertRaises(IllegalTransitionError):
            await self.service.execute(
                task_id, C.PAUSE, actor=self.user, expected_version=4
            )

    async def test_a_command_waiting_behind_a_writer_rejects_its_stale_version(self):
        """Both commands were decided on version 1; only the first one may apply."""
        task_id = await self.create_task()
        loser = TaskService(self.new_database())

        async with self.database.engine.connect() as winner:
            # The winner updates the row but has not committed yet.
            await winner.execute(
                text(
                    "UPDATE tasks SET state = 'cancelled', version = version + 1 "
                    "WHERE id = :id"
                ),
                {"id": task_id},
            )
            pending = asyncio.create_task(
                loser.execute(task_id, C.START, actor=self.system, expected_version=1)
            )
            await self.wait_for_lock_waiters(1)
            await winner.commit()

        with self.assertRaises(TaskConflictError):
            await pending
        self.assertEqual(
            await self.scalar("SELECT state FROM tasks WHERE id = :i", i=task_id),
            "cancelled",
        )
        self.assertEqual(
            await self.scalar("SELECT version FROM tasks WHERE id = :i", i=task_id), 2
        )
        # The loser's transaction, including its event, was rolled back.
        self.assertEqual(
            [event.command for event in await self.service.history(task_id)], [C.CREATE]
        )

    async def test_a_command_without_a_version_is_judged_on_the_latest_state(self):
        task_id = await self.task_in_state(S.RUNNING)
        second = TaskService(self.new_database())

        async with self.database.engine.connect() as first:
            await first.execute(
                text(
                    "UPDATE tasks SET state = 'paused', version = version + 1 "
                    "WHERE id = :id"
                ),
                {"id": task_id},
            )
            pending = asyncio.create_task(
                second.execute(task_id, C.CANCEL, actor=self.user)
            )
            await self.wait_for_lock_waiters(1)
            await first.commit()

        event = await pending
        # It did not act on the running state it could have read before waiting.
        self.assertEqual((event.from_state, event.to_state), (S.PAUSED, S.CANCELLED))
        self.assertEqual(event.task_version, 4)

    async def test_of_many_simultaneous_cancels_exactly_one_wins(self):
        task_id = await self.task_in_state(S.RUNNING)
        services = [TaskService(self.new_database()) for _ in range(6)]
        results = await asyncio.gather(
            *(
                service.execute(task_id, C.CANCEL, actor=self.user)
                for service in services
            ),
            return_exceptions=True,
        )
        winners = [result for result in results if not isinstance(result, Exception)]
        losers = [result for result in results if isinstance(result, Exception)]
        self.assertEqual(len(winners), 1)
        # A loser either lost the version race or arrived after the commit.
        self.assertTrue(
            all(
                isinstance(error, TaskConflictError | IllegalTransitionError)
                for error in losers
            ),
            losers,
        )
        events = await self.service.history(task_id)
        self.assertEqual([event.command for event in events].count(C.CANCEL), 1)
        self.assertEqual((await self.service.restore(task_id)).version, 3)

    async def test_two_different_commands_cannot_both_leave_a_running_task(self):
        task_id = await self.task_in_state(S.RUNNING)
        services = [TaskService(self.new_database()) for _ in range(2)]
        commands = [
            (services[0], C.BEGIN_EVALUATION, S.EVALUATING),
            (services[1], C.WAIT, S.WAITING),
        ]
        results = await asyncio.gather(
            *(
                service.execute(
                    task_id,
                    command,
                    actor=self.system,
                    wait_reason=WaitReason.USER if command is C.WAIT else None,
                )
                for service, command, _ in commands
            ),
            return_exceptions=True,
        )
        # Whichever committed first decides; the other one cannot also apply.
        ok = [
            (command, target)
            for (_, command, target), r in zip(commands, results, strict=True)
            if not isinstance(r, Exception)
        ]
        self.assertEqual(len(ok), 1)
        self.assertEqual((await self.service.restore(task_id)).state, ok[0][1])

    async def test_concurrent_step_starts_leave_a_single_running_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        services = [TaskService(self.new_database()) for _ in range(4)]
        results = await asyncio.gather(
            *(
                service.begin_step(task_id, "work", run=FIRST_RUN)
                for service in services
            ),
            return_exceptions=True,
        )
        started = [result for result in results if not isinstance(result, Exception)]
        self.assertEqual(len(started), 1)
        running = await self.scalar(
            "SELECT count(*) FROM task_steps WHERE task_id = :i AND status = 'running'",
            i=task_id,
        )
        self.assertEqual(running, 1)

    async def test_stop_now_does_not_overwrite_a_step_the_worker_just_finished(self):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "work", run=FIRST_RUN)
        stopper = TaskService(self.new_database())

        async with self.database.engine.connect() as worker:
            # The worker finishes its step but has not committed yet.
            await worker.execute(
                text(
                    "UPDATE task_steps SET status = 'succeeded', finished_at = now() "
                    "WHERE task_id = :id"
                ),
                {"id": task_id},
            )
            pending = asyncio.create_task(
                stopper.execute(task_id, C.STOP_NOW, actor=self.user)
            )
            await self.wait_for_lock_waiters(1)
            await worker.commit()

        event = await pending
        # Stop Now found the step already finished, so it interrupted nothing and
        # must not say it did.
        self.assertIsNone(event.step_name)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.current_step.status, StepStatus.SUCCEEDED)
        self.assertEqual(
            [log.message for log in snapshot.recent_logs],
            ["Stop Now: no step was running"],
        )


@requires_postgres
class RestoreTest(PostgresTaskTestCase):
    async def build_rich_task(self, service: TaskService) -> uuid.UUID:
        task_id = await self.create_task(
            service,
            input={"prompt": "Make it faster"},
            starting_commit="a" * 40,
            agent="codex",
            model="model-x",
        )
        await service.execute(task_id, C.START, actor=self.system)
        await service.begin_step(task_id, "implement", run=FIRST_RUN)
        await service.add_log(task_id, "editing parser.py", run=FIRST_RUN)
        await service.add_log(
            task_id, "tests are red", run=FIRST_RUN, level=LogLevel.WARNING
        )
        await service.update_attempt(
            task_id,
            run=FIRST_RUN,
            worktree=WorktreeState("agent/task-1", "/srv/worktrees/task-1", "b" * 40),
            review=ReviewState(ReviewStatus.IN_REVIEW, EvaluationResult.PASSED),
            pull_request=PullRequestInfo(
                12, "https://example.test/pr/12", PullRequestState.OPEN
            ),
        )
        await service.execute(
            task_id, C.WAIT, actor=self.system, wait_reason=WaitReason.USER
        )
        return task_id

    async def test_a_second_independent_service_sees_the_same_truth(self):
        task_id = await self.build_rich_task(self.service)

        other_process = TaskService(self.new_database())
        snapshot = await other_process.restore(task_id)

        self.assertEqual(snapshot, await self.service.restore(task_id))
        self.assertEqual(
            (snapshot.state, snapshot.wait_reason), (S.WAITING, WaitReason.USER)
        )
        self.assertEqual(snapshot.version, 3)
        self.assertEqual((snapshot.agent, snapshot.model), ("codex", "model-x"))
        self.assertEqual(snapshot.current_step.name, "implement")
        self.assertEqual(snapshot.current_step.status, StepStatus.RUNNING)
        self.assertEqual(
            [(log.level, log.message) for log in snapshot.recent_logs],
            [(LogLevel.INFO, "editing parser.py"), (LogLevel.WARNING, "tests are red")],
        )
        self.assertEqual(
            snapshot.attempt.worktree,
            WorktreeState("agent/task-1", "/srv/worktrees/task-1", "b" * 40),
        )
        self.assertEqual(
            snapshot.attempt.review,
            ReviewState(ReviewStatus.IN_REVIEW, EvaluationResult.PASSED),
        )
        self.assertEqual(
            snapshot.attempt.pull_request,
            PullRequestInfo(12, "https://example.test/pr/12", PullRequestState.OPEN),
        )
        self.assertEqual(snapshot.last_event.command, C.WAIT)
        self.assertEqual(snapshot.input, {"prompt": "Make it faster"})

    async def test_state_survives_the_original_process_going_away(self):
        """The 'client' or process that drove the task disconnects for good."""
        first = new_database()
        service = TaskService(first)
        task_id = await self.build_rich_task(service)
        before = await service.restore(task_id)
        await first.dispose()  # every connection of the first process is closed

        fresh = TaskService(self.new_database())
        self.assertEqual(await fresh.restore(task_id), before)

        # ...and the new process can carry on from exactly that state.
        await fresh.execute(
            task_id, C.UNBLOCK, actor=self.user, expected_version=before.version
        )
        await fresh.finish_step(task_id, before.current_step.id, StepStatus.SUCCEEDED)
        after = await self.service.restore(task_id)
        self.assertEqual((after.state, after.version), (S.RUNNING, before.version + 1))
        self.assertEqual(after.current_step.status, StepStatus.SUCCEEDED)
        self.assertEqual(after.attempt, before.attempt)

    async def test_task_that_a_disconnected_client_paused_is_still_paused_for_a_new_one(
        self,
    ):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.execute(
            task_id, C.PAUSE, actor=self.user, reason="going offline"
        )
        reconnecting = TaskService(self.new_database())
        snapshot = await reconnecting.restore(task_id)
        self.assertEqual(snapshot.state, S.PAUSED)
        self.assertEqual(snapshot.last_event.reason, "going offline")
        self.assertEqual(snapshot.last_event.actor.id, self.user_id)

    async def test_restore_is_isolated_per_task(self):
        first = await self.build_rich_task(self.service)
        second = await self.task_in_state(S.PAUSED)
        snapshot = await self.service.restore(second)
        self.assertNotEqual(first, second)
        self.assertEqual(snapshot.state, S.PAUSED)
        self.assertEqual(snapshot.recent_logs, ())
        self.assertIsNone(snapshot.attempt.pull_request)


@requires_postgres
class SchemaTest(PostgresTaskTestCase):
    async def insert_task(self, state: str, wait_reason: str | None = None):
        async with self.database.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input, "
                    "state, wait_reason, attempt, retry_count, version, "
                    "created_at, updated_at) VALUES (:id, :p, :u, 't', '{}', "
                    ":state, :wait, 1, 0, 1, now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "p": uuid.uuid4(),
                    "u": uuid.uuid4(),
                    "state": state,
                    "wait": wait_reason,
                },
            )

    async def test_database_accepts_exactly_the_states_of_the_enum(self):
        for state in TaskState:
            with self.subTest(state=state.value):
                waiting = state is S.WAITING
                await self.insert_task(state.value, "user" if waiting else None)
        with self.assertRaises(IntegrityError):
            await self.insert_task("running_or_something")

    async def test_wait_reason_exists_exactly_while_waiting(self):
        for state, reason in (
            ("running", "user"),
            ("waiting", None),
            ("waiting", "elsewhere"),
        ):
            with (
                self.subTest(state=state, reason=reason),
                self.assertRaises(IntegrityError),
            ):
                await self.insert_task(state, reason)
        for reason in WaitReason:
            await self.insert_task("waiting", reason.value)

    async def test_a_second_running_step_in_an_attempt_is_rejected_by_the_database(
        self,
    ):
        task_id = await self.task_in_state(S.RUNNING)
        await self.service.begin_step(task_id, "one", run=FIRST_RUN)
        with self.assertRaises(IntegrityError):
            async with self.database.engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO task_steps (task_id, attempt, sequence, name, "
                        "status, started_at) "
                        "VALUES (:id, 1, 2, 'two', 'running', now())"
                    ),
                    {"id": task_id},
                )

    async def test_rows_cannot_reference_a_missing_task(self):
        with self.assertRaises(IntegrityError):
            async with self.database.engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO task_logs (task_id, attempt, retry_count, level, "
                        "message, created_at) VALUES (:id, 1, 0, 'info', 'x', now())"
                    ),
                    {"id": uuid.uuid4()},
                )

    async def test_a_log_line_and_an_event_must_name_a_run(self):
        # The same insert is accepted with a valid run, so what is rejected is the
        # missing or negative retry count.
        task_id = await self.task_in_state(S.RUNNING)
        inserts = {
            "task_logs": (
                "INSERT INTO task_logs (task_id, attempt, {retry}level, message, "
                "created_at) VALUES (:id, 1, {value}'info', 'x', now())"
            ),
            "task_events": (
                "INSERT INTO task_events (task_id, attempt, {retry}command, "
                "to_state, actor_kind, task_version, created_at) "
                "VALUES (:id, 1, {value}'start', 'running', 'system', 1, now())"
            ),
        }
        for table, template in inserts.items():
            for retry, value, accepted in (
                ("retry_count, ", "0, ", True),
                ("retry_count, ", "2147483647, ", True),
                ("retry_count, ", "-1, ", False),
                ("", "", False),  # no retry count at all
            ):
                with self.subTest(table=table, retry_count=value or None):
                    statement = text(template.format(retry=retry, value=value))
                    if accepted:
                        async with self.database.engine.begin() as connection:
                            await connection.execute(statement, {"id": task_id})
                    else:
                        with self.assertRaises(IntegrityError):
                            async with self.database.engine.begin() as connection:
                                await connection.execute(statement, {"id": task_id})

    async def test_ids_of_users_and_projects_have_no_foreign_keys_yet(self):
        # Documented deviation: those tables do not exist yet (PAW-021).
        referenced = await self.scalar(
            "SELECT array_agg(DISTINCT confrelid::regclass::text) FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid IN "
            "('tasks'::regclass, 'task_events'::regclass, 'task_attempts'::regclass)"
        )
        self.assertEqual(referenced, ["tasks"])


@requires_postgres
class MigrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await asyncio.to_thread(migrate)
        # Leave the database at head for the other tests.
        self.addAsyncCleanup(asyncio.to_thread, migrate)

    async def existing_tables(self) -> set[str]:
        database = new_database()
        try:
            async with database.engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables "
                        "WHERE schemaname = current_schema() AND tablename LIKE 'task%'"
                    )
                )
                return {row[0] for row in rows}
        finally:
            await database.dispose()

    async def function_exists(self) -> bool:
        database = new_database()
        try:
            async with database.engine.connect() as connection:
                return bool(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_proc "
                                "WHERE proname = 'task_events_reject_change'"
                            )
                        )
                    ).scalar()
                )
        finally:
            await database.dispose()

    async def test_downgrade_removes_everything_and_upgrade_restores_it(self):
        self.assertEqual(await self.existing_tables(), set(TASK_TABLES))
        self.assertTrue(await self.function_exists())

        await asyncio.to_thread(migrate, "base", downgrade=True)
        self.assertEqual(await self.existing_tables(), set())
        self.assertFalse(await self.function_exists())

        await asyncio.to_thread(migrate)
        self.assertEqual(await self.existing_tables(), set(TASK_TABLES))
        self.assertTrue(await self.function_exists())

    async def test_upgrade_is_idempotent_at_head(self):
        await asyncio.to_thread(migrate)
        self.assertEqual(await self.existing_tables(), set(TASK_TABLES))

    async def test_the_migration_matches_the_orm_models(self):
        def only_task_objects(obj, name, type_, reflected, compare_to):
            table = obj if type_ == "table" else getattr(obj, "table", None)
            return table is not None and table.name.startswith("task")

        database = new_database()
        try:
            async with database.engine.connect() as connection:

                def diff(sync_connection):
                    context = MigrationContext.configure(
                        sync_connection, opts={"include_object": only_task_objects}
                    )
                    return compare_metadata(context, Base.metadata)

                differences = await connection.run_sync(diff)
        finally:
            await database.dispose()
        self.assertEqual(differences, [])

    async def test_every_constraint_and_index_is_named_by_the_naming_convention(self):
        expected = set()
        for table_name in TASK_TABLES:
            table = Base.metadata.tables[table_name]
            expected |= {str(c.name) for c in table.constraints}
            expected |= {str(i.name) for i in table.indexes}
        database = new_database()
        try:
            async with database.engine.connect() as connection:
                constraints = await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint c "
                        "JOIN pg_class t ON t.oid = c.conrelid "
                        "WHERE t.relname LIKE 'task%' "
                        "AND c.contype IN ('p', 'u', 'f', 'c')"
                    )
                )
                indexes = await connection.execute(
                    text(
                        "SELECT i.relname FROM pg_index x "
                        "JOIN pg_class i ON i.oid = x.indexrelid "
                        "JOIN pg_class t ON t.oid = x.indrelid "
                        "WHERE t.relname LIKE 'task%' AND NOT EXISTS "
                        "(SELECT 1 FROM pg_constraint c "
                        "WHERE c.conindid = x.indexrelid)"
                    )
                )
                actual = {row[0] for row in constraints} | {row[0] for row in indexes}
        finally:
            await database.dispose()
        self.assertEqual(actual, expected)
        self.assertIn("pk_tasks", actual)
        self.assertIn("fk_task_events_task_id_tasks", actual)
        self.assertIn("ck_tasks_wait_reason_matches_state", actual)
        self.assertIn("uq_task_steps_one_running", actual)


class OfflineMigrationTest(unittest.TestCase):
    """SQL rendering needs no database, so this runs everywhere."""

    def render(self, direction: str) -> str:
        output = io.StringIO()
        config = offline_config(output)
        with paw_environment(PAW_DATABASE_URL="postgresql://paw:pw@db.internal/paw"):
            if direction == "up":
                command.upgrade(config, "base:head", sql=True)
            else:
                command.downgrade(config, "head:base", sql=True)
        return output.getvalue()

    def test_upgrade_creates_the_tables_and_the_append_only_trigger(self):
        sql = self.render("up")
        self.assertNotIn("db.internal", sql)
        self.assertNotIn("paw:pw", sql)
        for table in TASK_TABLES:
            self.assertIn(f"CREATE TABLE {table} (", sql)
        self.assertIn("CREATE TRIGGER task_events_append_only", sql)
        self.assertIn("BEFORE UPDATE OR DELETE ON task_events", sql)
        self.assertIn("CONSTRAINT ck_tasks_wait_reason_matches_state CHECK", sql)
        self.assertIn("UPDATE alembic_version SET version_num='0032'", sql)
        self.assertNotIn("FOREIGN KEY(project_id)", sql)
        self.assertNotIn("FOREIGN KEY(created_by)", sql)

    def test_downgrade_drops_the_history_table_before_its_function(self):
        sql = self.render("down")
        self.assertIn("DROP TABLE task_events", sql)
        self.assertLess(
            sql.index("DROP TABLE task_events"),
            sql.index("DROP FUNCTION task_events_reject_change()"),
        )
        for table in (
            "task_logs",
            "task_tool_invocations",
            "task_steps",
            "task_attempts",
            "tasks",
        ):
            self.assertIn(f"DROP TABLE {table}", sql)


if __name__ == "__main__":
    unittest.main()
