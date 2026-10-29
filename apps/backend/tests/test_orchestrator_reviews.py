"""The independent reviews the backend guarantees (issue #297, Decision 0099, 5).

* ``add_required_reviews``: a plan of a task that writes, with a Worker node,
  gets ``paw-review-claude`` (``claude``) and ``paw-review-codex`` (``codex``):
  required, after every Worker, not after each other, a fixed title and goal;
  a plan without a Worker gets nothing; a planner's ``paw-`` key is refused
  (``reserved_key``); the limits count the added nodes;
* the configuration adds them only when the reviewer may be assigned both;
* the planner is told of them; a reviewer is told of its review base and gets
  no tool that writes; a reviewer's worktree must be read-only (and only a
  reviewer's may be);
* on PostgreSQL: a write task's DAG has the two reviews and runs each on its
  agent; a read-only task's has none; ``submit_plan`` adds them too; a
  planner's ``paw-review-*`` node or a plan the reviews push over 32 nodes is
  a failed planner call; the Claude review never escalates to ``codex``; a
  Worker that ran on ``codex`` makes the Codex review fail with
  ``ReviewNotIndependent`` (and the task fail) while the Claude review runs;
* the prompt injection regression (Decision 0099, "Test の方針"): a planner
  that puts the Worker on ``codex``, asks for ``project.pr.create``, a right
  the task does not hold or a repository outside the Working Set, or drops
  the reviews, gets the plan refused, ``GrantEscalation``,
  ``ScopeEscalation``, or the backend's reviews anyway.
"""

import unittest
import uuid

from paw_backend.agents.local import REQUIRED_REVIEWS_TEXT, node_prompts
from paw_backend.agents.toolkit import NodeToolkit
from paw_backend.authz import Capability, ProjectState, RepoAcl
from paw_backend.orchestrator import RoleAssignment
from paw_backend.orchestrator.config import OrchestratorConfig
from paw_backend.orchestrator.domain import ExecutionPlacement, NodeRole, RunOutcome
from paw_backend.orchestrator.errors import (
    REVIEW_NOT_INDEPENDENT,
    RUNTIME_ERROR_CLASSES,
    InvalidPlanError,
    PlanReason,
    ScopeEscalationError,
)
from paw_backend.orchestrator.limits import MAX_DEPENDENCIES, MAX_NODES
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.reviews import (
    CLAUDE_REVIEW,
    CODEX_REVIEW,
    REQUIRED_REVIEWS,
    add_required_reviews,
    forbidden_agents,
    refuse_reserved_keys,
)
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.orchestrator.scope import derive_child_scope
from paw_backend.orchestrator.workspaces import NodeWorktree
from paw_backend.tasks import RepoRole, TaskState, WorkingSetEntry
from paw_backend.tools import ScopedRepository, TaskScope

from .orchestrator_support import (
    ROOT,
    FakeAuthority,
    PostgresOrchestratorTestCase,
    make_plan,
    node,
    requires_postgres,
)
from .task_support import BASELINE
from .test_orchestrator_assignment import (
    ASSIGNMENT,
    LADDERS,
    decision_config,
    labelled,
    planned,
)

Out = RunOutcome
R = PlanReason
REVIEWS = (CLAUDE_REVIEW, CODEX_REVIEW)


