"""What a tool sandbox is asked for, and what it answers (Decision 0083, 6).

A sandbox is a rootless container that runs as the Linux account of the user who
created the task (Decision 0017 / 0029: the worktree is that account's), with no
network, bounded CPU / memory / PIDs and no credential. It is started by the
server's SSH wrapper (``deploy/ssh-git-wrapper``, protocol ``paw-sandbox/v1``),
which accepts only a fixed image and a fixed shape of arguments and re-checks
every path itself. This module holds the value types both sides agree on; the
backend builds them from the task scope (``plan.py``), never from a model.

* :class:`SandboxSpec`: what is mounted (one repository root, read-write only for
  a writing node's own worktree), what is masked (the scope's excluded and
  protected paths, the checkout's ``.git`` and git directories, other
  repositories nested inside), what is re-mounted read-only (the worktree's
  ``.git`` file) and whether the git shim's socket is mounted.
* :class:`SandboxCommand`: one command run inside a started sandbox (the
  ``bash`` of the model, or the fixed file helper of a file tool).
* :class:`SandboxExecResult`: its exit status and output. The output is the
  command's (a model wrote it): it is returned to the caller, which redacts it
  (``ToolRunner``), and is never logged here.
"""

import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from paw_backend.tools.scope import TargetError, normalise_path

