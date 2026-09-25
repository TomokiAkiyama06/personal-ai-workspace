"""The DAG store on a real PostgreSQL: persistence, fencing, concurrency.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. The store is SQL only, so these
tests drive it directly (the orchestrator's decisions are tested in
``test_orchestrator_run.py``): state changes of nodes, the epoch fence that stops a
worker that was replaced, the attempt fence that stops the report of an old run,
and races between separate connection pools (a second worker process).
"""

import asyncio
import unittest
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from paw_backend.orchestrator.domain import (
    AttemptState,
    DagState,
    NextStep,
    NodeRole,
    NodeState,
)
from paw_backend.orchestrator.errors import (
    DagAlreadyExistsError,
    DagNotFoundError,
    DagStateError,
    NodeStateError,
    StaleDagEpochError,
    StaleNodeAttemptError,
)
from paw_backend.orchestrator.result import NodeResult
from paw_backend.tasks import TaskNotFoundError, TaskRun

from .orchestrator_support import (
    PostgresOrchestratorTestCase,
    diamond,
    make_plan,
    node,
    requires_postgres,
)
from .queueing_support import raise_unexpected

S = NodeState
SIGNATURE = "a" * 64
RUN = TaskRun(1, 0)


def states(dag) -> dict[str, NodeState]:
    return {n.key: n.state for n in dag.nodes}


def result(text: str = "done") -> NodeResult:
    return NodeResult(text)


@requires_postgres
class CreateTest(PostgresOrchestratorTestCase):
    async def test_a_plan_is_stored_with_ready_roots_and_pending_dependents(self):
        task_id = await self.create_task()

        dag = await self.store.create(task_id, 1, diamond())

        self.assertEqual(dag.task_id, task_id)
        self.assertEqual((dag.attempt, dag.epoch, dag.owner), (1, 0, None))
        self.assertEqual(dag.state, DagState.ACTIVE)
        self.assertEqual([n.key for n in dag.nodes], ["a", "b", "c", "d", "e"])
        self.assertEqual([n.ordinal for n in dag.nodes], [0, 1, 2, 3, 4])
        self.assertEqual(
            states(dag),
            {
                "a": S.READY,
                "b": S.PENDING,
                "c": S.PENDING,
                "d": S.PENDING,
                "e": S.READY,
            },
        )
        self.assertEqual(dag.node("d").depends_on, ("b", "c"))
        self.assertEqual(dag.node("a").role, NodeRole.RESEARCHER)
        self.assertEqual((dag.node("a").attempt_count, dag.node("a").approach), (0, 0))
        # Reading it back gives the same DAG.
        self.assertEqual(await self.store.get(task_id, 1), dag)
        self.assertEqual(await self.store.get_by_id(dag.id), dag)

    async def test_everything_of_a_node_is_stored(self):
        repository = uuid.uuid4()
        plan = make_plan(
            node(
                "impl",
                title="Implement",
                goal="Write the code\nwith care",
                required=False,
                input={"files": ["a.py"], "n": 1},
                capabilities=["project.read"],
                repositories=[str(repository)],
            ),
            node("free", required=True),
        )
        task_id = await self.create_task()

        await self.store.create(task_id, 1, plan)

        stored = (await self.store.get(task_id, 1)).node("impl")
        self.assertEqual(stored.goal, "Write the code\nwith care")
        self.assertEqual(stored.input, {"files": ["a.py"], "n": 1})
        self.assertFalse(stored.required)
        self.assertEqual(stored.repositories, (repository,))
        self.assertEqual([c.value for c in stored.capabilities], ["project.read"])
        self.assertIsNone((await self.store.get(task_id, 1)).node("free").capabilities)

    async def test_an_attempt_has_one_dag_and_the_next_attempt_a_new_one(self):
        task_id = await self.create_task()
        first = await self.store.create(task_id, 1, diamond())

        with self.assertRaises(DagAlreadyExistsError):
            await self.store.create(task_id, 1, diamond())
        second = await self.store.create(task_id, 2, diamond())

        self.assertNotEqual(first.id, second.id)
        self.assertEqual((await self.store.get(task_id, 2)).id, second.id)
        # The refused plan left nothing behind.
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dags"), 2)
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dag_nodes"), 10)

    async def test_an_unknown_task_and_a_missing_dag(self):
        with self.assertRaises(TaskNotFoundError):
            await self.store.create(uuid.uuid4(), 1, diamond())
        self.assertIsNone(await self.store.get(await self.create_task(), 1))
        with self.assertRaises(DagNotFoundError):
            await self.store.get_by_id(uuid.uuid4())
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dags"), 0)

    async def test_concurrent_creations_of_the_same_attempt_store_one_dag(self):
        task_id = await self.create_task()
        stores = [self.new_store() for _ in range(6)]

        results = await asyncio.gather(
            *(store.create(task_id, 1, diamond()) for store in stores),
            return_exceptions=True,
        )

        raise_unexpected(results, DagAlreadyExistsError)
        self.assertEqual(
            sum(not isinstance(r, BaseException) for r in results), 1, results
        )
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dags"), 1)
        self.assertEqual(await self.scalar("SELECT count(*) FROM agent_dag_nodes"), 5)


