"""The pure scheduling rules: readiness, failure propagation, the DAG verdict.

No database. A small simulator drives the pure functions over random DAGs with
random failures and checks the properties the requirements state: every node runs
once, a node never starts before the nodes it depends on succeeded, a failure
blocks exactly the nodes that depend on the failed node (transitively), and
independent nodes keep running.
"""

import random
import unittest

from paw_backend.orchestrator.domain import NodeState
from paw_backend.orchestrator.scheduling import (
    DagVerdict,
    NodeView,
    dag_verdict,
    ready_batch,
    settle_states,
    transitive_dependents,
)

S = NodeState


def view(key, ordinal, state=S.PENDING, deps=(), required=True) -> NodeView:
    return NodeView(key, ordinal, state, tuple(deps), required)


def diamond(**states) -> list[NodeView]:
    """a -> (b, c) -> d, and an independent e."""
    return [
        view("a", 0, states.get("a", S.PENDING)),
        view("b", 1, states.get("b", S.PENDING), ["a"]),
        view("c", 2, states.get("c", S.PENDING), ["a"]),
        view("d", 3, states.get("d", S.PENDING), ["b", "c"]),
        view("e", 4, states.get("e", S.PENDING)),
    ]


class SettleStatesTest(unittest.TestCase):
    def test_a_node_without_dependencies_is_ready(self):
        self.assertEqual(
            settle_states(diamond()),
            {
                "a": S.READY,
                "b": S.PENDING,
                "c": S.PENDING,
                "d": S.PENDING,
                "e": S.READY,
            },
        )

    def test_dependents_become_ready_only_when_every_dependency_succeeded(self):
        states = settle_states(diamond(a=S.SUCCEEDED, b=S.SUCCEEDED))
        self.assertEqual(states["b"], S.SUCCEEDED)
        self.assertEqual(states["c"], S.READY)
        self.assertEqual(states["d"], S.PENDING)  # c has not succeeded yet
        states = settle_states(diamond(a=S.SUCCEEDED, b=S.SUCCEEDED, c=S.SUCCEEDED))
        self.assertEqual(states["d"], S.READY)

    def test_a_running_dependency_keeps_its_dependents_waiting(self):
        states = settle_states(diamond(a=S.RUNNING))
        self.assertEqual(states["a"], S.RUNNING)
        self.assertEqual((states["b"], states["c"], states["d"]), (S.PENDING,) * 3)

    def test_a_failure_blocks_its_transitive_dependents_and_only_them(self):
        states = settle_states(diamond(a=S.FAILED))
        self.assertEqual(states["a"], S.FAILED)
        self.assertEqual((states["b"], states["c"], states["d"]), (S.BLOCKED,) * 3)
        self.assertEqual(states["e"], S.READY)  # independent: unaffected

    def test_a_failure_in_one_branch_blocks_the_join_but_not_the_other_branch(self):
        states = settle_states(diamond(a=S.SUCCEEDED, b=S.FAILED))
        self.assertEqual(states["c"], S.READY)
        self.assertEqual(states["d"], S.BLOCKED)

    def test_a_cancelled_dependency_blocks_like_a_failed_one(self):
        self.assertEqual(settle_states(diamond(a=S.CANCELLED))["b"], S.BLOCKED)

    def test_blocked_nodes_recover_when_the_failed_node_is_reopened(self):
        nodes = diamond(a=S.READY, b=S.BLOCKED, c=S.BLOCKED, d=S.BLOCKED)
        states = settle_states(nodes)
        self.assertEqual(
            (states["a"], states["b"], states["c"], states["d"]),
            (S.READY, S.PENDING, S.PENDING, S.PENDING),
        )

    def test_settled_and_running_nodes_are_never_changed(self):
        for state in (S.RUNNING, S.SUCCEEDED, S.FAILED, S.CANCELLED):
            with self.subTest(state=state):
                nodes = [view("a", 0, S.FAILED), view("b", 1, state, ["a"])]
                self.assertEqual(settle_states(nodes)["b"], state)


