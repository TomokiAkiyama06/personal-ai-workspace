"""The Compute Resource Scheduler (PAW-036).

One scheduler per backend process owns the admission to the local models and the
residency of those models on the GPU (``REQUIREMENTS.md``, "GPU / Compute
Resource Scheduler", FIXED; the open choices are in Decision 0037, Proposed).

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
    not stop it: pausing running tasks is PAW-037), unloads every model (an
    Embedding / Reranker with a CPU copy moves there), confirms from the probe
    that no process of the workspace holds GPU memory and that the requested VRAM
    is free, and only then grants the lease. Any failure puts the scheduler back
    to normal. Releasing the lease lets ``refresh()`` load the models again.

Observed free VRAM (Decision 0042, Proposed)
    Processes the scheduler does not manage are invisible to its leases, but not
    to the probe. Work that allocates VRAM of its own (a request with
    ``vram_bytes``) is admitted only when the probe shows that much free beyond
    the headroom, after what the scheduler promised and the probe does not show
    yet; otherwise it waits (``INSUFFICIENT_FREE_VRAM``) and a rate-limited
    warning goes to the log and the injected sink (``alerts.py``). An Exclusive
    job whose VRAM would not be free even with every model unloaded (another
    workload holds it) waits the same way before anything is drained or
    unloaded. The GPU utilisation is never used for admission.

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
    free_vram_admits,
    free_vram_after_emptying,
    headroom_bytes,
)
from paw_backend.compute.alerts import DeferredWork, VramDeferral, VramWarnings
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
    KV cache. ``vram_bytes``: what an Exclusive job needs free; for the other
    classes, the VRAM the work allocates of its own, outside the model's
    reserved footprint (0 for work that only uses the model's KV cache; Decision
    0042). ``allow_cloud``:
    the caller may run the work on a cloud agent instead (it has checked the
    task's permission and quota); never for Exclusive.
    """

    resource_class: ResourceClass
    deployment: str | None = None
    context_tokens: int = 0
    vram_bytes: int = 0
    allow_cloud: bool = False

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
        if self.resource_class is ResourceClass.EXCLUSIVE:
            if self.deployment is not None:
                raise InvalidComputeArgumentError("deployment")
            if not 0 < self.vram_bytes <= _MAX_BYTES:
                raise InvalidComputeArgumentError("vram_bytes")
            if self.allow_cloud:
                raise InvalidComputeArgumentError("allow_cloud")
            return
        check_name("deployment", self.deployment)
        if not 0 <= self.vram_bytes <= _MAX_BYTES:
            raise InvalidComputeArgumentError("vram_bytes")


