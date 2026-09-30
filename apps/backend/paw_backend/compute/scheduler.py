"""The Compute Resource Scheduler (PAW-036).

One scheduler per backend process owns the admission to the local models and the
residency of those models on the GPU (``REQUIREMENTS.md``, "GPU / Compute
Resource Scheduler", FIXED; the open choices are in Decision 0037, Approved).

Admission
    A caller asks for a :class:`ComputeRequest` (a resource class, the model it
    needs, the context it will use) and gets a :class:`ComputeLease` it releases
    when the work ends. A request is admitted when the model is resident, the
    probe's reading is fresh, the request's context fits the model's KV cache
    pool within its class's share, and none of the relief steps holds it back.
    Otherwise it waits in a line ordered by class (Interactive, Coding, Support,
    Background) and, within a class, by arrival; a waiter that does not fit for
    lack of capacity holds back the waiters of the same model behind it, so a
    stream of small requests cannot starve a large one. A request that allows it
    goes to the cloud instead (Local / Cloud hybrid, ``allow_cloud``).
    Priority only orders the start of new work: nothing running is interrupted
    to admit a higher class (``REQUIREMENTS.md``, "PriorityとPreemptionは分離").

VRAM and residency
    ``refresh()`` reads the GPU through the read-only probe, accounts actual and
    reserved VRAM against the safety headroom (``accounting.py``) and takes at
    most **one** action per call: under pressure the next relief step (stop
    background work, unload the Memory Worker, move Embedding / Reranker to the
    CPU, suppress new local admissions, reduce the context, ask a human to change
    the main model); with room again the reverse; with room and no relief in
    force, loading a model its residency policy wants on the GPU. One step per
    reading lets the probe show the effect of an action before the next one.
    Model actions go through the injected :class:`ModelControl`; without one the
    scheduler only observes and admits.

Exclusive
    ``acquire`` of an Exclusive request (Kaggle, a Model Benchmark) stops new
    local GPU admissions, waits for the running local GPU work to end (it does
    not stop it: Full GPU Mode, ``full_gpu.py``, holds the tasks of that work and
    may ask it to stop with ``revoke_local_gpu``), unloads every model (an
    Embedding / Reranker with a CPU copy moves there), confirms from the probe
    that no process of the workspace holds GPU memory and that the requested VRAM
    is free, and only then grants the lease. Any failure puts the scheduler back
    to normal. Releasing the lease lets ``refresh()`` load the models again.

Everything happens on one event loop; the state changes between awaits are
synchronous, so the only lock serialises the model actions (and the Exclusive
transition) with each other.
"""

import asyncio
import contextlib
import logging
import math
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from itertools import count

from paw_backend.compute.accounting import (
    DeploymentUsage,
    VramView,
    account,
    headroom_bytes,
)
from paw_backend.compute.concurrency import (
    KvState,
    kv_refusal,
    parallelism,
    usable_tokens,
)
from paw_backend.compute.config import ComputeConfig, DeploymentSpec, check_name
from paw_backend.compute.control import check_control
from paw_backend.compute.domain import (
    CAPACITY_REFUSALS,
    CLASS_RANK,
    PERMANENT_REFUSALS,
    ROLE_ORDER,
    SHARED_CLASSES,
    SUPPORT_ROLES,
    DeploymentState,
    ExclusiveFailure,
    ModelRole,
    Placement,
    Refusal,
    Relief,
    ResidencyPolicy,
    ResourceClass,
    SchedulerMode,
)
from paw_backend.compute.errors import (
    ComputeUnavailableError,
    ExclusiveUnavailableError,
    InvalidComputeArgumentError,
)
from paw_backend.compute.limits import DEFAULT_REFRESH_SECONDS
from paw_backend.compute.probe import GpuDevice, GpuProcess
from paw_backend.orchestrator.config import Clock, SystemClock
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger("paw_backend.compute")

_MAX_TOKENS = 1 << 24
_MAX_BYTES = 1 << 50
_MAX_WAIT_SECONDS = 86_400.0
# The order in which an Exclusive job empties the GPU (the requirements' Full GPU
# Mode: Memory Worker, then Embedding / Reranker, then the main LLM).
_EXCLUSIVE_ORDER = (
    ModelRole.MEMORY_WORKER,
    ModelRole.EMBEDDING,
    ModelRole.RERANKER,
    ModelRole.MAIN,
)


@dataclass(frozen=True, slots=True)
class ComputeRequest:
    """What a caller needs.

    ``deployment``: the model the work runs on (required, except for Exclusive).
    ``context_tokens``: prompt and answer, what the work reserves in the model's
    KV cache. ``vram_bytes``: what an Exclusive job needs free. ``allow_cloud``:
    the caller may run the work on a cloud agent instead (it has checked the
    task's permission and quota); never for Exclusive. ``task_id``: the Agent
    Task the work belongs to (``None``: not a task's, such as a chat or a Memory
    Worker job), so that Full GPU Mode can hold that task (PAW-037,
    ``full_gpu.py``); never for Exclusive.
    """

    resource_class: ResourceClass
    deployment: str | None = None
    context_tokens: int = 0
    vram_bytes: int = 0
    allow_cloud: bool = False
    task_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.resource_class, ResourceClass):
            raise InvalidComputeArgumentError("resource_class")
        if isinstance(self.context_tokens, bool) or not isinstance(
            self.context_tokens, int
        ):
            raise InvalidComputeArgumentError("context_tokens")
        if not 0 <= self.context_tokens <= _MAX_TOKENS:
            raise InvalidComputeArgumentError("context_tokens")
        if isinstance(self.vram_bytes, bool) or not isinstance(self.vram_bytes, int):
            raise InvalidComputeArgumentError("vram_bytes")
        if not isinstance(self.allow_cloud, bool):
            raise InvalidComputeArgumentError("allow_cloud")
        if self.task_id is not None and not isinstance(self.task_id, uuid.UUID):
            raise InvalidComputeArgumentError("task_id")
        if self.resource_class is ResourceClass.EXCLUSIVE:
            if self.deployment is not None:
                raise InvalidComputeArgumentError("deployment")
            if self.task_id is not None:
                raise InvalidComputeArgumentError("task_id")
            if not 0 < self.vram_bytes <= _MAX_BYTES:
                raise InvalidComputeArgumentError("vram_bytes")
            if self.allow_cloud:
                raise InvalidComputeArgumentError("allow_cloud")
            return
        check_name("deployment", self.deployment)
        if self.vram_bytes != 0:
            raise InvalidComputeArgumentError("vram_bytes")


