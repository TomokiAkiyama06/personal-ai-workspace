"""The plan a planner proposes: validation, cycle detection, size limits (PAW-034).

No database. A plan is judged when it is built, so a table of bad plans (each with
the closed reason it must be refused for) and a table of plans at the limits cover
the whole schema; a random-graph property test checks the topological order and
the cycle detection on hundreds of DAGs.
"""

import random
import unittest
import uuid

from paw_backend.authz import Capability
from paw_backend.orchestrator.domain import ROLE_CEILING, NodeRole
from paw_backend.orchestrator.errors import InvalidPlanError, PlanReason
from paw_backend.orchestrator.limits import (
    MAX_DEPENDENCIES,
    MAX_DEPTH,
    MAX_EDGES,
    MAX_GOAL_CHARS,
    MAX_NODE_INPUT_BYTES,
    MAX_NODES,
    MAX_TITLE_CHARS,
)
from paw_backend.orchestrator.plan import NODE_FIELDS, Plan, PlanNode

R = PlanReason


def node(key: str = "a", **overrides) -> dict:
    data = {"key": key, "role": "worker", "title": f"Node {key}", "goal": "Do it"}
    data.update(overrides)
    return data


def plan(*nodes: dict) -> dict:
    return {"nodes": list(nodes)}


def chain(length: int) -> list[dict]:
    return [node(f"n{i}", depends_on=[f"n{i - 1}"] if i else []) for i in range(length)]


def layers(widths: list[int], fan_in: int) -> list[dict]:
    """Layered DAG: every node depends on the ``fan_in`` first nodes of the layer
    before it."""
    nodes: list[dict] = []
    previous: list[str] = []
    for level, width in enumerate(widths):
        current = [f"l{level}n{i}" for i in range(width)]
        for key in current:
            nodes.append(node(key, depends_on=previous[:fan_in]))
        previous = current
    return nodes


