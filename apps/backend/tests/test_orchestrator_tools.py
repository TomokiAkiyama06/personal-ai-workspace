"""The orchestrator with the REAL Tool Broker (PAW-031) behind a node's tools.

Real: ``ToolBroker`` with its registry, policy, authorizer (the delegating user's
rights are looked up), ``PostgresApprovalStore``, ``PostgresTaskActivity``,
``TrackerBudgetProvider``, ``TaskService`` with the approval revocation listener.
Faked: the agent runtimes (they call tools through the gateway) and the executor
(it records what it is handed). These tests check the acceptance conditions that
Decisions 0006 and 0007 put on the orchestrator: the ``TaskContext`` is built from
the task's run, a sub-agent's rights and scope are subsets of its parent's, the
working set's remotes decide which URLs pass, a terminated task is handed no
call, and the budget the broker charges is the parent task's.
"""

import asyncio
import unittest

from paw_backend.authz import (
    AgentGrant,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    ProjectRole,
    ProjectState,
    RepoAcl,
    RepoPermission,
    SystemRole,
    is_subgrant,
)
from paw_backend.orchestrator.domain import RunOutcome
from paw_backend.orchestrator.errors import NodeStopped
from paw_backend.orchestrator.gateway import TrackerBudgetProvider
from paw_backend.orchestrator.scope import scope_within
from paw_backend.tasks import TaskCommand, TaskRun
from paw_backend.tasks.queueing import BudgetKind, BudgetTracker
from paw_backend.tools import (
    ApprovalService,
    ArgumentKind,
    ArgumentSpec,
    BrokerReason,
    ExecutionStatus,
    LexicalPathResolver,
    PostgresApprovalStore,
    PostgresTaskActivity,
    ScopedRepository,
    TaskScope,
    ToolBroker,
    ToolCapability,
    ToolRegistry,
    ToolRunner,
    ToolSpec,
    Verdict,
)

from .authz_support import StaticDirectory, principal, uid
from .orchestrator_support import (
    PARENT_AGENT,
    FakeRuntime,
    PostgresOrchestratorTestCase,
    fail,
    make_plan,
    node,
    ok,
    requires_postgres,
    until,
)
from .tools_support import (
    HANDLE,
    REPO,
    REPO_REMOTE,
    ROOT,
    FakeExecutor,
    sample_specs,
)

Out = RunOutcome
REPO2 = uid(702)
WRITE = {"path": f"{ROOT}/src/a.py", "content": "x = 1"}
READ = {"path": f"{ROOT}/src/a.py"}
# A read-only tool that carries a URL and names a repository: what a repository
# read needs so that the URL is attributed to the repository's remotes.
FETCH = ToolSpec(
    "repo.fetch",
    frozenset({ToolCapability.READ, ToolCapability.NETWORK}),
    Capability.PROJECT_READ,
    {
        "url": ArgumentSpec(ArgumentKind.URL),
        "repository": ArgumentSpec(ArgumentKind.REPOSITORY),
    },
)


class Authority:
    """The caller's ``TaskAuthority`` with a real-looking working set."""

    def __init__(self, project_id, *, capabilities=None, acl=None, remotes=None):
        self.project_id = project_id
        self.capabilities = frozenset(
            capabilities
            if capabilities is not None
            else {
                Capability.PROJECT_READ,
                Capability.PROJECT_TASK_RUN,
                Capability.PROJECT_REPO_WRITE,
                Capability.PROJECT_PR_CREATE,
                Capability.PROJECT_AGENT_USE,  # not delegable: must not pass down
            }
        )
        self.acl = acl
        self.remotes = [REPO_REMOTE] if remotes is None else remotes
        self.grants_asked = 0

    async def parent_grant(self, task):
        self.grants_asked += 1
        return AgentGrant(PARENT_AGENT, self.capabilities, {task.project_id})

    async def parent_scope(self, task):
        acl = self.acl or RepoAcl.inherit(REPO, task.project_id)
        return TaskScope(
            path_roots=[ROOT],
            hosts=["github.com", "api.github.com"],
            projects={task.project_id: ProjectState.ACTIVE},
            credential_handles={HANDLE: ["github.com"]},
            repositories=[
                ScopedRepository(REPO, task.project_id, ROOT, acl, remotes=self.remotes)
            ],
        )


