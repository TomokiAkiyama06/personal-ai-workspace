"""The plan's ``agent`` and where a node starts on its ladder (issue #297,
Decision 0099, 4).

* ``PlanNode.agent``: an agent label or nothing; it counts toward the plan's
  size;
* ``OrchestratorConfig.assign``: an agent the node's role does not allow (an
  unknown label, a worker on ``claude`` / ``codex``, the planner role on
  ``codex``) refuses the whole plan (``agent_not_allowed``); a node without
  one gets its role's default; without ``[orchestrator.assignment]`` every role
  allows only its first rung; an assignment outside the role's ladder is
  refused when the configuration is built;
* the planner is told the agents (``node_prompts``, the ``submit_plan`` tool);
* on PostgreSQL: the accepted plan's nodes are stored with their assigned agent
  (never NULL) and start on its rung; a planner's plan with an agent its role
  does not allow is a failed planner call (the next planner is asked); an
  assigned node escalates up its ladder; a node stored before revision 0282
  (NULL) runs from the first rung, also after a Retry; a node whose assigned
  agent left the ladder fails with ``AssignmentUnavailable`` and never runs on
  another agent.
"""

import dataclasses
import unittest
import uuid

from paw_backend.agents.local import (
    PLANNER_PROMPT,
    READ_ONLY_PLANNER_PROMPT,
    agent_choices_text,
    node_prompts,
)
from paw_backend.agents.toolkit import PLAN_TOOL, NodeToolkit
from paw_backend.orchestrator import RoleAssignment
from paw_backend.orchestrator.config import OrchestratorConfig
from paw_backend.orchestrator.domain import NodeRole, RunOutcome
from paw_backend.orchestrator.errors import (
    ASSIGNMENT_UNAVAILABLE,
    RUNTIME_ERROR_CLASSES,
    InvalidOrchestratorArgumentError,
    InvalidPlanError,
    PlanReason,
)
from paw_backend.orchestrator.limits import MAX_PLAN_BYTES
from paw_backend.orchestrator.plan import Plan, PlanNode, plan_bytes
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.tasks import TaskCommand, TaskState
from paw_backend.tasks.queueing import BudgetPreset

from .orchestrator_support import (
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    make_plan,
    node,
    requires_postgres,
)

Out = RunOutcome
R = PlanReason

# The Decision's table (section 4 and 7).
LADDERS = {
    NodeRole.PLANNER: ("local", "claude"),
    NodeRole.WORKER: ("local", "codex"),
    NodeRole.RESEARCHER: ("local", "claude"),
    NodeRole.REVIEWER: ("claude", "codex"),
}
ASSIGNMENT = {
    NodeRole.WORKER: RoleAssignment(("local",), "local"),
    NodeRole.RESEARCHER: RoleAssignment(("local", "claude"), "local"),
    NodeRole.REVIEWER: RoleAssignment(("claude", "codex")),
    NodeRole.PLANNER: RoleAssignment(("local",), "local"),
}


def decision_config(**options) -> OrchestratorConfig:
    return OrchestratorConfig(LADDERS, assignment=ASSIGNMENT, **options)