class AddRequiredReviewsTest(unittest.TestCase):
    def test_two_reviews_after_every_worker(self):
        plan = add_required_reviews(
            make_plan(
                node("research", role="researcher"),
                node("a", "research"),
                node("b", "research"),
                node("check", "a", role="reviewer"),
            ),
            task_title="Fix the parser",
        )
        keys = [n.key for n in plan.nodes]
        self.assertEqual(keys[:4], ["research", "a", "b", "check"])
        self.assertEqual(set(keys[4:]), set(REVIEWS))
        for key in REVIEWS:
            review = next(n for n in plan.nodes if n.key == key)
            self.assertIs(review.role, NodeRole.REVIEWER)
            self.assertEqual(review.agent, REQUIRED_REVIEWS[key])
            self.assertTrue(review.required)
            # Every Worker, never the other review nor the planner's reviewer.
            self.assertEqual(review.depends_on, ("a", "b"))
            self.assertIsNone(review.capabilities)
            self.assertIsNone(review.repositories)
            self.assertIn('"Fix the parser"', review.goal)
            self.assertIn("a, b", review.goal)
            self.assertIn("review base", review.goal)
        self.assertEqual(
            {n.key: n.title for n in plan.nodes if n.key in REVIEWS},
            {
                CLAUDE_REVIEW: "Independent review by Claude",
                CODEX_REVIEW: "Independent review by Codex",
            },
        )

    def test_a_plan_without_a_worker_gets_nothing(self):
        plan = make_plan(node("r", role="researcher"), node("v", "r", role="reviewer"))
        self.assertIs(add_required_reviews(plan, task_title="t"), plan)

    def test_the_task_title_is_quoted_on_one_line(self):
        plan = add_required_reviews(
            make_plan(node("w")), task_title='Fix "it"\n\tnow\x00' + "x" * 300
        )
        goal = plan.nodes[-1].goal
        self.assertIn("Fix 'it' now", goal)
        self.assertNotIn("\x00", goal)
        self.assertNotIn("x" * 200, goal)

    def test_a_planners_reserved_key_is_refused(self):
        for key in ("paw-review-claude", "paw-review-codex", "paw-x"):
            with self.subTest(key), self.assertRaises(InvalidPlanError) as e:
                refuse_reserved_keys(
                    make_plan(node("w"), node(key, "w", role="reviewer"))
                )
            self.assertEqual(e.exception.reason, R.RESERVED_KEY)
            with self.assertRaises(InvalidPlanError):
                add_required_reviews(
                    make_plan(node("w"), node(key, "w", role="reviewer")),
                    task_title="t",
                )
        # ``paw_x`` and ``pawn`` are the planner's.
        refuse_reserved_keys(make_plan(node("paw_x"), node("pawn")))

    def test_the_limits_count_the_added_reviews(self):
        fits = [node("w")] + [node(f"n{i}", role="researcher") for i in range(29)]
        self.assertEqual(
            len(add_required_reviews(make_plan(*fits), task_title="t").nodes),
            MAX_NODES,
        )
        with self.assertRaises(InvalidPlanError) as e:
            add_required_reviews(
                make_plan(*fits, node("one-more", role="researcher")), task_title="t"
            )
        self.assertEqual(e.exception.reason, R.TOO_MANY_NODES)
        workers = [node(f"w{i}") for i in range(MAX_DEPENDENCIES + 1)]
        with self.assertRaises(InvalidPlanError) as e:
            add_required_reviews(make_plan(*workers), task_title="t")
        self.assertEqual(e.exception.reason, R.TOO_MANY_DEPENDENCIES)

    def test_forbidden_agents(self):
        self.assertEqual(forbidden_agents(CLAUDE_REVIEW, ["local"]), {"codex", "local"})
        self.assertEqual(
            forbidden_agents(CODEX_REVIEW, ["local", "codex"]),
            {"claude", "local", "codex"},
        )

    def test_review_not_independent_is_the_orchestrators_name(self):
        self.assertNotIn(REVIEW_NOT_INDEPENDENT, RUNTIME_ERROR_CLASSES)


class ConfigTest(unittest.TestCase):
    def test_the_reviews_are_added_only_when_both_agents_are_allowed(self):
        self.assertTrue(decision_config().adds_required_reviews)
        self.assertFalse(OrchestratorConfig(LADDERS).adds_required_reviews)
        self.assertFalse(OrchestratorConfig.uniform(("local",)).adds_required_reviews)
        only_claude = {
            **ASSIGNMENT,
            NodeRole.REVIEWER: RoleAssignment(("claude",)),
        }
        self.assertFalse(
            OrchestratorConfig(LADDERS, assignment=only_claude).adds_required_reviews
        )

    def test_the_next_rung_skips_what_it_must(self):
        config = OrchestratorConfig(
            {**LADDERS, NodeRole.REVIEWER: ("claude", "codex", "other")}
        )
        r = NodeRole.REVIEWER
        self.assertEqual(config.next_rung(r, 0), 1)
        self.assertEqual(config.next_rung(r, 0, skip=frozenset({"codex"})), 2)
        self.assertIsNone(config.next_rung(r, 0, skip=frozenset({"codex", "other"})))
        self.assertIsNone(config.next_rung(r, 2))


