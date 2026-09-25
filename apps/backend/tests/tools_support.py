"""Fixtures for the Tool Broker tests (not a test module: no ``test_`` prefix)."""

import uuid
from datetime import UTC, datetime, timedelta

from paw_backend.authz import (
    ALL_PROJECTS,
    AgentGrant,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    ProjectRole,
    ProjectState,
    RepoAcl,
    SystemRole,
)
from paw_backend.tools import (
    ApprovalLevel,
    ApprovalService,
    ArgumentKind,
    ArgumentSpec,
    BudgetStatus,
    Environment,
    InMemoryApprovalStore,
    LexicalPathResolver,
    ScopedRepository,
    TaskActivity,
    TaskContext,
    TaskRun,
    TaskScope,
    ToolBroker,
    ToolCall,
    ToolCapability,
    ToolRegistry,
    ToolRunner,
    ToolSpec,
)

from .authz_support import AGENT, P1, P2, U1, U2, StaticDirectory, principal, uid

ROOT = "/srv/paw-test/worktree"
# The repository of P1 that the default task scope holds: its worktree is ROOT
# and it inherits its project's permissions.
REPO = uid(701)
# The URL that addresses REPO (the backend registers it with the repository).
REPO_REMOTE = "https://github.com/org/repo"
REPO_API = "https://api.github.com/repos/org/repo"
HANDLE = "cred_" + "a1" * 16
OTHER_HANDLE = "cred_" + "b2" * 16
TASK = uid(501)
# The run of a task that was just created (attempt 1, never retried).
RUN = TaskRun(1, 0)
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

C = ToolCapability
A = ArgumentKind


def _args(**kinds: ArgumentSpec | ArgumentKind) -> dict[str, ArgumentSpec]:
    return {
        name: kind if isinstance(kind, ArgumentSpec) else ArgumentSpec(kind)
        for name, kind in kinds.items()
    }


def sample_specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            "repo.read_file",
            frozenset({C.READ}),
            Capability.PROJECT_READ,
            _args(path=A.PATH),
        ),
        ToolSpec(
            "repo.write_file",
            frozenset({C.WRITE}),
            Capability.PROJECT_REPO_WRITE,
            _args(path=A.PATH, content=ArgumentSpec(A.TEXT, max_length=500)),
        ),
        ToolSpec(
            "repo.delete_tree",
            frozenset({C.WRITE, C.DESTRUCTIVE}),
            Capability.PROJECT_REPO_WRITE,
            _args(path=A.PATH),
        ),
        ToolSpec(
            "tests.run",
            frozenset({C.EXECUTE}),
            Capability.PROJECT_TASK_RUN,
            {"selector": ArgumentSpec(A.TEXT, required=False, max_length=100)},
        ),
        ToolSpec(
            "web.fetch",
            frozenset({C.READ, C.NETWORK}),
            Capability.PROJECT_READ,
            _args(url=A.URL),
        ),
        ToolSpec(
            "issues.create",
            frozenset({C.WRITE, C.NETWORK}),
            Capability.PROJECT_PR_CREATE,
            _args(
                url=A.URL,
                repository=A.REPOSITORY,
                title=ArgumentSpec(A.TEXT, max_length=200),
            ),
        ),
        ToolSpec(
            "git.push",
            frozenset({C.WRITE, C.NETWORK, C.CREDENTIAL_USE}),
            Capability.PROJECT_PR_CREATE,
            _args(
                remote=A.URL, repository=A.REPOSITORY, credential=A.CREDENTIAL_HANDLE
            ),
        ),
        ToolSpec(
            "git.merge",
            frozenset({C.WRITE, C.NETWORK, C.CREDENTIAL_USE}),
            Capability.PROJECT_REPO_WRITE,
            _args(
                remote=A.URL,
                repository=A.REPOSITORY,
                credential=A.CREDENTIAL_HANDLE,
                pull_request=ArgumentSpec(A.INTEGER, minimum=1, maximum=10**6),
            ),
            min_level=ApprovalLevel.STRONG_APPROVAL,
        ),
        ToolSpec(
            "host.install_package",
            frozenset({C.WRITE, C.EXECUTE}),
            Capability.PROJECT_TASK_RUN,
            _args(package=ArgumentSpec(A.TEXT, max_length=100)),
            environment=Environment.HOST,
        ),
        ToolSpec(
            "credentials.read",
            frozenset({C.CREDENTIAL_USE}),
            Capability.GITHUB_USE,
            _args(credential=A.CREDENTIAL_HANDLE),
            returns_credential_plaintext=True,
        ),
        ToolSpec(
            "admin.set_role",
            frozenset({C.WRITE}),
            Capability.ADMIN_USERS_MANAGE,
            _args(project=A.PROJECT),
            min_level=ApprovalLevel.STRONG_APPROVAL,
        ),
        ToolSpec(
            "project.export",
            frozenset({C.WRITE}),
            Capability.PROJECT_REPO_WRITE,
            _args(project=A.PROJECT, path=A.PATH),
        ),
        ToolSpec(
            "notes.list",
            frozenset({C.READ}),
            Capability.MEMORY_USE,
            {},
        ),
        ToolSpec(
            "free.ping",
            frozenset({C.READ}),
            Capability.PROJECT_READ,
            {},
            requires_budget=False,
        ),
        ToolSpec(
            "flag.toggle",
            frozenset({C.EXECUTE}),
            Capability.PROJECT_TASK_RUN,
            {
                "enabled": ArgumentSpec(A.BOOLEAN),
                "count": ArgumentSpec(A.INTEGER, required=False, minimum=0, maximum=9),
            },
        ),
    ]