class ComputeLease:
    """Admitted work. Release it when the work ends (``async with`` does).

    ``revoked`` is set when the scheduler asks the holder to stop (a Background
    job under VRAM pressure, the work of a Memory Worker that is about to be
    unloaded, or local GPU work that Full GPU Mode preempts): the holder should
    wind down and release. Nothing is killed.
    """

    __slots__ = (
        "id",
        "resource_class",
        "deployment",
        "task_id",
        "placement",
        "tokens",
        "vram_bytes",
        "revoked",
        "granted_at",
        "_scheduler",
        "_released",
        "_held",
    )

    def __init__(
        self,
        scheduler: "ComputeScheduler",
        request: ComputeRequest,
        placement: Placement,
        tokens: int,
    ) -> None:
        self.id = uuid.uuid4()
        self.resource_class = request.resource_class
        self.deployment = request.deployment
        self.task_id = request.task_id
        self.placement = placement
        self.tokens = tokens
        self.vram_bytes = request.vram_bytes
        self.revoked = asyncio.Event()
        self.granted_at = scheduler._clock.monotonic()
        self._scheduler = scheduler
        self._released = False
        self._held: asyncio.Future | None = None

    @property
    def released(self) -> bool:
        return self._released

    async def release(self) -> None:
        """Give the capacity back. Idempotent. Waits for :meth:`hold_until`."""
        if self._held is not None and not self._held.done():
            return
        self._scheduler._release(self)

    def hold_until(self, work: asyncio.Future) -> None:
        """Keep the capacity until ``work`` ends, even when ``release`` is called
        before: work that did not stop when it was cancelled still uses the GPU."""
        if work.done():
            return
        self._held = work
        work.add_done_callback(lambda _: self._scheduler._release(self))

    async def __aenter__(self) -> "ComputeLease":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.release()

    def __repr__(self) -> str:
        return (
            f"ComputeLease({self.resource_class.value}, {self.placement.value}, "
            f"tokens={self.tokens})"
        )


@dataclass(frozen=True, slots=True)
class Admission:
    """The answer of ``try_acquire``: a lease, or why there is none now."""

    lease: ComputeLease | None
    refusal: Refusal | None


@dataclass(frozen=True, slots=True)
class DeploymentStatus:
    name: str
    role: ModelRole
    state: DeploymentState
    draining: bool
    leases: int
    reserved_tokens: int
    capacity_tokens: int
    sequences: int
    max_sequences: int
    observed_kv_fraction: float | None


@dataclass(frozen=True, slots=True)
class ComputeStatus:
    """A snapshot for monitoring (PAW-066 will show it). No pid, no command output."""

    mode: SchedulerMode
    relief: Relief
    probe_ok: bool
    sample_age_seconds: float | None
    vram: VramView | None
    utilization_percent: int | None
    deployments: tuple[DeploymentStatus, ...]
    leases: Mapping[ResourceClass, int]
    cloud_leases: int
    waiting: Mapping[ResourceClass, int]
    needs_human: bool
    # How long the Exclusive job has held the GPU (``None``: none holds it). A
    # lease that was never released keeps it for ever: an administrator ends it
    # with ``force_release_exclusive()``.
    exclusive_age_seconds: float | None = None

    def deployment(self, name: str) -> DeploymentStatus | None:
        for deployment in self.deployments:
            if deployment.name == name:
                return deployment
        return None


@dataclass(eq=False, slots=True)
class _Deployment:
    spec: DeploymentSpec
    state: DeploymentState
    leases: set[ComputeLease] = field(default_factory=set)
    reserved_tokens: int = 0
    pids: frozenset[int] | None = None
    observed: float | None = None
    draining: bool = False
    busy: bool = False  # a model action is running
    displaced: bool = False  # moved off the GPU by a relief step
    retry_at: float | None = None

    @property
    def counts_on_gpu(self) -> bool:
        """Whether its footprint is reserved: on the GPU, being moved, or in an
        unknown state after a failed action (fail closed)."""
        return self.busy or self.state in (DeploymentState.GPU, DeploymentState.FAILED)

    @property
    def order(self) -> int:
        return ROLE_ORDER[self.spec.role]


@dataclass(eq=False, slots=True)
class _Waiter:
    request: ComputeRequest
    future: asyncio.Future
    seq: int
    last: Refusal

    @property
    def key(self) -> tuple[int, int]:
        return (CLASS_RANK[self.request.resource_class], self.seq)


