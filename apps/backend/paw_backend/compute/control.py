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
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

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
    MAX_CONTROL_TIMEOUT_SECONDS,
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
    """``TypeError`` unless ``control`` has the four async methods."""
    require_async_method(control, "place", 2)
    require_async_method(control, "unload", 1)
    require_async_method(control, "processes", 1)
    require_async_method(control, "kv_usage", 1)


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
    """The argv of each action for one deployment (``None``: not available)."""

    gpu: tuple[str, ...] | None = None  # start the GPU runtime
    unload: tuple[str, ...] | None = None  # stop every runtime of the deployment
    cpu: tuple[str, ...] | None = None  # stop the GPU runtime, serve from the CPU
    # Print the pids of every process of the runtime, whitespace separated (for
    # a systemd unit: ``cat .../<unit>/cgroup.procs``, not only its MainPID).
    pids: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for parameter in ("gpu", "unload", "cpu", "pids"):
            object.__setattr__(
                self, parameter, _argv(parameter, getattr(self, parameter))
            )


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

    async def _run(self, argv: tuple[str, ...]) -> str:
        try:
            result = await self._runner.run(argv, timeout_seconds=self._timeout)
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
        # The runtimes' metrics endpoints are chosen with the runtime (PAW-017).
        return None