def sample_registry() -> ToolRegistry:
    return ToolRegistry(sample_specs())


def make_scope(**overrides) -> TaskScope:
    arguments = {
        "path_roots": [ROOT],
        "hosts": ["github.com", "api.github.com"],
        "projects": {P1: ProjectState.ACTIVE},
        # HANDLE is a GitHub credential: valid for GitHub hosts only.
        "credential_handles": {HANDLE: ["github.com", "api.github.com"]},
        "repositories": [
            ScopedRepository(
                REPO,
                P1,
                ROOT,
                RepoAcl.inherit(REPO, P1),
                remotes=[REPO_REMOTE, REPO_REMOTE + ".git", REPO_API],
            )
        ],
    }
    arguments.update(overrides)
    return TaskScope(**arguments)


def make_grant(*capabilities: Capability, projects=None) -> AgentGrant:
    return AgentGrant(
        AGENT,
        frozenset(
            capabilities
            or {
                Capability.PROJECT_READ,
                Capability.PROJECT_REPO_WRITE,
                Capability.PROJECT_TASK_RUN,
                Capability.PROJECT_PR_CREATE,
                Capability.MEMORY_USE,
            }
        ),
        {P1} if projects is None else projects,
    )


def make_context(**overrides) -> TaskContext:
    arguments = {
        "task_id": TASK,
        "delegator_id": U1,
        "grant": make_grant(),
        "scope": make_scope(),
        "primary_project_id": P1,
        # a task that was just created: attempt 1, never retried
        "run": RUN,
    }
    arguments.update(overrides)
    return TaskContext(**arguments)


def make_call(tool, arguments=None, *, context=None, correlation_id=None) -> ToolCall:
    return ToolCall(
        tool,
        {} if arguments is None else arguments,
        context or make_context(),
        correlation_id,
    )


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class FakeBudget:
    def __init__(self, status=BudgetStatus.WITHIN_BUDGET, *, error=None) -> None:
        self.status = status
        self.error = error
        self.checks: list[tuple[uuid.UUID, str]] = []
        self.charges: list[tuple[uuid.UUID, str]] = []

    async def check(self, task_id, tool):
        self.checks.append((task_id, tool))
        if self.error is not None:
            raise self.error
        return self.status

    async def charge(self, task_id, tool):
        self.charges.append((task_id, tool))
        if self.error is not None:
            raise self.error


