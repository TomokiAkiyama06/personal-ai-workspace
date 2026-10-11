"""The Tool Broker's file, shell and git tools and their sandbox (#208, Decision 0083).

Stage 2 of Decision 0083 (section 11): the tools the local model's tool loop
runs through the Tool Broker, and where they run.

* ``tools.py``: the tool specs (``repo.file.write``, ``repo.file.str_replace``,
  ``repo.shell.run``, ``repo.shell.read``, ``repo.git.read``) and
  :class:`RepoToolExecutor`;
* ``plan.py`` / ``spec.py``: which sandbox a call gets (one repository, masks
  over the scope's excluded paths, read-write only for the node's own worktree);
* ``pool.py``: the running sandboxes, one per node and repository, stopped on a
  timeout, a cancellation, the node's end or when idle;
* ``runner.py`` / ``wire.py``: the ``paw-sandbox/v1`` commands to the server's
  forced-command wrapper (``deploy/ssh-git-wrapper``), over SSH as the task
  creator's account (``SshSandboxRunner``) or directly for a single-user
  backend (``LocalWrapperSandboxRunner``);
* ``gitargs.py`` / ``gitread.py`` / ``shim.py``: the sandbox's ``git``, a shim
  that asks the backend, which runs a read-only, pinned git on the host.

Nothing here is wired into the application yet: the runtime that calls these
tools (``LocalAgentRuntime``) and its wiring come in stage 3.
"""

from paw_backend.sandbox.gitargs import (
    GitReadArgs,
    GitReadRefused,
    GitRefusal,
    parse_git_read,
)
from paw_backend.sandbox.gitread import GitReadAnswer, GitReader, RepositoryGit
from paw_backend.sandbox.plan import SandboxPlan, plan_sandbox, repository_of
from paw_backend.sandbox.pool import SandboxKey, SandboxPool
from paw_backend.sandbox.runner import (
    LocalWrapperSandboxRunner,
    SandboxRunner,
    SshSandboxRunner,
)
from paw_backend.sandbox.shim import ShimServer
from paw_backend.sandbox.spec import (
    CommandKind,
    SandboxCommand,
    SandboxExecResult,
    SandboxGoneError,
    SandboxMount,
    SandboxSpec,
    SandboxUnavailableError,
)
from paw_backend.sandbox.tools import (
    REPO_FILE_STR_REPLACE,
    REPO_FILE_WRITE,
    REPO_GIT_READ,
    REPO_SHELL_READ,
    REPO_SHELL_RUN,
    REPO_TOOL_NAMES,
    REPO_TOOL_SPECS,
    RepoToolExecutor,
    ToolExecutors,
)

__all__ = [
    "REPO_FILE_STR_REPLACE",
    "REPO_FILE_WRITE",
    "REPO_GIT_READ",
    "REPO_SHELL_READ",
    "REPO_SHELL_RUN",
    "REPO_TOOL_NAMES",
    "REPO_TOOL_SPECS",
    "CommandKind",
    "GitReadAnswer",
    "GitReadArgs",
    "GitReadRefused",
    "GitReader",
    "GitRefusal",
    "LocalWrapperSandboxRunner",
    "RepoToolExecutor",
    "RepositoryGit",
    "SandboxCommand",
    "SandboxExecResult",
    "SandboxGoneError",
    "SandboxKey",
    "SandboxMount",
    "SandboxPlan",
    "SandboxPool",
    "SandboxRunner",
    "SandboxSpec",
    "SandboxUnavailableError",
    "ShimServer",
    "SshSandboxRunner",
    "ToolExecutors",
    "parse_git_read",
    "plan_sandbox",
    "repository_of",
]