class FakeTools:
    async def call(self, name, arguments, approval_id=None):  # pragma: no cover
        raise AssertionError(name)


def reviewer_assignment(worktrees, roots, *, role=NodeRole.REVIEWER):
    return NodeAssignment(
        task_id=uuid.uuid4(),
        node_key=CLAUDE_REVIEW,
        role=role,
        title="Independent review by Claude",
        goal="Review",
        input={},
        upstream={},
        agent="claude",
        attempt=1,
        approach=0,
        tools=FakeTools(),
        budget=None,
        worktrees=worktrees,
        roots=roots,
    )


class ReviewerPromptTest(unittest.TestCase):
    def worktree(self, repo_id, **options):
        return NodeWorktree(
            repo_id,
            "/home/a/workspaces/.paw-worktrees/t/1/r/paw-review-claude",
            "paw/t/1/paw-review-claude",
            base_commit="a" * 40,
            **options,
        )

    def test_a_reviewer_reads_its_review_base_and_cannot_write(self):
        repo_id = uuid.uuid4()
        toolkit = NodeToolkit(
            reviewer_assignment(
                {repo_id: self.worktree(repo_id, read_only=True)},
                {repo_id: "/home/a/workspaces/project/repo"},
            )
        )
        names = [t["function"]["name"] for t in toolkit.tools]
        self.assertEqual(names, ["bash", "submit"])
        self.assertFalse(toolkit.writes)
        self.assertFalse(toolkit.workspace.first().writable)
        system, _ = node_prompts(toolkit)
        self.assertIn(
            "git log -p refs/paw/review-base/paw/t/1/paw-review-claude..HEAD", system
        )
        self.assertIn("read-only access", system)

    def test_without_a_review_worktree_the_prompt_is_as_before(self):
        repo_id = uuid.uuid4()
        toolkit = NodeToolkit(
            reviewer_assignment({}, {repo_id: "/home/a/workspaces/project/repo"})
        )
        system, _ = node_prompts(toolkit)
        self.assertNotIn("review-base", system)

    def test_the_planner_of_a_writing_task_is_told_of_the_reviews(self):
        from .test_orchestrator_assignment import planner_assignment

        choices = {role: decision_config().allowed_agents(role) for role in NodeRole}
        system, _ = node_prompts(NodeToolkit(planner_assignment(choices=choices)))
        self.assertIn(REQUIRED_REVIEWS_TEXT, system)
        self.assertIn("paw-review-claude", system)
        self.assertIn(f"at most {MAX_NODES - 2} nodes", system)
        system, _ = node_prompts(
            NodeToolkit(planner_assignment(read_only=True, choices=choices))
        )
        self.assertNotIn(REQUIRED_REVIEWS_TEXT, system)
        # Without the reviews configured, the planner is not told of them.
        plain = {
            role: OrchestratorConfig(LADDERS).allowed_agents(role) for role in NodeRole
        }
        system, _ = node_prompts(NodeToolkit(planner_assignment(choices=plain)))
        self.assertNotIn(REQUIRED_REVIEWS_TEXT, system)


class ReviewScopeTest(unittest.TestCase):
    def setUp(self):
        self.repo_id = uuid.uuid4()
        self.project_id = uuid.uuid4()
        self.parent = TaskScope(
            path_roots=[ROOT],
            hosts=[],
            projects={self.project_id: ProjectState.ACTIVE},
            repositories=[
                ScopedRepository(
                    self.repo_id,
                    self.project_id,
                    f"{ROOT}/one",
                    RepoAcl.inherit(self.repo_id, self.project_id),
                    role=RepoRole.TARGET,
                )
            ],
        )

    def worktree(self, read_only):
        return {
            self.repo_id: NodeWorktree(
                self.repo_id,
                f"{ROOT}/trees/review",
                "paw/t/1/review",
                protected=(f"{ROOT}/one",),
                read_only=read_only,
            )
        }

    def scope(self, role, read_only):
        return derive_child_scope(
            self.parent,
            role=role,
            repositories=None,
            worktrees=self.worktree(read_only),
        )

    def test_a_reviewer_reads_in_a_read_only_worktree_only(self):
        scope = self.scope(NodeRole.REVIEWER, True)
        self.assertEqual(scope.repositories[0].root, f"{ROOT}/trees/review")
        self.assertIn(f"{ROOT}/one", scope.excluded_paths)
        self.assertIn(f"{ROOT}/trees/review/.git", scope.excluded_paths)
        with self.assertRaises(ScopeEscalationError):
            self.scope(NodeRole.REVIEWER, False)

    def test_only_a_reviewer_has_a_read_only_worktree(self):
        with self.assertRaises(ScopeEscalationError):
            self.scope(NodeRole.WORKER, True)
        for role in (NodeRole.RESEARCHER, NodeRole.PLANNER):
            with self.subTest(role), self.assertRaises(ScopeEscalationError):
                self.scope(role, True)


