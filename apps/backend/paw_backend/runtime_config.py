"""The runtime configuration file (#208, Decision 0093, Approved).

One TOML file, named by ``PAW_RUNTIME_CONFIG_FILE`` (``/etc/paw/runtime.toml`` in
production), says which parts of the backend run and with which commands: the
Compute Scheduler (``[compute]``, its ``[[compute.deployments]]`` and their
``commands``), the main local model (``[local_model]``), the SSH key template
(``[ssh]``), the git runner (``[git]``), the sandbox runner (``[sandbox]``) and
the orchestrator's ladders (``[orchestrator.ladders]``). :func:`load_runtime_config`
reads it and returns a :class:`RuntimeWiring`, the arguments ``create_app`` takes
(``compute``, ``local_model``, ``sandbox``, ``git_runner``,
``orchestrator_config``). The parser only fills the existing dataclasses; their
own ``__post_init__`` checks still run.

Decision 0099, 7 (Approved; issue #297) adds the cloud's sections: ``[cloud]``
(``enabled``, ``single_user_only``: the production ``CloudPolicy``,
``agents/cloud_policy.py``), ``[agents.claude]`` / ``[agents.codex]`` (the CLI
runtimes' ``model``, ``session_base`` and ``relay_command``) and
``[orchestrator.assignment]`` (each role's ``allowed`` agents and ``default``).
They are parsed and checked (a CLI label on a ladder needs ``[cloud] enabled =
true`` and its ``[agents.<name>]``; ``codex`` is never a planner's rung;
``allowed`` stays inside the role's ladder; a worker is never assigned a CLI
runtime; the reviewer's ``allowed`` holds both ``claude`` and ``codex``), and
``create_app`` is given them as ``cli`` (a ``CliSetup``: Decision 0099, "PR の分け方"
6, after the CLI smoke) when a ladder names a CLI runtime; the CLI runtimes then
need ``PAW_SECRET_STORE_DIR`` and an ``[agents.<name>].session_base`` that is
``PAW_CLI_SESSION_DIR`` (:func:`check_environment`). ``relay_command`` runs
inside the CLI's container (Decision 0083, 6), so its ``argv[0]`` is not looked
up on this host.

**Whoever can write this file runs commands as the backend's user** (Decision
0093, 2), so the file and everything it names must be root's alone:

* The file: an absolute, canonical path without a symbolic link, opened with
  ``O_NOFOLLOW`` and checked on the opened descriptor (``fstat``): a regular file
  owned by root that neither its group nor others can write (``root:paw 0640``),
  at most 64 KiB of UTF-8 TOML. Every directory from it up to ``/`` is root's
  and not writable by its group or others.
* Every command's ``argv[0]`` (and ``nvidia_smi``, the sandbox ``wrapper`` and,
  for the local wrapper, the backend's own ``sys.executable``): a normalised
  absolute path, resolved here one component at a time; every directory it
  passes through, for every symbolic link on the way (at most 8), and the file
  it ends at are root's and not writable by the group or others (and not a
  sticky directory). A command that is a shell or a generic launcher (``sh``,
  ``bash``, ``env``, ``sudo``, ``python*``, ...) is refused: not a boundary, a
  guard against ``/bin/sh -c '...'``. A script's ``#!`` interpreter passes the
  same checks; ``#!/usr/bin/env`` and an interpreter that is itself a script are
  refused, and a Python script must say ``-I``.
* ``base_url`` and ``metrics_url``: ``http://127.0.0.1:<port>/...`` or
  ``http://[::1]:<port>/...`` only.

The values a Decision fixed (the scheduler's headroom, ``kv_safety``, class
ceilings and timeouts, ``min_host_available_bytes``, ``reasoning_history``, the
orchestrator's parallelism, node timeout and retries) cannot be set here: they
stay the code's defaults (Decision 0093, 4). Sizes are whole MiB
(``weights_mib``; ``kv_bytes_per_token`` alone is in bytes).

Anything wrong refuses the whole file (:class:`RuntimeConfigError`): no part is
enabled without the rest. An error names the section and the key and a fixed
reason, **never a value** (a path, a command, a URL). The file holds no secret:
there is no key for one, and an unknown key is refused.

The host is read through a :class:`Host` (``lstat``, ``readlink``, reading a file
without following a link, whether a file can run, a group's id); tests pass a
fake one. Everything here blocks: it runs once at start-up.
"""

import errno
import grp
import os
import re
import stat
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from paw_backend.agents.cli_wiring import CliAgentSetup, CliSetup
from paw_backend.agents.cloud_policy import CLI_AGENT_LABELS, CloudSettings
from paw_backend.agents.wiring import (
    DEFAULT_AGENT_LABEL,
    DEFAULT_MAIN_DEPLOYMENT,
    DEFAULT_MAIN_MODEL_ID,
    DEFAULT_SERVED_MODEL,
    LocalModelSetup,
)
from paw_backend.compute.config import ComputeConfig, DeploymentSpec
from paw_backend.compute.control import CommandModelControl, DeploymentCommands
from paw_backend.compute.domain import DeploymentState, ModelRole, ResidencyPolicy
from paw_backend.compute.errors import InvalidComputeArgumentError
from paw_backend.compute.limits import DEFAULT_MAX_CONTEXT_TOKENS
from paw_backend.compute.probe import DEFAULT_NVIDIA_SMI, NvidiaSmiProbe
from paw_backend.compute.wiring import ComputeSetup
from paw_backend.config import Settings
from paw_backend.orchestrator.config import OrchestratorConfig, RoleAssignment
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.limits import AGENT_LABEL_PATTERN, MODEL_PATTERN
from paw_backend.repositories.git import GitRunner
from paw_backend.repositories.ssh import (
    SshGitRunner,
    SshGitRunnerPolicy,
    TemplateSshKeyDirectory,
    validate_identity_template,
)
from paw_backend.sandbox.pool import DEFAULT_IDLE_SECONDS
from paw_backend.sandbox.runner import LocalWrapperSandboxRunner, SshSandboxRunner
from paw_backend.sandbox.wiring import SandboxSetup

