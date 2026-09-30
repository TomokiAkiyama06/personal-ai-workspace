"""The read-only GPU probe (PAW-036).

The scheduler reads the GPU through a :class:`GpuProbe`. The real one,
:class:`NvidiaSmiProbe`, runs exactly two commands, both queries that change
nothing on the GPU:

* ``nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu``
* ``nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory``

(both with ``--format=csv,noheader,nounits``). It never changes clocks,
persistence, power limits, the compute mode or MIG, never resets the GPU and never
signals a process; ``tests/test_compute_probe.py`` pins the two commands and
refuses such options anywhere in this package. The only process it may stop is
the ``nvidia-smi`` it started itself, when it outlives its timeout.

The command runner is injected so that the tests never start ``nvidia-smi``.
What a command prints is parsed strictly: an output the accounting cannot use
(a missing total, a used amount above the total, a row that does not parse) is
:class:`ProbeUnavailableError`, and the scheduler then admits no new local GPU
work (fail closed). Nothing the command printed is ever put in an error message
or a log.
"""

import asyncio
import contextlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from paw_backend.compute.errors import (
    InvalidComputeArgumentError,
    ProbeUnavailableError,
)
from paw_backend.compute.limits import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    MAX_COMMAND_TIMEOUT_SECONDS,
)

MIB = 1024**2

# Run by its absolute path, not looked up in the service's PATH (a directory
# another user can write to there could plant an ``nvidia-smi``).
DEFAULT_NVIDIA_SMI = "/usr/bin/nvidia-smi"
QUERY_GPU_ARGV: tuple[str, ...] = (
    DEFAULT_NVIDIA_SMI,
    "--query-gpu=index,uuid,name,memory.total,memory.used,utilization.gpu",
    "--format=csv,noheader,nounits",
)
QUERY_APPS_ARGV: tuple[str, ...] = (
    DEFAULT_NVIDIA_SMI,
    "--query-compute-apps=gpu_uuid,pid,used_memory",
    "--format=csv,noheader,nounits",
)
# What nvidia-smi prints for a value it does not have.
_NOT_AVAILABLE = frozenset({"[N/A]", "N/A", "[Not Supported]", "[Unknown Error]"})
_NO_PROCESSES = "No running processes found"
_MAX_OUTPUT_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class GpuDevice:
    index: int
    uuid: str
    name: str
    total_bytes: int
    used_bytes: int
    utilization_percent: int | None


@dataclass(frozen=True, slots=True)
class GpuProcess:
    """A process that holds GPU memory. ``used_bytes`` is ``None`` when the driver
    does not say (it is then only part of the device's ``used_bytes``)."""

    gpu_uuid: str
    pid: int
    used_bytes: int | None


@dataclass(frozen=True, slots=True)
class GpuSample:
    devices: tuple[GpuDevice, ...]
    processes: tuple[GpuProcess, ...] = ()

    def device(self, index: int) -> GpuDevice | None:
        for device in self.devices:
            if device.index == index:
                return device
        return None

    def processes_on(self, device: GpuDevice) -> tuple[GpuProcess, ...]:
        return tuple(p for p in self.processes if p.gpu_uuid == device.uuid)


class GpuProbe(Protocol):
    async def sample(self) -> GpuSample:
        """A reading of every GPU; :class:`ProbeUnavailableError` when there is
        none. Must not change anything on the GPU."""
        ...


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str