@requires_postgres
class AcquireTest(PostgresOrchestratorTestCase):
    async def test_every_take_over_raises_the_epoch_and_names_the_owner(self):
        dag = await self.make_dag()

        first = await self.store.acquire(dag.id, "worker-1", RUN)
        second = await self.store.acquire(dag.id, "worker-2", RUN)

        self.assertEqual((first.epoch, first.owner), (1, "worker-1"))
        self.assertEqual((second.epoch, second.owner), (2, "worker-2"))

    async def test_concurrent_take_overs_get_distinct_epochs(self):
        dag = await self.make_dag()
        stores = [self.new_store() for _ in range(8)]

        taken = await asyncio.gather(
            *(store.acquire(dag.id, f"w{i}", RUN) for i, store in enumerate(stores))
        )

        self.assertEqual(sorted(d.epoch for d in taken), list(range(1, 9)))
        final = await self.store.get_by_id(dag.id)
        self.assertEqual(final.epoch, 8)
        # The last one to take it over is the owner.
        self.assertEqual(final.owner, next(d.owner for d in taken if d.epoch == 8))

    async def test_a_take_over_of_the_wrong_attempt_or_a_cancelled_dag_is_refused(self):
        dag = await self.taken_dag()
        with self.assertRaises(DagStateError):
            await self.store.acquire(dag.id, "w2", TaskRun(2, 0))
        await self.store.cancel(dag.id, 1)
        with self.assertRaises(DagStateError):
            await self.store.acquire(dag.id, "w2", RUN)
        with self.assertRaises(DagNotFoundError):
            await self.store.acquire(uuid.uuid4(), "w2", RUN)
        self.assertEqual((await self.store.get_by_id(dag.id)).epoch, 1)

    async def test_a_take_over_makes_the_nodes_of_a_dead_worker_ready_again(self):
        dag = await self.taken_dag()
        await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.start_node(dag.id, 1, "e", max_attempts=6)
        running = await self.store.get_by_id(dag.id)
        self.assertEqual(
            (running.node("a").state, running.node("e").state), (S.RUNNING,) * 2
        )

        after = await self.store.acquire(dag.id, "w2", RUN)

        self.assertEqual(after.epoch, 2)
        self.assertEqual((after.node("a").state, after.node("e").state), (S.READY,) * 2)
        # The counters stay: the abandoned start still counts as an attempt.
        self.assertEqual(
            (after.node("a").attempt_count, after.node("a").rung_attempts), (1, 1)
        )
        attempts = await self.store.attempts(dag.id)
        self.assertEqual({a.state for a in attempts}, {AttemptState.INTERRUPTED})
        self.assertTrue(all(a.finished_at is not None for a in attempts))

    async def test_a_retry_reopens_failed_and_blocked_nodes_only(self):
        dag = await self.taken_dag()
        attempt = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.fail_node(
            dag.id,
            1,
            "a",
            attempt.number,
            error_class="E",
            signature=SIGNATURE,
            step=NextStep.GIVE_UP,
        )
        e = await self.store.start_node(dag.id, 1, "e", max_attempts=6)
        await self.store.complete_node(dag.id, 1, "e", e.number, result("e done"))
        failed = await self.store.finalize(dag.id, 1)
        self.assertEqual(failed.state, DagState.FAILED)
        self.assertEqual(
            states(failed),
            {
                "a": S.FAILED,
                "b": S.BLOCKED,
                "c": S.BLOCKED,
                "d": S.BLOCKED,
                "e": S.SUCCEEDED,
            },
        )

        # The same run again (a crash after the DAG failed): nothing is re-opened.
        same = await self.store.acquire(dag.id, "w2", RUN)
        self.assertEqual(same.state, DagState.FAILED)
        self.assertEqual(states(same), states(failed))

        # A Retry of the task: the failed node and what it blocked are open again.
        retried = await self.store.acquire(dag.id, "w3", TaskRun(1, 1))
        self.assertEqual(retried.state, DagState.ACTIVE)
        self.assertEqual(retried.task_retry_count, 1)
        self.assertEqual(
            states(retried),
            {
                "a": S.READY,
                "b": S.PENDING,
                "c": S.PENDING,
                "d": S.PENDING,
                "e": S.SUCCEEDED,
            },
        )
        # The finished node keeps its result; the reopened node starts its rung again.
        self.assertEqual(retried.node("e").result, result("e done"))
        self.assertEqual(retried.node("a").rung_attempts, 0)
        self.assertEqual(retried.node("a").attempt_count, 1)
        self.assertIsNone(retried.node("a").error_class)