@requires_postgres
class ToolsThroughTheOrchestratorTest(PostgresOrchestratorTestCase):
    async def build(self, **options):
        """A whole stack; returns ``(harness, executor, outcomes, authority)``."""
        database = self.new_database()
        sink = InMemoryAuditSink()
        directory = StaticDirectory(
            principal(
                SystemRole.USER,
                self.user_id,
                {self.project_id: ProjectRole.CONTRIBUTOR},
            )
        )
        store = PostgresApprovalStore(database)
        approvals = ApprovalService(store, sink)
        tracker = BudgetTracker(database)
        executor = FakeExecutor()
        broker = ToolBroker(
            ToolRegistry([*sample_specs(), FETCH]),
            Authorizer(sink, directory=directory),
            store,
            sink,
            budget=TrackerBudgetProvider(tracker),
            task_activity=PostgresTaskActivity(database),
            path_resolver=LexicalPathResolver(),
        )
        authority = options.pop("authority", None) or Authority(self.project_id)
        scripts = options.pop("scripts", {})
        outcomes: dict[str, list] = {}

        def caller(key, tool, arguments):
            async def behave(assignment):
                outcome = await assignment.tools.call(tool, arguments)
                outcomes.setdefault(key, []).append(outcome)
                return ok(f"{key} called {tool}")

            return behave

        runtime = FakeRuntime(
            "local", script={k: caller(k, *v) for k, v in scripts.items()}
        )
        h = self.harness(
            runtimes={"local": runtime},
            tools=ToolRunner(broker, executor),
            authority=authority,
            budget=tracker,
            task_listeners=[approvals.revoke_on_task_end],
            **options,
        )
        return h, executor, outcomes, authority, runtime

    async def test_a_node_reaches_the_tools_through_a_context_the_backend_built(self):
        h, executor, outcomes, authority, _ = await self.build(
            scripts={"impl": ("repo.write_file", WRITE)}
        )
        task_id = await self.prepare(h, make_plan(node("impl")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        (outcome,) = outcomes["impl"]
        self.assertEqual(outcome.status, ExecutionStatus.COMPLETED)
        (invocation,) = executor.invocations
        context = invocation.context
        snapshot = await h.tasks.restore(task_id)
        # The run comes from the task: the run the Start event gave the worker.
        self.assertEqual(context.run, TaskRun(1, 0))
        self.assertEqual(context.run, snapshot.run)
        self.assertEqual(
            (context.task_id, context.delegator_id), (task_id, self.user_id)
        )
        self.assertEqual(context.primary_project_id, self.project_id)
        # The sub-agent is its own agent, with a subset of its parent's grant.
        self.assertNotEqual(context.grant.agent_id, PARENT_AGENT)
        parent = await authority.parent_grant(snapshot)
        self.assertTrue(is_subgrant(context.grant, parent))
        self.assertEqual(
            context.grant.capabilities,
            {
                Capability.PROJECT_READ,
                Capability.PROJECT_TASK_RUN,
                Capability.PROJECT_REPO_WRITE,
            },
        )  # no project.pr.create (no role holds it), no project.agent.use
        self.assertTrue(
            scope_within(context.scope, await authority.parent_scope(snapshot))
        )
        self.assertEqual([r.repo_id for r in context.scope.repositories], [REPO])
        # The tool call was charged to the parent task's budget by the Broker.
        usage = {u.kind: u.consumed for u in await h.budget.usage(task_id)}
        self.assertEqual(usage[BudgetKind.TOOL_CALLS], 1)

    async def test_the_read_only_roles_cannot_write_and_hold_no_credentials(self):
        scripts = {
            "look": ("repo.read_file", READ),
            "review": ("repo.write_file", WRITE),
        }
        h, executor, outcomes, _, _ = await self.build(scripts=scripts)
        await self.prepare(
            h,
            make_plan(
                node("look", role="researcher"),
                node("review", "look", role="reviewer"),
            ),
        )

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        (read,) = outcomes["look"]
        self.assertEqual(read.status, ExecutionStatus.COMPLETED)
        (write,) = outcomes["review"]
        # A reviewer's grant has no write capability: the authorization says no.
        self.assertEqual(write.status, ExecutionStatus.NOT_EXECUTED)
        self.assertEqual(write.decision.verdict, Verdict.DENY)
        self.assertEqual(write.decision.reason, BrokerReason.AUTHZ_DENIED)
        self.assertEqual(len(executor.invocations), 1)  # only the read reached it
        for invocation in executor.invocations:
            self.assertEqual(invocation.context.scope.credential_handles, {})
            self.assertNotIn(
                Capability.PROJECT_REPO_WRITE, invocation.context.grant.capabilities
            )

    async def test_the_worker_gets_the_credential_handles_of_the_task(self):
        h, executor, outcomes, _, _ = await self.build(
            scripts={"impl": ("repo.read_file", READ)}
        )
        await self.prepare(h, make_plan(node("impl")))

        await h.orchestrator.run_once("w1")

        (invocation,) = executor.invocations
        self.assertEqual(
            dict(invocation.context.scope.credential_handles),
            {HANDLE: frozenset({"github.com"})},
        )

    async def test_a_plan_can_narrow_a_node_and_never_widen_it(self):
        scripts = {"impl": ("repo.write_file", WRITE)}
        h, executor, outcomes, _, runtime = await self.build(scripts=scripts)
        await self.prepare(h, make_plan(node("impl", capabilities=["project.read"])))

        await h.orchestrator.run_once("w1")

        (write,) = outcomes["impl"]  # asked for read only: writing is denied
        self.assertEqual(write.decision.reason, BrokerReason.AUTHZ_DENIED)
        self.assertEqual(executor.invocations, [])

    async def test_asking_for_a_right_the_parent_lacks_fails_the_node_for_good(self):
        authority = Authority(
            self.project_id,
            capabilities={Capability.PROJECT_READ, Capability.PROJECT_TASK_RUN},
        )
        h, executor, outcomes, _, runtime = await self.build(
            scripts={"impl": ("repo.write_file", WRITE)}, authority=authority
        )
        task_id = await self.prepare(
            h, make_plan(node("impl", capabilities=["project.repo.write"]))
        )

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        self.assertEqual(runtime.assignments, [])  # the runtime never started
        dag = await self.store.get(task_id, 1)
        (attempt,) = await self.store.attempts(dag.id, "impl")
        self.assertEqual(attempt.error_class, "GrantEscalation")  # not retried
        self.assertEqual(executor.invocations, [])

    async def test_a_repository_outside_the_working_set_is_refused(self):
        h, executor, outcomes, _, runtime = await self.build(
            scripts={"impl": ("repo.read_file", READ)}
        )
        task_id = await self.prepare(
            h, make_plan(node("impl", repositories=[str(REPO2)]))
        )

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        dag = await self.store.get(task_id, 1)
        (attempt,) = await self.store.attempts(dag.id, "impl")
        self.assertEqual(attempt.error_class, "ScopeEscalation")
        self.assertEqual(executor.invocations, [])

    async def test_a_url_passes_only_under_a_registered_remote_of_the_working_set(self):
        good = {"url": REPO_REMOTE + "/issues", "repository": str(REPO)}
        other = {"url": "https://github.com/other/repo", "repository": str(REPO)}
        scripts = {"good": ("repo.fetch", good), "other": ("repo.fetch", other)}
        h, executor, outcomes, _, _ = await self.build(scripts=scripts)
        await self.prepare(h, make_plan(node("good"), node("other")))

        await h.orchestrator.run_once("w1")

        self.assertEqual(outcomes["good"][0].status, ExecutionStatus.COMPLETED)
        denied = outcomes["other"][0]
        self.assertEqual(denied.decision.reason, BrokerReason.REMOTE_NOT_IN_REPOSITORY)
        self.assertEqual(len(executor.invocations), 1)

    async def test_a_repository_without_a_registered_remote_lets_no_url_through(self):
        authority = Authority(self.project_id, remotes=[])
        good = {"url": REPO_REMOTE + "/issues", "repository": str(REPO)}
        h, executor, outcomes, _, _ = await self.build(
            scripts={"impl": ("repo.fetch", good)}, authority=authority
        )
        await self.prepare(h, make_plan(node("impl")))

        await h.orchestrator.run_once("w1")

        self.assertEqual(
            outcomes["impl"][0].decision.reason, BrokerReason.REMOTE_NOT_IN_REPOSITORY
        )
        self.assertEqual(executor.invocations, [])

    async def test_an_acl_narrowed_meanwhile_takes_effect_on_the_next_call(self):
        authority = Authority(self.project_id)
        gate = asyncio.Event()
        calls = []

        async def two_writes(assignment):
            calls.append(await assignment.tools.call("repo.write_file", WRITE))
            await gate.wait()
            calls.append(await assignment.tools.call("repo.write_file", WRITE))
            return ok()

        h, executor, _, _, runtime = await self.build(authority=authority)
        runtime.script["impl"] = [two_writes]
        task_id = await self.prepare(h, make_plan(node("impl")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(calls) == 1, message="the first write")

        # The repository becomes read-only while the node runs.
        authority.acl = RepoAcl.override(REPO, self.project_id, {RepoPermission.READ})
        gate.set()
        await asyncio.wait_for(running, 120)

        self.assertEqual(calls[0].status, ExecutionStatus.COMPLETED)
        self.assertEqual(calls[1].decision.verdict, Verdict.DENY)
        self.assertEqual(calls[1].decision.reason, BrokerReason.AUTHZ_DENIED)
        self.assertEqual(len(executor.invocations), 1)
        self.assertGreater(authority.grants_asked, 2)  # asked again for every call
        self.assertEqual((await h.tasks.restore(task_id)).attempt.number, 1)

    async def test_the_broker_stops_a_call_that_needs_a_budget_the_task_has_used_up(
        self,
    ):
        h, executor, outcomes, _, _ = await self.build(
            scripts={"impl": ("repo.read_file", READ)}
        )
        task_id = await self.prepare(h, make_plan(node("impl")))
        await self.owner_sql(
            "UPDATE budget_usages SET limit_value = 0"
            " WHERE task_id = :t AND kind = 'tool_calls'",
            t=task_id,
        )

        await h.orchestrator.run_once("w1")

        self.assertEqual(
            outcomes["impl"][0].decision.reason, BrokerReason.BUDGET_EXCEEDED
        )
        self.assertEqual(executor.invocations, [])

    async def test_an_approval_of_a_task_that_ends_is_revoked_and_no_call_follows(self):
        release = asyncio.Event()
        seen = []

        async def needs_approval(assignment):
            seen.append(
                await assignment.tools.call(
                    "repo.delete_tree", {"path": f"{ROOT}/build"}
                )
            )
            await release.wait()
            try:
                await assignment.tools.call("repo.read_file", READ)
            except NodeStopped as stop:
                seen.append(stop.reason)
                raise
            return ok()

        h, executor, _, _, runtime = await self.build()
        runtime.script["impl"] = [needs_approval]
        task_id = await self.prepare(h, make_plan(node("impl")))
        running = asyncio.create_task(h.orchestrator.run_once("w1"))
        await until(lambda: len(seen) == 1, message="the approval request")
        outcome = seen[0]
        self.assertEqual(outcome.decision.verdict, Verdict.NEEDS_APPROVAL)
        self.assertEqual(
            await self.scalar(
                "SELECT status FROM tool_approvals WHERE task_id = :t", t=task_id
            ),
            "pending",
        )
        # The approval is bound to the run the worker was started for.
        self.assertEqual(
            tuple(
                (
                    await self.rows(
                        "SELECT task_attempt, task_retry_count FROM tool_approvals"
                    )
                )[0].values()
            ),
            (1, 0),
        )

        await h.tasks.execute(task_id, TaskCommand.CANCEL, actor=self.user)
        release.set()
        report = await asyncio.wait_for(running, 120)

        self.assertEqual(report.outcome, Out.TASK_ENDED)
        # The listener of the task service revoked it, and the node was stopped
        # before its next call could reach the Broker (no read was executed).
        self.assertEqual(
            await self.scalar(
                "SELECT status FROM tool_approvals WHERE task_id = :t", t=task_id
            ),
            "revoked",
        )
        self.assertEqual(len(seen), 2)
        self.assertEqual(executor.invocations, [])

    async def test_a_retried_task_hands_its_new_run_to_the_broker(self):
        h, executor, outcomes, _, runtime = await self.build(
            scripts={"impl": ("repo.read_file", READ)}
        )
        runtime.script["impl"] = [fail("Once", "first run", retryable=False)]
        task_id = await self.prepare(h, make_plan(node("impl")))
        report = await h.orchestrator.run_once("w1")
        self.assertEqual(report.outcome, Out.DAG_FAILED)

        # A Retry: the same attempt, a new run. The node is opened again.
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        await h.queue.enqueue(task_id)

        async def read(assignment):
            outcomes.setdefault("impl", []).append(
                await assignment.tools.call("repo.read_file", READ)
            )
            return ok()

        runtime.script["impl"] = [read]
        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        (invocation,) = executor.invocations
        self.assertEqual(invocation.context.run, TaskRun(1, 1))
        self.assertEqual(invocation.context.run, (await h.tasks.restore(task_id)).run)


if __name__ == "__main__":
    unittest.main()
