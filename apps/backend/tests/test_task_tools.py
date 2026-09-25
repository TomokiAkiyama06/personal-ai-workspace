"""Tool call state of a step: what a reconnecting client or backend can restore.

Only identity and execution state are stored (never arguments or output).
Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid

from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from paw_backend.tasks import (
    InvalidCommandArgumentError,
    StaleAttemptError,
    StepStatus,
    TaskCommand,
    TaskConflictError,
    TaskNotFoundError,
    TaskService,
    TaskState,
    TaskStepError,
    ToolInvocationStatus,
)
from paw_backend.tasks.service import (
    MAX_ACTIVE_TOOL_INVOCATIONS,
    MAX_RESTORE_TOOL_INVOCATIONS,
)

from .gate_support import ALWAYS_ACTIVE
from .task_support import FIRST_RUN, PostgresTaskTestCase, requires_postgres

C = TaskCommand
S = TaskState
T = ToolInvocationStatus


@requires_postgres
class ToolInvocationTest(PostgresTaskTestCase):
    async def running_step(self, state: TaskState = S.RUNNING):
        task_id = await self.task_in_state(state)
        step = await self.service.begin_step(task_id, "run-tests", run=FIRST_RUN)
        return task_id, step

    async def test_a_call_is_recorded_started_and_then_finished(self):
        task_id, step = await self.running_step()
        started = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        self.assertEqual(
            (started.tool_name, started.status, started.step_id),
            ("shell", T.STARTED, step.id),
        )
        self.assertIsNone(started.finished_at)
        self.assertIsInstance(started.id, uuid.UUID)

        finished = await self.service.finish_tool_invocation(
            task_id, started.id, T.SUCCEEDED
        )
        self.assertEqual((finished.id, finished.status), (started.id, T.SUCCEEDED))
        self.assertIsNotNone(finished.finished_at)
        self.assertGreaterEqual(finished.finished_at, finished.started_at)
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.tool_invocations, (finished,))

    async def test_a_client_that_disconnects_mid_call_leaves_the_call_restorable(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="git-push"
        )
        # A different process (a reconnecting client, a restarted backend).
        snapshot = await TaskService(
            self.new_database(), project_gate=ALWAYS_ACTIVE
        ).restore(task_id)
        self.assertEqual(snapshot.tool_invocations, (call,))
        self.assertEqual(snapshot.tool_invocations[0].status, T.STARTED)
        self.assertEqual(snapshot.current_step.id, step.id)

    async def test_stop_now_interrupts_the_calls_of_the_running_step(self):
        task_id, step = await self.running_step()
        done = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="read-file"
        )
        await self.service.finish_tool_invocation(task_id, done.id, T.SUCCEEDED)
        running = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await self.service.execute(
            task_id, C.STOP_NOW, actor=self.user, reason="dangerous tool call"
        )

        snapshot = await self.service.restore(task_id)
        by_id = {call.id: call for call in snapshot.tool_invocations}
        self.assertEqual(by_id[done.id].status, T.SUCCEEDED)
        self.assertEqual(by_id[running.id].status, T.INTERRUPTED)
        self.assertIsNotNone(by_id[running.id].finished_at)
        self.assertEqual(snapshot.current_step.status, StepStatus.INTERRUPTED)

    async def test_a_step_that_ends_takes_its_started_calls_with_it(self):
        for ending in ("fail", "finish", "restart"):
            with self.subTest(ending=ending):
                task_id, step = await self.running_step()
                call = await self.service.begin_tool_invocation(
                    task_id, step_id=step.id, tool_name="shell"
                )
                if ending == "fail":
                    await self.service.execute(task_id, C.FAIL, actor=self.system)
                elif ending == "finish":
                    await self.service.finish_step(
                        task_id, step.id, StepStatus.SUCCEEDED
                    )
                else:
                    await self.service.execute(task_id, C.CANCEL, actor=self.user)
                    await self.service.execute(task_id, C.RESTART, actor=self.user)
                status = await self.scalar(
                    "SELECT status FROM task_tool_invocations WHERE id = :i",
                    i=call.id,
                )
                self.assertEqual(status, "interrupted")

    async def test_a_graceful_cancel_leaves_the_worker_to_finish_its_call(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await self.service.execute(task_id, C.CANCEL, actor=self.user)
        self.assertEqual(
            (await self.service.restore(task_id)).tool_invocations[0].status,
            T.STARTED,
        )
        # Finishing is allowed in any task state.
        finished = await self.service.finish_tool_invocation(
            task_id, call.id, T.INTERRUPTED
        )
        self.assertEqual(finished.status, T.INTERRUPTED)

    async def test_only_the_current_step_s_calls_are_restored(self):
        task_id, first = await self.running_step()
        await self.service.begin_tool_invocation(
            task_id, step_id=first.id, tool_name="old-tool"
        )
        await self.service.finish_step(task_id, first.id, StepStatus.SUCCEEDED)
        second = await self.service.begin_step(task_id, "next", run=FIRST_RUN)
        current = await self.service.begin_tool_invocation(
            task_id, step_id=second.id, tool_name="new-tool"
        )
        snapshot = await self.service.restore(task_id)
        self.assertEqual(snapshot.tool_invocations, (current,))

    async def test_restore_returns_the_latest_hundred_calls_oldest_first(self):
        task_id, step = await self.running_step()
        await self.database_execute(
            "INSERT INTO task_tool_invocations "
            "(id, task_id, step_id, tool_name, status, started_at, finished_at) "
            "SELECT gen_random_uuid(), :t, :s, 'tool-' || n, 'succeeded', "
            "now() + n * interval '1 millisecond', "
            "now() + n * interval '1 millisecond' "
            "FROM generate_series(1, 105) AS n",
            t=task_id,
            s=step.id,
        )
        snapshot = await self.service.restore(task_id)
        names = [call.tool_name for call in snapshot.tool_invocations]
        self.assertEqual(len(names), 100)
        self.assertEqual((names[0], names[-1]), ("tool-6", "tool-105"))

    async def test_restore_keeps_a_started_call_older_than_the_latest_hundred(self):
        # The oldest call is still started; 150 calls that finished later must not
        # push it out of the restored list (a backend must be able to resume or
        # abort every call that is still in flight).
        task_id, step = await self.running_step()
        active = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="long-running"
        )
        await self.database_execute(
            "INSERT INTO task_tool_invocations "
            "(id, task_id, step_id, tool_name, status, started_at, finished_at) "
            "SELECT gen_random_uuid(), :t, :s, 'tool-' || n, 'succeeded', "
            "now() + n * interval '1 millisecond', "
            "now() + n * interval '1 millisecond' "
            "FROM generate_series(1, 150) AS n",
            t=task_id,
            s=step.id,
        )
        snapshot = await self.service.restore(task_id)
        calls = snapshot.tool_invocations
        self.assertEqual(len(calls), 101)
        self.assertEqual(calls[0], active)
        self.assertEqual(calls[0].status, T.STARTED)
        self.assertEqual(calls[1].tool_name, "tool-51")
        self.assertEqual(calls[-1].tool_name, "tool-150")

    async def test_restore_returns_every_started_call_of_the_step(self):
        # Far more started calls than the finished history keeps, mixed with newer
        # and older finished ones: none of the started ones may be missing.
        task_id, step = await self.running_step()
        insert = (
            "INSERT INTO task_tool_invocations "
            "(id, task_id, step_id, tool_name, status, started_at, finished_at) "
            "SELECT gen_random_uuid(), :t, :s, CAST(:prefix AS text) || n, "
            "CAST(:status AS text), "
            "now() + n * interval '1 second', "
            "CASE WHEN CAST(:status AS text) = 'started' THEN NULL "
            "ELSE now() + n * interval '1 second' END "
            "FROM generate_series(CAST(:first AS integer), CAST(:last AS integer)) AS n"
        )
        started_count = MAX_ACTIVE_TOOL_INVOCATIONS
        self.assertGreater(started_count, MAX_RESTORE_TOOL_INVOCATIONS)
        # started 1..1000 interleaved with finished ones before, among and after.
        await self.database_execute(
            insert,
            t=task_id,
            s=step.id,
            prefix="active-",
            status="started",
            first=1,
            last=started_count,
        )
        await self.database_execute(
            insert,
            t=task_id,
            s=step.id,
            prefix="done-",
            status="succeeded",
            first=-50,
            last=started_count + 50,
        )
        snapshot = await self.service.restore(task_id)
        calls = snapshot.tool_invocations

        active = [call for call in calls if call.status is T.STARTED]
        self.assertEqual(
            [call.tool_name for call in active],
            [f"active-{n}" for n in range(1, started_count + 1)],
        )
        finished = [call for call in calls if call.status is not T.STARTED]
        self.assertEqual(
            [call.tool_name for call in finished],
            [
                f"done-{n}"
                for n in range(
                    started_count + 50 - MAX_RESTORE_TOOL_INVOCATIONS + 1,
                    started_count + 51,
                )
            ],
        )
        # Oldest first overall.
        self.assertEqual(
            [call.started_at for call in calls],
            sorted(call.started_at for call in calls),
        )
        self.assertEqual(len(calls), started_count + MAX_RESTORE_TOOL_INVOCATIONS)

    async def test_a_step_refuses_a_call_beyond_the_active_limit(self):
        task_id, step = await self.running_step()
        await self.database_execute(
            "INSERT INTO task_tool_invocations "
            "(id, task_id, step_id, tool_name, status, started_at) "
            "SELECT gen_random_uuid(), :t, :s, 'tool-' || n, 'started', now() "
            "FROM generate_series(1, CAST(:n AS integer)) AS n",
            t=task_id,
            s=step.id,
            n=MAX_ACTIVE_TOOL_INVOCATIONS,
        )
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="one-too-many"
            )
        count = await self.scalar(
            "SELECT count(*) FROM task_tool_invocations WHERE step_id = :s",
            s=step.id,
        )
        self.assertEqual(count, MAX_ACTIVE_TOOL_INVOCATIONS)

        # Only calls still started count: finishing one makes room for exactly one.
        snapshot = await self.service.restore(task_id)
        await self.service.finish_tool_invocation(
            task_id, snapshot.tool_invocations[0].id, T.SUCCEEDED
        )
        again = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="fits-now"
        )
        self.assertEqual(again.status, T.STARTED)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="one-too-many"
            )

        # The limit is per step: another task's step is not affected.
        other_task, other_step = await self.running_step()
        self.assertEqual(
            (
                await self.service.begin_tool_invocation(
                    other_task, step_id=other_step.id, tool_name="shell"
                )
            ).status,
            T.STARTED,
        )

    async def test_the_broker_may_supply_the_invocation_id_once(self):
        task_id, step = await self.running_step()
        broker_id = uuid.uuid4()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell", invocation_id=broker_id
        )
        self.assertEqual(call.id, broker_id)
        with self.assertRaises(TaskConflictError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="shell", invocation_id=broker_id
            )

    async def test_a_call_needs_a_running_step_of_an_active_task_and_a_name(self):
        task_id, step = await self.running_step()
        with self.assertRaises(TaskNotFoundError):
            await self.service.begin_tool_invocation(
                uuid.uuid4(), step_id=step.id, tool_name="shell"
            )
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id + 10_000_000, tool_name="shell"
            )
        for name in ("", "   ", "x" * 101):
            with self.assertRaises(InvalidCommandArgumentError):
                await self.service.begin_tool_invocation(
                    task_id, step_id=step.id, tool_name=name
                )
        await self.service.finish_step(task_id, step.id, StepStatus.SUCCEEDED)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="shell"
            )
        # A paused task starts no new call either.
        paused_task, paused_step = await self.running_step()
        await self.service.execute(paused_task, C.PAUSE, actor=self.user)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                paused_task, step_id=paused_step.id, tool_name="shell"
            )
        self.assertEqual((await self.service.restore(task_id)).tool_invocations, ())
        self.assertEqual((await self.service.restore(paused_task)).tool_invocations, ())

    async def test_a_call_of_another_task_cannot_be_reached_through_this_one(self):
        mine, _ = await self.running_step()
        other, other_step = await self.running_step()
        foreign = await self.service.begin_tool_invocation(
            other, step_id=other_step.id, tool_name="shell"
        )
        with self.assertRaises(TaskStepError):
            await self.service.finish_tool_invocation(mine, foreign.id, T.SUCCEEDED)
        with self.assertRaises(TaskStepError):
            await self.service.begin_tool_invocation(
                mine, step_id=other_step.id, tool_name="shell"
            )
        self.assertEqual(
            (await self.service.restore(other)).tool_invocations[0].status, T.STARTED
        )

    async def test_finishing_needs_a_started_call_and_a_final_status(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        with self.assertRaises(InvalidCommandArgumentError):
            await self.service.finish_tool_invocation(task_id, call.id, T.STARTED)
        with self.assertRaises(TaskStepError):
            await self.service.finish_tool_invocation(
                task_id, uuid.uuid4(), T.SUCCEEDED
            )
        await self.service.finish_tool_invocation(task_id, call.id, T.FAILED)
        with self.assertRaises(TaskStepError):
            await self.service.finish_tool_invocation(task_id, call.id, T.SUCCEEDED)
        self.assertEqual(
            (await self.service.restore(task_id)).tool_invocations[0].status, T.FAILED
        )

    async def test_a_superseded_worker_cannot_report_a_call_of_the_old_attempt(self):
        task_id, step = await self.running_step()
        call = await self.service.begin_tool_invocation(
            task_id, step_id=step.id, tool_name="shell"
        )
        await self.service.execute(task_id, C.CANCEL, actor=self.user)
        await self.service.execute(task_id, C.RESTART, actor=self.user)
        with self.assertRaises(StaleAttemptError):
            await self.service.finish_tool_invocation(task_id, call.id, T.SUCCEEDED)
        # Restart already interrupted it; the late success did not overwrite that.
        status = await self.scalar(
            "SELECT status FROM task_tool_invocations WHERE id = :i", i=call.id
        )
        self.assertEqual(status, "interrupted")

    async def database_execute(self, sql: str, **parameters) -> None:
        async with self.database.engine.begin() as connection:
            await connection.execute(text(sql), parameters)


@requires_postgres
class ToolInvocationSchemaTest(PostgresTaskTestCase):
    async def test_the_table_stores_identity_and_state_but_no_call_content(self):
        columns = {
            row[0]
            for row in await self.rows(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'task_tool_invocations'"
            )
        }
        self.assertEqual(
            columns,
            {
                "id",
                "task_id",
                "step_id",
                "tool_name",
                "status",
                "started_at",
                "finished_at",
            },
        )

    async def test_the_database_keeps_finished_at_consistent_with_the_status(self):
        task_id = await self.task_in_state(S.RUNNING)
        step = await self.service.begin_step(task_id, "work", run=FIRST_RUN)
        for status, finished in (
            ("started", "now()"),
            ("succeeded", "NULL"),
            ("gone", "NULL"),
        ):
            with self.subTest(status=status, finished=finished):
                with self.assertRaises(IntegrityError):
                    async with self.database.engine.begin() as connection:
                        await connection.execute(
                            text(
                                "INSERT INTO task_tool_invocations (id, task_id, "
                                "step_id, tool_name, status, started_at, finished_at) "
                                "VALUES (gen_random_uuid(), :t, :s, 'shell', "
                                f"'{status}', now(), {finished})"
                            ),
                            {"t": task_id, "s": step.id},
                        )

    async def running_step(self):
        task_id = await self.task_in_state(S.RUNNING)
        return task_id, await self.service.begin_step(task_id, "work", run=FIRST_RUN)

    async def database_execute(self, sql: str, **parameters) -> None:
        async with self.database.engine.begin() as connection:
            await connection.execute(text(sql), parameters)

    async def test_calls_have_one_partial_index_for_each_kind_of_query(self):
        rows = await self.rows(
            "SELECT i.relname, x.indisunique, pg_get_indexdef(i.oid), "
            "pg_get_expr(x.indpred, x.indrelid) "
            "FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "WHERE t.relname = 'task_tool_invocations' AND x.indpred IS NOT NULL "
            "ORDER BY i.relname"
        )
        # The calls in flight (a bounded number per step) and the finished ones,
        # newest first (what ``restore`` returns, at most 100).
        self.assertEqual(
            [(name, unique, predicate) for name, unique, _, predicate in rows],
            [
                (
                    "ix_task_tool_invocations_finished",
                    False,
                    "((status)::text <> 'started'::text)",
                ),
                (
                    "ix_task_tool_invocations_started",
                    False,
                    "((status)::text = 'started'::text)",
                ),
            ],
        )
        definitions = {name: definition for name, _, definition, _ in rows}
        self.assertIn(
            "USING btree (step_id, started_at DESC, id DESC)",
            definitions["ix_task_tool_invocations_finished"],
        )
        self.assertIn(
            "USING btree (step_id)", definitions["ix_task_tool_invocations_started"]
        )

    async def test_every_query_on_started_calls_can_use_the_partial_index(self):
        # One long step with a large finished history and two calls in flight:
        # the queries about the calls in flight must not read the history.
        task_id, step = await self.running_step()
        await self.database_execute(
            "INSERT INTO task_tool_invocations (id, task_id, step_id, tool_name, "
            "status, started_at, finished_at) "
            "SELECT gen_random_uuid(), :t, :s, 'shell', 'succeeded', now(), now() "
            "FROM generate_series(1, 20000)",
            t=task_id,
            s=step.id,
        )
        for _ in range(2):
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="shell"
            )
        await self.database_execute("ANALYZE task_tool_invocations")

        captured = []

        def capture(connection, cursor, statement, parameters, context, many):
            captured.append((statement, dict(parameters)))

        engine = self.database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            await self.service.begin_tool_invocation(
                task_id, step_id=step.id, tool_name="shell"
            )
            await self.service.restore(task_id)
            await self.service.finish_step(task_id, step.id, StepStatus.SUCCEEDED)
        finally:
            event.remove(engine, "before_cursor_execute", capture)

        def statement_on_tool_calls(*, starting: str, excluding: str = "\0"):
            found = [
                (sql, parameters)
                for sql, parameters in captured
                if sql.lstrip().startswith(starting)
                and "task_tool_invocations" in sql
                and excluding not in sql
                and "task_tool_invocations.status !=" not in sql
            ]
            self.assertEqual(len(found), 1, starting)
            return found[0]

        queries = {
            "count of started calls": statement_on_tool_calls(
                starting="SELECT count(*)"
            ),
            "started calls to restore": statement_on_tool_calls(
                starting="SELECT task_tool_invocations", excluding="LIMIT"
            ),
            "interrupt started calls": statement_on_tool_calls(
                starting="UPDATE task_tool_invocations"
            ),
        }
        for label, (sql, parameters) in queries.items():
            with self.subTest(query=label):
                plan = await self.generic_plan(sql, parameters)
                self.assertIn("ix_task_tool_invocations_started", plan)
                self.assertNotIn("Seq Scan", plan)

    async def test_the_bounded_finished_calls_query_reads_only_the_rows_it_returns(
        self,
    ):
        # One long step: a large finished history whose physical order is not its
        # time order, and two calls in flight (the newest rows of the step).
        task_id, step = await self.running_step()
        await self.database_execute(
            "INSERT INTO task_tool_invocations (id, task_id, step_id, tool_name, "
            "status, started_at, finished_at) "
            "SELECT gen_random_uuid(), :t, :s, 'shell', "
            "(ARRAY['succeeded', 'failed', 'interrupted'])[1 + g % 3], "
            "now() - ((g * 7919) % 20011) * interval '1 second', now() "
            "FROM generate_series(1, 20000) AS g",
            t=task_id,
            s=step.id,
        )
        in_flight = {
            (
                await self.service.begin_tool_invocation(
                    task_id, step_id=step.id, tool_name="shell"
                )
            ).id
            for _ in range(2)
        }
        await self.database_execute("ANALYZE task_tool_invocations")

        captured = []

        def capture(connection, cursor, statement, parameters, context, many):
            captured.append((statement, dict(parameters)))

        engine = self.database.engine.sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            snapshot = await self.service.restore(task_id)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        (sql, parameters) = next(
            (statement, parameters)
            for statement, parameters in captured
            if statement.lstrip().startswith("SELECT task_tool_invocations")
            and "LIMIT" in statement
        )

        def nodes(plan):
            yield plan
            for child in plan.get("Plans", ()):
                yield from nodes(child)

        # The plan PostgreSQL makes for the values at hand, and the one it caches
        # for a prepared statement (planned without them).
        for mode in ("force_custom_plan", "force_generic_plan"):
            with self.subTest(plan_cache_mode=mode):
                (explained,) = await self.plan(sql, parameters, mode, analyze=True)
                found = list(nodes(explained["Plan"]))
                types = {node["Node Type"] for node in found}
                self.assertNotIn("Seq Scan", types)
                # Nothing is sorted: the index hands the rows over in order.
                self.assertFalse({t for t in types if "Sort" in t}, types)
                (scan,) = (
                    node
                    for node in found
                    if node.get("Index Name") == "ix_task_tool_invocations_finished"
                )
                # It stopped after the rows that were asked for: it did not read
                # the finished history, nor filter out the calls in flight.
                self.assertEqual(scan["Actual Rows"], MAX_RESTORE_TOOL_INVOCATIONS)
                self.assertNotIn("Rows Removed by Filter", scan)

        # The rows are the latest finished ones (newest first by start, then id).
        latest = [
            row[0]
            for row in await self.rows(
                "SELECT id FROM task_tool_invocations WHERE step_id = "
                f"{step.id} AND status <> 'started' "
                "ORDER BY started_at DESC, id DESC LIMIT "
                f"{MAX_RESTORE_TOOL_INVOCATIONS}"
            )
        ]
        self.assertEqual(
            {call.id for call in snapshot.tool_invocations},
            {*latest, *in_flight},
        )

    async def rows(self, sql: str):
        async with self.database.engine.connect() as connection:
            return (await connection.execute(text(sql))).all()


if __name__ == "__main__":
    unittest.main()