@requires_postgres
class NodeLifecycleTest(PostgresOrchestratorTestCase):
    async def test_a_node_runs_succeeds_and_makes_its_dependents_ready(self):
        dag = await self.taken_dag()
        attempt = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        self.assertEqual(
            (attempt.number, attempt.epoch, attempt.agent_index, attempt.approach),
            (1, 1, 0, 0),
        )
        self.assertEqual(attempt.state, AttemptState.RUNNING)
        self.assertEqual(states(await self.store.get_by_id(dag.id))["a"], S.RUNNING)

        done = await self.store.complete_node(dag.id, 1, "a", 1, result("a done"))

        self.assertEqual(
            states(done),
            {
                "a": S.SUCCEEDED,
                "b": S.READY,
                "c": S.READY,
                "d": S.PENDING,
                "e": S.READY,
            },
        )
        self.assertEqual(done.node("a").result, result("a done"))
        (stored,) = await self.store.attempts(dag.id)
        self.assertEqual(stored.state, AttemptState.SUCCEEDED)
        self.assertIsNotNone(stored.finished_at)

    async def test_the_join_is_ready_only_after_both_branches_succeeded(self):
        dag = await self.taken_dag()
        for key in ("a",):
            attempt = await self.store.start_node(dag.id, 1, key, max_attempts=6)
            await self.store.complete_node(dag.id, 1, key, attempt.number, result())
        b = await self.store.start_node(dag.id, 1, "b", max_attempts=6)
        after_b = await self.store.complete_node(dag.id, 1, "b", b.number, result())
        self.assertEqual(after_b.node("d").state, S.PENDING)
        c = await self.store.start_node(dag.id, 1, "c", max_attempts=6)
        after_c = await self.store.complete_node(dag.id, 1, "c", c.number, result())
        self.assertEqual(after_c.node("d").state, S.READY)

    async def test_start_refuses_a_node_that_is_not_ready(self):
        dag = await self.taken_dag()
        for key in ("b", "d", "nosuch"):
            with self.subTest(key=key), self.assertRaises(NodeStateError):
                await self.store.start_node(dag.id, 1, key, max_attempts=6)
        await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        with self.assertRaises(NodeStateError):  # already running
            await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        self.assertEqual(len(await self.store.attempts(dag.id)), 1)

    async def test_the_attempt_cap_of_a_rung_is_enforced_when_starting(self):
        dag = await self.taken_dag()
        for number in (1, 2):
            attempt = await self.store.start_node(dag.id, 1, "e", max_attempts=2)
            self.assertEqual(attempt.number, number)
            await self.store.fail_node(
                dag.id,
                1,
                "e",
                number,
                error_class="E",
                signature=SIGNATURE,
                step=NextStep.RETRY,
            )
        with self.assertRaises(NodeStateError):
            await self.store.start_node(dag.id, 1, "e", max_attempts=2)
        # A higher cap (a caller that allows more) starts it.
        third = await self.store.start_node(dag.id, 1, "e", max_attempts=3)
        self.assertEqual(third.number, 3)

    async def test_each_next_step_puts_the_node_where_the_orchestrator_decided(self):
        dag = await self.taken_dag()
        cases = [
            # (step, extra arguments, state, agent_index, approach, rung_attempts)
            (NextStep.RETRY, {}, S.READY, 0, 0, 1),
            (NextStep.HOLD, {}, S.READY, 0, 0, 2),
            (NextStep.ALTERNATIVE, {"agent_index": 0, "approach": 1}, S.READY, 0, 1, 3),
            (NextStep.ESCALATE, {"agent_index": 1, "approach": 2}, S.READY, 1, 2, 0),
            (NextStep.GIVE_UP, {}, S.FAILED, 1, 2, 1),
        ]
        for number, (step, extra, state, agent, approach, rung) in enumerate(cases, 1):
            with self.subTest(step=step.value):
                attempt = await self.store.start_node(dag.id, 1, "e", max_attempts=6)
                self.assertEqual(attempt.number, number)
                after = await self.store.fail_node(
                    dag.id,
                    1,
                    "e",
                    number,
                    error_class="Boom",
                    signature=SIGNATURE,
                    step=step,
                    **extra,
                )
                e = after.node("e")
                if step is NextStep.ESCALATE:
                    self.assertEqual(e.rung_attempts, 0)  # the new rung starts again
                    rung = 0
                self.assertEqual(
                    (e.state, e.agent_index, e.approach), (state, agent, approach)
                )
                self.assertEqual(e.error_class, "Boom")
                self.assertEqual(e.attempt_count, number)
                if step is not NextStep.ESCALATE:
                    self.assertEqual(e.rung_attempts, rung)
                # The attempt records how it ended and which rung / approach it ran on.
                stored = (await self.store.attempts(dag.id, "e"))[-1]
                self.assertEqual(stored.state, AttemptState.FAILED)
                self.assertEqual(
                    (stored.error_class, stored.failure_signature), ("Boom", SIGNATURE)
                )

    async def test_a_node_that_gives_up_blocks_only_its_dependents(self):
        dag = await self.taken_dag()
        a = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.complete_node(dag.id, 1, "a", a.number, result())
        b = await self.store.start_node(dag.id, 1, "b", max_attempts=6)

        after = await self.store.fail_node(
            dag.id,
            1,
            "b",
            b.number,
            error_class="E",
            signature=SIGNATURE,
            step=NextStep.GIVE_UP,
        )

        self.assertEqual(
            states(after),
            {
                "a": S.SUCCEEDED,
                "b": S.FAILED,
                "c": S.READY,
                "d": S.BLOCKED,
                "e": S.READY,
            },
        )

    async def test_alternative_and_escalation_must_move_forward(self):
        dag = await self.taken_dag()
        attempt = await self.store.start_node(dag.id, 1, "e", max_attempts=6)
        bad = [
            (NextStep.ALTERNATIVE, {"agent_index": 0, "approach": 0}),  # not higher
            (NextStep.ALTERNATIVE, {"agent_index": 1, "approach": 1}),  # rung changes
            (NextStep.ESCALATE, {"agent_index": 0, "approach": 1}),  # same rung
            (
                NextStep.ESCALATE,
                {"agent_index": 1, "approach": 0},
            ),  # approach not higher
        ]
        for step, extra in bad:
            with (
                self.subTest(step=step.value, extra=extra),
                self.assertRaises(NodeStateError),
            ):
                await self.store.fail_node(
                    dag.id,
                    1,
                    "e",
                    attempt.number,
                    error_class="E",
                    signature=SIGNATURE,
                    step=step,
                    **extra,
                )
        after = await self.store.get_by_id(dag.id)
        self.assertEqual(after.node("e").state, S.RUNNING)  # nothing changed
        self.assertEqual(
            (await self.store.attempts(dag.id, "e"))[0].state, AttemptState.RUNNING
        )

    async def test_finalize_judges_the_dag_from_the_rows(self):
        dag = await self.taken_dag(
            make_plan(node("a"), node("opt", required=False), node("b", "opt"))
        )
        with self.assertRaises(DagStateError):  # nodes are still ready
            await self.store.finalize(dag.id, 1)
        for key in ("a", "opt"):
            attempt = await self.store.start_node(dag.id, 1, key, max_attempts=6)
            if key == "a":
                await self.store.complete_node(dag.id, 1, key, attempt.number, result())
            else:
                await self.store.fail_node(
                    dag.id,
                    1,
                    key,
                    attempt.number,
                    error_class="E",
                    signature=SIGNATURE,
                    step=NextStep.GIVE_UP,
                )
        # b (required) is blocked by the failed optional node: the DAG failed.
        final = await self.store.finalize(dag.id, 1)
        self.assertEqual(final.state, DagState.FAILED)
        with self.assertRaises(DagStateError):  # a closed DAG takes no more writes
            await self.store.start_node(dag.id, 1, "a", max_attempts=6)

    async def test_finalize_succeeds_when_only_optional_nodes_failed(self):
        dag = await self.taken_dag(make_plan(node("a"), node("opt", required=False)))
        a = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.complete_node(dag.id, 1, "a", a.number, result())
        o = await self.store.start_node(dag.id, 1, "opt", max_attempts=6)
        await self.store.fail_node(
            dag.id,
            1,
            "opt",
            o.number,
            error_class="E",
            signature=SIGNATURE,
            step=NextStep.GIVE_UP,
        )

        final = await self.store.finalize(dag.id, 1)

        self.assertEqual(final.state, DagState.SUCCEEDED)

    async def test_cancel_leaves_a_dag_that_already_ended_as_it_is(self):
        dag = await self.taken_dag(make_plan(node("a")))
        a = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.complete_node(dag.id, 1, "a", a.number, result())
        finished = await self.store.finalize(dag.id, 1)
        self.assertEqual(finished.state, DagState.SUCCEEDED)

        after = await self.store.cancel(dag.id, 1)

        self.assertEqual(after.state, DagState.SUCCEEDED)
        self.assertEqual(states(after), {"a": S.SUCCEEDED})

    async def test_interrupt_returns_running_nodes_to_ready(self):
        dag = await self.taken_dag()
        await self.store.start_node(dag.id, 1, "a", max_attempts=6)

        after = await self.store.interrupt(dag.id, 1)

        self.assertEqual(after.node("a").state, S.READY)
        self.assertEqual(after.state, DagState.ACTIVE)
        (attempt,) = await self.store.attempts(dag.id)
        self.assertEqual(attempt.state, AttemptState.INTERRUPTED)

    async def test_cancel_cancels_every_node_that_did_not_finish(self):
        dag = await self.taken_dag()
        a = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.complete_node(dag.id, 1, "a", a.number, result())
        await self.store.start_node(dag.id, 1, "b", max_attempts=6)

        after = await self.store.cancel(dag.id, 1)

        self.assertEqual(after.state, DagState.CANCELLED)
        self.assertEqual(
            states(after),
            {
                "a": S.SUCCEEDED,
                "b": S.CANCELLED,
                "c": S.CANCELLED,
                "d": S.CANCELLED,
                "e": S.CANCELLED,
            },
        )
        self.assertEqual(
            (await self.store.attempts(dag.id, "b"))[0].state, AttemptState.INTERRUPTED
        )