def codex_placed(result: str = "done on codex"):
    """A Worker attempt that records it ran on ``codex`` (an escalation)."""

    async def run(assignment):
        await assignment.placement.record(
            ExecutionPlacement.CLOUD, agent="codex", model="gpt-5-codex"
        )
        return NodeOutcome.succeeded(NodeResult(result))

    return run


@requires_postgres
class RequiredReviewRunTest(PostgresOrchestratorTestCase):
    def decision_harness(self, runtimes, **options):
        return self.harness(
            runtimes=runtimes,
            ladders=LADDERS,
            config={"assignment": ASSIGNMENT},
            **options,
        )

    async def stored(self, task_id) -> dict:
        rows = await self.rows(
            "SELECT n.key, n.role, n.assigned_agent, n.agent_index, n.required,"
            " n.title FROM agent_dag_nodes n JOIN agent_dags d ON d.id = n.dag_id"
            " WHERE d.task_id = :t ORDER BY n.ordinal",
            t=task_id,
        )
        return {row["key"]: row for row in rows}

    async def edges(self, task_id):
        rows = await self.rows(
            "SELECT e.node_key, e.depends_on_key FROM agent_dag_edges e"
            " JOIN agent_dags d ON d.id = e.dag_id WHERE d.task_id = :t",
            t=task_id,
        )
        return {(row["node_key"], row["depends_on_key"]) for row in rows}

    async def run_task(self, h, **create):
        task_id = await self.create_task(**create)
        await h.orchestrator.enqueue_task(task_id, preset="standard")
        return task_id, await h.orchestrator.run_once("w1")

    async def test_a_writing_task_gets_both_reviews_each_on_its_agent(self):
        rt = labelled(
            local={"plan": planned(node("a"), node("b"))}, claude={}, codex={}
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_task(h)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        stored = await self.stored(task_id)
        self.assertEqual(
            {k: (r["assigned_agent"], r["agent_index"]) for k, r in stored.items()},
            {
                "a": ("local", 0),
                "b": ("local", 0),
                CLAUDE_REVIEW: ("claude", 0),
                CODEX_REVIEW: ("codex", 1),
            },
        )
        self.assertTrue(all(stored[k]["required"] for k in REVIEWS))
        self.assertEqual(
            {e for e in await self.edges(task_id) if e[0] in REVIEWS},
            {(k, w) for k in REVIEWS for w in ("a", "b")},
        )
        self.assertEqual(len(rt["claude"].calls_of(CLAUDE_REVIEW)), 1)
        self.assertEqual(len(rt["codex"].calls_of(CODEX_REVIEW)), 1)
        self.assertEqual(rt["codex"].calls_of(CLAUDE_REVIEW), [])
        self.assertEqual(rt["claude"].calls_of(CODEX_REVIEW), [])
        # Each review is handed the Workers' results, not the other review's.
        (call,) = rt["codex"].calls_of(CODEX_REVIEW)
        self.assertEqual(set(call.upstream), {"a", "b"})

    async def test_a_read_only_task_gets_no_review(self):
        rt = labelled(
            local={
                "plan": planned(
                    node("read", role="researcher"),
                    node("check", "read", role="reviewer", agent="codex"),
                )
            },
            claude={},
            codex={},
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_task(
            h,
            repositories=[
                WorkingSetEntry(self.repository_id, RepoRole.REFERENCED, BASELINE)
            ],
            read_only=True,
        )

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(set(await self.stored(task_id)), {"read", "check"})

    async def test_without_the_reviews_configured_a_plan_is_kept(self):
        rt = labelled(local={"plan": planned(node("a"))})
        h = self.harness(runtimes=rt)

        task_id, report = await self.run_task(h)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(set(await self.stored(task_id)), {"a"})

    async def test_a_submitted_plan_gets_the_reviews_and_no_reserved_key(self):
        h = self.decision_harness(labelled(local={}, claude={}, codex={}))
        task_id = await self.create_task()
        with self.assertRaises(InvalidPlanError) as e:
            await h.orchestrator.submit_plan(
                task_id,
                make_plan(node("w"), node(CODEX_REVIEW, "w", role="reviewer")),
            )
        self.assertEqual(e.exception.reason, R.RESERVED_KEY)
        dag = await h.orchestrator.submit_plan(task_id, make_plan(node("w")))
        self.assertEqual({n.key for n in dag.nodes}, {"w", *REVIEWS})

    async def test_a_planner_that_writes_a_review_or_too_many_nodes_is_refused(self):
        for label, nodes in (
            (
                "its own paw-review-codex (on claude)",
                [node("w"), node(CODEX_REVIEW, "w", role="reviewer", agent="claude")],
            ),
            (
                "31 nodes",
                [node("w")] + [node(f"n{i}", role="researcher") for i in range(30)],
            ),
        ):
            with self.subTest(label):
                rt = labelled(
                    local={"plan": planned(*nodes)},
                    claude={"plan": planned(node("w"))},
                    codex={},
                )
                h = self.decision_harness(rt)

                task_id, report = await self.run_task(h)

                self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
                # The first plan was a failed call; the second planner's
                # plan got the backend's reviews.
                self.assertEqual(len(rt["claude"].calls_of("plan")), 1)
                self.assertEqual(set(await self.stored(task_id)), {"w", *REVIEWS})

    async def test_the_claude_review_never_escalates_to_codex(self):
        stuck = NodeOutcome.failed("AgentContextExceeded", escalate=True)
        rt = labelled(
            local={"plan": planned(node("w"))},
            claude={CLAUDE_REVIEW: [stuck, NodeOutcome.succeeded(NodeResult("ok"))]},
            codex={},
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_task(h)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(rt["claude"].calls_of(CLAUDE_REVIEW)), 2)
        self.assertEqual(rt["codex"].calls_of(CLAUDE_REVIEW), [])
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node(CLAUDE_REVIEW).agent_index, 0)

    async def test_the_codex_review_has_nowhere_to_escalate(self):
        # The top rung: its repeated failure stays on codex until the loop
        # rule holds it for the human (never the Claude review's agent).
        stuck = NodeOutcome.failed("AgentContextExceeded", escalate=True)
        rt = labelled(
            local={"plan": planned(node("w"))},
            claude={},
            codex={CODEX_REVIEW: stuck},
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_task(h)

        self.assertEqual(report.outcome, Out.WAITING_FOR_USER)
        self.assertGreater(len(rt["codex"].calls_of(CODEX_REVIEW)), 1)
        self.assertEqual(rt["claude"].calls_of(CODEX_REVIEW), [])
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node(CODEX_REVIEW).agent_index, 1)

    async def test_a_review_that_fails_for_good_fails_the_task(self):
        rt = labelled(
            local={"plan": planned(node("w"))},
            claude={CLAUDE_REVIEW: NodeOutcome.failed("Down", retryable=False)},
            codex={},
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_task(h)

        # Required: the DAG fails (a Retry of the task re-opens it).
        self.assertEqual(report.outcome, Out.DAG_FAILED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)
        self.assertEqual(rt["codex"].calls_of(CLAUDE_REVIEW), [])

    async def test_a_worker_that_ran_on_codex_fails_the_codex_review(self):
        # Decision 0099, 5 (point 4 A): the Worker escalated to codex; the
        # Codex review is not independent, the Claude review still runs.
        stuck = NodeOutcome.failed("AgentContextExceeded", escalate=True)
        rt = labelled(
            local={"plan": planned(node("w")), "w": stuck},
            claude={},
            codex={"w": codex_placed()},
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_task(h)

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        self.assertEqual((await h.tasks.restore(task_id)).state, TaskState.FAILED)
        self.assertEqual(len(rt["codex"].calls_of("w")), 1)
        self.assertEqual(rt["codex"].calls_of(CODEX_REVIEW), [])
        self.assertEqual(len(rt["claude"].calls_of(CLAUDE_REVIEW)), 1)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node(CODEX_REVIEW).error_class, REVIEW_NOT_INDEPENDENT)
        self.assertEqual(dag.node(CODEX_REVIEW).attempt_count, 1)  # never retried
        self.assertEqual(dag.node(CLAUDE_REVIEW).error_class, None)

    async def test_a_worker_on_local_keeps_both_reviews_independent(self):
        async def local_placed(assignment):
            await assignment.placement.record(
                ExecutionPlacement.LOCAL_GPU, agent="local", model="main"
            )
            return NodeOutcome.succeeded(NodeResult("done"))

        rt = labelled(
            local={"plan": planned(node("w")), "w": local_placed}, claude={}, codex={}
        )
        h = self.decision_harness(rt)

        _task_id, report = await self.run_task(h)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(rt["codex"].calls_of(CODEX_REVIEW)), 1)


@requires_postgres
class PromptInjectionTest(PostgresOrchestratorTestCase):
    """What a planner talked into widening its plan gets (Decision 0099, 6)."""

    def decision_harness(self, runtimes, **options):
        return self.harness(
            runtimes=runtimes,
            ladders=LADDERS,
            config={"assignment": ASSIGNMENT},
            **options,
        )

    async def run_with(self, h):
        task_id = await self.create_task()
        await h.orchestrator.enqueue_task(task_id, preset="standard")
        return task_id, await h.orchestrator.run_once("w1")

    async def test_refused_plans_never_reach_a_dag(self):
        for label, nodes in (
            ("the worker on codex", [node("w", agent="codex")]),
            ("project.pr.create", [node("w", capabilities=["project.pr.create"])]),
            ("its own required review", [node("w"), node(CLAUDE_REVIEW, "w")]),
        ):
            with self.subTest(label):
                rt = labelled(
                    local={"plan": planned(*nodes)},
                    claude={"plan": planned(*nodes)},
                    codex={},
                )
                h = self.decision_harness(rt)

                task_id, report = await self.run_with(h)

                self.assertEqual(report.outcome, Out.PLAN_FAILED)
                self.assertIsNone(await self.store.get(task_id, 1))
                self.assertEqual(rt["codex"].assignments, [])

    async def test_a_right_the_task_lacks_is_a_grant_escalation(self):
        rt = labelled(
            local={"plan": planned(node("w", capabilities=["project.repo.write"]))},
            claude={},
            codex={},
        )
        authority = FakeAuthority(
            capabilities={Capability.PROJECT_READ, Capability.PROJECT_TASK_RUN}
        )
        h = self.decision_harness(rt, authority=authority)

        task_id, report = await self.run_with(h)

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("w").error_class, "GrantEscalation")
        self.assertEqual(rt["local"].calls_of("w"), [])
        # The reviews are there, and never ran (their Worker failed).
        self.assertEqual({n.key for n in dag.nodes}, {"w", *REVIEWS})
        self.assertEqual(rt["claude"].assignments, [])

    async def test_a_repository_outside_the_working_set_is_a_scope_escalation(self):
        rt = labelled(
            local={"plan": planned(node("w", repositories=[str(uuid.uuid4())]))},
            claude={},
            codex={},
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_with(h)

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("w").error_class, "ScopeEscalation")

    async def test_a_plan_without_reviews_gets_the_backends(self):
        # "Skip the review": the planner wrote none (or only its own).
        rt = labelled(
            local={"plan": planned(node("w"), node("mine", "w", role="reviewer"))},
            claude={},
            codex={},
        )
        h = self.decision_harness(rt)

        task_id, report = await self.run_with(h)

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual({n.key for n in dag.nodes}, {"w", "mine", *REVIEWS})
        self.assertEqual(len(rt["codex"].calls_of(CODEX_REVIEW)), 1)
        self.assertEqual(len(rt["claude"].calls_of(CLAUDE_REVIEW)), 1)
        # The planner's own reviewer started on its role's default (claude).
        self.assertEqual(len(rt["claude"].calls_of("mine")), 1)


if __name__ == "__main__":
    unittest.main()
