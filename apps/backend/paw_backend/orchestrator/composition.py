"""The production composition root of task execution (issue #125).

:func:`build_task_execution` builds, once per process (``paw_backend.app``), the
objects that run tasks, each wired to the others the way production needs:

* ``TaskService`` with the Project state gate (Decision 0020) and ONE transition
  listener, ``TaskEndCleanup.on_task_event`` (``task_end.py``): a task that ends
  keeps no open approval and no active ``session_only`` memory;
* the Tool Broker (PAW-031) with the task budget (``TrackerBudgetProvider``), the
  task's current state (``PostgresTaskActivity``), the worker's queue lease
  (``QueueLeaseVerifier`` on the same ``TaskQueue``: issue #126, Decision 0046),
  the registered ACL of a repository that is not yet in a task's scope
  (``RepositoryService``) and the repository-use gate (``TaskService``), behind
  a ``ToolRunner``. The registry
  holds the Working Set tools only (``WORKING_SET_TOOL_SPECS``, run by
  ``WorkingSetExecutor``): the tools that touch files, git or the network have no
  executor in the backend yet, and a tool that is not registered is refused;
* the production ``TaskAuthority`` (``StoredTaskAuthority``: the scope from the
  stored Working Set, ``authority.py``);
* the ``Orchestrator`` over all of them, **only when agent runtimes are given**:
  the backend has no ``AgentRuntime`` of its own yet (Decision 0047, 5), and an
  orchestrator without one could not run a node. Nothing claims queue entries
  here: starting workers (``Orchestrator.serve``) is left to the issue that brings
  the runtimes (Decision 0047, 5);
* with that orchestrator, and only with it, the Parallel Worktree / Integration
  Node (PAW-035, Decision 0036; issue #155): a ``GitWorktreeCoordinator`` that
  runs git as the task creator's Linux account (``LoginNameAccountDirectory``,
  Decision 0017) through the deployment's ``GitRunner`` (``git_runner``: the
  default ``SubprocessGitRunner`` runs git only as the backend's own Linux user
  and refuses any other account, fail closed; ``SshGitRunner`` reaches the
  account's own user, Decision 0029), with the ``RepositoryPolicy`` of the
  settings (the worktree area ``<home>/<workspace_subdir>/.paw-worktrees``, the
  git timeout). There is no switch to leave it out: every writing Worker node of
  a repository with a checkout gets its own worktree (``REQUIREMENTS.md``), and a
  task without one runs as before (Decision 0056);
* with the Compute Resource Scheduler (PAW-036, issue #165, Decision 0058) and
  only with it, the runtimes that run on a local model (``local_runtimes``,
  :class:`~paw_backend.compute.wiring.LocalRuntime`): each is wrapped in a
  ``HybridRuntime`` on the process's scheduler, so a node takes a lease before it
  uses the GPU and holds a Full GPU Mode off, and the GPU time is charged to the
  task (also late, through ``TrackerLateGpuCharge`` on the same
  ``BudgetTracker``); every local call is recorded in ``local_usage``
  (``PostgresLocalUsage``, Decision 0077). No ``CloudPolicy`` is injected
  (Decision 0037, 14);
* what the maintenance loop (``freshness_loop.build_freshness_loop``) runs: the
  freshness jobs and the task-end sweep (the application's lifespan starts it).

The Broker is built in :func:`build_tool_broker` alone, so that a change of its
interface (the queue lease of issue #126, Decision 0046) touches one place.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from paw_backend.authz import Authorizer, PostgresAuditSink
from paw_backend.compute import (
    ComputeScheduler,
    HybridRuntime,
    PostgresLocalUsage,
    TrackerLateGpuCharge,
)
from paw_backend.compute.wiring import LocalRuntime
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.integration import GitWorktreeCoordinator
from paw_backend.memory.versioning import FreshnessMaintenance
from paw_backend.orchestrator.authority import (
    RepositoryScopes,
    StoredProjectStates,
    StoredTaskAuthority,
)
from paw_backend.orchestrator.config import OrchestratorConfig
from paw_backend.orchestrator.gateway import QueueLeaseVerifier, TrackerBudgetProvider
from paw_backend.orchestrator.orchestrator import Orchestrator
from paw_backend.orchestrator.runtime import AgentRuntime
from paw_backend.orchestrator.store import DagStore
from paw_backend.orchestrator.task_end import TaskEndCleanup, TaskEndResidue
from paw_backend.projects import ProjectStateGate
from paw_backend.repositories import (
    LoginNameAccountDirectory,
    RepositoryPolicy,
    RepositoryService,
    SubprocessGitRunner,
)
from paw_backend.repositories.accounts import AccountDirectory
from paw_backend.repositories.git import GitRunner
from paw_backend.tasks import ProjectGate, TaskService
from paw_backend.tasks.queueing import BudgetTracker, LoopDetector, TaskQueue
from paw_backend.tools import (
    WORKING_SET_TOOL_SPECS,
    ApprovalService,
    PostgresApprovalStore,
    PostgresTaskActivity,
    ToolBroker,
    ToolRegistry,
    ToolRunner,
    WorkingSetExecutor,
)
from paw_backend.tools.interfaces import require_async_method


@dataclass(frozen=True, slots=True)
class TaskExecution:
    """Everything :func:`build_task_execution` built (``orchestrator`` and its
    ``worktrees`` are ``None`` without agent runtimes)."""

    tasks: TaskService
    queue: TaskQueue
    budget: BudgetTracker
    approvals: ApprovalService
    freshness: FreshnessMaintenance
    task_end: TaskEndCleanup
    authority: StoredTaskAuthority
    broker: ToolBroker
    tools: ToolRunner
    orchestrator: Orchestrator | None
    worktrees: GitWorktreeCoordinator | None = None


def build_repository_scopes(
    settings: Settings, database: Database, authorizer: Authorizer
) -> RepositoryService:
    """The repository service the authority and the Broker read registrations
    from. They use only its backend-internal reads (``scope_entries``,
    ``working_set_acl``), which run no git command, so the git runner is the
    default one whatever the deployment's is (Decision 0029)."""
    return RepositoryService.from_policy(
        database,
        authorizer,
        SubprocessGitRunner(),
        RepositoryPolicy.from_settings(settings),
    )