@requires_postgres
class FencingTest(PostgresOrchestratorTestCase):
    async def test_a_worker_that_was_replaced_cannot_write(self):
        dag = await self.taken_dag(owner="old")
        attempt = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        new = await self.store.acquire(dag.id, "new", RUN)
        before = await self.store.get_by_id(dag.id)
        self.assertEqual(new.epoch, 2)

        writes = {
            "start": lambda: self.store.start_node(dag.id, 1, "e", max_attempts=6),
            "complete": lambda: self.store.complete_node(
                dag.id, 1, "a", attempt.number, result()
            ),
            "fail": lambda: self.store.fail_node(
                dag.id,
                1,
                "a",
                attempt.number,
                error_class="E",
                signature=SIGNATURE,
                step=NextStep.GIVE_UP,
            ),
            "interrupt": lambda: self.store.interrupt(dag.id, 1),
            "cancel": lambda: self.store.cancel(dag.id, 1),
            "finalize": lambda: self.store.finalize(dag.id, 1),
        }
        for name, write in writes.items():
            with self.subTest(write=name), self.assertRaises(StaleDagEpochError):
                await write()

        self.assertEqual(await self.store.get_by_id(dag.id), before)
        self.assertEqual(
            {a.state for a in await self.store.attempts(dag.id)},
            {AttemptState.INTERRUPTED},
        )

    async def test_the_report_of_an_old_attempt_is_refused_even_by_the_new_owner(self):
        dag = await self.taken_dag(owner="old")
        old = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.acquire(dag.id, "new", RUN)
        fresh = await self.store.start_node(dag.id, 2, "a", max_attempts=6)
        self.assertEqual((old.number, fresh.number), (1, 2))

        # The old run (still executing somewhere) reports with the new epoch's
        # writer (a bug or a confused caller): the attempt fence stops it.
        with self.assertRaises(StaleNodeAttemptError):
            await self.store.complete_node(dag.id, 2, "a", old.number, result("stale"))
        with self.assertRaises(StaleNodeAttemptError):
            await self.store.fail_node(
                dag.id,
                2,
                "a",
                old.number,
                error_class="E",
                signature=SIGNATURE,
                step=NextStep.RETRY,
            )
        after = await self.store.complete_node(
            dag.id, 2, "a", fresh.number, result("fresh")
        )
        self.assertEqual(after.node("a").result, result("fresh"))

    async def test_a_node_cannot_be_completed_twice_or_out_of_state(self):
        dag = await self.taken_dag()
        attempt = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        await self.store.complete_node(dag.id, 1, "a", attempt.number, result("first"))

        with self.assertRaises(StaleNodeAttemptError):
            await self.store.complete_node(
                dag.id, 1, "a", attempt.number, result("second")
            )
        with self.assertRaises(StaleNodeAttemptError):  # never started
            await self.store.complete_node(dag.id, 1, "e", 1, result())

        self.assertEqual(
            (await self.store.get_by_id(dag.id)).node("a").result, result("first")
        )

    async def test_an_unknown_dag_is_not_found_for_every_write(self):
        missing = uuid.uuid4()
        with self.assertRaises(DagNotFoundError):
            await self.store.start_node(missing, 1, "a", max_attempts=6)
        with self.assertRaises(DagNotFoundError):
            await self.store.cancel(missing, 1)

    async def wait_for_lock_waiters(self, count: int) -> None:
        async def waiting() -> int:
            return await self.scalar(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock'"
                " AND datname = current_database() AND pid <> pg_backend_pid()"
            )

        for _ in range(500):
            if await waiting() >= count:
                return
            await asyncio.sleep(0.02)
        self.fail("the writers never waited for the DAG row lock")

    async def test_a_write_that_waits_for_the_lock_behind_a_take_over_is_refused(self):
        dag = await self.taken_dag(owner="old")
        attempt = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        stale = self.new_store()
        newcomer = self.new_store()
        holder = self.new_database()
        async with holder.engine.connect() as connection:
            await connection.begin()
            await connection.execute(
                text("SELECT id FROM agent_dags WHERE id = :id FOR UPDATE"),
                {"id": dag.id},
            )
            # The take-over queues for the row first, then the old worker's write.
            takeover = asyncio.create_task(newcomer.acquire(dag.id, "new", RUN))
            await self.wait_for_lock_waiters(1)
            write = asyncio.create_task(
                stale.complete_node(dag.id, 1, "a", attempt.number, result("late"))
            )
            await self.wait_for_lock_waiters(2)
            await connection.rollback()

        taken = await takeover
        with self.assertRaises(StaleDagEpochError):
            await write
        self.assertEqual(taken.epoch, 2)
        final = await self.store.get_by_id(dag.id)
        self.assertEqual(
            final.node("a").state, S.READY
        )  # not "succeeded": the write lost
        self.assertIsNone(final.node("a").result)

    async def test_a_write_that_wins_the_lock_before_a_take_over_is_kept_by_it(self):
        dag = await self.taken_dag(owner="old")
        attempt = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        stale = self.new_store()
        newcomer = self.new_store()
        holder = self.new_database()
        async with holder.engine.connect() as connection:
            await connection.begin()
            await connection.execute(
                text("SELECT id FROM agent_dags WHERE id = :id FOR UPDATE"),
                {"id": dag.id},
            )
            write = asyncio.create_task(
                stale.complete_node(dag.id, 1, "a", attempt.number, result("in time"))
            )
            await self.wait_for_lock_waiters(1)
            takeover = asyncio.create_task(newcomer.acquire(dag.id, "new", RUN))
            await self.wait_for_lock_waiters(2)
            await connection.rollback()

        await write  # it was still the owner when it got the lock
        taken = await takeover
        self.assertEqual(taken.node("a").state, S.SUCCEEDED)
        self.assertEqual(taken.node("a").result, result("in time"))
        self.assertEqual(taken.epoch, 2)

    async def test_two_nodes_finishing_at_once_still_make_their_join_ready(self):
        for round_ in range(15):
            with self.subTest(round=round_):
                task_id = await self.create_task()
                dag = await self.store.create(
                    task_id, 1, make_plan(node("x"), node("y"), node("j", "x", "y"))
                )
                await self.store.acquire(dag.id, "w", RUN)
                x = await self.store.start_node(dag.id, 1, "x", max_attempts=6)
                y = await self.store.start_node(dag.id, 1, "y", max_attempts=6)
                left, right = self.new_store(), self.new_store()

                await asyncio.gather(
                    left.complete_node(dag.id, 1, "x", x.number, result("x")),
                    right.complete_node(dag.id, 1, "y", y.number, result("y")),
                )

                final = await self.store.get_by_id(dag.id)
                self.assertEqual(final.node("j").state, S.READY)

    async def test_two_starts_of_the_same_node_start_it_once(self):
        for round_ in range(10):
            with self.subTest(round=round_):
                dag = await self.taken_dag(make_plan(node("only")))
                left, right = self.new_store(), self.new_store()

                results = await asyncio.gather(
                    left.start_node(dag.id, 1, "only", max_attempts=6),
                    right.start_node(dag.id, 1, "only", max_attempts=6),
                    return_exceptions=True,
                )

                raise_unexpected(results, NodeStateError)
                self.assertEqual(
                    sum(not isinstance(r, BaseException) for r in results), 1
                )
                self.assertEqual(len(await self.store.attempts(dag.id)), 1)

    async def test_a_worker_that_crashes_mid_node_is_replaced_and_its_report_is_lost(
        self,
    ):
        dag = await self.taken_dag(owner="crashed")
        started = await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        # The worker dies here. Its lease expires and another worker takes over.
        second = self.new_store()
        taken = await second.acquire(dag.id, "rescuer", RUN)
        self.assertEqual(taken.node("a").state, S.READY)
        restart = await second.start_node(dag.id, taken.epoch, "a", max_attempts=6)
        finished = await second.complete_node(
            dag.id, taken.epoch, "a", restart.number, result("rescued")
        )

        # The crashed worker comes back and reports what it had computed.
        with self.assertRaises(StaleDagEpochError):
            await self.store.complete_node(
                dag.id, 1, "a", started.number, result("zombie")
            )

        self.assertEqual(finished.node("a").result, result("rescued"))
        self.assertEqual(
            [(a.number, a.state) for a in await self.store.attempts(dag.id, "a")],
            [(1, AttemptState.INTERRUPTED), (2, AttemptState.SUCCEEDED)],
        )


