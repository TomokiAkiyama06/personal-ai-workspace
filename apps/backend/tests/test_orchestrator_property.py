"""Random DAGs through the whole orchestrator (real PostgreSQL, scripted agents).

For every random DAG (with random dependencies, required and optional nodes, nodes
that fail for good, nodes that fail once and then succeed, and a random cap on the
parallelism) the run must satisfy the requirements' rules, which are also checked
without a database in ``test_orchestrator_scheduling``:

* every node that can run runs to success **once** (a flaky node fails once first),
  a node that failed for good ran once, a blocked node never ran;
* no node starts before every node it depends on has ended;
* never more nodes run at once than the cap;
* the failure of a node blocks exactly the nodes that depend on it (transitively);
  independent nodes still run;
* the DAG (and so the task) succeeds exactly when every required node succeeded;
* each node received exactly the results of its direct dependencies.
"""

import asyncio
import random
import unittest

from paw_backend.orchestrator.domain import RunOutcome
from paw_backend.tasks import TaskState

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    make_plan,
    node,
    ok,
    requires_postgres,
)

SEEDS = range(16)


def behaviour(key: str, pause: float, seen: dict):
    """A node that takes a moment (so completion order is not fixed) and records
    the results it was given."""

    async def run(assignment):
        await asyncio.sleep(pause)
        seen[key] = {d: r.summary for d, r in assignment.upstream.items()}
        return ok(f"{key} done")

    return run


def random_plan(rng: random.Random):
    count = rng.randint(1, 10)
    keys = [f"n{i}" for i in range(count)]
    nodes = []
    for index, key in enumerate(keys):
        earlier = keys[:index]
        deps = rng.sample(earlier, k=min(len(earlier), rng.randint(0, 3)))
        role = rng.choice(["worker", "worker", "researcher", "reviewer", "planner"])
        required = rng.random() < 0.75 or index == 0
        nodes.append(node(key, *deps, role=role, required=required))
    order = list(range(count))
    rng.shuffle(order)  # the proposal lists the nodes in a random order
    return make_plan(*(nodes[i] for i in order))


@requires_postgres
class RandomDagTest(PostgresOrchestratorTestCase):
    async def test_random_dags_run_as_the_requirements_say(self):
        failed_dags = blocked_seen = flaky_seen = 0
        for seed in SEEDS:
            rng = random.Random(seed)
            plan = random_plan(rng)
            keys = [n.key for n in plan.nodes]
            failing = {k for k in keys if rng.random() < 0.2}
            flaky = {k for k in keys if k not in failing and rng.random() < 0.2}
            cap = rng.randint(1, 4)
            pauses = {k: rng.random() * 0.004 for k in keys}

            seen_upstream: dict[str, dict[str, str]] = {}
            script = {}
            for key in keys:
                if key in failing:
                    script[key] = fail("Fatal", f"{key} cannot", retryable=False)
                elif key in flaky:
                    script[key] = [
                        fail("Flaky", f"{key} flaked"),
                        behaviour(key, pauses[key], seen_upstream),
                    ]
                else:
                    script[key] = behaviour(key, pauses[key], seen_upstream)
            timeline = []
            runtime = FakeRuntime("local", script=script, timeline=timeline)
            h = self.harness(
                runtimes={"local": runtime}, config={"max_parallel_nodes": cap}
            )
            task_id = await self.prepare(h, plan)
            with self.subTest(seed=seed, nodes=len(keys), cap=cap):
                report = await asyncio.wait_for(h.orchestrator.run_once("w1"), 180)

                deps = {n.key: n.depends_on for n in plan.nodes}
                required = {n.key: n.required for n in plan.nodes}
                # A node is blocked when something it depends on failed or is blocked.
                blocked: set[str] = set()
                for n in plan.nodes:  # topological order
                    if any(d in failing or d in blocked for d in n.depends_on):
                        blocked.add(n.key)
                dag = await self.store.get(task_id, 1)
                for key in keys:
                    stored = dag.node(key)
                    calls = len(runtime.calls_of(key))
                    if key in blocked:
                        self.assertEqual(stored.state.value, "blocked", key)
                        self.assertEqual(calls, 0, key)
                    elif key in failing:
                        self.assertEqual(stored.state.value, "failed", key)
                        self.assertEqual(calls, 1, key)
                    else:
                        self.assertEqual(stored.state.value, "succeeded", key)
                        self.assertEqual(calls, 2 if key in flaky else 1, key)
                        # It succeeded exactly once, and received its dependencies'
                        # results (and only theirs).
                        self.assertEqual(stored.result.summary, f"{key} done")
                        self.assertEqual(
                            seen_upstream[key],
                            {d: f"{d} done" for d in deps[key]},
                            key,
                        )
                    self.assertEqual(stored.attempt_count, calls, key)
                # Order: a node starts after all its dependencies ended.
                position = {event: i for i, event in enumerate(timeline)}
                for key in keys:
                    for dependency in deps[key]:
                        first_start = min(
                            (
                                p
                                for (kind, k, _), p in position.items()
                                if kind == "start" and k == key
                            ),
                            default=None,
                        )
                        if first_start is None:
                            continue
                        last_end = max(
                            p
                            for (kind, k, _), p in position.items()
                            if kind == "end" and k == dependency
                        )
                        self.assertLess(last_end, first_start, (key, dependency))
                # The parallelism cap.
                live = peak = 0
                for kind, _key, _attempt in timeline:
                    live += 1 if kind == "start" else -1
                    peak = max(peak, live)
                self.assertLessEqual(peak, cap)
                # The verdict.
                succeeded = all(
                    dag.node(k).state.value == "succeeded" for k in keys if required[k]
                )
                self.assertEqual(
                    report.outcome,
                    RunOutcome.DAG_SUCCEEDED if succeeded else RunOutcome.DAG_FAILED,
                )
                snapshot = await h.tasks.restore(task_id)
                self.assertEqual(
                    snapshot.state,
                    TaskState.EVALUATING if succeeded else TaskState.FAILED,
                )
                failed_dags += not succeeded
                blocked_seen += bool(blocked)
                flaky_seen += bool(flaky)
        # The generator really produced failing DAGs, blocked nodes and flaky nodes.
        self.assertGreater(failed_dags, 3)
        self.assertGreater(blocked_seen, 3)
        self.assertGreater(flaky_seen, 3)


if __name__ == "__main__":
    unittest.main()
