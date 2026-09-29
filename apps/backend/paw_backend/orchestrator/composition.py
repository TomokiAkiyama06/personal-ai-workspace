"""The production composition root of task execution (issue #125).

:func:`build_task_execution` builds, once per process (``paw_backend.app``), the
objects that run tasks, each wired to the others the way production needs:

* ``TaskService`` with the Project state gate (Decision 0020) and ONE transition
  listener, ``TaskEndCleanup.on_task_event`` (``task_end.py``): a task that ends
  keeps no open approval and no active ``session_only`` memory;
* the Tool Broker (PAW-031) with the task budget (``TrackerBudgetProvider``), the
  task's current state (``PostgresTaskActivity``), the registered ACL of a
  repository that is not yet in a task's scope (``RepositoryService``) and the
  repository-use gate (``TaskService``), behind a ``ToolRunner``. The registry
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
* what the maintenance loop (``freshness_loop.build_freshness_loop``) runs: the
  freshness jobs and the task-end sweep (the application's lifespan starts it).

The Broker is built in :func:`build_tool_broker` alone, so that a change of its
interface (the queue lease of issue #126, Decision 0046) touches one place.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from paw_backend.authz import Authorizer, PostgresAuditSink
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.memory.versioning import FreshnessMaintenance
from paw_backend.orchestrator.authority import (
    RepositoryScopes,
    StoredProjectStates,
    StoredTaskAuthority,
)
from paw_backend.orchestrator.config import OrchestratorConfig
from paw_backend.orchestrator.gateway import TrackerBudgetProvider
from paw_backend.orchestrator.orchestrator import Orchestrator
from paw_backend.orchestrator.runtime import AgentRuntime
from paw_backend.orchestrator.store import DagStore
from paw_backend.orchestrator.task_end import TaskEndCleanup, TaskEndResidue
from paw_backend.projects import ProjectStateGate
from paw_backend.repositories import (
    RepositoryPolicy,
    RepositoryService,
    SubprocessGitRunner,
)
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
    """Everything :func:`build_task_execution` built (``orchestrator`` is
    ``None`` without agent runtimes)."""

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
    budget: BudgetTracker,
    approvals: PostgresApprovalStore,
    registrations: RepositoryScopes,
) -> ToolBroker:
    """The Tool Broker of the application (module docstring)."""
    return ToolBroker(
        ToolRegistry(WORKING_SET_TOOL_SPECS),
        authorizer,
        approvals,
        PostgresAuditSink(database),
        budget=TrackerBudgetProvider(budget),
        task_activity=PostgresTaskActivity(database),
        registrations=registrations,
        use_gate=tasks,
    )


def build_task_execution(
    settings: Settings,
    database: Database,
    authorizer: Authorizer,
    *,
    project_gate: ProjectGate | None = None,
    repositories: RepositoryScopes | None = None,
    runtimes: Mapping[str, AgentRuntime] | None = None,
    orchestrator_config: OrchestratorConfig | None = None,
) -> TaskExecution:
    """Build the task execution of the application (module docstring).

    ``project_gate`` defaults to ``ProjectStateGate()`` (a test passes another);
    ``repositories`` to :func:`build_repository_scopes`. ``runtimes`` and
    ``orchestrator_config`` come together or not at all (``TypeError``)."""
    if not isinstance(database, Database):
        raise TypeError("database must be a Database")
    if not isinstance(authorizer, Authorizer):
        raise TypeError("authorizer must be an Authorizer")
    if (runtimes is None) != (orchestrator_config is None):
        raise TypeError("runtimes and orchestrator_config come together")
    gate = project_gate if project_gate is not None else ProjectStateGate()
    if repositories is None:
        repositories = build_repository_scopes(settings, database, authorizer)
    require_async_method(repositories, "working_set_acl", 1)
    require_async_method(repositories, "scope_entries", 3)

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
        budget=budget,
        approvals=approval_store,
        registrations=repositories,
    )
    tools = ToolRunner(broker, WorkingSetExecutor(tasks))
    orchestrator = None
    if runtimes is not None and orchestrator_config is not None:
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
    )
