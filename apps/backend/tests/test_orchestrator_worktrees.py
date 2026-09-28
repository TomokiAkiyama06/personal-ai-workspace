"""The orchestrator with worktrees and an integration node (PAW-035).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. The first classes use a fake
``NodeWorkspaces`` (what the orchestrator asks and does with the answers); the
last one runs the real ``GitWorktreeCoordinator`` on temporary repositories of
the user running the tests (``SubprocessGitRunner``: no SSH, no other user).
"""

import asyncio
import uuid

from paw_backend.authz import Capability, RepoAcl
from paw_backend.orchestrator.domain import DagState, RunOutcome
from paw_backend.orchestrator.runtime import NodeOutcome
from paw_backend.orchestrator.workspaces import (
    IntegrationReport,
    IntegrationState,
    NodeWorktree,
    RepositoryIntegration,
    WorktreeConflictError,
    WorktreeProblem,
    WorktreeUnavailableError,
)
from paw_backend.tasks import TaskCommand, TaskState, WaitReason
from paw_backend.tasks.queueing import BudgetPreset
from paw_backend.tools import ScopedRepository

from .orchestrator_support import (
    ROOT,
    FakeAuthority,
    FakeRuntime,
    PostgresOrchestratorTestCase,
    make_plan,
    node,
    ok,
    requires_postgres,
)
from .repositories_support import fs, requires_git
from .worktrees_support import Workspace, commit_file, git

Out = RunOutcome
R1 = uuid.UUID(int=0x3501)


class FakeWorkspaces:
    """A ``NodeWorkspaces`` that records what it was asked."""

    def __init__(self, *, report=None, prepare_error=None, integrate_error=None):
        self.prepared: list = []
        self.integrated: list = []
        self.report = report
        self.prepare_error = prepare_error
        self.integrate_error = integrate_error

    async def prepare_node(self, request):
        self.prepared.append(request)
        if self.prepare_error is not None:
            raise self.prepare_error
        return {
            repository.repo_id: NodeWorktree(
                repository.repo_id,
                f"/srv/paw-orch/trees/{request.node_key}",
                f"paw/t/1/{request.node_key}",
                protected=(repository.root,),
            )
            for repository in request.scope.repositories
            if repository.root is not None
        }

    async def integrate(self, request):
        self.integrated.append(request)
        if self.integrate_error is not None:
            raise self.integrate_error
        if self.report is not None:
            return self.report
        return IntegrationReport(
            tuple(
                RepositoryIntegration(
                    repository.repo_id,
                    IntegrationState.MERGED,
                    branch="paw/t/1/_integration",
                    path="/srv/paw-orch/trees/_integration",
                    head="a" * 40,
                    merged=request.workers,
                )
                for repository in request.scope.repositories
            )
        )


class WorktreeTestCase(PostgresOrchestratorTestCase):
    def authority(self, **options):
        repositories = options.pop(
            "repositories",
            [
                ScopedRepository(
                    R1,
                    self.project_id,
                    f"{ROOT}/one",
                    RepoAcl.inherit(R1, self.project_id),
                )
            ],
        )
        return FakeAuthority(repositories=repositories, **options)


