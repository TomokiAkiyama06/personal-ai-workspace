"""Helpers for the sandbox tests of Decision 0083 (not a test module).

A **world** is a test user's home (``worktrees_support.Workspace``) with one
checkout and, for a writing node, its worktree made by the real
``GitWorktreeCoordinator`` (real git, the test process's own Linux user, no SSH,
no container). The checkout holds a file the scope excludes (``secret/``)
whose content must never be shown, and its history holds an old commit that
the node's range does not include.

A **host sandbox** (:class:`HostSandboxRunner`) stands in for the container:
it runs a command on the host in the directory the mount maps to, the file
helper of ``deploy/sandbox`` for the file tools, and a ``git`` on its ``PATH``
that is the real shim program talking to the real shim socket. It isolates
nothing; what is isolated (mounts, masks, no network) is the wrapper's and
Podman's, tested in ``test_ssh_git_wrapper_sandbox.py``.
"""

import asyncio
import os
import shutil
import stat
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from paw_backend.authz import AgentGrant, Capability
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.scope import derive_child_scope
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.sandbox.spec import (
    CommandKind,
    SandboxCommand,
    SandboxExecResult,
    SandboxGoneError,
    SandboxSpec,
)
from paw_backend.tasks.queueing.domain import QueueLease
from paw_backend.tools import TaskContext, TaskScope

from .worktrees_support import RUN, Workspace, commit_file, git

BACKEND = Path(__file__).resolve().parents[1]
FILE_HELPER = BACKEND / "deploy" / "sandbox" / "paw_file_tool.py"
GIT_SHIM = BACKEND / "deploy" / "sandbox" / "paw_git_shim.py"
WRAPPER = BACKEND / "deploy" / "ssh-git-wrapper" / "paw_git_wrapper.py"


class World:
    """A checkout and (``writer=True``) a writing node's worktree of it."""

    def __init__(self, *, writer: bool = True, base: bool = True) -> None:
        self.ws = Workspace()
        self.repo_id = self.ws.add_checkout("repo")
        self.checkout = self.ws.checkout(self.repo_id)
        # The shared history: an old commit, then the secret's last change.
        commit_file(self.checkout, "old.txt", "old\n", "old history")
        self.old_commit = git("rev-parse", "HEAD", cwd=self.checkout)
        commit_file(self.checkout, "secret/key.txt", "s3cret-content\n", "secret")
        self.parent = self.ws.scope(
            excluded_paths=[f"{self.checkout}/secret"],
        )
        self.worktree: str | None = None
        self.branch: str | None = None
        self.base_commit: str | None = None
        if writer:
            prepared = asyncio.run(
                self.ws.coordinator().prepare_node(
                    self.ws.node_request("a", scope=self.parent)
                )
            )
            node = prepared[self.repo_id]
            self.worktree = node.path
            self.branch = node.branch
            self.base_commit = git("rev-parse", "HEAD", cwd=node.path)
            if base:
                git(
                    "update-ref",
                    f"refs/paw/base/{node.branch}",
                    self.base_commit,
                    cwd=self.checkout,
                )
            scope = derive_child_scope(
                self.parent,
                role=NodeRole.WORKER,
                repositories=None,
                worktrees=prepared,
            )
            # The worktree's copy of the excluded directory is excluded too
            # (what a scope that excludes a path of the repository means here).
            self.scope = replace(
                scope,
                excluded_paths=(*scope.excluded_paths, f"{node.path}/secret"),
            )
        else:
            self.scope = derive_child_scope(
                self.parent, role=NodeRole.RESEARCHER, repositories=None
            )

    @property
    def account(self) -> LinuxAccount:
        return self.ws.account

    @property
    def root(self) -> str:
        return self.worktree or self.checkout

    def context(self, scope: TaskScope | None = None) -> TaskContext:
        project = self.ws.project_id
        agent = uuid.uuid4()
        return TaskContext(
            task_id=self.ws.task.id,
            delegator_id=self.ws.user_id,
            grant=AgentGrant(
                agent,
                frozenset({Capability.PROJECT_READ, Capability.PROJECT_REPO_WRITE}),
                frozenset({project}),
            ),
            scope=scope or self.scope,
            primary_project_id=project,
            run=RUN,
            lease=QueueLease(7, "worker", 1),
        )

    def close(self) -> None:
        self.ws.close()


def _shim_program(directory: str) -> str:
    """The shim program, pointed at ``directory`` instead of ``/run/paw-shim``,
    reporting its working directory as the sandbox path it would be."""
    source = GIT_SHIM.read_text()
    source = source.replace(
        'SHIM_DIRECTORY = "/run/paw-shim"', f"SHIM_DIRECTORY = {directory!r}"
    )
    source = source.replace(
        "cwd = os.getcwd()", "cwd = os.environ.get('PAW_TEST_SANDBOX_CWD', os.getcwd())"
    )
    return source


class HostSandboxRunner:
    """A ``SandboxRunner`` that runs on the host (see the module docstring)."""

    def __init__(self, shim_base: str | None = None) -> None:
        self.shim_base = shim_base
        self.started: dict[uuid.UUID, SandboxSpec] = {}
        self.stopped: list[uuid.UUID] = []
        self.commands: list[SandboxCommand] = []
        self.gone: set[uuid.UUID] = set()
        self.bin = tempfile.mkdtemp(prefix="paw-sbx-bin-")

    def close(self) -> None:
        shutil.rmtree(self.bin, ignore_errors=True)

    def _install_shim(self, directory: str) -> None:
        shim = Path(self.bin, "git")
        shim.write_text(f"#!{sys.executable}\n" + _shim_program(directory))
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    async def start(self, account, sandbox_id, spec):
        self.started[sandbox_id] = spec

    async def stop(self, account, sandbox_id):
        self.stopped.append(sandbox_id)
        self.started.pop(sandbox_id, None)

    async def exec(self, account, sandbox_id, command, *, timeout_s):
        if sandbox_id in self.gone or sandbox_id not in self.started:
            raise SandboxGoneError()
        self.commands.append(command)
        spec = self.started[sandbox_id]
        cwd = spec.to_host(command.workdir)
        assert cwd is not None
        environment = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": tempfile.gettempdir(),
        }
        if command.kind is CommandKind.SHELL:
            if spec.shim and self.shim_base is not None:
                directory = os.path.join(self.shim_base, sandbox_id.hex)
                await asyncio.to_thread(self._install_shim, directory)
                environment["PAW_TEST_SANDBOX_CWD"] = command.workdir
            argv = ["bash", "-c", command.argument]
            stdin = b""
        else:
            target = spec.to_host(command.argument)
            assert target is not None
            helper = (
                "write" if command.kind is CommandKind.FILE_WRITE else "str-replace"
            )
            argv = [sys.executable, "-I", str(FILE_HELPER), helper, target]
            stdin = command.stdin
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(stdin), timeout_s)
        return SandboxExecResult(
            process.returncode or 0, out.decode(), err.decode(), False
        )
