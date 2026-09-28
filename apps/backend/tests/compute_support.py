"""Fakes of the Compute Resource Scheduler tests (PAW-036).

Nothing here touches a GPU. ``FakeProbe`` answers what a read-only probe would
see; ``FakeControl`` records the load / unload / CPU fallback actions the
scheduler asks for and simulates their effect on the probe (a model placed on the
GPU shows up as a process that holds its memory). No test starts a process,
loads a model or allocates VRAM.
"""

import asyncio
import heapq
from collections.abc import Iterable

from paw_backend.compute import (
    ComputeConfig,
    ComputeScheduler,
    DeploymentSpec,
    DeploymentState,
    GpuDevice,
    GpuProcess,
    GpuSample,
    ModelRole,
    Placement,
    ProbeUnavailableError,
    ResidencyPolicy,
)

GIB = 1024**3
MIB = 1024**2
GPU_UUID = "GPU-00000000-0000-0000-0000-000000000000"
EXTERNAL_PID = 9_999  # a process the workspace does not own (another workload)


class ManualClock:
    """Time that only moves when the test says so (``advance``)."""

    def __init__(self) -> None:
        self._now = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._counter = 0

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self._counter += 1
        heapq.heappush(self._sleepers, (self._now + seconds, self._counter, future))
        await future

    @property
    def sleeping(self) -> int:
        return sum(1 for *_, future in self._sleepers if not future.done())

    async def advance(self, seconds: float) -> None:
        """Move time and wake every sleeper whose time has come."""
        self._now += seconds
        while self._sleepers and self._sleepers[0][0] <= self._now:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)
        await settle()


async def settle(rounds: int = 20) -> None:
    """Let the event loop run the tasks that are ready."""
    for _ in range(rounds):
        await asyncio.sleep(0)


class FakeProbe:
    """A GPU with ``total`` bytes. ``external`` bytes are used by a process the
    workspace does not own; ``resident`` maps the pids of the fake models to the
    bytes they hold."""

    def __init__(self, *, total: int = 96 * GIB, external: int = 0) -> None:
        self.total = total
        self.external = external
        self.resident: dict[int, int] = {}
        self.utilization: int | None = 10
        self.fail = False
        self.calls = 0

    @property
    def used(self) -> int:
        return self.external + sum(self.resident.values())

    async def sample(self) -> GpuSample:
        self.calls += 1
        if self.fail:
            raise ProbeUnavailableError()
        processes = [
            GpuProcess(GPU_UUID, pid, used) for pid, used in self.resident.items()
        ]
        if self.external:
            processes.append(GpuProcess(GPU_UUID, EXTERNAL_PID, self.external))
        return GpuSample(
            devices=(
                GpuDevice(
                    0, GPU_UUID, "Fake GPU", self.total, self.used, self.utilization
                ),
            ),
            processes=tuple(processes),
        )


class FakeControl:
    """Records what the scheduler asks of the model runtimes.

    ``place(name, LOCAL_GPU)`` gives the deployment a pid that holds its
    ``gpu_bytes`` on the fake GPU; ``place(name, LOCAL_CPU)`` and ``unload`` free
    it (unless ``linger`` names it: its memory then stays, as a runtime that did
    not really exit). ``fail`` holds ``(action, name)`` pairs that raise.
    """

    def __init__(self, probe: FakeProbe, specs: Iterable[DeploymentSpec]) -> None:
        self.probe = probe
        self.specs = {spec.name: spec for spec in specs}
        self.actions: list[tuple[str, str]] = []
        self.pids: dict[str, int] = {}
        self.kv: dict[str, float | None] = {}
        self.fail: set[tuple[str, str]] = set()
        self.linger: set[str] = set()
        self._next_pid = 1_000
        self.gate: asyncio.Event | None = None  # when set, actions wait for it

    def start_on_gpu(self, name: str) -> None:
        """A model that is already resident when the scheduler starts."""
        self._next_pid += 1
        self.pids[name] = self._next_pid
        self.probe.resident[self._next_pid] = self.specs[name].gpu_bytes

    def _free(self, name: str) -> None:
        pid = self.pids.pop(name, None)
        if pid is not None and name not in self.linger:
            self.probe.resident.pop(pid, None)

    async def place(self, deployment: str, placement: Placement) -> None:
        self.actions.append((f"place:{placement.value}", deployment))
        if self.gate is not None:
            await self.gate.wait()
        if (f"place:{placement.value}", deployment) in self.fail:
            raise RuntimeError("fake failure")
        self._free(deployment)
        if placement is Placement.LOCAL_GPU:
            self.start_on_gpu(deployment)

    async def unload(self, deployment: str) -> None:
        self.actions.append(("unload", deployment))
        if self.gate is not None:
            await self.gate.wait()
        if ("unload", deployment) in self.fail:
            raise RuntimeError("fake failure")
        self._free(deployment)

    async def processes(self, deployment: str) -> frozenset[int]:
        pid = self.pids.get(deployment)
        return frozenset() if pid is None else frozenset({pid})

    async def kv_usage(self, deployment: str) -> float | None:
        return self.kv.get(deployment)