@requires_postgres
class NodeWorktreeTest(WorktreeTestCase):
    async def test_only_writing_workers_get_a_worktree_and_their_scope_uses_it(self):
        runtime = FakeRuntime("local")
        workspaces = FakeWorkspaces()
        h = self.harness(
            runtimes={"local": runtime},
            authority=self.authority(),
            worktrees=workspaces,
        )
        plan = make_plan(
            node("research", role="researcher"),
            node("a", "research"),
            node("b", "research"),
            node("c", "a", "b"),
            node("readonly", capabilities=["project.read"]),
            node("review", "c", role="reviewer"),
        )
        task_id = await self.prepare(h, plan)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        asked = {request.node_key: request for request in workspaces.prepared}
        # Worker nodes that may write: a, b, c. Not the researcher, the reviewer
        # or the worker whose plan asked for reading only.
        self.assertEqual(set(asked), {"a", "b", "c"})
        self.assertEqual(asked["c"].upstream_workers, ("a", "b"))
        self.assertEqual(asked["a"].upstream_workers, ())
        self.assertEqual(asked["a"].task.id, task_id)
        for key in ("a", "b", "c"):
            (assignment,) = runtime.calls_of(key)
            (worktree,) = assignment.worktrees.values()
            self.assertEqual(worktree.path, f"/srv/paw-orch/trees/{key}")
        for key in ("research", "readonly", "review"):
            (assignment,) = runtime.calls_of(key)
            self.assertEqual(dict(assignment.worktrees), {})

    async def test_a_conflict_between_upstream_branches_fails_the_node_at_once(self):
        runtime = FakeRuntime("local")
        workspaces = FakeWorkspaces(prepare_error=WorktreeConflictError())
        h = self.harness(
            runtimes={"local": runtime},
            authority=self.authority(),
            worktrees=workspaces,
        )
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").error_class, "WorktreeConflict")
        self.assertEqual(len(workspaces.prepared), 1)  # no retry
        self.assertEqual(runtime.calls_of("a"), [])  # the agent never ran

    async def test_an_unavailable_worktree_is_retried(self):
        runtime = FakeRuntime("local")
        workspaces = FakeWorkspaces(
            prepare_error=WorktreeUnavailableError(WorktreeProblem.GIT_FAILED)
        )
        h = self.harness(
            runtimes={"local": runtime},
            authority=self.authority(),
            worktrees=workspaces,
            config={"max_attempts_per_rung": 2},
        )
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_FAILED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").error_class, "WorktreeUnavailable")
        self.assertEqual(len(workspaces.prepared), 2)

    async def test_without_a_checkout_there_is_no_worktree(self):
        runtime = FakeRuntime("local")
        workspaces = FakeWorkspaces()
        h = self.harness(
            runtimes={"local": runtime},
            authority=self.authority(
                repositories=[ScopedRepository(R1, self.project_id)]
            ),
            worktrees=workspaces,
        )
        await self.prepare(h, make_plan(node("a")))
        self.assertEqual(
            (await h.orchestrator.run_once("w1")).outcome, Out.DAG_SUCCEEDED
        )
        self.assertEqual(workspaces.prepared, [])

    async def test_a_worktree_the_seam_made_up_is_a_scope_escalation(self):
        class Rogue(FakeWorkspaces):
            async def prepare_node(self, request):
                other = uuid.UUID(int=0x3599)
                return {other: NodeWorktree(other, "/srv/elsewhere", "paw/x")}

        h = self.harness(authority=self.authority(), worktrees=Rogue())
        task_id = await self.prepare(h, make_plan(node("a")))
        self.assertEqual((await h.orchestrator.run_once("w1")).outcome, Out.DAG_FAILED)
        dag = await self.store.get(task_id, 1)
        self.assertEqual(dag.node("a").error_class, "ScopeEscalation")