class PlanNodeAgentTest(unittest.TestCase):
    def test_an_agent_is_a_label(self):
        for value, reason in (
            (5, R.BAD_TYPE),
            (["local"], R.BAD_TYPE),
            (b"local", R.BAD_TYPE),
            ("", R.AGENT_NOT_ALLOWED),
            ("Claude", R.AGENT_NOT_ALLOWED),
            ("claude opus", R.AGENT_NOT_ALLOWED),
            ("1local", R.AGENT_NOT_ALLOWED),
            ("a" * 65, R.AGENT_NOT_ALLOWED),
        ):
            with self.subTest(value=value), self.assertRaises(InvalidPlanError) as e:
                make_plan(node("a", agent=value))
            self.assertEqual(e.exception.reason, reason)

    def test_an_agent_is_optional_and_kept(self):
        plan = make_plan(node("a", agent="local"), node("b", "a"))
        self.assertEqual([n.agent for n in plan.nodes], ["local", None])
        self.assertEqual(plan.nodes[0].to_mapping()["agent"], "local")
        # A node without one has no ``agent`` in its mapping (the size of a
        # plan without agents is what it was).
        self.assertNotIn("agent", plan.nodes[1].to_mapping())
        self.assertEqual(Plan.from_mapping(plan.to_mapping()), plan)
        self.assertIsNone(make_plan(node("a", agent=None)).nodes[0].agent)

    def test_the_agent_counts_toward_the_plans_size(self):
        bare = make_plan(node("a"))
        assigned = make_plan(node("a", agent="researcher-x"))
        self.assertEqual(
            assigned.encoded_bytes - bare.encoded_bytes,
            len(',"agent":"researcher-x"'),
        )


class AssignTest(unittest.TestCase):
    def test_a_node_without_an_agent_gets_its_roles_default(self):
        config = decision_config()
        plan = config.assign(
            make_plan(
                node("r", role="researcher"),
                node("w", "r"),
                node("v", "w", role="reviewer"),
                node("p", role="planner"),
            )
        )
        agents = {n.key: n.agent for n in plan.nodes}
        # The reviewer has no default: the first rung its role allows.
        self.assertEqual(
            agents, {"r": "local", "w": "local", "v": "claude", "p": "local"}
        )
        self.assertEqual(
            {n.key: config.start_rung(n.role, n.agent) for n in plan.nodes},
            {"r": 0, "w": 0, "v": 0, "p": 0},
        )

    def test_an_allowed_agent_is_kept_and_starts_on_its_rung(self):
        config = decision_config()
        plan = config.assign(
            make_plan(
                node("r", role="researcher", agent="claude"),
                node("v", "r", role="reviewer", agent="codex"),
            )
        )
        self.assertEqual([n.agent for n in plan.nodes], ["claude", "codex"])
        self.assertEqual(
            [config.start_rung(n.role, n.agent) for n in plan.nodes], [1, 1]
        )

    def test_an_agent_the_role_does_not_allow_refuses_the_whole_plan(self):
        # Decision 0099, 6: a planner talked into sending the worker to the
        # cloud, or any agent outside the Admin's file, gets nothing stored.
        config = decision_config()
        for label, nodes in (
            ("a worker on codex", [node("w", agent="codex")]),
            ("a worker on claude", [node("w", agent="claude")]),
            ("an unknown label", [node("r", role="researcher", agent="gpt")]),
            ("the planner role on codex", [node("p", role="planner", agent="codex")]),
            ("the planner role on claude", [node("p", role="planner", agent="claude")]),
            ("a reviewer on local", [node("v", role="reviewer", agent="local")]),
            (
                "one bad node among good ones",
                [
                    node("r", role="researcher", agent="claude"),
                    node("w", "r", agent="codex"),
                ],
            ),
        ):
            with self.subTest(label), self.assertRaises(InvalidPlanError) as e:
                config.assign(make_plan(*nodes))
            self.assertEqual(e.exception.reason, R.AGENT_NOT_ALLOWED)

    def test_without_an_assignment_only_the_first_rung_is_allowed(self):
        # Before ``[orchestrator.assignment]`` is configured, a plan cannot move
        # a node off the first rung (what the orchestrator did before).
        config = OrchestratorConfig(LADDERS)
        plan = config.assign(make_plan(node("w"), node("v", "w", role="reviewer")))
        self.assertEqual([n.agent for n in plan.nodes], ["local", "claude"])
        for nodes in (
            [node("w", agent="codex")],
            [node("r", role="researcher", agent="claude")],
        ):
            with self.subTest(nodes), self.assertRaises(InvalidPlanError) as e:
                config.assign(make_plan(*nodes))
            self.assertEqual(e.exception.reason, R.AGENT_NOT_ALLOWED)
        self.assertEqual(
            config.allowed_agents(NodeRole.RESEARCHER),
            RoleAssignment(("local",), "local"),
        )

    def test_an_assigned_plan_that_grows_too_large_is_refused(self):
        # The defaults written in count toward the limit.
        full = [node(f"n{i}", input={"pad": "x" * 15_000}) for i in range(8)]
        base = make_plan(*full, node("last", input={"pad": ""}))
        room = MAX_PLAN_BYTES - base.encoded_bytes
        big = make_plan(*full, node("last", input={"pad": "x" * (room - 1)}))
        self.assertEqual(plan_bytes(big.nodes), MAX_PLAN_BYTES - 1)
        with self.assertRaises(InvalidPlanError) as e:
            OrchestratorConfig.uniform(("local",)).assign(big)
        self.assertEqual(e.exception.reason, R.TOO_LARGE)

    def test_an_assignment_outside_the_ladder_or_misshapen_is_refused(self):
        for label, assignment in (
            (
                "allowed outside the ladder",
                {**ASSIGNMENT, NodeRole.WORKER: RoleAssignment(("local", "claude"))},
            ),
            ("a role missing", {NodeRole.WORKER: ASSIGNMENT[NodeRole.WORKER]}),
            ("not role assignments", {role: ("local",) for role in NodeRole}),
            ("not a mapping", [ASSIGNMENT]),
        ):
            with (
                self.subTest(label),
                self.assertRaises(InvalidOrchestratorArgumentError) as e,
            ):
                OrchestratorConfig(LADDERS, assignment=assignment)
            self.assertEqual(e.exception.parameter, "assignment")
        for allowed, default in (
            ((), None),
            (("local", "local"), None),
            (("Local",), None),
            ("local", None),
            (("local",), "claude"),
        ):
            with (
                self.subTest(allowed=allowed, default=default),
                self.assertRaises(InvalidOrchestratorArgumentError),
            ):
                RoleAssignment(allowed, default)

    def test_assign_takes_a_plan(self):
        with self.assertRaises(InvalidOrchestratorArgumentError):
            decision_config().assign({"nodes": [node("a")]})

    def test_assignment_unavailable_is_the_orchestrators_name(self):
        # A runtime cannot report it (Decision 0099, 4).
        self.assertNotIn(ASSIGNMENT_UNAVAILABLE, RUNTIME_ERROR_CLASSES)


