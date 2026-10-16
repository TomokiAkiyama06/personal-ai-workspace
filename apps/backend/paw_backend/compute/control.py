"""The seam to the model runtimes (PAW-036): load, unload, CPU fallback.

The scheduler decides; a :class:`ModelControl` acts. It is a Protocol because the
runtimes (vLLM / SGLang for the main model and the Memory Worker, an embedding
server) are chosen by the benchmarks, and so that every test uses a fake: **no
test loads or unloads a model**.

:class:`CommandModelControl` is the real adapter: it runs the commands an
administrator configured for each deployment (typically ``systemctl start`` /
``stop`` of a unit that runs the runtime, and a command that prints the pids of
its processes). It only turns the scheduler's decision into that argv; it has no
command of its own, and a deployment or an action that is not configured is
refused. Commands run without a shell (the argv is a tuple of strings), with a
timeout, and what they print is never logged or put in an error. The scheduler
never gives it a pid to act on: it only ever reads pids, to know which of the
processes the probe sees are the workspace's.

The host's memory (issue #182, Decision 0039, 4; the values: Decision 0072,
Approved): before it starts a GPU runtime it reads the host's ``MemAvailable``
and starts nothing below ``min_host_available_bytes`` (40 GiB) or when it cannot
be read (:class:`HostMemoryLowError`, logged as a warning). Its commands run
with ``MAX_JOBS`` / ``FLASHINFER_NVCC_THREADS`` capped (``DEFAULT_JIT_BUILD_ENV``)
for a command that starts the runtime itself; a systemd unit does not inherit
them and sets them itself (``docs/DEPLOYMENT_UPDATE.md``).

How a runtime's systemd unit ended (``unit_state``; Decision 0092, 1, Approved;
issue #251): for a deployment whose ``unit`` is named, ``/usr/bin/systemctl show
-p Result -p ActiveState -p InvocationID -- <unit>`` (a read the systemd D-Bus
policy allows any user, no polkit rule), parsed strictly into a
:class:`UnitState`; ``None`` without a unit. The scheduler reads it only when the
runtime's processes are gone, before it starts the unit again, and when a runtime
asks (``ComputeScheduler.model_server_oom_killed``). Its output is never logged
or put in an error either.

How full a runtime's KV cache is (``kv_usage``; Decision 0076, 3): read from
the deployment's ``metrics_url`` (vLLM's ``/metrics``, ``vllm:kv_cache_usage_perc``;
:mod:`paw_backend.compute.metrics`), ``None`` without one or when it cannot be
read.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

import httpx

from paw_backend.compute.config import check_int, check_name
from paw_backend.compute.domain import Placement
from paw_backend.compute.errors import (
    HostMemoryLowError,
    InvalidComputeArgumentError,
    ModelControlError,
)
from paw_backend.compute.host import read_mem_available_bytes
from paw_backend.compute.limits import (
    DEFAULT_CONTROL_TIMEOUT_SECONDS,
    DEFAULT_JIT_BUILD_ENV,
    DEFAULT_MIN_HOST_AVAILABLE_BYTES,
    DEFAULT_UNIT_SHOW_TIMEOUT_SECONDS,
    MAX_CONTROL_TIMEOUT_SECONDS,
)
from paw_backend.compute.metrics import (
    DEFAULT_METRICS_TIMEOUT_SECONDS,
    check_metrics_url,
    read_kv_usage,
)
from paw_backend.compute.probe import (
    CommandResult,
    CommandRunner,
    SubprocessRunner,
    check_timeout,
)
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger("paw_backend.compute")

_MAX_PIDS = 1_024
_MAX_HOST_BYTES = 1 << 50
_MIB = 1024**2
# The KV usage is read at every refresh (5 s): a reading may not take longer.
_MAX_METRICS_TIMEOUT = 5.0
# A unit's name (Decision 0092, 1): a ``.service`` only, nothing ``systemctl``
# could read as an option or a path.
_UNIT_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9@._-]{0,200}\.service")
_SYSTEMCTL = "/usr/bin/systemctl"
_UNIT_PROPERTIES = ("Result", "ActiveState", "InvocationID")
# ``org.freedesktop.systemd1(5)``: the unit's ``ActiveState`` ...
_ACTIVE_STATES = frozenset(
    {
        "active",
        "reloading",
        "inactive",
        "failed",
        "activating",
        "deactivating",
        "maintenance",
        "refreshing",
    }
)
# ... and the ones of a unit that ended or is ending (its ``Result`` is final).
_ENDED_STATES = frozenset({"deactivating", "failed", "inactive"})
_RESULT_PATTERN = re.compile(r"[a-z][a-z-]{0,63}")
# ``$INVOCATION_ID``: 128 bits in hex; empty for a unit that never ran.
_INVOCATION_PATTERN = re.compile(r"(?:[0-9a-f]{32})?")
OOM_KILL_RESULT = "oom-kill"


@dataclass(frozen=True, slots=True)
class UnitState:
    """What ``systemctl show`` says of a runtime's unit (Decision 0092, 1).

    ``invocation_id`` tells one run of the unit from the next: it stays the same
    while the unit is ``deactivating`` and changes at every start."""

    active_state: str
    result: str
    invocation_id: str

    @property
    def ended(self) -> bool:
        """The unit ended or is ending: its ``result`` is its run's."""
        return self.active_state in _ENDED_STATES

    @property
    def oom_killed(self) -> bool:
        """The OOM killer ended a process of the unit (``Result=oom-kill``)."""
        return self.result == OOM_KILL_RESULT