class FakeTaskActivity:
    """Answers what the test says about the task; records every question."""

    def __init__(self, answer=TaskActivity.ACTIVE, *, error=None) -> None:
        self.answer = answer
        self.error = error
        self.checks: list[uuid.UUID] = []
        self.runs: list[TaskRun] = []  # the run each question was about

    async def check(self, task_id, run):
        self.checks.append(task_id)
        self.runs.append(run)
        if self.error is not None:
            raise self.error
        return self.answer


class FakeExecutor:
    """Records invocations; returns ``result`` or raises ``error``."""

    def __init__(self, result=None, *, error=None) -> None:
        self.result = {"ok": True} if result is None else result
        self.error = error
        self.invocations = []

    async def execute(self, invocation):
        self.invocations.append(invocation)
        if self.error is not None:
            raise self.error
        return self.result


class DictResolver:
    """A file system that only knows the symlinks it is told about."""

    def __init__(self, links: dict[str, str] | None = None) -> None:
        self.links = links or {}
        self.calls: list[str] = []

    async def resolve(self, path: str) -> str:
        self.calls.append(path)
        for link, target in self.links.items():
            if path == link or path.startswith(link + "/"):
                return target + path[len(link) :]
        return path


class StepUp:
    def __init__(self, answer=True) -> None:
        self.answer = answer
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def verify(self, user_id, approval_id):
        self.calls.append((user_id, approval_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Harness:
    """A broker with in-memory adapters, wired the way production wires it."""

    def __init__(self, **overrides) -> None:
        self.clock = overrides.pop("clock", Clock())
        self.sink = overrides.pop("sink", InMemoryAuditSink())
        self.approvals = overrides.pop("approvals", InMemoryApprovalStore())
        self.budget = overrides.pop("budget", FakeBudget())
        # The default task is alive; a test moves it with ``task_activity.answer``.
        self.task_activity = overrides.pop("task_activity", FakeTaskActivity())
        self.directory = overrides.pop(
            "directory",
            StaticDirectory(
                principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
                principal(SystemRole.USER, U2, {P1: ProjectRole.VIEWER}),
            ),
        )
        self.authorizer = overrides.pop(
            "authorizer",
            Authorizer(self.sink, directory=self.directory, clock=self.clock),
        )
        # The broker's own audit sink can be a different (failing) one.
        self.broker_sink = overrides.pop("broker_sink", self.sink)
        self.executor = overrides.pop("executor", FakeExecutor())
        self.events: list = []
        listeners = overrides.pop("listeners", (self.events.append,))
        self.broker = ToolBroker(
            overrides.pop("registry", sample_registry()),
            self.authorizer,
            self.approvals,
            self.broker_sink,
            budget=self.budget,
            task_activity=self.task_activity,
            path_resolver=overrides.pop("path_resolver", LexicalPathResolver()),
            clock=self.clock,
            listeners=listeners,
            **overrides,
        )
        self.runner = ToolRunner(self.broker, self.executor)
        self.service = ApprovalService(
            self.approvals,
            self.sink,
            step_up=StepUp(True),
            clock=self.clock,
            listeners=(self.events.append,),
        )

    def tool_events(self):
        """The audit rows the broker wrote (authz rows are named by capability)."""
        return [e for e in self.sink.events if e.action.startswith("tool.")]


__all__ = [
    "ALL_PROJECTS",
    "HANDLE",
    "OTHER_HANDLE",
    "P1",
    "P2",
    "REPO",
    "ROOT",
    "RUN",
    "TASK",
    "U1",
    "U2",
    "AGENT",
    "FakeTaskActivity",
    "Harness",
    "make_call",
    "make_context",
    "make_grant",
    "make_scope",
    "principal",
    "sample_registry",
]