class FakeWorkspaceTools:
    async def call(self, name, arguments):  # pragma: no cover - never called
        raise AssertionError(name)


class FakeBudget:
    async def charge(self, kind, amount):  # pragma: no cover - never called
        raise AssertionError(kind)

    async def remaining(self, kind):  # pragma: no cover - never called
        raise AssertionError(kind)


def planner_assignment(*, read_only=False, choices=None) -> NodeAssignment:
    return NodeAssignment(
        task_id=uuid.uuid4(),
        node_key="plan",
        role=NodeRole.PLANNER,
        title="Fix the parser",
        goal="Fix the parser",
        input={},
        upstream={},
        agent="local",
        attempt=1,
        approach=0,
        tools=FakeWorkspaceTools(),
        budget=FakeBudget(),
        read_only_task=read_only,
        agent_choices=choices,
    )


class PlannerPromptTest(unittest.TestCase):
    def choices(self):
        return {role: decision_config().allowed_agents(role) for role in NodeRole}

    def test_the_planner_is_told_the_agents_of_each_role(self):
        text = agent_choices_text(self.choices(), read_only=False)
        self.assertIn('"agent"', text)
        self.assertIn("worker: local (default local)", text)
        self.assertIn("researcher: local or claude (default local)", text)
        self.assertIn("reviewer: claude or codex (default claude)", text)
        self.assertIn("refuses the whole plan", text)
        system, _ = node_prompts(
            NodeToolkit(planner_assignment(choices=self.choices()))
        )
        self.assertTrue(system.endswith(text))
        self.assertTrue(system.startswith(PLANNER_PROMPT.split("{workspace}")[0]))

    def test_a_read_only_planner_is_not_told_of_the_worker(self):
        text = agent_choices_text(self.choices(), read_only=True)
        self.assertNotIn("worker", text)
        self.assertIn("researcher: local or claude (default local)", text)
        system, _ = node_prompts(
            NodeToolkit(planner_assignment(read_only=True, choices=self.choices()))
        )
        self.assertTrue(system.endswith(text))
        self.assertTrue(
            system.startswith(READ_ONLY_PLANNER_PROMPT.split("{workspace}")[0])
        )

    def test_without_choices_the_prompt_is_as_before(self):
        self.assertEqual(agent_choices_text(None, read_only=False), "")
        system, _ = node_prompts(NodeToolkit(planner_assignment()))
        self.assertNotIn('"agent"', system)

    def test_the_submit_plan_tool_names_the_field(self):
        plan = PLAN_TOOL["function"]["parameters"]["properties"]["plan"]
        self.assertEqual(plan["type"], "object")
        self.assertIn("agent", plan["description"])