def parse_unit_state(output: str) -> UnitState:
    """The three ``Key=Value`` lines of ``systemctl show``, in any order;
    anything else raises ``ModelControlError`` (without the output)."""
    values: dict[str, str] = {}
    lines = output.splitlines()
    if len(lines) != len(_UNIT_PROPERTIES):
        raise ModelControlError()
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or key not in _UNIT_PROPERTIES or key in values:
            raise ModelControlError()
        values[key] = value
    state = UnitState(values["ActiveState"], values["Result"], values["InvocationID"])
    if (
        state.active_state not in _ACTIVE_STATES
        or _RESULT_PATTERN.fullmatch(state.result) is None
        or _INVOCATION_PATTERN.fullmatch(state.invocation_id) is None
    ):
        raise ModelControlError()
    return state


def check_unit(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _UNIT_PATTERN.fullmatch(value) is None:
        raise InvalidComputeArgumentError("unit")
    return value


class ModelControl(Protocol):
    async def place(self, deployment: str, placement: Placement) -> None:
        """Serve ``deployment`` from the GPU (``LOCAL_GPU``: load it) or from its
        CPU copy (``LOCAL_CPU``: the GPU copy is stopped). Returns when the
        runtime is ready; raises when it could not."""
        ...

    async def unload(self, deployment: str) -> None:
        """Stop the runtime of ``deployment`` and free its memory."""
        ...

    async def processes(self, deployment: str) -> frozenset[int]:
        """The pids of **every** process of ``deployment`` (empty: not running):
        the whole unit (its ``cgroup.procs``), not only its main pid, since the
        runtimes (vLLM, SGLang) hold the GPU memory in child processes. Read
        only; the scheduler matches them against the probe's processes."""
        ...

    async def kv_usage(self, deployment: str) -> float | None:
        """How full the runtime's KV cache is (0..1), ``None`` when unknown."""
        ...


def check_control(control: object) -> None:
    """``TypeError`` unless ``control`` has the four async methods (and, when it
    has one, an async ``unit_state(deployment) -> UnitState | None``: how the
    deployment's systemd unit ended, Decision 0092; a control without it is
    never asked)."""
    require_async_method(control, "place", 2)
    require_async_method(control, "unload", 1)
    require_async_method(control, "processes", 1)
    require_async_method(control, "kv_usage", 1)
    if getattr(control, "unit_state", None) is not None:
        require_async_method(control, "unit_state", 1)


def _argv(parameter: str, value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str | bytes) or not isinstance(value, tuple | list):
        raise InvalidComputeArgumentError(parameter)
    if not value or len(value) > 64:
        raise InvalidComputeArgumentError(parameter)
    for part in value:
        if not isinstance(part, str) or not part or "\x00" in part or len(part) > 4_096:
            raise InvalidComputeArgumentError(parameter)
    return tuple(value)


@dataclass(frozen=True, slots=True)
class DeploymentCommands:
    """The argv of each action for one deployment (``None``: not available),
    and where its runtime's Prometheus metrics are (``metrics_url``, e.g.
    ``http://127.0.0.1:18080/metrics`` for vLLM; ``None``: its KV usage is not
    known), and the systemd unit that runs it (``unit``, e.g.
    ``paw-llm-main.service``; ``None``: how it ended is not read, Decision 0092,
    1)."""

    gpu: tuple[str, ...] | None = None  # start the GPU runtime
    unload: tuple[str, ...] | None = None  # stop every runtime of the deployment
    cpu: tuple[str, ...] | None = None  # stop the GPU runtime, serve from the CPU
    # Print the pids of every process of the runtime, whitespace separated (for
    # a systemd unit: ``cat .../<unit>/cgroup.procs``, not only its MainPID).
    pids: tuple[str, ...] | None = None
    metrics_url: str | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        for parameter in ("gpu", "unload", "cpu", "pids"):
            object.__setattr__(
                self, parameter, _argv(parameter, getattr(self, parameter))
            )
        check_metrics_url(self.metrics_url)
        check_unit(self.unit)


class CommandModelControl:
    """Runs the configured commands (see the module)."""

    def __init__(
        self,
        commands: Mapping[str, DeploymentCommands],
        *,
        runner: CommandRunner | None = None,
        timeout: float = DEFAULT_CONTROL_TIMEOUT_SECONDS,
        min_host_available_bytes: int = DEFAULT_MIN_HOST_AVAILABLE_BYTES,
        host_memory: Callable[[], int] = read_mem_available_bytes,
        metrics_timeout: float = DEFAULT_METRICS_TIMEOUT_SECONDS,
        metrics_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(commands, Mapping):
            raise InvalidComputeArgumentError("commands")
        for name, entry in commands.items():
            check_name("commands", name)
            if not isinstance(entry, DeploymentCommands):
                raise InvalidComputeArgumentError("commands")
        self._commands = dict(commands)
        self._timeout = check_timeout(timeout, maximum=MAX_CONTROL_TIMEOUT_SECONDS)
        # 0: no minimum (``MemAvailable`` is then not read).
        self._min_host_bytes = check_int(
            "min_host_available_bytes",
            min_host_available_bytes,
            minimum=0,
            maximum=_MAX_HOST_BYTES,
        )
        if not callable(host_memory):
            raise InvalidComputeArgumentError("host_memory")
        self._host_memory = host_memory
        # ``metrics_transport``: tests pass an ``httpx.MockTransport``.
        self._metrics_timeout = check_timeout(
            metrics_timeout, "metrics_timeout", maximum=_MAX_METRICS_TIMEOUT
        )
        self._metrics_transport = metrics_transport
        self._runner = (
            runner
            if runner is not None
            else SubprocessRunner(extra_env=DEFAULT_JIT_BUILD_ENV)
        )

    def _command(self, deployment: str, action: str) -> tuple[str, ...]:
        entry = self._commands.get(deployment)
        argv = None if entry is None else getattr(entry, action)
        if argv is None:
            raise ModelControlError()
        return argv

    async def _run(self, argv: tuple[str, ...], seconds: float | None = None) -> str:
        try:
            result = await self._runner.run(
                argv, timeout_seconds=self._timeout if seconds is None else seconds
            )
        except Exception:
            raise ModelControlError() from None
        if not isinstance(result, CommandResult) or result.returncode != 0:
            raise ModelControlError()
        return result.stdout

    def _check_host_memory(self, deployment: str) -> None:
        """Before a GPU runtime starts: enough ``MemAvailable`` for its JIT
        builds and its own host memory, or nothing is started."""
        if not self._min_host_bytes:
            return
        try:
            available = self._host_memory()
        except Exception as error:
            logger.warning(
                "Deployment %s not started: the host's available memory "
                "could not be read (%s)",
                deployment,
                type(error).__name__,
            )
            raise HostMemoryLowError() from None
        if (
            isinstance(available, bool)
            or not isinstance(available, int)
            or not 0 <= available <= _MAX_HOST_BYTES
        ):
            logger.warning(
                "Deployment %s not started: the host's available memory "
                "could not be read",
                deployment,
            )
            raise HostMemoryLowError()
        if available < self._min_host_bytes:
            logger.warning(
                "Deployment %s not started: the host has %d MiB available, "
                "below the minimum of %d MiB",
                deployment,
                available // _MIB,
                self._min_host_bytes // _MIB,
            )
            raise HostMemoryLowError()

    async def place(self, deployment: str, placement: Placement) -> None:
        if placement is Placement.LOCAL_GPU:
            argv = self._command(deployment, "gpu")
            self._check_host_memory(deployment)
            await self._run(argv)
        elif placement is Placement.LOCAL_CPU:
            await self._run(self._command(deployment, "cpu"))
        else:
            raise ModelControlError()

    async def unload(self, deployment: str) -> None:
        await self._run(self._command(deployment, "unload"))

    async def processes(self, deployment: str) -> frozenset[int]:
        output = await self._run(self._command(deployment, "pids"))
        pids = set()
        for word in output.split():
            if not word.isascii() or not word.isdigit():
                raise ModelControlError()
            pid = int(word)
            if pid:  # systemd prints 0 for a unit that does not run
                pids.add(pid)
        if len(pids) > _MAX_PIDS:
            raise ModelControlError()
        return frozenset(pids)

    async def kv_usage(self, deployment: str) -> float | None:
        entry = self._commands.get(deployment)
        if entry is None or entry.metrics_url is None:
            return None
        return await read_kv_usage(
            entry.metrics_url,
            timeout_seconds=self._metrics_timeout,
            transport=self._metrics_transport,
        )

    async def unit_state(self, deployment: str) -> UnitState | None:
        """How the deployment's unit is or ended (``None``: it names no unit).
        Raises ``ModelControlError`` when it cannot be read."""
        entry = self._commands.get(deployment)
        if entry is None or entry.unit is None:
            return None
        argv = (_SYSTEMCTL, "show")
        for name in _UNIT_PROPERTIES:
            argv += ("-p", name)
        output = await self._run(
            (*argv, "--", entry.unit),
            min(self._timeout, DEFAULT_UNIT_SHOW_TIMEOUT_SECONDS),
        )
        return parse_unit_state(output)
