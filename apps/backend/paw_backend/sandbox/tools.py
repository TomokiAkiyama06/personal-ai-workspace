"""The file, shell and git tools of the Tool Broker and their executor (0083, 2).

Decision 0083 gives the local model's tool loop the benchmark's tools
(``bash``, ``str_replace``, ``write_file``), each run through the Tool Broker
(every call is decided, audited, charged and fenced: Decision 0006 / 0046 /
0057). The runtime (a later stage) maps them onto these tools:

* ``repo.file.write``: ``write``, ``project.repo.write`` (in scope: ``SCOPED_AUTO``);
* ``repo.file.str_replace``: ``write``, ``project.repo.write`` (``SCOPED_AUTO``);
* ``repo.shell.run``: ``execute``, ``project.repo.write`` (``SCOPED_AUTO``);
* ``repo.shell.read``: ``execute``, ``project.read`` (``SCOPED_AUTO``);
* ``repo.git.read``: ``read``, ``project.read`` (``AUTO``).

Each names what it touches with a required path (``path`` / ``workdir``), so the
broker decides it on that repository's ACL and Working Set role (a ``referenced``
repository is neither written nor executed in: Decision 0030) and the scope's
excluded paths (``.git``, the user's checkout, other worktrees) are refused
before anything runs. ``repo.shell.read`` is the ``bash`` of a node that may
not write (Planner, Researcher, Reviewer, a reading Worker): the same command in
a read-only sandbox. ``repo.git.read`` is what the sandbox's ``git`` shim calls
back (it is never shown to a model as a tool of its own).

:class:`RepoToolExecutor` runs an allowed call in the node's sandbox for the
repository (``plan.py``, ``pool.py``) as the task creator's Linux account. Its
results are plain JSON (the runner redacts them): a command's exit status and
output, or a fixed error code a model can act on (``{"error": ...}``). An
executor failure (the sandbox cannot be started: ``SandboxUnavailableError``, a
``PermissionError``) is raised and reported by the runner as its type only.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType

from paw_backend.authz import Capability
from paw_backend.integration.layout import node_marker
from paw_backend.repositories.accounts import AccountDirectory
from paw_backend.repositories.errors import (
    GitCommandError,
    GitFailure,
    LinuxAccountUnavailableError,
)
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.sandbox.gitargs import (
    REFUSAL_HINTS,
    GitReadRefused,
    GitRefusal,
    parse_git_read,
)
from paw_backend.sandbox.gitread import (
    GitReader,
    RepositoryGit,
    branch_changes,
    commit_paths,
    pathspecs,
)
from paw_backend.sandbox.plan import SandboxPlan, plan_sandbox, repository_of
from paw_backend.sandbox.pool import SandboxKey, SandboxPool
from paw_backend.sandbox.spec import (
    MAX_STDIN_BYTES,
    CommandKind,
    SandboxCommand,
    SandboxExecResult,
    SandboxOutOfMemoryError,
    SandboxSpec,
    SandboxUnavailableError,
    checked_sandbox_path,
)
from paw_backend.tools.calls import TaskContext, ToolCall, ToolInvocation
from paw_backend.tools.capabilities import ToolCapability
from paw_backend.tools.registry import (
    MAX_TEXT_LENGTH,
    ArgumentKind,
    ArgumentSpec,
    ToolSpec,
)
from paw_backend.tools.runner import ExecutionStatus, ToolExecutor, ToolOutcome
from paw_backend.tools.scope import (
    PathResolver,
    RealpathResolver,
    ScopedRepository,
    normalise_path,
    path_within,
)

logger = logging.getLogger(__name__)

REPO_FILE_WRITE = "repo.file.write"
REPO_FILE_STR_REPLACE = "repo.file.str_replace"
REPO_SHELL_RUN = "repo.shell.run"
REPO_SHELL_READ = "repo.shell.read"
REPO_GIT_READ = "repo.git.read"
REPO_GIT_CHANGES = "repo.git.changes"
REPO_GIT_COMMIT = "repo.git.commit"
#: The longest ``git`` argument list the shim sends (JSON).
MAX_GIT_ARGUMENTS_CHARS = 16_384

_TEXT = ArgumentSpec(ArgumentKind.TEXT, max_length=MAX_TEXT_LENGTH)
_PATH = ArgumentSpec(ArgumentKind.PATH)

REPO_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        REPO_FILE_WRITE,
        frozenset({ToolCapability.WRITE}),
        Capability.PROJECT_REPO_WRITE,
        {"path": _PATH, "content": _TEXT},
    ),
    ToolSpec(
        REPO_FILE_STR_REPLACE,
        frozenset({ToolCapability.WRITE}),
        Capability.PROJECT_REPO_WRITE,
        {"path": _PATH, "old_str": _TEXT, "new_str": _TEXT},
    ),
    ToolSpec(
        REPO_SHELL_RUN,
        frozenset({ToolCapability.EXECUTE}),
        Capability.PROJECT_REPO_WRITE,
        {"workdir": _PATH, "command": _TEXT},
    ),
    ToolSpec(
        REPO_SHELL_READ,
        frozenset({ToolCapability.EXECUTE}),
        Capability.PROJECT_READ,
        {"workdir": _PATH, "command": _TEXT},
    ),
    ToolSpec(
        REPO_GIT_READ,
        frozenset({ToolCapability.READ}),
        Capability.PROJECT_READ,
        {
            "workdir": _PATH,
            "arguments": ArgumentSpec(
                ArgumentKind.TEXT, max_length=MAX_GIT_ARGUMENTS_CHARS
            ),
        },
    ),
)
REPO_TOOL_SPECS += (
    # A Worker node's changes since its base (Decision 0083, 10): what its
    # ``submit`` is checked against. The runtime calls it, never a model.
    ToolSpec(
        REPO_GIT_CHANGES,
        frozenset({ToolCapability.READ}),
        Capability.PROJECT_READ,
        {"workdir": _PATH},
    ),
    # Commit the declared, uncommitted paths of the node's own worktree, as the
    # fixed author with the node's ``PAW-Node`` trailer (Decision 0083, 10;
    # Decision 0036, 7: the node commits, never the backend on its own).
    ToolSpec(
        REPO_GIT_COMMIT,
        frozenset({ToolCapability.WRITE}),
        Capability.PROJECT_REPO_WRITE,
        {
            "workdir": _PATH,
            "paths": ArgumentSpec(ArgumentKind.TEXT, max_length=MAX_TEXT_LENGTH),
        },
    ),
)
REPO_TOOL_NAMES = frozenset(spec.name for spec in REPO_TOOL_SPECS)
_WRITING = frozenset({REPO_FILE_WRITE, REPO_FILE_STR_REPLACE, REPO_SHELL_RUN})

#: How the shim's nested call reaches the broker: ``ToolRunner.run`` (bound
#: after the runner exists, :meth:`RepoToolExecutor.bind_git_caller`).
GitCaller = Callable[[ToolCall], Awaitable[ToolOutcome]]

_GIT_FAILURES = MappingProxyType(
    {
        GitFailure.OUTPUT_TOO_LARGE: (
            "output_too_large",
            "the output is too large; narrow it with paths, --stat or -n <count>",
        ),
        GitFailure.UNSAFE_OUTPUT: ("output_not_utf8", "the output is not UTF-8 text"),
        GitFailure.TIMEOUT: ("timeout", "git took too long"),
    }
)


def _refusal(code: GitRefusal) -> dict[str, object]:
    return {"error": code.value, "message": REFUSAL_HINTS[code]}


class RepoToolExecutor:
    """Runs the allowed calls of :data:`REPO_TOOL_SPECS` (a ``ToolExecutor``).

    ``accounts`` maps the task's creator (``TaskContext.delegator_id``) to the
    Linux account the sandbox and git run as; ``git`` reads git on the host
    (``gitread.py``); ``pool`` holds the sandboxes. ``resolver`` resolves
    symbolic links (``RealpathResolver``; the broker resolved them for its own
    check, and this one makes sure the path did not leave its repository)."""

    def __init__(
        self,
        pool: SandboxPool,
        git: GitReader,
        accounts: AccountDirectory,
        *,
        resolver: PathResolver | None = None,
    ) -> None:
        if not isinstance(pool, SandboxPool):
            raise TypeError("pool must be a SandboxPool")
        if not isinstance(git, GitReader):
            raise TypeError("git must be a GitReader")
        if not callable(getattr(accounts, "account_of", None)):
            raise TypeError("accounts must be an AccountDirectory")
        self._pool = pool
        self._git = git
        self._accounts = accounts
        self._resolver: PathResolver = resolver or RealpathResolver()
        self._git_caller: GitCaller | None = None

    def bind_git_caller(self, caller: GitCaller) -> None:
        """How the git shim's calls reach the broker (``ToolRunner.run``)."""
        if not callable(caller):
            raise TypeError("caller must be callable")
        self._git_caller = caller

    async def execute(self, invocation: ToolInvocation) -> object:
        tool = invocation.tool
        arguments = invocation.arguments
        if tool == REPO_GIT_READ:
            return await self._git_read(
                invocation.context, str(arguments["workdir"]), arguments["arguments"]
            )
        if tool in (REPO_GIT_CHANGES, REPO_GIT_COMMIT):
            return await self._branch(
                invocation.context,
                str(arguments["workdir"]),
                None if tool == REPO_GIT_CHANGES else arguments["paths"],
            )
        if tool in (REPO_SHELL_RUN, REPO_SHELL_READ):
            workdir = str(arguments["workdir"])
            place = await self._place(
                invocation.context, workdir, writable=tool in _WRITING
            )
            command = SandboxCommand(
                CommandKind.SHELL, place.container(workdir), str(arguments["command"])
            )
            result = await self._pool.run(
                place.key,
                place.account,
                place.plan.spec,
                command,
                shim=self._shim_handler(invocation.context, place.plan.spec),
            )
            _check_memory(result)
            return {
                "exit_code": result.exit_code,
                "output": result.stdout + result.stderr,
                "truncated": result.truncated,
            }
        if tool in (REPO_FILE_WRITE, REPO_FILE_STR_REPLACE):
            path = str(arguments["path"])
            place = await self._place(invocation.context, path, writable=True)
            if path == place.repository_root:
                return {"error": "not_a_file"}
            if tool == REPO_FILE_WRITE:
                kind = CommandKind.FILE_WRITE
                document: dict[str, object] = {"content": arguments["content"]}
            else:
                kind = CommandKind.FILE_STR_REPLACE
                document = {"old": arguments["old_str"], "new": arguments["new_str"]}
            stdin = json.dumps(document, ensure_ascii=True).encode("ascii")
            if len(stdin) > MAX_STDIN_BYTES:
                return {"error": "too_large"}
            target = place.container(path)
            command = SandboxCommand(
                kind, place.container(place.repository_root), target, stdin=stdin
            )
            result = await self._pool.run(
                place.key, place.account, place.plan.spec, command
            )
            _check_memory(result)
            return _file_result(result)
        raise ValueError("not a repository tool")

    # -- where a call runs -----------------------------------------------------------

    async def _account(self, context: TaskContext) -> LinuxAccount:
        try:
            return await self._accounts.account_of(context.delegator_id)
        except LinuxAccountUnavailableError:
            raise SandboxUnavailableError("account_unavailable") from None

    async def _repository(self, context: TaskContext, path: str) -> ScopedRepository:
        repository = repository_of(context.scope, path)
        if repository is None or repository.root is None:
            raise SandboxUnavailableError("no_repository")
        # The broker resolved symbolic links for its own decision; a link that
        # leads out of the repository it was decided on is not followed here.
        real = await self._resolver.resolve(path)
        real_root = await self._resolver.resolve(repository.root)
        if not (real == real_root or path_within(real, real_root)):
            raise SandboxUnavailableError("outside_repository")
        return repository

    async def _locate(
        self, context: TaskContext, root: str, account: LinuxAccount
    ) -> RepositoryGit:
        try:
            return await self._git.locate(
                root, account=account, checkouts=context.scope.excluded_paths
            )
        except (GitReadRefused, GitCommandError):
            raise SandboxUnavailableError("repository_unavailable") from None

    async def _place(
        self, context: TaskContext, path: str, *, writable: bool
    ) -> "_Place":
        repository = await self._repository(context, path)
        account = await self._account(context)
        assert repository.root is not None
        git = await self._locate(context, repository.root, account)
        if writable and git.review:
            # A reviewer's worktree is read-only (Decision 0099, 5), whatever
            # the grant says.
            raise SandboxUnavailableError("read_only_worktree")
        plan = plan_sandbox(
            context.scope, repository, git, writable=writable, shim=self._pool.shims
        )
        key = SandboxKey(
            context.task_id,
            context.run,
            context.grant.agent_id,
            repository.repo_id,
            writable,
        )
        return _Place(repository.root, account, plan, key)

    # -- the git shim ----------------------------------------------------------------

    def _shim_handler(self, context: TaskContext, spec: SandboxSpec):
        async def answer(cwd: str, argv: list[str]) -> dict[str, object]:
            try:
                host_cwd = spec.to_host(checked_sandbox_path(cwd))
            except ValueError:
                host_cwd = None
            if host_cwd is None:
                return _refusal(GitRefusal.PATH)
            caller = self._git_caller
            if caller is None:
                return _refusal(GitRefusal.UNAVAILABLE)
            words = [_host_word(word, spec) for word in argv]
            outcome = await caller(
                ToolCall(
                    REPO_GIT_READ,
                    {"workdir": host_cwd, "arguments": json.dumps(words)},
                    context,
                )
            )
            if outcome.status is not ExecutionStatus.COMPLETED:
                if outcome.status is ExecutionStatus.NOT_EXECUTED:
                    reason = outcome.decision.reason
                    code = "refused" if reason is None else str(reason.value)
                    return {"error": code, "message": "the tool broker refused it"}
                return {"error": "failed", "message": "git failed"}
            result = outcome.result
            if isinstance(result, Mapping) and isinstance(result.get("exit_code"), int):
                return {"exit": result["exit_code"], "output": result.get("output", "")}
            if isinstance(result, Mapping) and isinstance(result.get("error"), str):
                return {"error": result["error"], "message": result.get("message", "")}
            return {"error": "failed", "message": "git failed"}

        return answer

    async def _branch(
        self, context: TaskContext, workdir: str, paths: object
    ) -> dict[str, object]:
        """``repo.git.changes`` (``paths`` is ``None``) and ``repo.git.commit``
        on the node's own worktree with its base (Decision 0083, 10)."""
        try:
            repository = await self._repository(context, workdir)
            assert repository.root is not None
            if workdir != repository.root:
                raise GitReadRefused(GitRefusal.PATH)
            account = await self._account(context)
            git = await self._git.locate(
                repository.root,
                account=account,
                checkouts=context.scope.excluded_paths,
            )
            if not git.has_history:
                raise GitReadRefused(GitRefusal.HISTORY)
            if git.review:
                # A reviewer's read-only worktree (Decision 0099, 5).
                raise GitReadRefused(GitRefusal.UNAVAILABLE)
            plan = plan_sandbox(
                context.scope, repository, git, writable=True, shim=False
            )
            everything = await pathspecs(
                (),
                workdir=repository.root,
                root=repository.root,
                scope=context.scope,
                resolver=self._resolver,
                hidden=plan.hidden,
            )
            changes = await branch_changes(
                self._git, git, account=account, pathspecs=everything
            )
            if paths is None:
                return {
                    "base": changes.base,
                    "head": changes.head,
                    "changed": list(changes.changed),
                    "uncommitted": list(changes.uncommitted),
                    "nested": list(changes.nested),
                }
            wanted = json.loads(str(paths))
            if (
                not isinstance(wanted, list)
                or not wanted
                or not all(isinstance(p, str) for p in wanted)
                or len(set(wanted)) != len(wanted)
                or not set(wanted) <= set(changes.uncommitted)
                # A nested repository would be committed as a gitlink.
                or set(wanted) & set(changes.nested)
            ):
                raise GitReadRefused(GitRefusal.PATH)
            # Each path passes the broker's scope check (and the hidden paths).
            await pathspecs(
                wanted,
                workdir=repository.root,
                root=repository.root,
                scope=context.scope,
                resolver=self._resolver,
                hidden=plan.hidden,
            )
            assert git.branch is not None
            marker = node_marker(context.task_id, git.branch.rsplit("/", 1)[-1])
            head = await commit_paths(
                self._git, git, sorted(wanted), account=account, marker=marker
            )
        except GitReadRefused as refused:
            return _refusal(refused.code)
        except (ValueError, SandboxUnavailableError):
            return _refusal(GitRefusal.UNAVAILABLE)
        except GitCommandError as error:
            code, message = _GIT_FAILURES.get(
                error.failure, ("git_failed", "git failed")
            )
            return {"error": code, "message": message}
        return {"commit": head}

    async def _git_read(
        self, context: TaskContext, workdir: str, arguments: object
    ) -> dict[str, object]:
        try:
            argv = json.loads(str(arguments))
            if not isinstance(argv, list) or not all(isinstance(w, str) for w in argv):
                raise ValueError("arguments")
            args = parse_git_read(argv)
            repository = await self._repository(context, workdir)
            assert repository.root is not None
            account = await self._account(context)
            git = await self._git.locate(
                repository.root,
                account=account,
                checkouts=context.scope.excluded_paths,
            )
            plan = plan_sandbox(
                context.scope,
                repository,
                git,
                writable=git.pinned is not None,
                shim=False,
            )
            specs = await pathspecs(
                args.paths,
                workdir=workdir,
                root=repository.root,
                scope=context.scope,
                resolver=self._resolver,
                hidden=plan.hidden,
            )
            answer = await self._git.read(git, args, account=account, pathspecs=specs)
        except GitReadRefused as refused:
            return _refusal(refused.code)
        except ValueError:
            return _refusal(GitRefusal.SUBCOMMAND)
        except SandboxUnavailableError:
            return _refusal(GitRefusal.UNAVAILABLE)
        except GitCommandError as error:
            code, message = _GIT_FAILURES.get(
                error.failure, ("git_failed", "git failed")
            )
            return {"error": code, "message": message}
        return {"exit_code": answer.exit_code, "output": answer.output}