def planned(*nodes: dict) -> NodeOutcome:
    return NodeOutcome.succeeded(NodeResult("a plan"), plan={"nodes": list(nodes)})


def labelled(**scripts) -> dict[str, FakeRuntime]:
    return {
        label: FakeRuntime(label, script=script) for label, script in scripts.items()
    }


@requires_postgres
class AssignedRunTest(PostgresOrchestratorTestCase):
    def decision_harness(self, runtimes):
        return self.harness(
            runtimes=runtimes, ladders=LADDERS, config={"assignment": ASSIGNMENT}
        )

    async def stored(self, task_id, attempt=1):
        rows = await self.rows(
            "SELECT n.key, n.assigned_agent, n.agent_index FROM agent_dag_nodes n"
            " JOIN agent_dags d ON d.id = n.dag_id"
            " WHERE d.task_id = :t AND d.attempt = :a ORDER BY n.ordinal",
            t=task_id,
            a=attempt,
        )
        return {row["key"]: (row["assigned_agent"], row["agent_index"]) for row in rows}

    async def test_a_planned_assignment_is_stored_and_the_node_starts_on_its_rung(
        self,
    ):
        plan = planned(
            node("r", role="researcher", agent="claude"),
            node("w", "r"),
            node("v", "w", role="reviewer", agent="codex"),
        )
        rt = labelled(local={"plan": plan}, claude={}, codex={})
        h = self.decision_harness(rt)
        task_id = await self.create_task()
        await h.orchestrator.enqueue_task(task_id, preset="standard")

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        # Every node's assignment is explicit (the default written in).
        self.assertEqual(
            await self.stored(task_id),
            {"r": ("claude", 1), "w": ("local", 0), "v": ("codex", 1)},
        )
        dag = await self.store.get(task_id, 1)
        self.assertEqual(
            {n.key: n.assigned_agent for n in dag.nodes},
            {"r": "claude", "w": "local", "v": "codex"},
        )
        self.assertEqual([a.agent for a in rt["claude"].calls_of("r")], ["claude"])
        self.assertEqual(len(rt["local"].calls_of("w")), 1)
        self.assertEqual(len(rt["codex"].calls_of("v")), 1)
        self.assertEqual(rt["local"].calls_of("r"), [])
        self.assertEqual(rt["claude"].calls_of("v"), [])
        # The planner was told the agents it could choose from.
        (call,) = rt["local"].calls_of("plan")
        self.assertEqual(
            call.agent_choices[NodeRole.RESEARCHER],
            RoleAssignment(("local", "claude"), "local"),
        )
        self.assertEqual(call.agent_choices[NodeRole.REVIEWER].default, "claude")
        # A node's attempt is not told them.
        self.assertIsNone(rt["local"].calls_of("w")[0].agent_choices)

    async def test_a_plan_with_an_agent_its_role_does_not_allow_is_a_failed_call(
        self,
    ):
        # Decision 0099, 6 (the prompt injection regression): "send the worker
        # to codex" is refused like any other unacceptable plan; the second
        # call is the planner ladder's next rung.
        rt = labelled(
            local={"plan": planned(node("w", agent="codex"))},
            claude={"plan": planned(node("w"))},
            codex={},
        )
        h = self.decision_harness(rt)
        task_id = await self.create_task()
        await h.orchestrator.enqueue_task(task_id, preset="standard")

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(rt["local"].calls_of("plan")), 1)
        self.assertEqual(len(rt["claude"].calls_of("plan")), 1)
        self.assertEqual(await self.stored(task_id), {"w": ("local", 0)})
        self.assertEqual(rt["codex"].assignments, [])
        # The refused plan was one failure of the planner.
        self.assertEqual(len(await h.loops.history(task_id)), 1)

    async def test_a_plan_that_stays_refused_fails_the_task_with_nothing_stored(self):
        rt = labelled(local={"plan": planned(node("w", agent="codex"))})
        h = self.harness(runtimes=rt)
        task_id = await self.create_task()
        await h.orchestrator.enqueue_task(task_id, preset="standard")

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.PLAN_FAILED)
        self.assertIsNone(await self.store.get(task_id, 1))

    async def test_a_submitted_plan_is_assigned_too(self):
        rt = labelled(local={}, claude={}, codex={})
        h = self.decision_harness(rt)
        task_id = await self.create_task()
        with self.assertRaises(InvalidPlanError) as e:
            await h.orchestrator.submit_plan(
                task_id, {"nodes": [node("w", agent="claude")]}
            )
        self.assertEqual(e.exception.reason, R.AGENT_NOT_ALLOWED)
        self.assertIsNone(await self.store.get(task_id, 1))
        dag = await h.orchestrator.submit_plan(
            task_id, make_plan(node("r", role="researcher", agent="claude"), node("w"))
        )
        self.assertEqual(
            {n.key: (n.assigned_agent, n.agent_index) for n in dag.nodes},
            {"r": ("claude", 1), "w": ("local", 0)},
        )

    async def test_an_assigned_node_escalates_up_its_ladder(self):
        stuck = NodeOutcome.failed("AgentContextExceeded", escalate=True)
        rt = labelled(
            local={"r": stuck},
            claude={"r": NodeOutcome.succeeded(NodeResult("ok"))},
            codex={},
        )
        h = self.decision_harness(rt)
        task_id = await self.prepare(h, make_plan(node("r", role="researcher")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(rt["local"].calls_of("r")), 1)
        self.assertEqual(len(rt["claude"].calls_of("r")), 1)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(
            (dag.node("r").assigned_agent, dag.node("r").agent_index), ("local", 1)
        )

    async def test_a_node_assigned_the_top_rung_cannot_escalate(self):
        # A researcher assigned claude starts on the last rung: an escalation
        # has nowhere to go and the node is retried on claude (never on local).
        stuck = NodeOutcome.failed("AgentContextExceeded", escalate=True)
        rt = labelled(
            local={},
            claude={"r": [stuck, NodeOutcome.succeeded(NodeResult("ok"))]},
            codex={},
        )
        h = self.decision_harness(rt)
        task_id = await self.prepare(
            h, make_plan(node("r", role="researcher", agent="claude"))
        )

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(rt["claude"].calls_of("r")), 2)
        self.assertEqual(rt["local"].calls_of("r"), [])
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("r").agent_index, 1)

    async def legacy_task(self, h, plan: Plan) -> uuid.UUID:
        """A DAG stored as before revision 0282: no assignment (NULL)."""
        task_id = await self.create_task()
        await self.store.create(task_id, 1, plan)
        await h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.STANDARD)
        return task_id

    async def test_a_node_stored_without_an_assignment_runs_from_the_first_rung(self):
        rt = labelled(
            local={
                "r": [
                    fail("Down", retryable=False),
                    NodeOutcome.succeeded(NodeResult("ok")),
                ]
            },
            claude={},
            codex={},
        )
        h = self.decision_harness(rt)
        task_id = await self.legacy_task(h, make_plan(node("r", role="researcher")))
        self.assertEqual(await self.stored(task_id), {"r": (None, 0)})

        first = await h.orchestrator.run_once("w1")

        self.assertEqual(first.outcome, Out.DAG_FAILED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)
        # A Retry reopens the old DAG: it still runs on the first rung.
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        await h.queue.enqueue(task_id)
        second = await h.orchestrator.run_once("w1")

        self.assertEqual(second.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(rt["local"].calls_of("r")), 2)
        self.assertEqual(rt["claude"].assignments, [])
        self.assertEqual(await self.stored(task_id), {"r": (None, 0)})

    async def test_a_node_whose_agent_left_the_ladder_fails_without_running(self):
        for label, ladder, agent, rung in (
            ("removed", ("local",), "claude", 1),
            ("moved above its rung", ("claude", "local"), "local", 0),
            # Codex review (PR #301): an unescalated node never runs on the agent
            # that took its rung when its own moved down the ladder.
            ("moved below its rung", ("claude", "local"), "claude", 1),
        ):
            with self.subTest(label):
                rt = labelled(local={}, claude={})
                h = self.harness(runtimes=rt, ladder=ladder)
                task_id = await self.create_task()
                plan = make_plan(node("r", role="researcher", agent=agent))
                await self.store.create(task_id, 1, plan, start_rungs={"r": rung})
                await h.orchestrator.enqueue_task(task_id, preset="standard")

                report = await h.orchestrator.run_once("w1")

                self.assertEqual(report.outcome, Out.DAG_FAILED)
                self.assertEqual(rt["local"].calls_of("r"), [])
                self.assertEqual(rt["claude"].calls_of("r"), [])
                dag = await self.store.get(task_id, 1)
                self.assertEqual(dag.node("r").error_class, ASSIGNMENT_UNAVAILABLE)
                # Failed for good at once: no retry on any agent.
                self.assertEqual(dag.node("r").attempt_count, 1)

    async def test_the_store_needs_a_rung_for_each_assigned_node(self):
        task_id = await self.create_task()
        plan = make_plan(node("r", role="researcher", agent="claude"), node("w"))
        for rungs in (None, {}, {"r": 1, "w": 0}, {"r": 9}, {"r": -1}, {"r": True}):
            with (
                self.subTest(rungs=rungs),
                self.assertRaises(InvalidOrchestratorArgumentError) as e,
            ):
                await self.store.create(task_id, 1, plan, start_rungs=rungs)
            self.assertEqual(e.exception.parameter, "start_rungs")
        self.assertIsNone(await self.store.get(task_id, 1))

    async def test_the_database_refuses_an_assignment_that_is_not_a_label(self):
        dag = await self.make_dag(make_plan(node("a")))
        with self.assertRaises(Exception) as caught:
            await self.owner_sql(
                "UPDATE agent_dag_nodes SET assigned_agent = 'Not A Label'"
                " WHERE dag_id = :d",
                d=dag.id,
            )
        self.assertIn("assigned_agent_format", str(caught.exception))


class ReplaceTest(unittest.TestCase):
    def test_replacing_the_agent_checks_it_again(self):
        accepted = make_plan(node("a")).nodes[0]
        self.assertEqual(dataclasses.replace(accepted, agent="local").agent, "local")
        with self.assertRaises(InvalidPlanError):
            dataclasses.replace(accepted, agent="Not A Label")
        self.assertIsInstance(accepted, PlanNode)


if __name__ == "__main__":
    unittest.main()