class ReadyBatchTest(unittest.TestCase):
    def test_independent_nodes_start_together_lowest_ordinal_first(self):
        nodes = [view(f"n{i}", i, S.READY) for i in range(6)]
        self.assertEqual(ready_batch(nodes, 4), ("n0", "n1", "n2", "n3"))
        self.assertEqual(ready_batch(nodes, 10), tuple(f"n{i}" for i in range(6)))

    def test_only_ready_nodes_start(self):
        nodes = [
            view("a", 0, S.RUNNING),
            view("b", 1, S.PENDING, ["a"]),
            view("c", 2, S.READY),
            view("d", 3, S.SUCCEEDED),
            view("e", 4, S.BLOCKED),
        ]
        self.assertEqual(ready_batch(nodes, 5), ("c",))

    def test_capacity_and_exclusions(self):
        nodes = [view(f"n{i}", i, S.READY) for i in range(4)]
        self.assertEqual(ready_batch(nodes, 0), ())
        self.assertEqual(ready_batch(nodes, -1), ())
        self.assertEqual(ready_batch(nodes, 2, exclude=frozenset({"n0"})), ("n1", "n2"))


class VerdictTest(unittest.TestCase):
    def test_the_dag_is_active_while_a_node_is_ready_or_running(self):
        for state in (S.READY, S.RUNNING):
            nodes = [view("a", 0, S.SUCCEEDED), view("b", 1, state, ["a"])]
            self.assertEqual(dag_verdict(nodes), DagVerdict.ACTIVE)

    def test_the_dag_succeeds_when_every_required_node_succeeded(self):
        nodes = [
            view("a", 0, S.SUCCEEDED),
            view("b", 1, S.FAILED, required=False),
            view("c", 2, S.BLOCKED, ["b"], required=False),
        ]
        self.assertEqual(dag_verdict(nodes), DagVerdict.SUCCEEDED)

    def test_the_dag_fails_when_a_required_node_did_not_succeed(self):
        for state in (S.FAILED, S.BLOCKED, S.CANCELLED, S.PENDING):
            with self.subTest(state=state):
                nodes = [view("a", 0, S.SUCCEEDED), view("b", 1, state, ["a"])]
                self.assertEqual(dag_verdict(nodes), DagVerdict.FAILED)

    def test_transitive_dependents(self):
        nodes = diamond()
        self.assertEqual(transitive_dependents(nodes, "a"), {"b", "c", "d"})
        self.assertEqual(transitive_dependents(nodes, "b"), {"d"})
        self.assertEqual(transitive_dependents(nodes, "d"), frozenset())
        self.assertEqual(transitive_dependents(nodes, "e"), frozenset())


def random_dag(rng: random.Random) -> list[NodeView]:
    count = rng.randint(1, 25)
    nodes: list[NodeView] = []
    for ordinal in range(count):
        earlier = [n.key for n in nodes]
        deps = rng.sample(earlier, k=min(len(earlier), rng.randint(0, 3)))
        required = rng.random() < 0.8
        nodes.append(view(f"n{ordinal}", ordinal, S.PENDING, sorted(deps), required))
    return nodes


def simulate(nodes: list[NodeView], failing: set[str], max_parallel: int, rng):
    """Run the pure scheduler to the end.

    Returns ``(events, final states, verdict)`` where ``events`` lists
    ``("start", key)`` and ``("finish", key)`` in the order they happened. A node
    in ``failing`` fails; every other one succeeds. Which running node finishes
    next is chosen by ``rng`` (completion order is free).
    """
    states = {n.key: n.state for n in nodes}
    events: list[tuple[str, str]] = []
    running: list[str] = []

    def views():
        return [
            NodeView(n.key, n.ordinal, states[n.key], n.depends_on, n.required)
            for n in nodes
        ]

    for _ in range(10 * len(nodes) + 10):
        states = settle_states(views())
        for key in ready_batch(views(), max_parallel - len(running)):
            states[key] = S.RUNNING
            running.append(key)
            events.append(("start", key))
            assert len(running) <= max_parallel
        verdict = dag_verdict(views())
        if verdict is not DagVerdict.ACTIVE:
            return events, states, verdict
        key = running.pop(rng.randrange(len(running)))
        states[key] = S.FAILED if key in failing else S.SUCCEEDED
        events.append(("finish", key))
    raise AssertionError("the simulation did not end")


def started(events) -> list[str]:
    return [key for kind, key in events if kind == "start"]