@requires_postgres
class IntegrationNodeTest(WorktreeTestCase):
    async def test_the_worker_branches_are_integrated_before_evaluation(self):
        workspaces = FakeWorkspaces()
        h = self.harness(authority=self.authority(), worktrees=workspaces)
        plan = make_plan(node("a"), node("b"), node("r", "a", "b", role="reviewer"))
        task_id = await self.prepare(h, plan)

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        (request,) = workspaces.integrated
        self.assertEqual(request.workers, ("a", "b"))
        self.assertEqual([r.repo_id for r in request.scope.repositories], [R1])
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)
        # The attempt names the integration branch, not a worker's branch.
        worktree = snapshot.attempt.worktree
        self.assertEqual(
            (worktree.branch, worktree.path, worktree.head_commit),
            ("paw/t/1/_integration", "/srv/paw-orch/trees/_integration", "a" * 40),
        )
        messages = [log.message for log in snapshot.recent_logs]
        self.assertIn(f"Integration of repository {R1}: 2 branch(es) merged", messages)

    async def test_a_conflict_puts_the_task_in_waiting_for_a_human(self):
        report = IntegrationReport(
            (
                RepositoryIntegration(
                    R1,
                    IntegrationState.CONFLICT,
                    branch="paw/t/1/_integration",
                    path="/srv/paw-orch/trees/_integration",
                    merged=("a",),
                    blocking_node="b",
                    conflicted_files=("secret/path.py",),
                ),
            )
        )
        h = self.harness(
            authority=self.authority(), worktrees=FakeWorkspaces(report=report)
        )
        task_id = await self.prepare(h, make_plan(node("a"), node("b")))

        outcome = await h.orchestrator.run_once("w1")

        self.assertEqual(outcome.outcome, Out.INTEGRATION_CONFLICT)
        self.assertEqual(outcome.dag_state, DagState.SUCCEEDED)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.WAITING)
        self.assertEqual(snapshot.wait_reason, WaitReason.USER)
        messages = [log.message for log in snapshot.recent_logs]
        self.assertIn(
            f"Integration of repository {R1}: the branch of node b conflicts"
            " (1 file(s))",
            messages,
        )
        # A file name is not written to the task log.
        self.assertFalse(any("secret/path.py" in m for m in messages))

    async def test_after_the_human_resolved_it_the_integration_runs_again(self):
        conflict = IntegrationReport(
            (RepositoryIntegration(R1, IntegrationState.CONFLICT, blocking_node="a"),)
        )
        workspaces = FakeWorkspaces(report=conflict)
        h = self.harness(authority=self.authority(), worktrees=workspaces)
        runtime = h.runtimes["local"]
        task_id = await self.prepare(h, make_plan(node("a")))
        self.assertEqual(
            (await h.orchestrator.run_once("w1")).outcome, Out.INTEGRATION_CONFLICT
        )

        # The human resolved it; the task is unblocked and enqueued again.
        workspaces.report = None
        await h.tasks.execute(task_id, TaskCommand.UNBLOCK, actor=self.user)
        await h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.STANDARD)
        report = await h.orchestrator.run_once("w2")

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(workspaces.integrated), 2)
        self.assertEqual(len(runtime.calls_of("a")), 1)  # the node did not run again
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)

    async def test_a_git_failure_fails_the_task_and_a_retry_integrates_again(self):
        workspaces = FakeWorkspaces(
            integrate_error=WorktreeUnavailableError(WorktreeProblem.GIT_FAILED)
        )
        h = self.harness(authority=self.authority(), worktrees=workspaces)
        task_id = await self.prepare(h, make_plan(node("a")))

        report = await h.orchestrator.run_once("w1")

        self.assertEqual(report.outcome, Out.INTEGRATION_FAILED)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.FAILED)
        self.assertIn(
            "Integration failed (git_failed)",
            [log.message for log in snapshot.recent_logs],
        )

        workspaces.integrate_error = None
        await h.tasks.execute(task_id, TaskCommand.RETRY, actor=self.user)
        await h.orchestrator.enqueue_task(task_id, preset=BudgetPreset.STANDARD)
        again = await h.orchestrator.run_once("w2")
        self.assertEqual(again.outcome, Out.DAG_SUCCEEDED)
        self.assertEqual(len(workspaces.integrated), 2)

    async def test_an_unexpected_error_is_named_by_its_class_only(self):
        workspaces = FakeWorkspaces(integrate_error=RuntimeError("token=abc"))
        h = self.harness(authority=self.authority(), worktrees=workspaces)
        task_id = await self.prepare(h, make_plan(node("a")))
        self.assertEqual(
            (await h.orchestrator.run_once("w1")).outcome, Out.INTEGRATION_FAILED
        )
        messages = [log.message for log in (await h.tasks.restore(task_id)).recent_logs]
        self.assertIn("Integration failed (RuntimeError)", messages)
        self.assertFalse(any("abc" in m for m in messages))

    async def test_a_failed_dag_is_not_integrated(self):
        workspaces = FakeWorkspaces()
        runtime = FakeRuntime(
            "local", script={"a": NodeOutcome.failed("Boom", retryable=False)}
        )
        h = self.harness(
            runtimes={"local": runtime},
            authority=self.authority(),
            worktrees=workspaces,
        )
        await self.prepare(h, make_plan(node("a")))
        self.assertEqual((await h.orchestrator.run_once("w1")).outcome, Out.DAG_FAILED)
        self.assertEqual(workspaces.integrated, [])

    async def test_without_workspaces_nothing_changes(self):
        h = self.harness(authority=self.authority())
        task_id = await self.prepare(h, make_plan(node("a")))
        self.assertEqual(
            (await h.orchestrator.run_once("w1")).outcome, Out.DAG_SUCCEEDED
        )
        snapshot = await h.tasks.restore(task_id)
        self.assertIsNone(snapshot.attempt.worktree.branch)

    def test_the_seam_is_checked_when_the_orchestrator_is_built(self):
        class Broken:
            def prepare_node(self, request):  # not async
                return {}

            async def integrate(self, request):
                return IntegrationReport()

        with self.assertRaises(TypeError):
            self.harness(worktrees=Broken())