class RefusedPlansTest(unittest.TestCase):
    def assertRefused(self, data, reason: PlanReason):
        with self.assertRaises(InvalidPlanError) as caught:
            Plan.from_mapping(data)
        self.assertEqual(caught.exception.reason, reason)

    def test_every_bad_plan_is_refused_for_its_reason(self):
        big_input = {"text": "x" * (MAX_NODE_INPUT_BYTES + 1)}
        deep = current = {}
        for _ in range(9):
            current["n"] = {}
            current = current["n"]
        cases = [
            # the plan itself
            ("a list", [node()], R.NOT_A_MAPPING),
            ("None", None, R.NOT_A_MAPPING),
            ("text", "nodes", R.NOT_A_MAPPING),
            (
                "unknown top-level field",
                {"nodes": [node()], "extra": 1},
                R.UNKNOWN_FIELD,
            ),
            ("no nodes field", {}, R.MISSING_FIELD),
            ("nodes as text", {"nodes": "a"}, R.BAD_TYPE),
            ("nodes as a mapping", {"nodes": node()}, R.BAD_TYPE),
            ("nodes as None", {"nodes": None}, R.BAD_TYPE),
            ("nodes as a number", {"nodes": 3}, R.BAD_TYPE),
            ("no node", plan(), R.EMPTY),
            # a node
            ("a node that is text", plan("a"), R.NOT_A_MAPPING),
            ("a misspelled field", plan(node(depend_on=[])), R.UNKNOWN_FIELD),
            ("a non-text field name", plan({**node(), 5: 1}), R.UNKNOWN_FIELD),
            *[
                (
                    f"no {name}",
                    plan({k: v for k, v in node().items() if k != name}),
                    R.MISSING_FIELD,
                )
                for name in ("key", "role", "title", "goal")
            ],
            # keys
            ("an upper-case key", plan(node("A")), R.BAD_KEY),
            ("a key that starts with a digit", plan(node("1a")), R.BAD_KEY),
            ("an empty key", plan(node("")), R.BAD_KEY),
            ("a key with a space", plan(node("a b")), R.BAD_KEY),
            ("a key of 33 characters", plan(node("a" * 33)), R.BAD_KEY),
            ("a key with a newline at the end", plan(node("a\n")), R.BAD_KEY),
            ("a key that is a number", plan(node(5)), R.BAD_TYPE),
            ("a repeated key", plan(node("a"), node("a")), R.DUPLICATE_KEY),
            # roles
            ("an unknown role", plan(node(role="boss")), R.UNKNOWN_ROLE),
            ("a role in capitals", plan(node(role="Worker")), R.UNKNOWN_ROLE),
            ("a role that is a number", plan(node(role=5)), R.BAD_TYPE),
            # texts
            ("a blank title", plan(node(title="  ")), R.BAD_TEXT),
            ("a long title", plan(node(title="t" * (MAX_TITLE_CHARS + 1))), R.BAD_TEXT),
            ("a title with a newline", plan(node(title="a\nb")), R.BAD_TEXT),
            ("a title with a NUL", plan(node(title="a\x00b")), R.BAD_TEXT),
            ("a title with a surrogate", plan(node(title="a\ud800b")), R.BAD_TEXT),
            ("a title that is a number", plan(node(title=5)), R.BAD_TYPE),
            ("a blank goal", plan(node(goal="\n ")), R.BAD_TEXT),
            ("a long goal", plan(node(goal="g" * (MAX_GOAL_CHARS + 1))), R.BAD_TEXT),
            ("a goal with a NUL", plan(node(goal="a\x00b")), R.BAD_TEXT),
            ("a goal with a bell", plan(node(goal="a\x07b")), R.BAD_TEXT),
            ("a goal with a surrogate", plan(node(goal="\udfff")), R.BAD_TEXT),
            ("a goal that is bytes", plan(node(goal=b"do it")), R.BAD_TYPE),
            # dependencies
            ("dependencies as text", plan(node(depends_on="a")), R.BAD_TYPE),
            ("dependencies as a mapping", plan(node(depends_on={"a": 1})), R.BAD_TYPE),
            (
                "a dependency that is not a key",
                plan(node(depends_on=["A B"])),
                R.BAD_KEY,
            ),
            ("a dependency that is a number", plan(node(depends_on=[1])), R.BAD_TYPE),
            (
                "an unknown dependency",
                plan(node("a"), node("b", depends_on=["zzz"])),
                R.UNKNOWN_DEPENDENCY,
            ),
            (
                "a node that depends on itself",
                plan(node("a", depends_on=["a"])),
                R.SELF_DEPENDENCY,
            ),
            (
                "a repeated dependency",
                plan(node("a"), node("b", depends_on=["a", "a"])),
                R.DUPLICATE_DEPENDENCY,
            ),
            (
                "too many dependencies",
                plan(
                    *[node(f"d{i}") for i in range(MAX_DEPENDENCIES + 1)],
                    node(
                        "z", depends_on=[f"d{i}" for i in range(MAX_DEPENDENCIES + 1)]
                    ),
                ),
                R.TOO_MANY_DEPENDENCIES,
            ),
            # flags, inputs, narrowing
            ("required as text", plan(node(required="yes")), R.BAD_TYPE),
            ("required as a number", plan(node(required=1)), R.BAD_TYPE),
            ("input as a list", plan(node(input=[1])), R.BAD_TYPE),
            ("input with NaN", plan(node(input={"x": float("nan")})), R.BAD_INPUT),
            ("input with a NUL", plan(node(input={"x": "a\x00"})), R.BAD_INPUT),
            ("input with a number key", plan(node(input={1: "a"})), R.BAD_INPUT),
            ("input with a set", plan(node(input={"x": {1}})), R.BAD_INPUT),
            ("input nested too deep", plan(node(input=deep)), R.BAD_INPUT),
            ("input too large", plan(node(input=big_input)), R.TOO_LARGE),
            (
                "capabilities as text",
                plan(node(capabilities="project.read")),
                R.BAD_TYPE,
            ),
            (
                "an unknown capability",
                plan(node(capabilities=["project.fly"])),
                R.UNKNOWN_CAPABILITY,
            ),
            ("a capability that is a number", plan(node(capabilities=[3])), R.BAD_TYPE),
            (
                "a capability no role may hold",
                plan(node(capabilities=["admin.audit.view"])),
                R.CAPABILITY_ABOVE_ROLE,
            ),
            (
                "a planner that asks to write",
                plan(node(role="planner", capabilities=["project.repo.write"])),
                R.CAPABILITY_ABOVE_ROLE,
            ),
            (
                "a reviewer that asks to run tasks",
                plan(node(role="reviewer", capabilities=["project.task.run"])),
                R.CAPABILITY_ABOVE_ROLE,
            ),
            (
                "a researcher that asks for the pull request capability",
                plan(node(role="researcher", capabilities=["project.pr.create"])),
                R.CAPABILITY_ABOVE_ROLE,
            ),
            ("repositories as text", plan(node(repositories="x")), R.BAD_TYPE),
            (
                "a repository that is not an id",
                plan(node(repositories=["r1"])),
                R.BAD_REPOSITORY,
            ),
            (
                "a repository id in capitals",
                plan(node(repositories=[str(uuid.uuid4()).upper()])),
                R.BAD_REPOSITORY,
            ),
            ("a repository that is a number", plan(node(repositories=[1])), R.BAD_TYPE),
            # the graph
            (
                "a cycle of two",
                plan(node("a", depends_on=["b"]), node("b", depends_on=["a"])),
                R.CYCLE,
            ),
            (
                "a cycle of three behind an independent node",
                plan(
                    node("free"),
                    node("a", depends_on=["c"]),
                    node("b", depends_on=["a"]),
                    node("c", depends_on=["b"]),
                ),
                R.CYCLE,
            ),
            (
                "a cycle that hangs off a valid start",
                plan(
                    node("start"),
                    node("a", depends_on=["start", "b"]),
                    node("b", depends_on=["a"]),
                ),
                R.CYCLE,
            ),
            ("a chain that is too deep", plan(*chain(MAX_DEPTH + 1)), R.TOO_DEEP),
            (
                "too many edges",
                plan(*layers([8, 8, 8, 8], fan_in=5)),
                R.TOO_MANY_EDGES,
            ),
            (
                "too many nodes",
                plan(*[node(f"n{i}") for i in range(MAX_NODES + 1)]),
                R.TOO_MANY_NODES,
            ),
            (
                "only optional nodes",
                plan(node("a", required=False), node("b", required=False)),
                R.NO_REQUIRED_NODE,
            ),
            (
                "a plan whose inputs together are too large",
                plan(
                    *[
                        node(f"n{i}", input={"t": "x" * (MAX_NODE_INPUT_BYTES - 64)})
                        for i in range(10)
                    ]
                ),
                R.TOO_LARGE,
            ),
        ]
        for label, data, reason in cases:
            with self.subTest(label):
                self.assertRefused(data, reason)

    def test_the_error_message_never_quotes_the_plan(self):
        secret = "sk-secret-node-key-value"
        with self.assertRaises(InvalidPlanError) as caught:
            Plan.from_mapping(plan(node("a", goal=secret, depends_on=[secret.upper()])))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(caught.exception.reason, R.BAD_KEY)

    def test_the_schema_lists_exactly_the_fields_of_the_documentation(self):
        self.assertEqual(
            NODE_FIELDS,
            {
                "key",
                "role",
                "title",
                "goal",
                "depends_on",
                "required",
                "input",
                "capabilities",
                "repositories",
            },
        )