def update_node(assignments: str) -> str:
    return f"UPDATE agent_dag_nodes SET {assignments} WHERE dag_id = :d AND key = 'a'"


def insert_node(key="k", ordinal=9, role="worker", data="{}") -> str:
    return (
        "INSERT INTO agent_dag_nodes"
        " (dag_id, key, ordinal, role, title, goal, input, required, state)"
        f" VALUES (:d, '{key}', {ordinal}, '{role}', 't', 'g', '{data}', true, 'ready')"
    )


def insert_attempt(key="a", number=1, epoch=1) -> str:
    return (
        "INSERT INTO agent_dag_node_attempts"
        " (dag_id, node_key, number, agent_index, approach, epoch, state)"
        f" VALUES (:d, '{key}', {number}, 0, 0, {epoch}, 'running')"
    )


def update_attempts(assignments: str) -> str:
    return f"UPDATE agent_dag_node_attempts SET {assignments} WHERE dag_id = :d"


INSERT_DAG = (
    "INSERT INTO agent_dags (id, task_id, attempt, node_count, plan_bytes)"
    " SELECT gen_random_uuid(), task_id, {attempt}, {count}, {size}"
    " FROM agent_dags WHERE id = :d"
)


@requires_postgres
class DatabaseRulesTest(PostgresOrchestratorTestCase):
    """What PostgreSQL refuses whatever the application does (CHECK constraints)."""

    async def refused(self, sql: str, **parameters) -> None:
        async with self.database.session() as session:
            with self.assertRaises(DBAPIError):
                await session.execute(text(sql), parameters)
                await session.commit()

    async def test_the_database_refuses_inconsistent_rows(self):
        dag = await self.taken_dag()
        big = "jsonb_build_object('x', repeat('a', 70000))"
        cases = [
            ("a result on a node that did not succeed", update_node("result = '{}'")),
            ("a succeeded node without a result", update_node("state = 'succeeded'")),
            ("a running node without an attempt", update_node("state = 'running'")),
            (
                "a result that is not an object",
                update_node("state = 'succeeded', result = '[1]'"),
            ),
            (
                "a result above the size backstop",
                update_node(f"state = 'succeeded', result = {big}"),
            ),
            ("an unknown node state", update_node("state = 'done'")),
            ("more rung attempts than attempts", update_node("rung_attempts = 3")),
            ("a rung outside the ladder", update_node("agent_index = 4")),
            ("an approach beyond the loop detector's", update_node("approach = 101")),
            (
                "an owner without an epoch",
                "UPDATE agent_dags SET epoch = 0 WHERE id = :d",
            ),
            ("a negative epoch", "UPDATE agent_dags SET epoch = -1 WHERE id = :d"),
            (
                "an unknown DAG state",
                "UPDATE agent_dags SET state = 'paused' WHERE id = :d",
            ),
            (
                "a node that depends on itself",
                "INSERT INTO agent_dag_edges VALUES (:d, 'a', 'a')",
            ),
            (
                "an edge to a node of nowhere",
                "INSERT INTO agent_dag_edges VALUES (:d, 'a', 'zz')",
            ),
            ("a node with an invalid key", insert_node(key="Bad Key")),
            ("a node with an unknown role", insert_node(role="boss")),
            ("a node with an input that is not an object", insert_node(data="[]")),
            ("two nodes at the same ordinal", insert_node(ordinal=0)),
            (
                "a second DAG for the same attempt",
                INSERT_DAG.format(attempt=1, count=1, size=1),
            ),
            ("a DAG of no node", INSERT_DAG.format(attempt=9, count=0, size=1)),
            (
                "a plan declared as zero bytes",
                INSERT_DAG.format(attempt=9, count=1, size=0),
            ),
            (
                "a plan declared one byte over the limit",
                INSERT_DAG.format(attempt=9, count=1, size=131073),
            ),
            (
                "a DAG of an unknown task",
                "INSERT INTO agent_dags (id, task_id, attempt, node_count, plan_bytes)"
                " VALUES (gen_random_uuid(), gen_random_uuid(), 1, 1, 1)",
            ),
        ]
        for label, sql in cases:
            with self.subTest(label):
                await self.refused(sql, d=dag.id)
        # Nothing was changed by the refused statements.
        self.assertEqual(await self.store.get_by_id(dag.id), dag)

    async def test_the_database_accepts_a_plan_of_exactly_the_limit(self):
        dag = await self.taken_dag()
        async with self.database.session() as session:
            await session.execute(
                text(INSERT_DAG.format(attempt=9, count=1, size=131072)), {"d": dag.id}
            )
            await session.commit()
        self.assertEqual(
            await self.scalar("SELECT plan_bytes FROM agent_dags WHERE attempt = 9"),
            131072,
        )

    async def test_the_stored_plan_size_is_the_plans_encoded_size(self):
        plan = diamond()
        task_id = await self.create_task()

        await self.store.create(task_id, 1, plan)

        self.assertEqual(
            await self.scalar("SELECT plan_bytes FROM agent_dags"), plan.encoded_bytes
        )

    async def test_the_database_refuses_inconsistent_attempts(self):
        dag = await self.taken_dag()
        await self.store.start_node(dag.id, 1, "a", max_attempts=6)
        cases = [
            (
                "a running attempt that has finished",
                update_attempts("finished_at = now()"),
            ),
            (
                "a finished attempt that still runs",
                update_attempts("state = 'succeeded'"),
            ),
            (
                "a failed attempt without a class",
                update_attempts("state = 'failed', finished_at = now()"),
            ),
            (
                "a signature that is not a hash",
                update_attempts("failure_signature = 'nope'"),
            ),
            ("the same attempt number twice", insert_attempt(number=1)),
            ("an attempt of no epoch", insert_attempt(key="e", epoch=0)),
        ]
        for label, sql in cases:
            with self.subTest(label):
                await self.refused(sql, d=dag.id)


if __name__ == "__main__":
    unittest.main()