@requires_postgres
@requires_git
class RealGitIntegrationTest(WorktreeTestCase):
    """Two Worker nodes of one repository run in parallel in their own worktrees,
    commit there, and are integrated into the task's integration branch; the
    user's checkout and its default branch never change."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.ws = Workspace(user_id=self.user_id, project_id=self.project_id)
        self.addCleanup(self.ws.close)
        self.repo = self.ws.add_checkout("repo")
        self.checkout = self.ws.checkout(self.repo)
        self.main_head = git("rev-parse", "HEAD", cwd=self.checkout)

    def real_authority(self):
        return FakeAuthority(
            repositories=self.ws.repositories,
            capabilities={
                Capability.PROJECT_READ,
                Capability.PROJECT_TASK_RUN,
                Capability.PROJECT_REPO_WRITE,
            },
        )

    async def run_task(self, script, plan):
        def working(key, name, content):
            async def behave(assignment):
                (worktree,) = assignment.worktrees.values()
                await asyncio.to_thread(commit_file, worktree.path, name, content)
                return ok(f"{key} done")

            return behave

        runtime = FakeRuntime(
            "local", script={key: working(key, *args) for key, args in script.items()}
        )
        h = self.harness(
            runtimes={"local": runtime},
            authority=self.real_authority(),
            worktrees=self.ws.coordinator(),
        )
        task_id = await self.prepare(h, plan)
        report = await h.orchestrator.run_once("w1")
        return h, runtime, task_id, report

    def assert_checkout_untouched(self):
        self.assertEqual(git("rev-parse", "main", cwd=self.checkout), self.main_head)
        self.assertEqual(
            git("symbolic-ref", "--short", "HEAD", cwd=self.checkout), "main"
        )
        self.assertEqual(git("status", "--porcelain", cwd=self.checkout), "")
        self.assertFalse(
            {"push", "fetch", "checkout", "reset"} & set(self.ws.runner.subcommands())
        )

    async def test_parallel_workers_are_integrated_and_the_task_goes_to_evaluation(
        self,
    ):
        h, runtime, task_id, report = await self.run_task(
            {"a": ("a.txt", "from a\n"), "b": ("b.txt", "from b\n")},
            make_plan(node("a"), node("b")),
        )

        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        (a,) = runtime.calls_of("a")
        (b,) = runtime.calls_of("b")
        self.assertNotEqual(a.worktrees[self.repo].path, b.worktrees[self.repo].path)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.EVALUATING)
        integration = snapshot.attempt.worktree
        self.assertEqual(integration.branch, f"paw/{task_id}/1/_integration")
        self.assertEqual(fs.read(integration.path, "a.txt"), "from a\n")
        self.assertEqual(fs.read(integration.path, "b.txt"), "from b\n")
        self.assertEqual(
            git("rev-parse", "HEAD", cwd=integration.path), integration.head_commit
        )
        self.assert_checkout_untouched()

    async def test_a_real_conflict_waits_for_the_human(self):
        h, _runtime, task_id, report = await self.run_task(
            {"a": ("code.txt", "a's version\n"), "b": ("code.txt", "b's version\n")},
            make_plan(node("a"), node("b")),
        )

        self.assertEqual(report.outcome, Out.INTEGRATION_CONFLICT)
        snapshot = await h.tasks.restore(task_id)
        self.assertEqual(snapshot.state, TaskState.WAITING)
        integration = f"{self.ws.base}/{task_id}/1/{self.repo}/_integration"
        self.assertEqual(git("status", "--porcelain", cwd=integration), "")
        self.assertEqual(fs.read(integration, "code.txt"), "a's version\n")
        self.assert_checkout_untouched()

    async def test_a_dependent_worker_builds_on_its_upstream(self):
        h, runtime, task_id, report = await self.run_task(
            {"a": ("a.txt", "from a\n"), "b": ("b.txt", "from b\n")},
            make_plan(node("a"), node("b", "a")),
        )
        self.assertEqual(report.outcome, Out.DAG_SUCCEEDED)
        (b,) = runtime.calls_of("b")
        self.assertEqual(fs.read(b.worktrees[self.repo].path, "a.txt"), "from a\n")
        self.assert_checkout_untouched()