class ComputeLease:
    """Admitted work. Release it when the work ends (``async with`` does).

    ``revoked`` is set when the scheduler asks the holder to stop (a Background
    job under VRAM pressure, or the work of a Memory Worker that is about to be
    unloaded): the holder should wind down and release. Nothing is killed.
    """

    __slots__ = (
        "id",
        "resource_class",
        "deployment",
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
        self.placement = placement
        self.tokens = tokens
        # Only work on the GPU holds VRAM (a CPU or cloud lease holds none).
        self.vram_bytes = request.vram_bytes if placement is Placement.LOCAL_GPU else 0
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
    # Waiters whose last refusal was ``INSUFFICIENT_FREE_VRAM``, and whether an
    # Exclusive job waits for VRAM another workload holds (Decision 0042).
    vram_waiting: int = 0
    exclusive_waiting_for_vram: bool = False

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
    # It waited for free VRAM at some point (Decision 0042 §7: a wait that
    # runs out while the probe is lost still warns that it gave up).
    waited_for_vram: bool = False

    def __post_init__(self) -> None:
        self.note(self.last)

    def note(self, refusal: Refusal) -> None:
        self.last = refusal
        if refusal is Refusal.INSUFFICIENT_FREE_VRAM:
            self.waited_for_vram = True

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
        vram_warnings: object | None = None,
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
        self._warnings = VramWarnings(
            clock.monotonic,
            interval_seconds=config.vram_warning_interval_seconds,
            sink=vram_warnings,
        )
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
        # What was external when the VRAM leases (an Exclusive job's, or the
        # shared ones with ``vram_bytes``) last changed (see account).
        self._extra_baseline = 0
        # What released VRAM leases may have allocated that no reading has
        # shown yet: rebased on the first reading taken after the release (see
        # _rebase).
        self._pending_release = 0
        # The VRAM view of the last reading before the probe was lost: the
        # final warning of a wait that runs out without a reading (Decision
        # 0042 §7).
        self._last_view: VramView | None = None
        # An Exclusive request is being served (it may be waiting for VRAM
        # another workload holds while the mode is still normal).
        self._exclusive_pending = False
        self._exclusive_waits_for_vram = False
        self._drained: asyncio.Event | None = None
        self._cloud: set[ComputeLease] = set()
        self._waiters: list[_Waiter] = []
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
            if refusal is Refusal.INSUFFICIENT_FREE_VRAM:
                self._warn_request(request)
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
        if refusal is Refusal.INSUFFICIENT_FREE_VRAM and not (
            request.allow_cloud and cloud_after == 0
        ):
            self._warn_request(request, gave_up=timeout == 0)
        if refusal in PERMANENT_REFUSALS or (request.allow_cloud and cloud_after == 0):
            if request.allow_cloud:
                return self._grant(request, Placement.CLOUD)
            raise ComputeUnavailableError(refusal)
        if timeout == 0:
            raise ComputeUnavailableError(refusal)
        if len(self._waiters) >= self._config.max_waiters:
            if request.allow_cloud:
                return self._grant(request, Placement.CLOUD)
            raise ComputeUnavailableError(Refusal.QUEUE_FULL)
        future = asyncio.get_running_loop().create_future()
        waiter = _Waiter(
            request, future, next(self._sequence), self._queued_reason(request, refusal)
        )
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
        if waiter.last is Refusal.INSUFFICIENT_FREE_VRAM or (
            waiter.waited_for_vram and waiter.last is Refusal.PROBE_UNAVAILABLE
        ):
            self._warn_request(request, gave_up=True)
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
            vram_waiting=sum(
                1
                for waiter in self._waiters
                if waiter.last is Refusal.INSUFFICIENT_FREE_VRAM
            ),
            exclusive_waiting_for_vram=self._exclusive_waits_for_vram,
        )

    # -- the probe ------------------------------------------------------------

    async def _sample(self) -> None:
        # Only releases made before this reading started can be seen in it.
        carried = self._pending_release
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
        if carried:
            # Leases released before this reading, with memory no earlier
            # reading showed: rebased on this one (with the models' processes
            # it found).
            view = self._vram()
            self._extra_baseline = view.external + min(carried, view.extra_use)
            self._pending_release -= carried

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
        if self._device is not None:
            self._last_view = self._vram()
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
        return account(
            self._device,
            self._processes,
            usages,
            headroom=self._headroom(),
            extra_reserved=self._extra_reserved(),
            extra_baseline=self._extra_baseline,
        )

    def _extra_reserved(self) -> int:
        """VRAM promised outside the models' footprints: the Exclusive job's, or
        what the shared GPU leases allocate of their own (Decision 0042). The two
        never meet: an Exclusive lease is granted only once no local GPU lease
        is left."""
        # A released lease's reservation still counts while no reading has
        # shown what it left behind (see _rebase).
        if self._exclusive is not None:
            return self._pending_release + self._exclusive.vram_bytes
        return self._pending_release + sum(
            lease.vram_bytes
            for entry in self._deployments.values()
            for lease in entry.leases
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
        on_cpu = self._on_cpu(entry)
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
            if queue and self._queued_ahead(request, gpu=False):
                return Refusal.QUEUED_BEHIND, Placement.LOCAL_CPU
            if len(entry.leases) >= spec.max_sequences:
                return Refusal.SEQUENCES_FULL, Placement.LOCAL_CPU
            return None, Placement.LOCAL_CPU
        if self._mode is not SchedulerMode.NORMAL:
            return Refusal.EXCLUSIVE_MODE, Placement.LOCAL_GPU
        if not available or entry.state is not DeploymentState.GPU:
            return Refusal.NOT_RESIDENT, Placement.LOCAL_GPU
        if queue and self._queued_ahead(request, gpu=True):
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
        if refusal is None and not free_vram_admits(self._vram(), request.vram_bytes):
            refusal = Refusal.INSUFFICIENT_FREE_VRAM
        return refusal, Placement.LOCAL_GPU

    @staticmethod
    def _on_cpu(entry: _Deployment) -> bool:
        """Work on ``entry`` is placed on its CPU copy now."""
        return (
            not entry.busy and not entry.draining and entry.state is DeploymentState.CPU
        )

    def _queued_ahead(self, request: ComputeRequest, *, gpu: bool) -> bool:
        rank = CLASS_RANK[request.resource_class]
        # Only a GPU placement holds VRAM (a CPU lease holds none).
        needs_vram = gpu and request.vram_bytes > 0
        return any(
            CLASS_RANK[waiter.request.resource_class] <= rank
            and (
                # VRAM is the whole GPU's: work that needs some queues behind
                # earlier work that waits for it, whatever its model (as in
                # _pump; a waiter held back by its own model does not count) ...
                (needs_vram and waiter.last is Refusal.INSUFFICIENT_FREE_VRAM)
                # ... and work on the same model queues behind its waiters,
                # except those that only wait for VRAM when it needs none.
                or (
                    waiter.request.deployment == request.deployment
                    and (
                        needs_vram or waiter.last is not Refusal.INSUFFICIENT_FREE_VRAM
                    )
                )
            )
            for waiter in self._waiters
        )

    def _queued_reason(self, request: ComputeRequest, refusal: Refusal) -> Refusal:
        """What a new waiter queued behind others waits for, as _pump labels it:
        its model's capacity, or VRAM (a chain of VRAM waiters must not hold
        back work that needs none: see _queued_ahead)."""
        if refusal is not Refusal.QUEUED_BEHIND:
            return refusal
        rank = CLASS_RANK[request.resource_class]
        ahead = [
            waiter
            for waiter in sorted(self._waiters, key=lambda w: w.key)
            if CLASS_RANK[waiter.request.resource_class] <= rank
            and not waiter.future.done()
        ]
        for waiter in ahead:
            if (
                waiter.request.deployment == request.deployment
                and waiter.last in CAPACITY_REFUSALS
            ):
                return waiter.last
        needs_vram = request.vram_bytes > 0 and not self._on_cpu(
            self._deployments[request.deployment]
        )
        if needs_vram and any(
            waiter.last is Refusal.INSUFFICIENT_FREE_VRAM for waiter in ahead
        ):
            return Refusal.INSUFFICIENT_FREE_VRAM
        return refusal

    def _warn_request(self, request: ComputeRequest, *, gave_up: bool = False) -> None:
        # The last reading, even a stale one (a wait that ran out); without any
        # the probe's own warning has been logged.
        if self._device is not None:
            view = self._vram()
        elif gave_up and self._last_view is not None:
            view = self._last_view
        else:
            return
        self._warnings.emit(
            VramDeferral(
                DeferredWork.REQUEST,
                request.resource_class,
                request.vram_bytes,
                view.observed_free,
                view.external,
                view.headroom,
                gave_up=gave_up,
            )
        )

    def _grant(self, request: ComputeRequest, placement: Placement) -> ComputeLease:
        if placement is Placement.CLOUD:
            lease = ComputeLease(self, request, placement, 0)
            self._cloud.add(lease)
            return lease
        if request.resource_class is ResourceClass.EXCLUSIVE:
            lease = ComputeLease(self, request, placement, 0)
            self._extra_baseline = self._vram().external
            self._exclusive = lease
            return lease
        entry = self._deployments[request.deployment]
        tokens = 0
        if placement is Placement.LOCAL_GPU and entry.spec.kv_capacity_tokens:
            tokens = request.context_tokens
        lease = ComputeLease(self, request, placement, tokens)
        if lease.vram_bytes:
            self._rebase()
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
            logger.info("Exclusive GPU job ended; the models are loaded again")
        else:
            entry = self._deployments[lease.deployment]
            if lease in entry.leases:
                if lease.vram_bytes:
                    self._rebase(released=lease.vram_bytes)
                entry.leases.discard(lease)
                entry.reserved_tokens -= lease.tokens
        if self._drained is not None and not self._local_gpu_leases():
            self._drained.set()
        self._pump()

    def _rebase(self, *, released: int = 0) -> None:
        """Before the shared VRAM leases change: what is external now is not
        theirs (see account: only what grows over it is absorbed).

        On a release (``released``: that lease's reservation), what the lease
        absorbed becomes external too: its process may keep the memory (a
        caching allocator), and it must not cover what the remaining leases
        promised and have not allocated yet. Which lease's process holds what is
        not known, so the released lease is taken to have absorbed as much as it
        could (``min(released, extra_use)``): memory a remaining lease holds may
        be counted twice until that lease ends, never overcommitted."""
        if self._device is None:
            # No reading (the probe is lost): the whole release is rebased on
            # the next one.
            self._pending_release += released
            return
        view = self._vram()
        absorbed = min(released, view.extra_use)
        self._extra_baseline = view.external + absorbed
        # What the reading does not show yet (the lease may have allocated
        # after it was taken) is rebased on the next reading, before the
        # remaining leases' reservations could absorb it.
        self._pending_release += released - absorbed

    def _local_gpu_leases(self) -> int:
        return sum(
            1
            for entry in self._deployments.values()
            for lease in entry.leases
            if lease.placement is Placement.LOCAL_GPU
        )

    def _pump(self) -> None:
        """Admit the waiters that fit, highest class first, FIFO within a class."""
        # The capacity refusal each blocked model's first waiter got.
        blocked: dict[str, Refusal] = {}
        vram_blocked = False
        for waiter in sorted(self._waiters, key=lambda w: w.key):
            if waiter.future.done():
                continue
            deployment = waiter.request.deployment
            if deployment in blocked:
                # It waits behind its model's earlier waiter now, not for VRAM
                # (a refusal of an earlier pass must not hold back other
                # models' VRAM work: see _queued_ahead).
                waiter.note(blocked[deployment])
                continue
            # A waiter placed on the CPU copy holds no VRAM.
            needs_vram = waiter.request.vram_bytes > 0 and not self._on_cpu(
                self._deployments[deployment]
            )
            if needs_vram and vram_blocked:
                waiter.note(Refusal.INSUFFICIENT_FREE_VRAM)
                continue
            refusal, placement = self._judge(waiter.request, queue=False)
            if refusal is None:
                self._waiters.remove(waiter)
                waiter.future.set_result(self._grant(waiter.request, placement))
                continue
            waiter.note(refusal)
            if refusal in CAPACITY_REFUSALS:
                blocked[deployment] = refusal
            elif refusal is Refusal.INSUFFICIENT_FREE_VRAM:
                # Later work that needs VRAM does not overtake it (a stream of
                # small jobs cannot starve a large one); work inside a model's
                # footprint allocates nothing and goes on.
                vram_blocked = True
                self._warn_request(waiter.request)

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
            floor = 0 if always else self._margin()
            if room - need >= floor:
                entry.displaced = False
                return _Action(entry, "gpu")
            if room + view.external - need >= floor:
                # It would fit but for another workload's VRAM (Decision 0042).
                self._warnings.emit(
                    VramDeferral(
                        DeferredWork.MODEL_LOAD,
                        None,
                        need,
                        view.observed_free,
                        view.external,
                        view.headroom,
                    )
                )
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
        if self._mode is not SchedulerMode.NORMAL or self._exclusive_pending:
            raise ExclusiveUnavailableError(ExclusiveFailure.BUSY)
        self._exclusive_pending = True
        deadline = self._clock.monotonic() + wait_seconds
        try:
            async with self._control_lock:
                await self._sample()
            if not self._fresh():
                raise ExclusiveUnavailableError(ExclusiveFailure.PROBE_UNAVAILABLE)
            if self._control is None and any(
                entry.counts_on_gpu for entry in self._ordered
            ):
                raise ExclusiveUnavailableError(ExclusiveFailure.CANNOT_UNLOAD)
            while True:
                # Decision 0042: while another workload holds the VRAM the job
                # needs, nothing is drained or unloaded (it could not help);
                # local work goes on and the job waits.
                await self._await_external_room(request, deadline)
                self._mode = SchedulerMode.DRAINING
                # Running work is waited for, not stopped (Background work too:
                # its safe pause / drain is PAW-037's).
                logger.info("Exclusive GPU job requested: draining local GPU work")
                await self._drain(deadline)
                async with self._control_lock:
                    await self._sample()
                    if self._clock.monotonic() > deadline:
                        # The reading ended after the caller's limit: nothing
                        # is unloaded.
                        raise ExclusiveUnavailableError(
                            ExclusiveFailure.NOT_FREED
                            if self._fresh()
                            else ExclusiveFailure.PROBE_UNAVAILABLE
                        )
                    if self._fresh() and self._external_room(request):
                        moved = await self._empty_gpu()
                        await self._verify(request.vram_bytes, moved)
                        lease = self._grant(request, Placement.LOCAL_GPU)
                        self._mode = SchedulerMode.EXCLUSIVE
                        logger.info("Exclusive GPU job started")
                        return lease
                # Another workload took the VRAM while the GPU drained: back to
                # normal, and wait again.
                self._mode = SchedulerMode.NORMAL
                self._pump()
        except BaseException:
            self._mode = SchedulerMode.NORMAL
            self._pump()
            raise
        finally:
            self._exclusive_pending = False
            self._exclusive_waits_for_vram = False

    def _external_room(self, request: ComputeRequest) -> bool:
        return free_vram_after_emptying(self._vram()) >= request.vram_bytes

    async def _await_external_room(
        self, request: ComputeRequest, deadline: float
    ) -> None:
        """Wait until the VRAM the job needs would be free with every model of
        the workspace unloaded (Decision 0042). A reading that is missing or
        stale counts as not free (fail closed). At the deadline: ``NOT_FREED``
        (``PROBE_UNAVAILABLE`` when there was no fresh reading)."""
        while not (self._fresh() and self._external_room(request)):
            self._exclusive_waits_for_vram = True
            gave_up = self._clock.monotonic() >= deadline
            if self._fresh():
                self._warn_exclusive(request, gave_up=gave_up)
            if gave_up:
                raise ExclusiveUnavailableError(
                    ExclusiveFailure.NOT_FREED
                    if self._fresh()
                    else ExclusiveFailure.PROBE_UNAVAILABLE
                )
            await self._clock.sleep(
                min(
                    self._config.verify_poll_seconds,
                    deadline - self._clock.monotonic(),
                )
            )
            async with self._control_lock:
                await self._sample()
            if self._clock.monotonic() >= deadline:
                # The poll ended after the caller's limit: VRAM freed too late
                # does not start the drain (nothing is unloaded).
                if self._fresh() and self._external_room(request):
                    self._warn_exclusive(request, gave_up=True)
                    raise ExclusiveUnavailableError(ExclusiveFailure.NOT_FREED)
        self._exclusive_waits_for_vram = False

    def _warn_exclusive(self, request: ComputeRequest, *, gave_up: bool) -> None:
        view = self._vram()
        self._warnings.emit(
            VramDeferral(
                DeferredWork.EXCLUSIVE,
                ResourceClass.EXCLUSIVE,
                request.vram_bytes,
                view.observed_free,
                view.external,
                view.headroom,
                gave_up=gave_up,
            )
        )

    async def _drain(self, deadline: float) -> None:
        """Wait for the running local GPU work to end (``DRAIN_TIMEOUT`` at the
        deadline)."""
        if not self._local_gpu_leases():
            return
        self._drained = asyncio.Event()
        waiter = asyncio.ensure_future(self._drained.wait())
        try:
            drained = await self._wait_for(
                waiter, max(0.0, deadline - self._clock.monotonic())
            )
        finally:
            waiter.cancel()
            self._drained = None
        if not drained:
            raise ExclusiveUnavailableError(ExclusiveFailure.DRAIN_TIMEOUT)

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