def main_spec(**overrides) -> DeploymentSpec:
    """66 GiB on the GPU; a KV pool of 131,072 tokens (20 GiB / 160 KiB)."""
    values = dict(
        name="main",
        role=ModelRole.MAIN,
        weights_bytes=40 * GIB,
        kv_pool_bytes=20 * GIB,
        runtime_bytes=4 * GIB,
        workspace_bytes=2 * GIB,
        kv_bytes_per_token=160 * 1024,
        max_sequences=16,
        max_context_tokens=65_536,
        residency=ResidencyPolicy.ALWAYS,
        initial=DeploymentState.GPU,
    )
    values.update(overrides)
    return DeploymentSpec(**values)


def memory_spec(**overrides) -> DeploymentSpec:
    """12 GiB on the GPU; a KV pool of 65,536 tokens."""
    values = dict(
        name="memory",
        role=ModelRole.MEMORY_WORKER,
        weights_bytes=8 * GIB,
        kv_pool_bytes=4 * GIB,
        kv_bytes_per_token=64 * 1024,
        max_sequences=4,
        max_context_tokens=16_384,
        residency=ResidencyPolicy.IF_ROOM,
        initial=DeploymentState.GPU,
    )
    values.update(overrides)
    return DeploymentSpec(**values)


def embedding_spec(**overrides) -> DeploymentSpec:
    """2 GiB on the GPU, no KV pool, may fall back to the CPU."""
    values = dict(
        name="embed",
        role=ModelRole.EMBEDDING,
        weights_bytes=2 * GIB,
        max_sequences=8,
        max_context_tokens=8_192,
        residency=ResidencyPolicy.IF_ROOM,
        cpu_fallback=True,
        initial=DeploymentState.GPU,
    )
    values.update(overrides)
    return DeploymentSpec(**values)


def default_specs() -> tuple[DeploymentSpec, ...]:
    """80 GiB in all: on a 96 GiB GPU with ~4.8 GiB of headroom, 11.2 GiB free."""
    return (main_spec(), memory_spec(), embedding_spec())


def build(
    specs: Iterable[DeploymentSpec] | None = None,
    *,
    external: int = 0,
    total: int = 96 * GIB,
    control: bool = True,
    **config,
) -> tuple[ComputeScheduler, FakeProbe, FakeControl | None, ManualClock]:
    """A scheduler over the fakes. The deployments that start on the GPU are
    resident on the fake GPU already."""
    specs = tuple(default_specs() if specs is None else specs)
    probe = FakeProbe(total=total, external=external)
    fake = FakeControl(probe, specs)
    for spec in specs:
        if spec.initial is DeploymentState.GPU:
            fake.start_on_gpu(spec.name)
    clock = ManualClock()
    scheduler = ComputeScheduler(
        ComputeConfig(deployments=specs, **config),
        probe,
        control=fake if control else None,
        clock=clock,
    )
    return scheduler, probe, (fake if control else None), clock