class AcceptedPlansTest(unittest.TestCase):
    def test_plans_at_every_limit_are_accepted(self):
        cases = [
            ("one node", plan(node())),
            ("the most nodes", plan(*[node(f"n{i}") for i in range(MAX_NODES)])),
            ("the longest chain", plan(*chain(MAX_DEPTH))),
            ("a key of 32 characters", plan(node("a" * 32))),
            ("the longest title", plan(node(title="t" * MAX_TITLE_CHARS))),
            ("the longest goal", plan(node(goal="g" * MAX_GOAL_CHARS))),
            (
                "the most dependencies",
                plan(
                    *[node(f"d{i}") for i in range(MAX_DEPENDENCIES)],
                    node("z", depends_on=[f"d{i}" for i in range(MAX_DEPENDENCIES)]),
                ),
            ),
            ("the most edges", plan(*layers([8, 8, 8, 8], fan_in=4))),
            (
                "an input at the limit",
                plan(node(input={"t": "x" * (MAX_NODE_INPUT_BYTES - 8)})),
            ),
            ("a goal with lines", plan(node(goal="one\n\ttwo\nthree"))),
            (
                "an optional node next to a required one",
                plan(node("a"), node("b", required=False)),
            ),
        ]
        for label, data in cases:
            with self.subTest(label):
                accepted = Plan.from_mapping(data)
                self.assertGreaterEqual(len(accepted.nodes), 1)
        self.assertEqual(
            len(Plan.from_mapping(plan(*layers([8, 8, 8, 8], 4))).edges), MAX_EDGES
        )

    def test_the_nodes_come_out_in_a_deterministic_topological_order(self):
        proposal = plan(
            node("d", depends_on=["b", "c"]),
            node("c", depends_on=["a"]),
            node("b", depends_on=["a"]),
            node("a"),
            node("free"),
        )

        accepted = Plan.from_mapping(proposal)

        # Among the nodes that can go, the one proposed first goes first.
        self.assertEqual([n.key for n in accepted.nodes], ["a", "c", "b", "d", "free"])
        self.assertEqual(
            accepted.edges, (("c", "a"), ("b", "a"), ("d", "b"), ("d", "c"))
        )
        self.assertEqual(accepted.depth, 3)
        self.assertEqual(accepted.ordinal("d"), 3)
        # Accepting an accepted plan changes nothing.
        self.assertEqual(Plan(accepted.nodes), accepted)

    def test_a_node_is_normalised(self):
        cap = Capability
        repository = uuid.uuid4()
        accepted = PlanNode.from_mapping(
            node(
                "a",
                role=NodeRole.WORKER,
                title="  spaced  ",
                goal="\n goal \n",
                depends_on=("z", "b"),
                capabilities=[cap.PROJECT_REPO_WRITE, "project.read", cap.PROJECT_READ],
                repositories=[repository, str(repository)],
            )
        )

        self.assertEqual(accepted.title, "spaced")
        self.assertEqual(accepted.goal, "goal")
        self.assertEqual(accepted.depends_on, ("b", "z"))
        self.assertEqual(
            accepted.capabilities, (cap.PROJECT_READ, cap.PROJECT_REPO_WRITE)
        )
        self.assertEqual(accepted.repositories, (repository,))
        self.assertTrue(accepted.required)
        self.assertEqual(accepted.input, {})

    def test_every_role_may_hold_its_whole_ceiling_and_nothing_beyond_it(self):
        for role in NodeRole:
            ceiling = sorted(c.value for c in ROLE_CEILING[role])
            with self.subTest(role=role.value):
                self.assertEqual(
                    PlanNode.from_mapping(
                        node(role=role.value, capabilities=ceiling)
                    ).capabilities
                    is not None,
                    True,
                )
                for capability in Capability:
                    if capability in ROLE_CEILING[role]:
                        continue
                    with self.assertRaises(InvalidPlanError):
                        PlanNode.from_mapping(
                            node(role=role.value, capabilities=[capability.value])
                        )

    def test_only_the_worker_may_write(self):
        writing = {Capability.PROJECT_REPO_WRITE}
        for role in NodeRole:
            self.assertEqual(
                bool(ROLE_CEILING[role] & writing), role is NodeRole.WORKER, role
            )
            self.assertNotIn(Capability.PROJECT_PR_CREATE, ROLE_CEILING[role])
            self.assertNotIn(Capability.AGENT_USE, ROLE_CEILING[role])
            self.assertNotIn(Capability.PROJECT_AGENT_USE, ROLE_CEILING[role])

    def test_the_plan_is_detached_from_the_callers_containers(self):
        inner = {"list": [1, 2]}
        data = plan(node("a", input=inner))
        accepted = Plan.from_mapping(data)

        inner["list"].append(3)
        data["nodes"][0]["goal"] = "changed"

        self.assertEqual(accepted.nodes[0].input, {"list": [1, 2]})
        self.assertEqual(accepted.nodes[0].goal, "Do it")

    def test_a_plan_survives_a_round_trip_through_data(self):
        accepted = Plan.from_mapping(
            plan(
                node("a", role="researcher", capabilities=["project.read"]),
                node("b", depends_on=["a"], input={"k": [1, {"x": None}]}),
                node(
                    "c",
                    role="reviewer",
                    required=False,
                    depends_on=["b"],
                    repositories=[str(uuid.uuid4())],
                ),
            )
        )
        self.assertEqual(Plan.from_mapping(accepted.to_mapping()), accepted)