def build_tool_broker(
    database: Database,
    authorizer: Authorizer,
    *,
    tasks: TaskService,
    queue: TaskQueue,
    budget: BudgetTracker,
    approvals: PostgresApprovalStore,
    registrations: RepositoryScopes,
) -> ToolBroker:
    """The Tool Broker of the application (module docstring). Every call is
    fenced on the worker's lease, read from ``queue`` (issue #126, Decision 0046;
    Decision 0047, 7): without a verifier the Broker refuses every call
    (``lease_unavailable``)."""
    return ToolBroker(
        ToolRegistry(WORKING_SET_TOOL_SPECS),
        authorizer,
        approvals,
        PostgresAuditSink(database),
        budget=TrackerBudgetProvider(budget),
        task_activity=PostgresTaskActivity(database),
        lease=QueueLeaseVerifier(queue),
        registrations=registrations,
        use_gate=tasks,
    )


def build_worktrees(
    settings: Settings,
    database: Database,
    *,
    git_runner: GitRunner,
    accounts: AccountDirectory | None = None,
) -> GitWorktreeCoordinator:
    """The worktrees of the orchestrator (module docstring): one
    ``RepositoryPolicy`` of the settings for the account directory (its lowest
    uid of a person) and the coordinator (its worktree area and git timeout)."""
    policy = RepositoryPolicy.from_settings(settings)
    if accounts is None:
        accounts = LoginNameAccountDirectory(database, policy=policy)
    return GitWorktreeCoordinator(runner=git_runner, accounts=accounts, policy=policy)


def _with_local_runtimes(
    runtimes: Mapping[str, AgentRuntime] | None,
    local_runtimes: Mapping[str, LocalRuntime],
    scheduler: ComputeScheduler,
    budget: BudgetTracker,
    database: Database,
) -> dict[str, AgentRuntime]:
    """``runtimes`` and each local runtime in a ``HybridRuntime`` on
    ``scheduler`` (module docstring), whose calls are recorded in ``local_usage``
    (Decision 0077). A label given twice is a ``TypeError``."""
    if not isinstance(local_runtimes, Mapping):
        raise TypeError("local_runtimes must be a mapping")
    combined: dict[str, AgentRuntime] = dict(runtimes or {})
    late_charge = TrackerLateGpuCharge(budget)
    usage = PostgresLocalUsage(database)
    for label, local in local_runtimes.items():
        if not isinstance(local, LocalRuntime):
            raise TypeError("each local runtime must be a LocalRuntime")
        if label in combined:
            raise TypeError("a runtime label is given twice")
        combined[label] = HybridRuntime(
            scheduler,
            local.runtime,
            deployment=local.deployment,
            local_model=local.local_model,
            resource_class=local.resource_class,
            wait_seconds=local.wait_seconds,
            late_gpu_charge=late_charge,
            usage=usage,
        )
    return combined