@dataclass(frozen=True, slots=True)
class _Action:
    deployment: _Deployment
    operation: str  # "gpu", "cpu" or "unload"
    then: Callable[[], None] | None = None


def check_seconds(parameter: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidComputeArgumentError(parameter)
    if value != value or not 0 <= value <= _MAX_WAIT_SECONDS:
        raise InvalidComputeArgumentError(parameter)
    return float(value)


class ComputeScheduler:
    def __init__(
        self,
        config: ComputeConfig,
        probe: object,
        *,
        control: object | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(config, ComputeConfig):
            raise TypeError("config must be a ComputeConfig")
        require_async_method(probe, "sample", 0)
        if control is not None:
            check_control(control)
        clock = clock or SystemClock()
        if not callable(getattr(clock, "monotonic", None)):
            raise TypeError("clock must have monotonic()")
        require_async_method(clock, "sleep", 1)
        self._config = config
        self._probe = probe
        self._control = control
        self._clock = clock
        self._deployments: dict[str, _Deployment] = {
            spec.name: _Deployment(spec, spec.initial) for spec in config.deployments
        }
        self._ordered = sorted(self._deployments.values(), key=lambda d: d.order)
        self._device: GpuDevice | None = None
        self._processes: tuple[GpuProcess, ...] = ()
        self._sampled_at: float | None = None
        self._probe_failing = False
        self._relief = Relief.NONE
        self._mode = SchedulerMode.NORMAL
        self._exclusive: ComputeLease | None = None
        # What was external when the Exclusive lease was granted (see account).
        self._exclusive_baseline = 0
        self._drained: asyncio.Event | None = None
        self._cloud: set[ComputeLease] = set()
        self._waiters: list[_Waiter] = []
        # Tasks whose local GPU request was refused (not queued) while an
        # Exclusive job drained or held the GPU: Full GPU Mode holds them too.
        self._refused_tasks: set[uuid.UUID] = set()
        self._sequence = count()
        self._control_lock = asyncio.Lock()

    # -- public -------------------------------------------------------------

    @property
    def config(self) -> ComputeConfig:
        return self._config

    async def refresh(self) -> ComputeStatus:
        """Read the GPU, take at most one residency action, admit waiters.

        A model action can take minutes (a load); the probe keeps being read
        while it runs, so the models already on the GPU keep admitting work."""
        async with self._control_lock:
            await self._sample()
            if self._mode is SchedulerMode.NORMAL and self._fresh():
                action = self._decide()
                if action is not None:
                    await self._perform_sampling(action)
            self._pump()
        return self.status()

    def force_release_exclusive(self) -> bool:
        """End the Exclusive job's lease although its holder has not released it
        (it crashed, or lost the lease): an administrative action, for PAW-037's
        API to expose to Owner / Admin only. The holder's lease is ``revoked``
        and released; the next ``refresh()`` loads the models again (only into
        memory the probe sees free: a job that still runs keeps its memory as
        external). ``False`` when no Exclusive job holds the GPU."""
        lease = self._exclusive
        if lease is None:
            return False
        logger.warning(
            "Exclusive GPU lease released by force after %.0f s",
            self._clock.monotonic() - lease.granted_at,
        )
        lease.revoked.set()
        self._release(lease)
        return True

    def gpu_task_ids(self) -> frozenset[uuid.UUID]:
        """The Agent Tasks whose work holds a local GPU lease or waits for one
        (their requests named a ``task_id``), and, while an Exclusive job drains
        or holds the GPU, those whose local GPU request it refused without
        queuing it (no wait, a full line). Full GPU Mode holds these tasks
        (PAW-037); a lease on a model's CPU copy and a cloud lease are not
        counted: that work does not need the GPU."""
        if self._mode is SchedulerMode.NORMAL:
            self._refused_tasks.clear()  # of an Exclusive request that ended
        tasks = set(self._refused_tasks)
        tasks |= {
            lease.task_id
            for entry in self._deployments.values()
            for lease in entry.leases
            if lease.placement is Placement.LOCAL_GPU and lease.task_id is not None
        }
        for waiter in self._waiters:
            task_id = waiter.request.task_id
            if task_id is None or waiter.future.done():
                continue
            entry = self._deployments[waiter.request.deployment]
            if entry.state is not DeploymentState.CPU:
                tasks.add(task_id)
        return frozenset(tasks)

    def _note_refused(
        self, request: ComputeRequest, placement: Placement, refusal: Refusal
    ) -> None:
        """Remember the task of a local GPU request refused while an Exclusive
        job drains or holds the GPU (see ``gpu_task_ids``). A refusal that
        waiting cannot change (a context longer than the model takes) is not
        the Exclusive job's doing: its task is not held for it."""
        if (
            request.task_id is not None
            and placement is Placement.LOCAL_GPU
            and refusal not in PERMANENT_REFUSALS
            and self._mode is not SchedulerMode.NORMAL
        ):
            self._refused_tasks.add(request.task_id)

    def revoke_local_gpu(self) -> int:
        """Ask every holder of a local GPU lease to stop (``revoked``): Full GPU
        Mode's preemption of the work that did not drain in time (PAW-037,
        Decision 0055). Cooperative like the relief steps: nothing is killed, the
        holder stops its call and releases; one that ignores it keeps its lease
        and the Exclusive request goes on waiting for it. The number of leases
        asked."""
        asked = 0
        for entry in self._deployments.values():
            for lease in entry.leases:
                if lease.placement is Placement.LOCAL_GPU and not lease.released:
                    lease.revoked.set()
                    asked += 1
        if asked:
            logger.warning("Local GPU work asked to stop for an Exclusive job")
        return asked

    async def serve(
        self, stop: asyncio.Event, *, interval: float = DEFAULT_REFRESH_SECONDS
    ) -> None:
        """``refresh()`` every ``interval`` seconds until ``stop`` is set."""
        if not isinstance(stop, asyncio.Event):
            raise InvalidComputeArgumentError("stop")
        interval = check_seconds("interval", interval)
        if interval == 0:
            raise InvalidComputeArgumentError("interval")
        while not stop.is_set():
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # one bad reading must not end the loop
                logger.error("Compute refresh failed (%s)", type(error).__name__)
            waiter = asyncio.ensure_future(stop.wait())
            try:
                await self._wait_for(waiter, interval)
            finally:
                waiter.cancel()

    async def try_acquire(self, request: ComputeRequest) -> Admission:
        """A lease now, or the reason there is none; never waits, never the cloud."""
        self._check_request(request)
        if request.resource_class is ResourceClass.EXCLUSIVE:
            raise InvalidComputeArgumentError("request")
        refusal, placement = self._judge(request, queue=True)
        if refusal is not None:
            self._note_refused(request, placement, refusal)
            return Admission(None, refusal)
        return Admission(self._grant(request, placement), None)

    async def acquire(
        self,
        request: ComputeRequest,
        *,
        wait_seconds: float,
        cloud_after_seconds: float = 0.0,
    ) -> ComputeLease:
        """A lease, waiting at most ``wait_seconds`` for local capacity.

        With ``allow_cloud``, work that is not admitted locally within
        ``cloud_after_seconds`` (0: at once) gets a ``CLOUD`` lease instead.
        Raises :class:`ComputeUnavailableError` (the last refusal) otherwise; a
        request that can never be admitted (its context is longer than the model
        takes) is refused at once. An Exclusive request raises
        :class:`ExclusiveUnavailableError`; its ``wait_seconds`` bounds the wait for
        running local work to end.
        """
        self._check_request(request)
        timeout = check_seconds("wait_seconds", wait_seconds)
        cloud_after = check_seconds("cloud_after_seconds", cloud_after_seconds)
        if request.resource_class is ResourceClass.EXCLUSIVE:
            return await self._acquire_exclusive(request, timeout)
        refusal, placement = self._judge(request, queue=True)
        if refusal is None:
            return self._grant(request, placement)
        if refusal in PERMANENT_REFUSALS or (request.allow_cloud and cloud_after == 0):
            if request.allow_cloud:
                return self._grant(request, Placement.CLOUD)
            self._note_refused(request, placement, refusal)
            raise ComputeUnavailableError(refusal)
        if timeout == 0:
            self._note_refused(request, placement, refusal)
            raise ComputeUnavailableError(refusal)
        if len(self._waiters) >= self._config.max_waiters:
            if request.allow_cloud:
                return self._grant(request, Placement.CLOUD)
            self._note_refused(request, placement, Refusal.QUEUE_FULL)
            raise ComputeUnavailableError(Refusal.QUEUE_FULL)
        future = asyncio.get_running_loop().create_future()
        waiter = _Waiter(request, future, next(self._sequence), refusal)
        self._waiters.append(waiter)
        wait = min(timeout, cloud_after) if request.allow_cloud else timeout
        try:
            await self._wait_for(future, wait)
        except BaseException:
            self._drop(waiter)
            if future.done() and not future.cancelled():
                self._release(future.result())
            raise
        self._drop(waiter)
        if future.done():
            return future.result()
        future.cancel()
        if request.allow_cloud:
            return self._grant(request, Placement.CLOUD)
        self._note_refused(request, placement, waiter.last)
        raise ComputeUnavailableError(waiter.last)

    def parallelism(
        self,
        deployment: str,
        context_tokens: int,
        resource_class: ResourceClass = ResourceClass.CODING,
    ) -> int:
        """How many more requests of ``context_tokens`` the model would admit on
        the GPU now (the dynamic concurrency: it falls as contexts grow; 0 while
        the model is not on the GPU, the probe is stale, a relief step or an
        Exclusive job holds the class back)."""
        entry = self._deployment(deployment)
        if not isinstance(resource_class, ResourceClass) or (
            resource_class is ResourceClass.EXCLUSIVE
        ):
            raise InvalidComputeArgumentError("resource_class")
        request = ComputeRequest(
            resource_class, deployment=deployment, context_tokens=context_tokens
        )
        refusal, placement = self._judge(request, queue=False)
        if placement is not Placement.LOCAL_GPU or (
            refusal is not None and refusal not in CAPACITY_REFUSALS
        ):
            return 0  # not on the GPU, or held back by more than capacity
        return parallelism(
            self._kv(entry),
            context_tokens,
            resource_class,
            safety=self._config.kv_safety,
            ceilings=self._config.class_ceilings,
        )

    def status(self) -> ComputeStatus:
        fresh = self._fresh()
        leases = dict.fromkeys(ResourceClass, 0)
        for entry in self._deployments.values():
            for lease in entry.leases:
                leases[lease.resource_class] += 1
        if self._exclusive is not None:
            leases[ResourceClass.EXCLUSIVE] += 1
        waiting = dict.fromkeys(SHARED_CLASSES, 0)
        for waiter in self._waiters:
            waiting[waiter.request.resource_class] += 1
        age = None
        if self._sampled_at is not None:
            age = self._clock.monotonic() - self._sampled_at
        return ComputeStatus(
            mode=self._mode,
            relief=self._relief,
            probe_ok=fresh,
            sample_age_seconds=age,
            vram=self._vram() if fresh else None,
            utilization_percent=(
                self._device.utilization_percent if fresh and self._device else None
            ),
            deployments=tuple(
                DeploymentStatus(
                    name=entry.spec.name,
                    role=entry.spec.role,
                    state=entry.state,
                    draining=entry.draining,
                    leases=len(entry.leases),
                    reserved_tokens=entry.reserved_tokens,
                    capacity_tokens=entry.spec.kv_capacity_tokens,
                    sequences=len(entry.leases),
                    max_sequences=entry.spec.max_sequences,
                    observed_kv_fraction=entry.observed,
                )
                for entry in self._deployments.values()
            ),
            leases=leases,
            cloud_leases=len(self._cloud),
            waiting=waiting,
            needs_human=self._relief is Relief.MAIN_CHANGE_NEEDED,
            exclusive_age_seconds=(
                None
                if self._exclusive is None
                else self._clock.monotonic() - self._exclusive.granted_at
            ),
        )

    # -- the probe ------------------------------------------------------------

    async def _sample(self) -> None:
        try:
            sample = await self._probe.sample()
            device = sample.device(self._config.gpu_index)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._lose_sample(type(error).__name__)
            return
        if device is None:
            self._lose_sample("GpuNotFound")
            return
        sampled_at = self._clock.monotonic()
        # The models are asked about their processes and KV use (commands that
        # can take a while) before anything of this reading is published: the
        # reading, its freshness and what the models said become visible to
        # admission together, never a fresh reading beside the previous one's
        # KV use.
        inspected: dict[_Deployment, tuple[frozenset[int] | None, float | None]] = {}
        for entry in self._ordered:
            if not entry.busy and entry.counts_on_gpu:
                inspected[entry] = await self._inspect(entry)
        if self._probe_failing:
            logger.info("GPU probe is available again")
        self._probe_failing = False
        self._device = device
        self._processes = sample.processes_on(device)
        self._sampled_at = sampled_at
        for entry in self._ordered:
            if entry.busy:
                continue
            if not entry.counts_on_gpu:
                entry.pids = frozenset()
                entry.observed = None
            elif entry in inspected:
                entry.pids, entry.observed = inspected[entry]

    async def _inspect(
        self, entry: _Deployment
    ) -> tuple[frozenset[int] | None, float | None]:
        if self._control is None:
            return None, None
        name = entry.spec.name
        try:
            pids = await self._control.processes(name)
            if not isinstance(pids, frozenset) or not all(
                type(pid) is int and pid > 0 for pid in pids
            ):
                pids = None
        except asyncio.CancelledError:
            raise
        except Exception:
            pids = None  # not known: its reservation is assumed in use
        try:
            observed = await self._control.kv_usage(name)
        except asyncio.CancelledError:
            raise
        except Exception:
            observed = None
        if (
            isinstance(observed, bool)
            or not isinstance(observed, int | float)
            or not 0 <= observed <= 1
        ):
            observed = None
        return pids, (None if observed is None else float(observed))

    def _lose_sample(self, error_class: str) -> None:
        if not self._probe_failing:
            logger.warning(
                "GPU probe is unavailable (%s): no new local GPU work is admitted",
                error_class,
            )
        self._probe_failing = True
        self._sampled_at = None
        self._device = None
        self._processes = ()

    def _fresh(self) -> bool:
        if self._sampled_at is None or self._device is None:
            return False
        age = self._clock.monotonic() - self._sampled_at
        return age <= self._config.probe_max_age_seconds

    def _headroom(self) -> int:
        assert self._device is not None
        return headroom_bytes(
            self._device.total_bytes,
            minimum_bytes=self._config.headroom_min_bytes,
            fraction=self._config.headroom_fraction,
        )

    def _margin(self) -> int:
        if self._config.restore_margin_bytes is not None:
            return self._config.restore_margin_bytes
        return self._headroom()

    def _vram(self) -> VramView:
        assert self._device is not None
        usages = tuple(
            DeploymentUsage(entry.spec.gpu_bytes, entry.pids)
            for entry in self._ordered
            if entry.counts_on_gpu
        )
        exclusive = self._exclusive is not None
        return account(
            self._device,
            self._processes,
            usages,
            headroom=self._headroom(),
            extra_reserved=self._exclusive.vram_bytes if exclusive else 0,
            extra_baseline=self._exclusive_baseline if exclusive else 0,
        )

    # -- admission ------------------------------------------------------------

    def _check_request(self, request: object) -> None:
        if not isinstance(request, ComputeRequest):
            raise InvalidComputeArgumentError("request")
        if request.deployment is not None:
            self._deployment(request.deployment)

    def _deployment(self, name: object) -> _Deployment:
        entry = self._deployments.get(name) if isinstance(name, str) else None
        if entry is None:
            raise InvalidComputeArgumentError("deployment")
        return entry

    def _kv(self, entry: _Deployment) -> KvState:
        return KvState(
            capacity_tokens=entry.spec.kv_capacity_tokens,
            reserved_tokens=entry.reserved_tokens,
            sequences=len(entry.leases),
            max_sequences=entry.spec.max_sequences,
            observed_fraction=entry.observed,
        )

    def _judge(
        self, request: ComputeRequest, *, queue: bool
    ) -> tuple[Refusal | None, Placement]:
        """Whether ``request`` is admitted now, and where."""
        entry = self._deployment(request.deployment)
        spec = entry.spec
        cls = request.resource_class
        tokens = request.context_tokens
        config = self._config
        if tokens > spec.max_context_tokens:
            return Refusal.CONTEXT_TOO_LONG, Placement.LOCAL_GPU
        available = not entry.busy and not entry.draining
        on_cpu = available and entry.state is DeploymentState.CPU
        # The GPU KV share limits GPU placements only: a CPU lease reserves no KV.
        if (
            not on_cpu
            and spec.kv_capacity_tokens
            and tokens
            > usable_tokens(
                self._kv(entry),
                cls,
                safety=config.kv_safety,
                ceilings=config.class_ceilings,
            )
        ):
            return Refusal.CONTEXT_TOO_LONG, Placement.LOCAL_GPU
        if on_cpu:
            # The CPU copy needs neither the probe nor the GPU (it also serves
            # while an Exclusive job holds the GPU).
            if queue and self._queued_ahead(request):
                return Refusal.QUEUED_BEHIND, Placement.LOCAL_CPU
            if len(entry.leases) >= spec.max_sequences:
                return Refusal.SEQUENCES_FULL, Placement.LOCAL_CPU
            return None, Placement.LOCAL_CPU
        if self._mode is not SchedulerMode.NORMAL:
            return Refusal.EXCLUSIVE_MODE, Placement.LOCAL_GPU
        if not available or entry.state is not DeploymentState.GPU:
            return Refusal.NOT_RESIDENT, Placement.LOCAL_GPU
        if queue and self._queued_ahead(request):
            return Refusal.QUEUED_BEHIND, Placement.LOCAL_GPU
        if not self._fresh():
            return Refusal.PROBE_UNAVAILABLE, Placement.LOCAL_GPU
        relief = self._relief
        if cls is ResourceClass.BACKGROUND and relief >= Relief.BACKGROUND_STOPPED:
            return Refusal.BACKGROUND_PAUSED, Placement.LOCAL_GPU
        if (
            relief >= Relief.ADMISSION_SUPPRESSED
            and cls is not ResourceClass.INTERACTIVE
        ):
            return Refusal.ADMISSION_SUPPRESSED, Placement.LOCAL_GPU
        if relief >= Relief.CONTEXT_REDUCED and tokens > math.floor(
            spec.max_context_tokens * config.pressure_context_fraction
        ):
            return Refusal.CONTEXT_REDUCED, Placement.LOCAL_GPU
        refusal = kv_refusal(
            self._kv(entry),
            tokens,
            cls,
            safety=config.kv_safety,
            ceilings=config.class_ceilings,
        )
        return refusal, Placement.LOCAL_GPU

    def _queued_ahead(self, request: ComputeRequest) -> bool:
        rank = CLASS_RANK[request.resource_class]
        return any(
            waiter.request.deployment == request.deployment
            and CLASS_RANK[waiter.request.resource_class] <= rank
            for waiter in self._waiters
        )

    def _grant(self, request: ComputeRequest, placement: Placement) -> ComputeLease:
        if placement is Placement.CLOUD:
            lease = ComputeLease(self, request, placement, 0)
            self._cloud.add(lease)
            return lease
        if request.resource_class is ResourceClass.EXCLUSIVE:
            lease = ComputeLease(self, request, placement, 0)
            self._exclusive_baseline = self._vram().external
            self._exclusive = lease
            return lease
        entry = self._deployments[request.deployment]
        tokens = 0
        if placement is Placement.LOCAL_GPU and entry.spec.kv_capacity_tokens:
            tokens = request.context_tokens
        lease = ComputeLease(self, request, placement, tokens)
        entry.leases.add(lease)
        entry.reserved_tokens += tokens
        return lease

    def _release(self, lease: ComputeLease) -> None:
        if lease._released or lease._scheduler is not self:
            return
        lease._released = True
        if lease.placement is Placement.CLOUD:
            self._cloud.discard(lease)
        elif lease is self._exclusive:
            self._exclusive = None
            self._mode = SchedulerMode.NORMAL
            self._refused_tasks.clear()
            logger.info("Exclusive GPU job ended; the models are loaded again")
        else:
            entry = self._deployments[lease.deployment]
            if lease in entry.leases:
                entry.leases.discard(lease)
                entry.reserved_tokens -= lease.tokens
        if self._drained is not None and not self._local_gpu_leases():
            self._drained.set()
        self._pump()

    def _local_gpu_leases(self) -> int:
        return sum(
            1
            for entry in self._deployments.values()
            for lease in entry.leases
            if lease.placement is Placement.LOCAL_GPU
        )

    def _pump(self) -> None:
        """Admit the waiters that fit, highest class first, FIFO within a class."""
        blocked: set[str] = set()
        for waiter in sorted(self._waiters, key=lambda w: w.key):
            if waiter.future.done():
                continue
            deployment = waiter.request.deployment
            if deployment in blocked:
                continue
            refusal, placement = self._judge(waiter.request, queue=False)
            if refusal is None:
                self._waiters.remove(waiter)
                waiter.future.set_result(self._grant(waiter.request, placement))
                continue
            waiter.last = refusal
            if refusal in CAPACITY_REFUSALS:
                blocked.add(deployment)

    def _drop(self, waiter: _Waiter) -> None:
        with contextlib.suppress(ValueError):
            self._waiters.remove(waiter)

    async def _wait_for(self, awaitable: asyncio.Future, seconds: float) -> bool:
        """Wait for ``awaitable`` at most ``seconds`` (the injected clock)."""
        if awaitable.done():
            return True
        timer = asyncio.ensure_future(self._clock.sleep(seconds))
        try:
            await asyncio.wait({awaitable, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer
        return awaitable.done()

    # -- relief and residency -----------------------------------------------------

    def _set_relief(self, relief: Relief) -> None:
        if relief is self._relief:
            return
        rising = relief > self._relief
        self._relief = relief
        if relief is Relief.MAIN_CHANGE_NEEDED:
            logger.warning(
                "VRAM pressure persists after every relief step: the main model's "
                "configuration must be changed by a human"
            )
        elif rising:
            logger.warning("VRAM pressure: relief step %s", relief.name.lower())
        else:
            logger.info("VRAM pressure eased: relief step %s", relief.name.lower())

    def _revoke(self, predicate: Callable[[ComputeLease], bool]) -> None:
        for entry in self._deployments.values():
            for lease in entry.leases:
                if predicate(lease):
                    lease.revoked.set()

    def _decide(self) -> _Action | None:
        view = self._vram()
        if view.under_pressure:
            return self._relieve()
        for entry in self._ordered:
            # The pressure ended before a drained model was moved: keep it.
            if entry.draining and not entry.busy:
                entry.draining = False
        if self._relief is not Relief.NONE:
            return self._restore(view)
        return self._fill(view)

    def _relieve(self) -> _Action | None:
        relief = self._relief
        if relief is Relief.NONE:
            self._set_relief(Relief.BACKGROUND_STOPPED)
            self._revoke(
                lambda lease: (
                    lease.resource_class is ResourceClass.BACKGROUND
                    and lease.placement is Placement.LOCAL_GPU
                )
            )
            return None
        if relief is Relief.BACKGROUND_STOPPED:
            return self._displace(
                frozenset({ModelRole.MEMORY_WORKER}), Relief.MEMORY_WORKER_UNLOADED
            )
        if relief is Relief.MEMORY_WORKER_UNLOADED:
            return self._displace(SUPPORT_ROLES, Relief.SUPPORT_ON_CPU)
        if relief < Relief.MAIN_CHANGE_NEEDED:
            self._set_relief(Relief(relief + 1))
        return None

    def _displaceable(self, entry: _Deployment, roles: frozenset[ModelRole]) -> bool:
        spec = entry.spec
        if spec.role not in roles or entry.state is not DeploymentState.GPU:
            return False
        if spec.role is ModelRole.MEMORY_WORKER:
            return True
        return spec.cpu_fallback or spec.residency is ResidencyPolicy.IF_ROOM

    def _displace(self, roles: frozenset[ModelRole], then: Relief) -> _Action | None:
        targets = [
            entry
            for entry in self._ordered
            if not entry.busy and self._displaceable(entry, roles)
        ]
        if self._control is None or not targets:
            self._set_relief(then)
            return None
        for entry in targets:
            if not entry.draining:
                entry.draining = True
                for lease in entry.leases:
                    lease.revoked.set()
        ready = [entry for entry in targets if not entry.leases]
        if not ready:
            return None  # wait for the running work to end
        entry = ready[0]

        def after() -> None:
            if entry.state is not DeploymentState.FAILED:
                entry.displaced = True
            if not any(
                not other.busy and self._displaceable(other, roles)
                for other in self._ordered
            ):
                self._set_relief(then)

        return _Action(entry, "cpu" if entry.spec.cpu_fallback else "unload", after)

    def _restore(self, view: VramView) -> _Action | None:
        margin = self._margin()
        if view.available < margin:
            return None  # not room enough to undo anything yet
        relief = self._relief
        if relief >= Relief.ADMISSION_SUPPRESSED:
            self._set_relief(Relief(relief - 1))
            return None
        if relief is Relief.SUPPORT_ON_CPU:
            return self._bring_back(SUPPORT_ROLES, view, Relief.MEMORY_WORKER_UNLOADED)
        if relief is Relief.MEMORY_WORKER_UNLOADED:
            return self._bring_back(
                frozenset({ModelRole.MEMORY_WORKER}), view, Relief.BACKGROUND_STOPPED
            )
        self._set_relief(Relief.NONE)
        return None

    def _bring_back(
        self, roles: frozenset[ModelRole], view: VramView, then: Relief
    ) -> _Action | None:
        def waiting() -> list[_Deployment]:
            return [
                entry
                for entry in self._ordered
                if entry.spec.role in roles
                and entry.displaced
                and not entry.busy
                and entry.state in (DeploymentState.CPU, DeploymentState.UNLOADED)
            ]

        candidates = waiting()
        if self._control is None or not candidates:
            self._set_relief(then)
            return None
        entry = candidates[0]
        if view.available - entry.spec.gpu_bytes < self._margin():
            return None

        def after() -> None:
            entry.displaced = False
            if not waiting():
                self._set_relief(then)

        return _Action(entry, "gpu", after)

    def _fill(self, view: VramView) -> _Action | None:
        """Load a model its residency policy wants on the GPU, if it fits."""
        if self._control is None:
            return None
        now = self._clock.monotonic()
        for entry in self._ordered:
            if entry.busy or entry.state is DeploymentState.GPU:
                continue
            if entry.state is DeploymentState.FAILED and (
                entry.retry_at is not None and now < entry.retry_at
            ):
                continue
            need = entry.spec.gpu_bytes
            room = view.available + (need if entry.counts_on_gpu else 0)
            always = entry.spec.residency is ResidencyPolicy.ALWAYS
            if room - need >= (0 if always else self._margin()):
                entry.displaced = False
                return _Action(entry, "gpu")
            if always:
                return None  # a model that must be resident goes first
        return None

    async def _perform(self, action: _Action) -> bool:
        """Run one model action; the deployment is ``FAILED`` when it raises."""
        entry = action.deployment
        name = entry.spec.name
        entry.busy = True
        try:
            if action.operation == "gpu":
                await self._control.place(name, Placement.LOCAL_GPU)
                state = DeploymentState.GPU
            elif action.operation == "cpu":
                await self._control.place(name, Placement.LOCAL_CPU)
                state = DeploymentState.CPU
            else:
                await self._control.unload(name)
                state = DeploymentState.UNLOADED
        except BaseException as error:
            entry.state = DeploymentState.FAILED
            entry.pids = None
            entry.retry_at = self._clock.monotonic() + self._config.failed_retry_seconds
            if not isinstance(error, Exception):
                raise
            logger.warning(
                "Model action %s of deployment %s failed (%s)",
                action.operation,
                name,
                type(error).__name__,
            )
            ok = False
        else:
            entry.state = state
            entry.retry_at = None
            entry.pids = None if state is DeploymentState.GPU else frozenset()
            entry.observed = None
            logger.info("Model action %s of deployment %s done", action.operation, name)
            ok = True
        finally:
            entry.busy = False
            entry.draining = False
        if action.then is not None:
            action.then()
        return ok

    async def _perform_sampling(self, action: _Action) -> bool:
        """``_perform`` while the probe keeps being read and waiters admitted
        (the deployment being acted on is ``busy``: it admits nothing)."""
        task = asyncio.ensure_future(self._perform(action))
        interval = min(DEFAULT_REFRESH_SECONDS, self._config.probe_max_age_seconds / 3)
        try:
            while not await self._wait_for(task, interval):
                await self._sample()
                self._pump()
        except BaseException:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise
        return task.result()

    # -- exclusive ------------------------------------------------------------

    async def _acquire_exclusive(
        self, request: ComputeRequest, wait_seconds: float
    ) -> ComputeLease:
        if self._mode is not SchedulerMode.NORMAL:
            raise ExclusiveUnavailableError(ExclusiveFailure.BUSY)
        self._mode = SchedulerMode.DRAINING
        try:
            async with self._control_lock:
                await self._sample()
            if not self._fresh():
                raise ExclusiveUnavailableError(ExclusiveFailure.PROBE_UNAVAILABLE)
            if self._control is None and any(
                entry.counts_on_gpu for entry in self._ordered
            ):
                raise ExclusiveUnavailableError(ExclusiveFailure.CANNOT_UNLOAD)
            # Running work is waited for, not stopped (Background work too: Full
            # GPU Mode holds the tasks and may revoke the leases, PAW-037).
            logger.info("Exclusive GPU job requested: draining local GPU work")
            if self._local_gpu_leases():
                self._drained = asyncio.Event()
                waiter = asyncio.ensure_future(self._drained.wait())
                try:
                    drained = await self._wait_for(waiter, wait_seconds)
                finally:
                    waiter.cancel()
                    self._drained = None
                if not drained:
                    raise ExclusiveUnavailableError(ExclusiveFailure.DRAIN_TIMEOUT)
            async with self._control_lock:
                moved = await self._empty_gpu()
                await self._verify(request.vram_bytes, moved)
                lease = self._grant(request, Placement.LOCAL_GPU)
                self._mode = SchedulerMode.EXCLUSIVE
                logger.info("Exclusive GPU job started")
                return lease
        except BaseException:
            self._mode = SchedulerMode.NORMAL
            self._pump()
            raise

    async def _empty_gpu(self) -> tuple[_Deployment, ...]:
        """Move every model off the GPU; the ones it moved."""
        order = {role: index for index, role in enumerate(_EXCLUSIVE_ORDER)}
        moved = []
        for entry in sorted(self._ordered, key=lambda e: order[e.spec.role]):
            if not entry.counts_on_gpu:
                continue
            operation = "cpu" if entry.spec.cpu_fallback else "unload"
            entry.displaced = False
            moved.append(entry)
            if not await self._perform(_Action(entry, operation)):
                raise ExclusiveUnavailableError(ExclusiveFailure.CANNOT_UNLOAD)
        return tuple(moved)

    async def _verify(self, vram_bytes: int, moved: tuple[_Deployment, ...]) -> None:
        """Wait until the probe shows no process of the ``moved`` models on the
        GPU and ``vram_bytes`` free (``NOT_FREED`` after the verify timeout)."""
        deadline = self._clock.monotonic() + self._config.verify_timeout_seconds
        while True:
            await self._sample()
            if await self._freed(vram_bytes, moved):
                return
            if self._clock.monotonic() >= deadline:
                raise ExclusiveUnavailableError(ExclusiveFailure.NOT_FREED)
            await self._clock.sleep(self._config.verify_poll_seconds)

    async def _freed(self, vram_bytes: int, moved: tuple[_Deployment, ...]) -> bool:
        # Without a model control nothing of the workspace was on the GPU
        # (``_acquire_exclusive`` refused otherwise): only the VRAM is checked.
        # A model that was not on the GPU may have no pids command: only the
        # ``moved`` ones must say where their processes are.
        if not self._fresh() or (moved and self._control is None):
            return False
        if self._control is not None:
            on_gpu = {process.pid for process in self._processes}
            for entry in self._ordered:
                try:
                    pids = await self._control.processes(entry.spec.name)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pids = None
                if not isinstance(pids, frozenset):
                    if entry in moved:
                        return False
                    continue
                if pids & on_gpu:
                    return False
        return self._vram().available >= vram_bytes