class CommandRunner(Protocol):
    async def run(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> CommandResult:
        """Run ``argv`` (no shell), wait at most ``timeout_seconds``. Raises
        ``TimeoutError`` or ``OSError``."""
        ...


class SubprocessRunner:
    """Runs a command without a shell, with a minimal environment, stdin closed
    and stderr discarded, and stops it (only it) when it outlives the timeout."""

    async def run(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"},
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                # ``read(n)`` returns as soon as some output is there, not all
                # of it: read until the end (``nvidia-smi`` writes a row in more
                # than one piece). Past the limit the command is stopped: a child
                # blocked on a full pipe would never exit.
                stdout = b""
                while len(stdout) <= _MAX_OUTPUT_BYTES:
                    chunk = await process.stdout.read(_MAX_OUTPUT_BYTES + 1)
                    if not chunk:
                        break
                    stdout += chunk
                if len(stdout) > _MAX_OUTPUT_BYTES:
                    process.kill()  # the child this runner started, nothing else
                await process.wait()
        except BaseException:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()  # the child this runner started, nothing else
                with contextlib.suppress(Exception):
                    await process.wait()
            raise
        if len(stdout) > _MAX_OUTPUT_BYTES:
            return CommandResult(-1, "")
        return CommandResult(process.returncode, stdout.decode("utf-8", "replace"))


def check_timeout(value: object, parameter: str = "timeout") -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value <= MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise InvalidComputeArgumentError(parameter)
    return float(value)


class NvidiaSmiProbe:
    """Samples the GPUs with the two read-only ``nvidia-smi`` queries."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        executable: str = DEFAULT_NVIDIA_SMI,
    ) -> None:
        self._timeout = check_timeout(timeout)
        self._runner = runner if runner is not None else SubprocessRunner()
        if (
            not isinstance(executable, str)
            or not os.path.isabs(executable)
            or "\x00" in executable
            or len(executable) > 4_096
        ):
            raise InvalidComputeArgumentError("executable")
        self._gpu_argv = (executable, *QUERY_GPU_ARGV[1:])
        self._apps_argv = (executable, *QUERY_APPS_ARGV[1:])

    async def sample(self) -> GpuSample:
        devices = parse_gpu_rows(await self._query(self._gpu_argv))
        processes = parse_process_rows(await self._query(self._apps_argv))
        known = {device.uuid for device in devices}
        if any(process.gpu_uuid not in known for process in processes):
            raise ProbeUnavailableError()
        return GpuSample(devices=devices, processes=processes)

    async def _query(self, argv: tuple[str, ...]) -> str:
        try:
            result = await self._runner.run(argv, timeout_seconds=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ProbeUnavailableError() from None
        if not isinstance(result, CommandResult) or result.returncode != 0:
            raise ProbeUnavailableError()
        return result.stdout


def _rows(text: str, width: int) -> list[list[str]]:
    if not isinstance(text, str):
        raise ProbeUnavailableError()
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != width:
            raise ProbeUnavailableError()
        rows.append(fields)
    return rows


def _integer(text: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if not text.isascii() or not text.isdigit():
        raise ProbeUnavailableError()
    value = int(text)
    if value < minimum or (maximum is not None and value > maximum):
        raise ProbeUnavailableError()
    return value


def parse_gpu_rows(text: str) -> tuple[GpuDevice, ...]:
    """The devices of a ``--query-gpu`` output (at least one)."""
    devices = []
    for index, uuid, name, total, used, utilization in _rows(text, 6):
        if not uuid or not name:
            raise ProbeUnavailableError()
        total_mib = _integer(total, minimum=1)
        used_mib = _integer(used, maximum=total_mib)
        devices.append(
            GpuDevice(
                index=_integer(index),
                uuid=uuid,
                name=name,
                total_bytes=total_mib * MIB,
                used_bytes=used_mib * MIB,
                utilization_percent=(
                    None
                    if utilization in _NOT_AVAILABLE
                    else _integer(utilization, maximum=100)
                ),
            )
        )
    if not devices:
        raise ProbeUnavailableError()
    if len({d.index for d in devices}) != len(devices) or len(
        {d.uuid for d in devices}
    ) != len(devices):
        raise ProbeUnavailableError()
    return tuple(devices)


def parse_process_rows(text: str) -> tuple[GpuProcess, ...]:
    """The processes of a ``--query-compute-apps`` output (maybe none)."""
    if isinstance(text, str) and text.strip() == _NO_PROCESSES:
        return ()
    processes = []
    for uuid, pid, used in _rows(text, 3):
        if not uuid:
            raise ProbeUnavailableError()
        processes.append(
            GpuProcess(
                gpu_uuid=uuid,
                pid=_integer(pid, minimum=1),
                used_bytes=None if used in _NOT_AVAILABLE else _integer(used) * MIB,
            )
        )
    return tuple(processes)