class _Place:
    """Where one call runs: the repository root, the account, the sandbox."""

    __slots__ = ("account", "key", "plan", "repository_root")

    def __init__(
        self,
        repository_root: str,
        account: LinuxAccount,
        plan: SandboxPlan,
        key: SandboxKey,
    ) -> None:
        self.repository_root = repository_root
        self.account = account
        self.plan = plan
        self.key = key

    def container(self, host_path: str) -> str:
        found = self.plan.spec.to_container(host_path)
        if found is None:
            raise SandboxUnavailableError("outside_repository")
        return found


def _host_word(word: str, spec: SandboxSpec) -> str:
    """A word of the shim's ``git`` with an absolute sandbox path turned into
    the host path it is (anything else is left as it is: an option or a
    revision never starts with ``/``, and another absolute path is refused as
    out of scope by the read)."""
    if not word.startswith("/"):
        return word
    try:
        path = normalise_path(word)
    except Exception:
        return word
    return spec.to_host(path) or word


def _check_memory(result: SandboxExecResult) -> None:
    """A command whose container counted an OOM kill ends the node's attempt
    (Decision 0092, 5; Decision 0083, 4): raised, so the runner reports only
    its type and the toolkit turns it into ``AgentOutOfMemory``."""
    if result.out_of_memory:
        raise SandboxOutOfMemoryError()