class RandomDagPropertyTest(unittest.TestCase):
    def assert_edges_respected(self, nodes, events):
        """Every node starts after each of its dependencies has finished."""
        position = {event: i for i, event in enumerate(events)}
        for node in nodes:
            if ("start", node.key) not in position:
                continue
            for dependency in node.depends_on:
                self.assertLess(
                    position[("finish", dependency)], position[("start", node.key)]
                )

    def test_random_dags_without_failures_run_every_node_once_in_order(self):
        for seed in range(300):
            rng = random.Random(seed)
            nodes = random_dag(rng)
            parallel = rng.randint(1, 6)
            with self.subTest(seed=seed):
                events, states, verdict = simulate(nodes, set(), parallel, rng)
                self.assertEqual(sorted(started(events)), sorted(n.key for n in nodes))
                self.assertEqual(
                    sorted(k for kind, k in events if kind == "finish"),
                    sorted(n.key for n in nodes),
                )
                self.assertEqual(set(states.values()), {S.SUCCEEDED})
                self.assertIs(verdict, DagVerdict.SUCCEEDED)
                self.assert_edges_respected(nodes, events)

    def test_random_dags_with_failures_propagate_as_specified(self):
        failed_runs = 0
        blocked_seen = 0
        for seed in range(400):
            rng = random.Random(5000 + seed)
            nodes = random_dag(rng)
            failing = {n.key for n in nodes if rng.random() < 0.25}
            parallel = rng.randint(1, 6)
            with self.subTest(seed=seed):
                events, states, verdict = simulate(nodes, failing, parallel, rng)
                order = started(events)
                by_key = {n.key: n for n in nodes}
                # A failing node blocks everything that depends on it, and only
                # that. A failing node that is itself blocked never ran.
                blocked: set[str] = set()
                for node in nodes:  # ordinal order: dependencies come first
                    if any(d in blocked or d in failing for d in node.depends_on):
                        blocked.add(node.key)
                for key in by_key:
                    if key in blocked:
                        self.assertIs(states[key], S.BLOCKED, key)
                        self.assertNotIn(key, order)
                    elif key in failing:
                        self.assertIs(states[key], S.FAILED, key)
                        self.assertEqual(order.count(key), 1)
                    else:
                        # Independent of every failure: it still ran, once.
                        self.assertIs(states[key], S.SUCCEEDED, key)
                        self.assertEqual(order.count(key), 1)
                self.assert_edges_respected(nodes, events)
                required_ok = all(
                    states[k] is S.SUCCEEDED for k, n in by_key.items() if n.required
                )
                self.assertIs(
                    verdict, DagVerdict.SUCCEEDED if required_ok else DagVerdict.FAILED
                )
                failed_runs += not required_ok
                blocked_seen += bool(blocked)
        # The property test really saw failing DAGs and blocked nodes.
        self.assertGreater(failed_runs, 40)
        self.assertGreater(blocked_seen, 40)

    def test_the_same_dag_and_choices_give_the_same_events(self):
        for seed in range(100):
            rng = random.Random(9000 + seed)
            nodes = random_dag(rng)
            failing = {n.key for n in nodes if rng.random() < 0.2}
            parallel = rng.randint(1, 4)
            first = simulate(nodes, failing, parallel, random.Random(seed))
            second = simulate(nodes, failing, parallel, random.Random(seed))
            with self.subTest(seed=seed):
                self.assertEqual(first, second)

    def test_with_one_slot_the_nodes_run_in_ordinal_order_of_readiness(self):
        # A chain of independent nodes with one slot starts them 0, 1, 2, ...
        nodes = [view(f"n{i}", i) for i in range(8)]
        events, states, verdict = simulate(nodes, set(), 1, random.Random(0))
        self.assertEqual(started(events), [f"n{i}" for i in range(8)])
        self.assertIs(verdict, DagVerdict.SUCCEEDED)

    def test_all_independent_nodes_start_before_any_finishes_given_room(self):
        nodes = [view(f"n{i}", i) for i in range(5)]
        events, _, _ = simulate(nodes, set(), 8, random.Random(0))
        self.assertEqual([kind for kind, _ in events[:5]], ["start"] * 5)


if __name__ == "__main__":
    unittest.main()