RUNTIME_CONFIG_VERSION = 1
#: The setting that names the file (also the "where" of a refused file).
FILE_SETTING = "PAW_RUNTIME_CONFIG_FILE"
MAX_CONFIG_BYTES = 64 * 1024
#: At most this many symbolic links while one path is resolved.
MAX_LINK_HOPS = 8
MAX_DEPLOYMENTS = 16
MAX_IDLE_SECONDS = 86_400
_MAX_PATH = 4_096
_MIB = 1024 * 1024
_MAX_MIB = (1 << 50) // _MIB
_MAX_KV_BYTES_PER_TOKEN = 1 << 50
_MAX_SEQUENCES = 4_096
_MAX_TOKENS = 1 << 24
# Linux reads this much of a script to find its interpreter (BINPRM_BUF_SIZE).
_SHEBANG_BYTES = 256
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

#: ``argv[0]`` names that start a shell or run any program they are given.
_LAUNCHERS = frozenset(
    {"sh", "bash", "dash", "zsh", "env", "sudo", "su", "perl", "busybox"}
)
_LOOPBACK_NETLOC = re.compile(r"(?:127\.0\.0\.1|\[::1\]):([0-9]{1,5})")
_AGENT_LABEL = re.compile(AGENT_LABEL_PATTERN)
_MODEL = re.compile(MODEL_PATTERN)
_GROUP_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")

# The keys of each table. A key a Decision fixed is "not settable" (its value is
# the code's default), any other unknown key is "unknown key".
_TOP_KEYS = frozenset(
    {
        "version",
        "compute",
        "local_model",
        "ssh",
        "git",
        "sandbox",
        "orchestrator",
        "cloud",
        "agents",
    }
)
_COMPUTE_KEYS = frozenset(
    {"nvidia_smi", "gpu_index", "restore_margin_mib", "deployments"}
)
_COMPUTE_FIXED = frozenset(
    {
        "headroom_min_bytes",
        "headroom_fraction",
        "kv_safety",
        "class_ceilings",
        "probe_max_age_seconds",
        "restore_margin_bytes",
        "pressure_context_fraction",
        "max_waiters",
        "verify_timeout_seconds",
        "verify_poll_seconds",
        "failed_retry_seconds",
        "vram_warning_interval_seconds",
        "refresh_seconds",
        "kept_vram_warnings",
        "probe_timeout",
    }
)
_DEPLOYMENT_KEYS = frozenset(
    {
        "name",
        "role",
        "weights_mib",
        "kv_pool_mib",
        "runtime_mib",
        "workspace_mib",
        "kv_bytes_per_token",
        "max_sequences",
        "max_context_tokens",
        "residency",
        "cpu_fallback",
        "initial",
        "commands",
    }
)
_DEPLOYMENT_FIXED = frozenset(
    {"weights_bytes", "kv_pool_bytes", "runtime_bytes", "workspace_bytes"}
)
_COMMAND_ACTIONS = ("gpu", "unload", "cpu", "pids")
_COMMANDS_KEYS = frozenset({*_COMMAND_ACTIONS, "metrics_url", "unit"})
_COMMANDS_FIXED = frozenset(
    {"min_host_available_bytes", "timeout", "metrics_timeout", "env"}
)
_LOCAL_MODEL_KEYS = frozenset(
    {"base_url", "deployment", "served_model", "model_id", "agent_label"}
)
_LOCAL_MODEL_FIXED = frozenset({"reasoning_history", "limits"})
_SSH_KEYS = frozenset({"identity_template"})
_GIT_KEYS = frozenset({"runner"})
_SANDBOX_KEYS = frozenset(
    {"runner", "shim_base", "shim_group", "idle_seconds", "wrapper", "wrapper_options"}
)
_ORCHESTRATOR_KEYS = frozenset({"ladders", "assignment"})
_ORCHESTRATOR_FIXED = frozenset(
    {
        "max_parallel_nodes",
        "node_timeout_seconds",
        "poll_seconds",
        "heartbeat_seconds",
        "retry_backoff_seconds",
        "max_attempts_per_rung",
        "max_plan_attempts",
    }
)
_DEFAULT_IDENTITY_TEMPLATE = "/etc/paw/ssh-keys/{user}.key"
_CLOUD_KEYS = frozenset({"enabled", "single_user_only"})
_AGENT_KEYS = frozenset({"model", "session_base", "relay_command"})
# The runtimes' other arguments: the policy, the toolkit's limits and the node
# timeout (Decision 0021, 3 / 0083, 8) are the code's.
_AGENT_FIXED = frozenset({"cloud_policy", "limits", "node_seconds"})
_ASSIGNMENT_KEYS = frozenset({"allowed", "default"})


class RuntimeConfigError(Exception):
    """The file cannot be used. ``where`` is ``<section>.<key>`` (or the
    setting's name for the file itself), ``reason`` a fixed text: neither
    carries a value from the file."""

    def __init__(self, where: str, reason: str) -> None:
        self.where = where
        self.reason = reason
        super().__init__(f"{where}: {reason}")


class Host(Protocol):
    """What the parser reads of the host (a fake in the tests)."""

    def lstat(self, path: str) -> os.stat_result:
        """``os.lstat``; ``OSError`` when it is missing."""
        ...

    def readlink(self, path: str) -> str: ...

    def read_file(self, path: str, limit: int) -> tuple[os.stat_result, bytes]:
        """Open ``path`` without following a link, ``fstat`` the descriptor and
        read at most ``limit + 1`` bytes from it (none when it is not a
        regular file). ``OSError`` (``ELOOP`` for a link) when it cannot."""
        ...

    def can_execute(self, path: str) -> bool: ...

    def group_id(self, name: str) -> int | None:
        """The group's id, ``None`` when there is no such group."""
        ...