def _file_result(result: SandboxExecResult) -> dict[str, object]:
    """The file helper's answer (one JSON object on stdout), checked."""
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        answer = None
    if isinstance(answer, dict):
        if answer.get("ok") is True and result.exit_code == 0:
            return {"ok": True, **{k: v for k, v in answer.items() if k == "chars"}}
        error = answer.get("error")
        if isinstance(error, str) and result.exit_code != 0:
            kept = {"error": error}
            if isinstance(answer.get("count"), int):
                kept["count"] = answer["count"]
            return kept
    return {"error": "failed"}


class ToolExecutors:
    """One ``ToolExecutor`` that hands each tool to the executor it belongs to
    (the Working Set tools to ``WorkingSetExecutor``, these to
    :class:`RepoToolExecutor`, ...). A tool no executor is given for raises."""

    def __init__(self, executors: Mapping[str, ToolExecutor]) -> None:
        for name, executor in executors.items():
            if not isinstance(name, str) or not callable(
                getattr(executor, "execute", None)
            ):
                raise TypeError("executors maps tool names to executors")
        self._executors = MappingProxyType(dict(executors))

    async def execute(self, invocation: ToolInvocation) -> object:
        executor = self._executors.get(invocation.tool)
        if executor is None:
            raise LookupError("no executor for this tool")
        return await executor.execute(invocation)