class RandomGraphTest(unittest.TestCase):
    """Random DAGs: the order respects every edge; a back edge is always a cycle."""

    def random_dag(self, rng: random.Random) -> list[dict]:
        count = rng.randint(1, 20)
        keys = [f"n{i}" for i in range(count)]
        order = keys[:]
        rng.shuffle(order)  # the proposal lists the nodes in a random order
        nodes = {}
        for index, key in enumerate(keys):
            candidates = keys[:index]
            picked = rng.sample(candidates, k=min(len(candidates), rng.randint(0, 3)))
            nodes[key] = node(
                key, depends_on=picked, required=rng.random() < 0.8 or index == 0
            )
        return [nodes[key] for key in order]

    def test_the_order_respects_every_edge_on_random_dags(self):
        for seed in range(300):
            rng = random.Random(seed)
            proposal = self.random_dag(rng)
            with self.subTest(seed=seed):
                if len({n["key"] for n in proposal if n["required"]}) == 0:
                    continue
                accepted = Plan.from_mapping(plan(*proposal))
                position = {n.key: i for i, n in enumerate(accepted.nodes)}
                self.assertEqual(len(position), len(proposal))
                for key, dependency in accepted.edges:
                    self.assertLess(position[dependency], position[key])
                # The same proposal always gives the same order.
                again = Plan.from_mapping(plan(*proposal))
                self.assertEqual(
                    [n.key for n in again.nodes], [n.key for n in accepted.nodes]
                )

    def test_adding_a_back_edge_to_a_random_dag_is_always_a_cycle(self):
        checked = 0
        for seed in range(300):
            rng = random.Random(1000 + seed)
            proposal = self.random_dag(rng)
            by_key = {n["key"]: n for n in proposal}
            # A dependency chain: pick a node with a dependency, walk to a root,
            # then make the root depend on the node: that closes a cycle.
            starts = [k for k, n in by_key.items() if n["depends_on"]]
            if not starts:
                continue
            start = rng.choice(starts)
            current = start
            while by_key[current]["depends_on"]:
                current = rng.choice(by_key[current]["depends_on"])
            root = by_key[current]
            root["depends_on"] = [*root["depends_on"], start]
            with self.subTest(seed=seed), self.assertRaises(InvalidPlanError) as caught:
                Plan.from_mapping(plan(*proposal))
            self.assertIn(
                caught.exception.reason,
                {R.CYCLE, R.TOO_MANY_DEPENDENCIES},
            )
            checked += 1
        self.assertGreater(checked, 150)


if __name__ == "__main__":
    unittest.main()