def build_task_execution(
    settings: Settings,
    database: Database,
    authorizer: Authorizer,
    *,
    project_gate: ProjectGate | None = None,
    repositories: RepositoryScopes | None = None,
    runtimes: Mapping[str, AgentRuntime] | None = None,
    orchestrator_config: OrchestratorConfig | None = None,
    git_runner: GitRunner | None = None,
    accounts: AccountDirectory | None = None,
    scheduler: ComputeScheduler | None = None,
    local_runtimes: Mapping[str, LocalRuntime] | None = None,
) -> TaskExecution:
    """Build the task execution of the application (module docstring).

    ``project_gate`` defaults to ``ProjectStateGate()`` (a test passes another);
    ``repositories`` to :func:`build_repository_scopes`. ``runtimes`` and
    ``orchestrator_config`` come together or not at all (``TypeError``);
    ``local_runtimes`` (the runtimes on a local model, wrapped in a
    ``HybridRuntime``) need the ``scheduler`` and ``orchestrator_config`` and
    may come with or without ``runtimes``, under other labels (``TypeError``).
    ``git_runner`` is the deployment's ``GitRunner`` for the worktrees (default
    ``SubprocessGitRunner()``), ``accounts`` their account directory (default
    ``LoginNameAccountDirectory``; a test passes its own). Both are checked
    whether or not an orchestrator is built (``TypeError``)."""
    if not isinstance(database, Database):
        raise TypeError("database must be a Database")
    if not isinstance(authorizer, Authorizer):
        raise TypeError("authorizer must be an Authorizer")
    if local_runtimes is not None:
        if scheduler is None:
            raise TypeError("local_runtimes need the compute scheduler")
        if orchestrator_config is None:
            raise TypeError("local_runtimes need an orchestrator_config")
    elif (runtimes is None) != (orchestrator_config is None):
        raise TypeError("runtimes and orchestrator_config come together")
    if scheduler is not None and not isinstance(scheduler, ComputeScheduler):
        raise TypeError("scheduler must be a ComputeScheduler")
    gate = project_gate if project_gate is not None else ProjectStateGate()
    if repositories is None:
        repositories = build_repository_scopes(settings, database, authorizer)
    require_async_method(repositories, "working_set_acl", 1)
    require_async_method(repositories, "scope_entries", 3)
    if git_runner is None:
        git_runner = SubprocessGitRunner()
    require_async_method(git_runner, "run", 1)
    if accounts is not None:
        require_async_method(accounts, "account_of", 1)

    approval_store = PostgresApprovalStore(database)
    approvals = ApprovalService(approval_store, PostgresAuditSink(database))
    freshness = FreshnessMaintenance(database)
    task_end = TaskEndCleanup(approvals, freshness, TaskEndResidue(database))
    tasks = TaskService(database, project_gate=gate, listeners=[task_end.on_task_event])
    queue = TaskQueue(database, project_gate=gate)
    budget = BudgetTracker(database)
    authority = StoredTaskAuthority(tasks, repositories, StoredProjectStates(database))
    broker = build_tool_broker(
        database,
        authorizer,
        tasks=tasks,
        queue=queue,
        budget=budget,
        approvals=approval_store,
        registrations=repositories,
    )
    tools = ToolRunner(broker, WorkingSetExecutor(tasks))
    orchestrator = None
    worktrees = None
    if local_runtimes is not None and scheduler is not None:
        runtimes = _with_local_runtimes(
            runtimes, local_runtimes, scheduler, budget, database
        )
    if runtimes is not None and orchestrator_config is not None:
        worktrees = build_worktrees(
            settings, database, git_runner=git_runner, accounts=accounts
        )
        orchestrator = Orchestrator(
            tasks=tasks,
            queue=queue,
            budget=budget,
            loops=LoopDetector(database),
            store=DagStore(database),
            activity=PostgresTaskActivity(database),
            tools=tools,
            authority=authority,
            runtimes=runtimes,
            config=orchestrator_config,
            worktrees=worktrees,
        )
    return TaskExecution(
        tasks=tasks,
        queue=queue,
        budget=budget,
        approvals=approvals,
        freshness=freshness,
        task_end=task_end,
        authority=authority,
        broker=broker,
        tools=tools,
        orchestrator=orchestrator,
        worktrees=worktrees,
    )