class RealHost:
    """The host's own file system and group database."""

    def lstat(self, path: str) -> os.stat_result:
        return os.lstat(path)

    def readlink(self, path: str) -> str:
        return os.readlink(path)

    def read_file(self, path: str, limit: int) -> tuple[os.stat_result, bytes]:
        descriptor = os.open(path, _READ_FLAGS)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                return info, b""
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(remaining, 65_536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return info, b"".join(chunks)
        finally:
            os.close(descriptor)

    def can_execute(self, path: str) -> bool:
        return os.access(path, os.X_OK)

    def group_id(self, name: str) -> int | None:
        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None


@dataclass(frozen=True, slots=True)
class RuntimeWiring:
    """The ``create_app`` arguments the file enables (``None``: not enabled).
    ``parts`` is the summary of what is enabled, without paths or commands.

    ``cloud`` and ``agents`` (Decision 0099, 7) become ``create_app``'s ``cli``
    when a ladder names a CLI runtime (:meth:`cli_setup`). ``assignment`` is
    also in ``orchestrator_config`` (Decision 0099, 4: the plan's ``agent`` is
    checked against it)."""

    compute: ComputeSetup | None = None
    local_model: LocalModelSetup | None = None
    sandbox: SandboxSetup | None = None
    git_runner: GitRunner | None = None
    orchestrator_config: OrchestratorConfig | None = None
    cloud: CloudSettings | None = None
    agents: Mapping[str, CliAgentSetup] = field(default_factory=dict)
    assignment: Mapping[NodeRole, RoleAssignment] | None = None
    parts: tuple[str, ...] = field(default=())

    def create_app_arguments(self) -> dict[str, object]:
        """The keyword arguments for ``create_app`` (only the enabled ones)."""
        names = (
            "compute",
            "local_model",
            "sandbox",
            "git_runner",
            "orchestrator_config",
        )
        arguments: dict[str, object] = {
            name: getattr(self, name)
            for name in names
            if getattr(self, name) is not None
        }
        cli = self.cli_setup()
        if cli is not None:
            arguments["cli"] = cli
        return arguments

    def cli_labels(self) -> frozenset[str]:
        """The CLI runtimes the orchestrator's ladders name."""
        if self.orchestrator_config is None:
            return frozenset()
        return frozenset(
            label
            for ladder in self.orchestrator_config.ladders.values()
            for label in ladder
            if label in CLI_AGENT_LABELS
        )

    def cli_setup(self) -> CliSetup | None:
        """``create_app``'s ``cli`` (Decision 0099): when a ladder names a CLI
        runtime (the parser then made sure ``[cloud]`` is enabled and that
        runtime's ``[agents.<name>]`` is there), and also, with the
        orchestrator, when ``[cloud] enabled = true`` and an ``[agents.<name>]``
        is written that no ladder names (Decision 0100, 4's 2: the
        connection's health probe runs before the ladder names the CLI). It
        holds every ``[agents.<name>]``; the runtimes are built only for the
        labels a ladder names (``build_cli_services``)."""
        labels = self.cli_labels()
        if self.cloud is None:
            return None
        if not labels and not (
            self.cloud.enabled is True
            and self.agents
            and self.orchestrator_config is not None
        ):
            return None
        return CliSetup(
            self.cloud,
            {label: self.agents[label] for label in sorted(self.agents)},
        )

    def summary(self) -> str:
        """One line for the start-up log, e.g. ``compute: deployments=main
        control=yes; local_model: yes; sandbox: ssh; git: ssh; orchestrator:
        ladders=local``."""
        return "; ".join(self.parts) if self.parts else "nothing enabled"


# --- reading the file ---------------------------------------------------------


def _canonical_absolute(value: str) -> bool:
    return (
        value.startswith("/")
        and not value.startswith("//")
        and "\x00" not in value
        and len(value) <= _MAX_PATH
        and os.path.normpath(value) == value
    )


def _trusted_directory(info: Any, root_uid: int) -> str | None:
    """Why a directory is not root's alone (``None``: it is)."""
    if not stat.S_ISDIR(info.st_mode):
        return "is not a directory"
    if info.st_uid != root_uid:
        return "a directory above it is not owned by root"
    if info.st_mode & 0o022:
        return "a directory above it is writable by its group or others"
    if info.st_mode & stat.S_ISVTX:
        return "a directory above it is a sticky directory"
    return None


def read_config_file(path: str, host: Host, *, root_uid: int = 0) -> bytes:
    """The file's bytes after the checks of Decision 0093, 2."""
    where = FILE_SETTING
    if not isinstance(path, str) or not _canonical_absolute(path):
        raise RuntimeConfigError(where, "must be a canonical absolute path")
    if path == "/":
        raise RuntimeConfigError(where, "is not a regular file")
    directory = "/"
    for part in path.split("/")[1:-1]:
        _check_trusted_directory(where, host, directory, root_uid)
        directory = os.path.join(directory, part)
        try:
            info = host.lstat(directory)
        except OSError:
            raise RuntimeConfigError(where, "does not exist") from None
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeConfigError(where, "a directory above it is a symbolic link")
    _check_trusted_directory(where, host, directory, root_uid)
    try:
        info, data = host.read_file(path, MAX_CONFIG_BYTES)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RuntimeConfigError(where, "is a symbolic link") from None
        if error.errno == errno.ENOENT:
            raise RuntimeConfigError(where, "does not exist") from None
        raise RuntimeConfigError(where, "cannot be read") from None
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeConfigError(where, "is not a regular file")
    if info.st_uid != root_uid:
        raise RuntimeConfigError(where, "is not owned by root")
    if info.st_mode & 0o022:
        raise RuntimeConfigError(where, "is writable by its group or others")
    if len(data) > MAX_CONFIG_BYTES:
        raise RuntimeConfigError(where, "is too large (64 KiB at most)")
    return data


def _check_trusted_directory(where: str, host: Host, path: str, root_uid: int) -> None:
    try:
        info = host.lstat(path)
    except OSError:
        raise RuntimeConfigError(where, "does not exist") from None
    problem = _trusted_directory(info, root_uid)
    if problem is not None:
        raise RuntimeConfigError(where, problem)


def load_runtime_config(
    path: str | os.PathLike[str],
    settings: Settings,
    *,
    host: Host | None = None,
    root_uid: int = 0,
    python_executable: str | None = None,
) -> RuntimeWiring:
    """Read and check the file at ``path`` (see the module) and build its
    wiring; :class:`RuntimeConfigError` for anything wrong. ``settings``
    gives the SSH connection (``PAW_REPOSITORY_SSH_*``). ``root_uid`` and
    ``host`` are for the tests; ``python_executable`` is the interpreter
    the local sandbox wrapper runs with (default ``sys.executable``)."""
    host = host if host is not None else RealHost()
    data = read_config_file(os.fspath(path), host, root_uid=root_uid)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeConfigError(FILE_SETTING, "is not UTF-8") from None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        # Its message can quote the file: only a fixed reason.
        raise RuntimeConfigError(FILE_SETTING, "is not valid TOML") from None
    return _Parser(
        settings,
        host,
        root_uid=root_uid,
        python_executable=(
            sys.executable if python_executable is None else python_executable
        ),
    ).parse(document)


def check_environment(wiring: RuntimeWiring, settings: Settings) -> None:
    """What the file needs of the environment's settings, checked by the
    backend's start and by ``runtime-config-check`` alike: the orchestrator
    (its local agent runtime) needs ``PAW_DATABASE_URL`` (``create_app``
    refuses ``local_runtimes`` without a database). Nothing is connected to."""
    if wiring.orchestrator_config is not None and settings.database_url is None:
        raise RuntimeConfigError("orchestrator", "needs PAW_DATABASE_URL")
    if (
        wiring.cloud is not None
        and wiring.cloud.enabled
        and settings.database_url is None
    ):
        # The policy counts the users and reads the task's project.
        raise RuntimeConfigError("cloud", "needs PAW_DATABASE_URL")
    # The CLIs a ladder names, and those whose connection is probed (Decision
    # 0100, 4: every ``[agents.<name>]`` while the cloud is enabled).
    cli = wiring.cli_setup()
    labels = frozenset(()) if cli is None else frozenset(cli.agents)
    if labels and settings.secret_store_dir is None:
        # The CLIs' credentials are the connections' (Decision 0087, 1).
        raise RuntimeConfigError("agents", "needs PAW_SECRET_STORE_DIR")
    for label in sorted(labels):
        agent = wiring.agents.get(label)
        if agent is not None and agent.session_base != str(settings.cli_session_dir):
            # The session units (``paw-cli-session@.service``) and the Codex
            # login collection look for the sessions there.
            raise RuntimeConfigError(
                f"agents.{label}.session_base", "must be PAW_CLI_SESSION_DIR"
            )


# --- the parser -----------------------------------------------------------------


def _table(where: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeConfigError(where, "must be a table")
    return value


def _keys(
    where: str,
    table: Mapping[str, Any],
    allowed: frozenset[str],
    fixed: frozenset[str] = frozenset(),
) -> None:
    for key in table:
        if key in allowed:
            continue
        name = f"{where}.{key}" if where else key
        if key in fixed:
            raise RuntimeConfigError(name, "not settable (a Decision fixed its value)")
        raise RuntimeConfigError(name, "unknown key")


def _int(where: str, value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimeConfigError(
            where, f"must be an integer from {minimum} to {maximum}"
        )
    return value


def _string(where: str, value: object) -> str:
    if type(value) is not str or not value:
        raise RuntimeConfigError(where, "must be a non-empty string")
    return value


def _choice(where: str, value: object, choices: tuple[str, ...]) -> str:
    if type(value) is not str or value not in choices:
        raise RuntimeConfigError(where, "must be one of: " + ", ".join(choices))
    return value


def _absolute(where: str, value: object) -> str:
    if type(value) is not str or not _canonical_absolute(value) or value == "/":
        raise RuntimeConfigError(where, "must be a canonical absolute path")
    return value


def _loopback_url(where: str, value: object, *, path_required: bool = True) -> str:
    reason = "must be http://127.0.0.1:<port>/... or http://[::1]:<port>/... (loopback)"
    if (
        type(value) is not str
        or not value
        or len(value) > 2_048
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
        or "?" in value
        or "#" in value
    ):
        raise RuntimeConfigError(where, reason)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise RuntimeConfigError(where, reason) from None
    match = _LOOPBACK_NETLOC.fullmatch(parts.netloc)
    if (
        parts.scheme != "http"
        or match is None
        or not 1 <= int(match.group(1)) <= 65_535
        or not parts.path.startswith("/")
        or (path_required and parts.path == "/")
    ):
        raise RuntimeConfigError(where, reason)
    return value


def _launcher(name: str) -> bool:
    return name in _LAUNCHERS or name.startswith("python")


@dataclass(frozen=True, slots=True)
class _Resolved:
    path: str  # the regular file it ends at (no link on the way)
    names: tuple[str, ...]  # the base names on the way (each link's, the file's)


class _Parser:
    def __init__(
        self,
        settings: Settings,
        host: Host,
        *,
        root_uid: int,
        python_executable: str,
    ) -> None:
        self._settings = settings
        self._host = host
        self._root = root_uid
        self._python = python_executable
        self._identity_template: object = _DEFAULT_IDENTITY_TEMPLATE
        self._keys_directory: TemplateSshKeyDirectory | None = None
        self._policy: SshGitRunnerPolicy | None = None

    # The checks of a program on the host (Decision 0093, 2).

    def _resolve(self, where: str, path: str, what: str) -> _Resolved:
        """Follow ``path`` one component at a time: every directory it passes
        through (for every link, the link's own directory first) is root's
        alone, and it ends at a regular file root's alone."""
        host = self._host
        pending = path.split("/")[1:]
        current = "/"
        self._directory(where, current, what)
        hops = 0
        # The names the file goes by: each link in the last place, the file.
        names: list[str] = []
        while pending:
            part = pending.pop(0)
            if part in ("", "."):
                continue
            if part == "..":
                current = os.path.dirname(current)
                continue
            candidate = os.path.join(current, part)
            last = not any(p not in ("", ".") for p in pending)
            try:
                info = host.lstat(candidate)
            except OSError:
                raise RuntimeConfigError(where, f"{what} does not exist") from None
            if stat.S_ISLNK(info.st_mode):
                if last:
                    names.append(part)
                hops += 1
                if hops > MAX_LINK_HOPS:
                    raise RuntimeConfigError(
                        where, f"{what} has too many symbolic links (8 at most)"
                    )
                try:
                    target = host.readlink(candidate)
                except OSError:
                    raise RuntimeConfigError(where, f"{what} cannot be read") from None
                if not target or "\x00" in target:
                    raise RuntimeConfigError(where, f"{what} cannot be read")
                if target.startswith("/"):
                    current = "/"
                pending[:0] = target.split("/")
                continue
            if not last:
                # A directory on the way.
                if not stat.S_ISDIR(info.st_mode):
                    raise RuntimeConfigError(where, f"{what} does not exist")
                current = candidate
                self._directory(where, current, what)
                continue
            # The last component: the file itself.
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeConfigError(where, f"{what} is not a regular file")
            if info.st_uid != self._root:
                raise RuntimeConfigError(where, f"{what} is not owned by root")
            if info.st_mode & 0o022:
                raise RuntimeConfigError(
                    where, f"{what} is writable by its group or others"
                )
            names.append(part)
            return _Resolved(candidate, tuple(names))
        raise RuntimeConfigError(where, f"{what} is not a regular file")

    def _directory(self, where: str, path: str, what: str) -> None:
        try:
            info = self._host.lstat(path)
        except OSError:
            raise RuntimeConfigError(where, f"{what} does not exist") from None
        if not stat.S_ISDIR(info.st_mode):
            raise RuntimeConfigError(where, f"{what} does not exist")
        if info.st_uid != self._root:
            raise RuntimeConfigError(
                where, f"a directory of {what} is not owned by root"
            )
        if info.st_mode & 0o022:
            raise RuntimeConfigError(
                where, f"a directory of {what} is writable by its group or others"
            )
        if info.st_mode & stat.S_ISVTX:
            raise RuntimeConfigError(where, f"a directory of {what} is sticky")

    def _executable(self, where: str, path: str, what: str) -> _Resolved:
        resolved = self._resolve(where, path, what)
        info = self._host.lstat(resolved.path)
        if not info.st_mode & 0o111 or not self._host.can_execute(resolved.path):
            raise RuntimeConfigError(where, f"{what} is not executable")
        return resolved

    def _head(self, where: str, path: str, what: str) -> bytes:
        try:
            info, data = self._host.read_file(path, _SHEBANG_BYTES)
        except OSError:
            raise RuntimeConfigError(where, f"{what} cannot be read") from None
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeConfigError(where, f"{what} is not a regular file")
        return data[:_SHEBANG_BYTES]

    def _program(self, where: str, path: str) -> None:
        """``argv[0]`` of a command: see the module."""
        if not _canonical_absolute(path) or path == "/":
            raise RuntimeConfigError(
                where, "argv[0] must be a normalised absolute path"
            )
        resolved = self._executable(where, path, "argv[0]")
        if any(_launcher(name) for name in resolved.names):
            raise RuntimeConfigError(
                where, "argv[0] is a shell or a generic launcher (no shell is used)"
            )
        head = self._head(where, resolved.path, "argv[0]")
        if head.startswith(b"#!"):
            self._interpreter(where, head)

    def _interpreter(self, where: str, head: bytes) -> None:
        what = "the script's interpreter"
        line, newline, _ = head[2:].partition(b"\n")
        if not newline:
            raise RuntimeConfigError(where, f"{what} line has no end")
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeConfigError(where, f"{what} cannot be read") from None
        # As the kernel reads it: the interpreter, then the rest as one argument.
        words = text.strip(" \t").split(None, 1)
        interpreter = words[0] if words else ""
        argument = words[1].strip(" \t") if len(words) > 1 else ""
        if not interpreter or not _canonical_absolute(interpreter):
            raise RuntimeConfigError(
                where, f"{what} must be a normalised absolute path"
            )
        if os.path.basename(interpreter) == "env":
            raise RuntimeConfigError(
                where, f"{what} must not be looked up in PATH (#!/usr/bin/env)"
            )
        resolved = self._executable(where, interpreter, what)
        if "env" in resolved.names:
            raise RuntimeConfigError(
                where, f"{what} must not be looked up in PATH (#!/usr/bin/env)"
            )
        if self._head(where, resolved.path, what).startswith(b"#!"):
            raise RuntimeConfigError(where, f"{what} is itself a script")
        if any(name.startswith("python") for name in resolved.names) and (
            argument != "-I"
        ):
            raise RuntimeConfigError(
                where, "a Python script must start with #!<python> -I"
            )

    # The sections.

    def parse(self, document: dict[str, Any]) -> RuntimeWiring:
        version = document.get("version")
        if type(version) is not int or version != RUNTIME_CONFIG_VERSION:
            raise RuntimeConfigError("version", "must be 1")
        _keys("", document, _TOP_KEYS)
        parts: list[str] = []
        cloud = None
        if "cloud" in document:
            cloud = self._cloud(_table("cloud", document["cloud"]))
            parts.append(
                "cloud: enabled single_user" if cloud.enabled else "cloud: disabled"
            )
        agents: dict[str, CliAgentSetup] = {}
        if "agents" in document:
            agents = self._agents(_table("agents", document["agents"]))

        compute = None
        if "compute" in document:
            compute, deployments, controlled = self._compute(
                _table("compute", document["compute"])
            )
            parts.append(
                f"compute: deployments={','.join(deployments)} "
                f"control={'yes' if controlled else 'no'}"
            )
        local_model = None
        if "local_model" in document:
            local_model = self._local_model(
                _table("local_model", document["local_model"]), compute
            )
            parts.append("local_model: yes")
        identity_template = _DEFAULT_IDENTITY_TEMPLATE
        if "ssh" in document:
            ssh = _table("ssh", document["ssh"])
            _keys("ssh", ssh, _SSH_KEYS)
            identity_template = ssh.get("identity_template", identity_template)
        self._identity_template = identity_template
        if "ssh" in document:
            self._ssh_keys()
        git_kind = "subprocess"
        if "git" in document:
            git = _table("git", document["git"])
            _keys("git", git, _GIT_KEYS)
            git_kind = _choice(
                "git.runner", git.get("runner", "subprocess"), ("subprocess", "ssh")
            )
        sandbox = None
        if "sandbox" in document:
            sandbox, sandbox_kind = self._sandbox(
                _table("sandbox", document["sandbox"])
            )
            if sandbox_kind == "ssh" and git_kind != "ssh":
                # The sandbox's git shim runs git on the host as the account:
                # ``subprocess`` cannot read another account's checkout.
                raise RuntimeConfigError(
                    "git.runner", 'must be "ssh" with [sandbox] runner = "ssh"'
                )
            parts.append(f"sandbox: {sandbox_kind}")
        git_runner = None
        if git_kind == "ssh":
            git_runner = SshGitRunner(self._ssh_keys(), policy=self._ssh_policy())
        if "git" in document:
            parts.append(f"git: {git_kind}")
        orchestrator = None
        assignment = None
        if "orchestrator" in document:
            # ``create_app`` builds the local agent runtime only with
            # ``local_model``, ``sandbox`` and ``orchestrator_config`` together.
            runtimes: set[str] = set()
            if local_model is not None and sandbox is not None:
                runtimes.add(local_model.agent_label)
            orchestrator, assignment = self._orchestrator(
                _table("orchestrator", document["orchestrator"]),
                runtimes,
                cloud,
                agents,
            )
            parts.append(
                "orchestrator: ladders=" + ",".join(sorted(orchestrator.labels))
            )
            if assignment is not None:
                parts.append("assignment: yes")
        return RuntimeWiring(
            compute=compute,
            local_model=local_model,
            sandbox=sandbox,
            git_runner=git_runner,
            orchestrator_config=orchestrator,
            cloud=cloud,
            agents=agents,
            assignment=assignment,
            parts=tuple(parts),
        )

    def _cloud(self, table: dict[str, Any]) -> CloudSettings:
        _keys("cloud", table, _CLOUD_KEYS)
        enabled = table.get("enabled", False)
        if type(enabled) is not bool:
            raise RuntimeConfigError("cloud.enabled", "must be true or false")
        if table.get("single_user_only", True) is not True:
            # Decision 0099's point 5 A: only while one user is active.
            raise RuntimeConfigError(
                "cloud.single_user_only", "must be true (Decision 0099, 5)"
            )
        return CloudSettings(enabled=enabled, single_user_only=True)

    def _agents(self, table: dict[str, Any]) -> dict[str, CliAgentSetup]:
        _keys("agents", table, frozenset(CLI_AGENT_LABELS))
        agents: dict[str, CliAgentSetup] = {}
        for name in CLI_AGENT_LABELS:
            if name not in table:
                continue
            where = f"agents.{name}"
            section = _table(where, table[name])
            _keys(where, section, _AGENT_KEYS, _AGENT_FIXED)
            for key in ("model", "session_base", "relay_command"):
                if key not in section:
                    raise RuntimeConfigError(f"{where}.{key}", "is required")
            model = section["model"]
            if type(model) is not str or _MODEL.fullmatch(model) is None:
                raise RuntimeConfigError(f"{where}.model", "invalid value")
            session_base = _absolute(f"{where}.session_base", section["session_base"])
            relay = section["relay_command"]
            if (
                not isinstance(relay, list)
                or not 1 <= len(relay) <= 64
                or not all(
                    type(part) is str
                    and part
                    and "\x00" not in part
                    and len(part) <= _MAX_PATH
                    for part in relay
                )
            ):
                raise RuntimeConfigError(
                    f"{where}.relay_command",
                    "must be a list of 1 to 64 non-empty strings (no shell)",
                )
            agents[name] = CliAgentSetup(name, model, session_base, tuple(relay))
        return agents

    def _compute(
        self, table: dict[str, Any]
    ) -> tuple[ComputeSetup, tuple[str, ...], bool]:
        _keys("compute", table, _COMPUTE_KEYS, _COMPUTE_FIXED)
        nvidia_smi = _absolute(
            "compute.nvidia_smi", table.get("nvidia_smi", DEFAULT_NVIDIA_SMI)
        )
        self._executable("compute.nvidia_smi", nvidia_smi, "the program")
        gpu_index = _int(
            "compute.gpu_index", table.get("gpu_index", 0), minimum=0, maximum=63
        )
        margin = table.get("restore_margin_mib")
        if margin is not None:
            margin = (
                _int("compute.restore_margin_mib", margin, minimum=0, maximum=_MAX_MIB)
                * _MIB
            )
        raw = table.get("deployments")
        if (
            not isinstance(raw, list)
            or not 1 <= len(raw) <= MAX_DEPLOYMENTS
            or not all(isinstance(entry, dict) for entry in raw)
        ):
            raise RuntimeConfigError(
                "compute.deployments",
                f"must be 1 to {MAX_DEPLOYMENTS} [[compute.deployments]] tables",
            )
        specs: list[DeploymentSpec] = []
        commands: dict[str, DeploymentCommands] = {}
        for index, entry in enumerate(raw):
            where = f"compute.deployments[{index}]"
            spec = self._deployment(where, entry)
            if any(other.name == spec.name for other in specs):
                raise RuntimeConfigError(f"{where}.name", "duplicate deployment name")
            specs.append(spec)
            if "commands" in entry:
                commands[spec.name] = self._commands(
                    f"{where}.commands", _table(f"{where}.commands", entry["commands"])
                )
        config = _build(
            "compute",
            lambda: ComputeConfig(
                deployments=tuple(specs),
                gpu_index=gpu_index,
                restore_margin_bytes=margin,
            ),
        )
        control = (
            _build("compute", lambda: CommandModelControl(commands))
            if commands
            else None
        )
        probe = _build(
            "compute.nvidia_smi", lambda: NvidiaSmiProbe(executable=nvidia_smi)
        )
        setup = _build(
            "compute", lambda: ComputeSetup(config=config, probe=probe, control=control)
        )
        return setup, tuple(spec.name for spec in specs), control is not None

    def _deployment(self, where: str, table: dict[str, Any]) -> DeploymentSpec:
        _keys(where, table, _DEPLOYMENT_KEYS, _DEPLOYMENT_FIXED)
        for key in ("name", "role", "weights_mib"):
            if key not in table:
                raise RuntimeConfigError(f"{where}.{key}", "is required")
        name = table["name"]
        if type(name) is not str:
            raise RuntimeConfigError(f"{where}.name", "must be a string")

        def mib(key: str, minimum: int) -> int:
            return (
                _int(
                    f"{where}.{key}",
                    table.get(key, 0),
                    minimum=minimum,
                    maximum=_MAX_MIB,
                )
                * _MIB
            )

        role = ModelRole(
            _choice(f"{where}.role", table["role"], tuple(r.value for r in ModelRole))
        )
        residency = ResidencyPolicy(
            _choice(
                f"{where}.residency",
                table.get("residency", ResidencyPolicy.IF_ROOM.value),
                tuple(r.value for r in ResidencyPolicy),
            )
        )
        initial = DeploymentState(
            _choice(
                f"{where}.initial",
                table.get("initial", DeploymentState.UNLOADED.value),
                ("gpu", "cpu", "unloaded"),
            )
        )
        cpu_fallback = table.get("cpu_fallback", False)
        if type(cpu_fallback) is not bool:
            raise RuntimeConfigError(f"{where}.cpu_fallback", "must be true or false")
        values = {
            "name": name,
            "role": role,
            "weights_bytes": mib("weights_mib", 1),
            "kv_pool_bytes": mib("kv_pool_mib", 0),
            "runtime_bytes": mib("runtime_mib", 0),
            "workspace_bytes": mib("workspace_mib", 0),
            "kv_bytes_per_token": _int(
                f"{where}.kv_bytes_per_token",
                table.get("kv_bytes_per_token", 0),
                minimum=0,
                maximum=_MAX_KV_BYTES_PER_TOKEN,
            ),
            "max_sequences": _int(
                f"{where}.max_sequences",
                table.get("max_sequences", 1),
                minimum=1,
                maximum=_MAX_SEQUENCES,
            ),
            "max_context_tokens": _int(
                f"{where}.max_context_tokens",
                table.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS),
                minimum=1,
                maximum=_MAX_TOKENS,
            ),
            "residency": residency,
            "cpu_fallback": cpu_fallback,
            "initial": initial,
        }
        try:
            return DeploymentSpec(**values)
        except InvalidComputeArgumentError as error:
            key = {
                "weights_bytes": "weights_mib",
                "kv_pool_bytes": "kv_pool_mib",
                "runtime_bytes": "runtime_mib",
                "workspace_bytes": "workspace_mib",
            }.get(error.parameter, error.parameter)
            raise RuntimeConfigError(f"{where}.{key}", "invalid value") from None

    def _commands(self, where: str, table: dict[str, Any]) -> DeploymentCommands:
        _keys(where, table, _COMMANDS_KEYS, _COMMANDS_FIXED)
        argvs: dict[str, tuple[str, ...]] = {}
        for action in _COMMAND_ACTIONS:
            if action not in table:
                continue
            key = f"{where}.{action}"
            argv = table[action]
            if (
                not isinstance(argv, list)
                or not 1 <= len(argv) <= 64
                or not all(
                    type(part) is str
                    and part
                    and "\x00" not in part
                    and len(part) <= _MAX_PATH
                    for part in argv
                )
            ):
                raise RuntimeConfigError(
                    key, "must be a list of 1 to 64 non-empty strings (no shell)"
                )
            self._program(key, argv[0])
            argvs[action] = tuple(argv)
        metrics_url = table.get("metrics_url")
        if metrics_url is not None:
            _loopback_url(f"{where}.metrics_url", metrics_url)
        unit = table.get("unit")
        try:
            return DeploymentCommands(**argvs, metrics_url=metrics_url, unit=unit)
        except InvalidComputeArgumentError as error:
            raise RuntimeConfigError(
                f"{where}.{error.parameter}", "invalid value"
            ) from None

    def _local_model(
        self, table: dict[str, Any], compute: ComputeSetup | None
    ) -> LocalModelSetup:
        _keys("local_model", table, _LOCAL_MODEL_KEYS, _LOCAL_MODEL_FIXED)
        if compute is None:
            raise RuntimeConfigError("local_model", "needs [compute]")
        if "base_url" not in table:
            raise RuntimeConfigError("local_model.base_url", "is required")
        base_url = _loopback_url("local_model.base_url", table["base_url"])
        deployment = table.get("deployment", DEFAULT_MAIN_DEPLOYMENT)
        if type(deployment) is not str or compute.config.spec(deployment) is None:
            raise RuntimeConfigError(
                "local_model.deployment", "must name a [[compute.deployments]] entry"
            )
        served_model = table.get("served_model", DEFAULT_SERVED_MODEL)
        if (
            type(served_model) is not str
            or not 1 <= len(served_model) <= 128
            or not served_model.isprintable()
            or served_model != served_model.strip()
        ):
            raise RuntimeConfigError("local_model.served_model", "invalid value")
        model_id = table.get("model_id", DEFAULT_MAIN_MODEL_ID)
        if type(model_id) is not str or _MODEL.fullmatch(model_id) is None:
            raise RuntimeConfigError("local_model.model_id", "invalid value")
        label = table.get("agent_label", DEFAULT_AGENT_LABEL)
        if type(label) is not str or _AGENT_LABEL.fullmatch(label) is None:
            raise RuntimeConfigError(
                "local_model.agent_label", "must match [a-z][a-z0-9._-]{0,63}"
            )
        if label in CLI_AGENT_LABELS:
            # The policy and the ladders' checks tell the runtimes apart by it.
            raise RuntimeConfigError(
                "local_model.agent_label", "must not be a cli runtime's label"
            )
        return _build(
            "local_model",
            lambda: LocalModelSetup(
                base_url,
                deployment=deployment,
                served_model=served_model,
                model_id=model_id,
                agent_label=label,
            ),
        )

    def _ssh_keys(self) -> TemplateSshKeyDirectory:
        if self._keys_directory is None:
            template = self._identity_template
            try:
                if type(template) is not str:
                    raise ValueError("identity_template")
                validate_identity_template(template)
            except ValueError:
                raise RuntimeConfigError(
                    "ssh.identity_template",
                    "must be a canonical absolute path with {user} once",
                ) from None
            self._keys_directory = TemplateSshKeyDirectory(template)
        return self._keys_directory

    def _ssh_policy(self) -> SshGitRunnerPolicy:
        if self._policy is None:
            try:
                self._policy = SshGitRunnerPolicy.from_settings(self._settings)
            except (TypeError, ValueError):
                raise RuntimeConfigError(
                    "PAW_REPOSITORY_SSH_*", "invalid value"
                ) from None
        return self._policy

    def _sandbox(self, table: dict[str, Any]) -> tuple[SandboxSetup, str]:
        _keys("sandbox", table, _SANDBOX_KEYS)
        if "runner" not in table:
            raise RuntimeConfigError("sandbox.runner", "is required")
        kind = _choice("sandbox.runner", table["runner"], ("ssh", "local_wrapper"))
        shim_base = table.get("shim_base")
        if shim_base is not None:
            shim_base = _absolute("sandbox.shim_base", shim_base)
        shim_group = table.get("shim_group")
        if shim_group is not None:
            if type(shim_group) is not str or _GROUP_NAME.fullmatch(shim_group) is None:
                raise RuntimeConfigError("sandbox.shim_group", "must be a group name")
            gid = self._host.group_id(shim_group)
            if gid is None:
                raise RuntimeConfigError("sandbox.shim_group", "no such group")
            shim_group = gid
        idle = table.get("idle_seconds", DEFAULT_IDLE_SECONDS)
        if (
            type(idle) not in (int, float)
            or idle != idle
            or not 0 < idle <= MAX_IDLE_SECONDS
        ):
            raise RuntimeConfigError(
                "sandbox.idle_seconds",
                f"must be a number above 0, {MAX_IDLE_SECONDS} at most",
            )
        if kind == "ssh":
            for key in ("wrapper", "wrapper_options"):
                if key in table:
                    raise RuntimeConfigError(
                        f"sandbox.{key}", 'only with runner = "local_wrapper"'
                    )
            runner: object = SshSandboxRunner(
                self._ssh_keys(), policy=self._ssh_policy()
            )
        else:
            if "wrapper" not in table:
                raise RuntimeConfigError("sandbox.wrapper", "is required")
            wrapper = _absolute("sandbox.wrapper", table["wrapper"])
            # Run as ``[python, "-I", wrapper, ...]``: the file is read, not run.
            self._resolve("sandbox.wrapper", wrapper, "the wrapper")
            options = table.get("wrapper_options", [])
            if (
                not isinstance(options, list)
                or not all(
                    type(o) is str and "\x00" not in o and len(o) <= _MAX_PATH
                    for o in options
                )
                or len(options) > 64
            ):
                raise RuntimeConfigError(
                    "sandbox.wrapper_options", "must be a list of strings"
                )
            python = self._python
            if type(python) is not str or not _canonical_absolute(python):
                raise RuntimeConfigError(
                    "sandbox.runner", "the backend's python is not a canonical path"
                )
            self._executable("sandbox.runner", python, "the backend's python")
            runner = _build(
                "sandbox.wrapper",
                lambda: LocalWrapperSandboxRunner(
                    wrapper, options=tuple(options), python=python
                ),
            )
        setup = _build(
            "sandbox",
            lambda: SandboxSetup(
                runner, shim_base=shim_base, shim_group=shim_group, idle_seconds=idle
            ),
        )
        return setup, kind

    def _orchestrator(
        self,
        table: dict[str, Any],
        runtimes: set[str],
        cloud: CloudSettings | None,
        agents: Mapping[str, CliAgentSetup],
    ) -> tuple[OrchestratorConfig, dict[NodeRole, RoleAssignment] | None]:
        _keys("orchestrator", table, _ORCHESTRATOR_KEYS, _ORCHESTRATOR_FIXED)
        if not runtimes:
            raise RuntimeConfigError(
                "orchestrator",
                "no agent runtime is enabled ([compute], [local_model] and "
                "[sandbox] are all needed)",
            )
        if "ladders" not in table:
            raise RuntimeConfigError("orchestrator.ladders", "is required")
        ladders_table = _table("orchestrator.ladders", table["ladders"])
        roles = tuple(role.value for role in NodeRole)
        _keys("orchestrator.ladders", ladders_table, frozenset(roles))
        ladders: dict[NodeRole, tuple[str, ...]] = {}
        for role in NodeRole:
            where = f"orchestrator.ladders.{role.value}"
            if role.value not in ladders_table:
                raise RuntimeConfigError(where, "is required")
            ladder = ladders_table[role.value]
            if (
                not isinstance(ladder, list)
                or not ladder
                or not all(type(label) is str for label in ladder)
            ):
                raise RuntimeConfigError(where, "must be a non-empty list of labels")
            for label in ladder:
                if label in CLI_AGENT_LABELS:
                    self._cli_rung(where, role, label, cloud, agents)
                elif label not in runtimes:
                    raise RuntimeConfigError(
                        where, "names a label that is not an enabled agent runtime"
                    )
            ladders[role] = tuple(ladder)
        assignment = None
        if "assignment" in table:
            assignment = self._assignment(
                _table("orchestrator.assignment", table["assignment"]), ladders
            )
        config = _build(
            "orchestrator.ladders",
            lambda: OrchestratorConfig(ladders, assignment=assignment),
        )
        return config, assignment

    @staticmethod
    def _cli_rung(
        where: str,
        role: NodeRole,
        label: str,
        cloud: CloudSettings | None,
        agents: Mapping[str, CliAgentSetup],
    ) -> None:
        """A CLI runtime on a ladder (Decision 0099, 7)."""
        if role is NodeRole.PLANNER and label != "claude":
            raise RuntimeConfigError(
                where,
                "names a cli label other than claude (Decision 0099, 3)",
            )
        if cloud is None or not cloud.enabled:
            raise RuntimeConfigError(
                where, "names a cli runtime's label without [cloud] enabled = true"
            )
        if label not in agents:
            raise RuntimeConfigError(
                where, f"names a cli runtime's label without its [agents.{label}]"
            )

    def _assignment(
        self, table: dict[str, Any], ladders: Mapping[NodeRole, tuple[str, ...]]
    ) -> dict[NodeRole, RoleAssignment]:
        """``[orchestrator.assignment]`` (Decision 0099, 4 and 7)."""
        roles = tuple(role.value for role in NodeRole)
        _keys("orchestrator.assignment", table, frozenset(roles))
        assignment: dict[NodeRole, RoleAssignment] = {}
        for role in NodeRole:
            where = f"orchestrator.assignment.{role.value}"
            if role.value not in table:
                raise RuntimeConfigError(where, "is required")
            entry = _table(where, table[role.value])
            _keys(where, entry, _ASSIGNMENT_KEYS)
            allowed = entry.get("allowed")
            if (
                not isinstance(allowed, list)
                or not allowed
                or not all(
                    type(label) is str and _AGENT_LABEL.fullmatch(label) is not None
                    for label in allowed
                )
                or len(set(allowed)) != len(allowed)
            ):
                raise RuntimeConfigError(
                    f"{where}.allowed", "must be a non-empty list of distinct labels"
                )
            if any(label not in ladders[role] for label in allowed):
                raise RuntimeConfigError(
                    f"{where}.allowed",
                    f"names a label that is not on orchestrator.ladders.{role.value}",
                )
            if role is NodeRole.WORKER and any(
                label in CLI_AGENT_LABELS for label in allowed
            ):
                # A worker runs on the cloud only by an escalation (Decision
                # 0099, 4: point 4 A).
                raise RuntimeConfigError(
                    f"{where}.allowed", "must not name a cli runtime (Decision 0099, 4)"
                )
            if role is NodeRole.REVIEWER and not {"claude", "codex"} <= set(allowed):
                raise RuntimeConfigError(
                    f"{where}.allowed",
                    "must include both claude and codex (Decision 0099, 5)",
                )
            default = entry.get("default")
            if default is None and role is not NodeRole.REVIEWER:
                raise RuntimeConfigError(f"{where}.default", "is required")
            if default is not None and (
                type(default) is not str or default not in allowed
            ):
                raise RuntimeConfigError(f"{where}.default", "must be one of allowed")
            assignment[role] = RoleAssignment(tuple(allowed), default)
        return assignment


def _build[T](where: str, make: Callable[[], T]) -> T:
    """Run a dataclass's own checks; their error becomes a fixed reason."""
    try:
        return make()
    except InvalidComputeArgumentError as error:
        raise RuntimeConfigError(
            f"{where}.{error.parameter}", "invalid value"
        ) from None
    except (TypeError, ValueError):
        raise RuntimeConfigError(where, "invalid value") from None


__all__ = [
    "FILE_SETTING",
    "MAX_CONFIG_BYTES",
    "MAX_LINK_HOPS",
    "RUNTIME_CONFIG_VERSION",
    "CliAgentSetup",
    "Host",
    "RealHost",
    "RoleAssignment",
    "RuntimeConfigError",
    "RuntimeWiring",
    "check_environment",
    "load_runtime_config",
    "read_config_file",
]