#: Where a repository is mounted: ``/workspace`` when the node has one
#: repository, ``/workspace/<repository id>`` when it has several (Decision
#: 0083, 10: names are unique only inside a project, ids everywhere).
WORKSPACE = "/workspace"
#: Where the git shim's socket directory is mounted (read-only).
SHIM_MOUNT = "/run/paw-shim"
#: A command run in a sandbox may run this long (Decision 0083, 8: as the
#: benchmark, 300 seconds); the wrapper enforces it inside the container
#: (``timeout -k 5``) and the backend stops the container if that fails.
COMMAND_TIMEOUT_SECONDS = 300
#: The most output kept of one stream of one command (the wrapper discards the
#: rest and says so); the runtime shows the model far less (12,000 characters).
MAX_STREAM_BYTES = 4 * 1024 * 1024
#: The most data a file tool sends on stdin (its arguments, JSON).
MAX_STDIN_BYTES = 1024 * 1024
MAX_MOUNTS = 1
MAX_MASKS = 256
MAX_COMMAND_CHARS = 65_536
#: A shell command is one argument of ``bash -lc`` in the container: Linux
#: takes at most 128 KiB per argument, and the wrapper refuses more than this.
MAX_COMMAND_BYTES = 100 * 1024
# What a mount or mask path may not hold: the wrapper hands such paths to
# ``podman --mount``, whose value is read as CSV (fields separated by commas,
# quotes significant). A path inside the sandbox (a working directory, a file)
# is a word of its own and only must not hold a control character.
_UNSAFE_MOUNT_CHARS = re.compile(r'[,"\x00-\x1f\x7f]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SandboxUnavailableError(PermissionError):
    """The sandbox could not be started as asked: a mask could not be placed (a
    path is a link, missing, or of a shape a mount cannot take), the wrapper
    refused, or the container did not start. Decision 0083, 4: a node then
    fails with ``PermissionError`` (``sandbox_unavailable``). ``reason`` is a
    fixed code, never a path or anything the wrapper printed."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"sandbox_unavailable ({reason})")
        self.reason = reason


class SandboxGoneError(Exception):
    """The container is no longer running (it was stopped, reached its lifetime
    or the host restarted): the pool starts a new one and tries once more."""


class CommandKind(StrEnum):
    """What may run in a sandbox. The wrapper maps each to a fixed program."""

    SHELL = "shell"  # bash -lc <command>, under timeout -k 5 300
    FILE_WRITE = "file-write"  # the file helper: write <path> (stdin: content)
    FILE_STR_REPLACE = "file-str-replace"  # the helper: str-replace <path>


def checked_mount_path(value: str) -> str:
    """A canonical absolute path a mount can take, or ``SandboxUnavailableError``
    (``unmountable_path``)."""
    try:
        path = normalise_path(value)
    except TargetError:
        raise SandboxUnavailableError("unmountable_path") from None
    if path != value or path == "/" or _UNSAFE_MOUNT_CHARS.search(path):
        raise SandboxUnavailableError("unmountable_path")
    return path


def checked_sandbox_path(value: object) -> str:
    """A canonical absolute path inside the sandbox (under ``/workspace``), or
    ``ValueError``."""
    try:
        path = normalise_path(value)
    except TargetError:
        raise ValueError("not a sandbox path") from None
    if path != value or _CONTROL_CHARS.search(path):
        raise ValueError("not a sandbox path")
    if path != WORKSPACE and not path.startswith(f"{WORKSPACE}/"):
        raise ValueError("a sandbox path lies under /workspace")
    return path


@dataclass(frozen=True, slots=True)
class SandboxMount:
    """One host directory mounted into the sandbox at ``target``."""

    source: str
    target: str
    writable: bool = False

    def __post_init__(self) -> None:
        checked_mount_path(self.source)
        checked_mount_path(self.target)
        if self.target != WORKSPACE and not self.target.startswith(f"{WORKSPACE}/"):
            raise ValueError("a mount target lies under /workspace")
        if type(self.writable) is not bool:
            raise TypeError("writable must be a bool")


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """The whole shape of one sandbox. Two specs that are equal give the same
    container; the pool restarts a sandbox whose spec changed (a scope that
    changed between two calls)."""

    mounts: tuple[SandboxMount, ...]
    # Host paths inside a mount that the sandbox must not see: an empty
    # read-only tmpfs (a directory) or an empty read-only file is put over each.
    masks: tuple[str, ...] = ()
    # Host paths inside a writable mount that the sandbox may read but not
    # change (the worktree's ``.git`` file: Decision 0083, 2).
    readonly: tuple[str, ...] = ()
    # Whether the git shim's socket directory is mounted (``SHIM_MOUNT``).
    shim: bool = False

    def __post_init__(self) -> None:
        if not self.mounts or len(self.mounts) > MAX_MOUNTS:
            raise ValueError("a sandbox mounts exactly one repository")
        if not all(isinstance(m, SandboxMount) for m in self.mounts):
            raise TypeError("mounts must be SandboxMount objects")
        if len(self.masks) + len(self.readonly) > MAX_MASKS:
            raise SandboxUnavailableError("too_many_masks")
        for path in (*self.masks, *self.readonly):
            checked_mount_path(path)
            if not any(_strictly_within(path, m.source) for m in self.mounts):
                raise ValueError("a mask lies inside a mount")
        for path in self.readonly:
            if not any(
                _strictly_within(path, m.source) and m.writable for m in self.mounts
            ):
                raise ValueError("a read-only path lies inside a writable mount")
        if type(self.shim) is not bool:
            raise TypeError("shim must be a bool")
        object.__setattr__(self, "masks", tuple(sorted(set(self.masks))))
        object.__setattr__(self, "readonly", tuple(sorted(set(self.readonly))))

    def to_container(self, host_path: str) -> str | None:
        """Where ``host_path`` is inside the sandbox (``None``: not mounted)."""
        for mount in self.mounts:
            if host_path == mount.source:
                return mount.target
            if host_path.startswith(mount.source + "/"):
                return mount.target + host_path[len(mount.source) :]
        return None

    def to_host(self, container_path: str) -> str | None:
        """Where the sandbox path ``container_path`` is on the host (``None``:
        not inside a mount). ``container_path`` must be canonical."""
        for mount in self.mounts:
            if container_path == mount.target:
                return mount.source
            if container_path.startswith(mount.target + "/"):
                return mount.source + container_path[len(mount.target) :]
        return None


def _strictly_within(path: str, root: str) -> bool:
    return path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    """One command in a started sandbox. ``workdir`` is a sandbox path (under a
    mount); ``argument`` is the shell command (``SHELL``) or the sandbox path of
    the file (the file kinds); ``stdin`` is what a file helper reads."""

    kind: CommandKind
    workdir: str
    argument: str = field(repr=False)
    stdin: bytes = field(default=b"", repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", CommandKind(self.kind))
        checked_sandbox_path(self.workdir)
        if type(self.argument) is not str or not self.argument:
            raise ValueError("a sandbox command needs its argument")
        try:
            size = len(self.argument.encode("utf-8"))  # no lone surrogate
        except UnicodeEncodeError:
            raise ValueError("the argument of a sandbox command is not valid") from None
        if (
            "\x00" in self.argument
            or len(self.argument) > MAX_COMMAND_CHARS
            or size > MAX_COMMAND_BYTES
        ):
            raise ValueError("the argument of a sandbox command is not valid")
        if self.kind is not CommandKind.SHELL:
            checked_sandbox_path(self.argument)
        if type(self.stdin) is not bytes or len(self.stdin) > MAX_STDIN_BYTES:
            raise ValueError("stdin of a sandbox command is too large")
        if self.kind is CommandKind.SHELL and self.stdin:
            raise ValueError("a shell command reads no stdin")


@dataclass(frozen=True, slots=True)
class SandboxExecResult:
    """How one command ended. ``exit_code`` is the command's (124 after the 300
    second limit, as ``timeout`` reports it); ``truncated`` says that a stream
    was longer than ``MAX_STREAM_BYTES`` and its rest was dropped."""

    exit_code: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    truncated: bool = False


def new_sandbox_id() -> uuid.UUID:
    return uuid.uuid4()
