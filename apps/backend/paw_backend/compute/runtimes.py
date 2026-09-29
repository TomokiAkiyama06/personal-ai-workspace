"""What connects the scheduler to its callers (PAW-036).

* :class:`HybridRuntime` is an orchestrator ``AgentRuntime`` (PAW-034). It asks
  the scheduler for a lease before a node runs on the local model, so the number
  of local nodes that run at once follows the KV cache and not the static
  ``max_parallel_nodes`` alone (that stays the upper bound, Decision 0021). When
  the local GPU is busy and the injected :class:`CloudPolicy` allows it for the
  node, the node runs on the cloud runtime instead (Local / Cloud hybrid). The
  policy is the caller's: the requirements allow the cloud only "within the
  task's dependency / permission / quota", which the backend knows and the
  scheduler does not. Without a policy nothing goes to the cloud. Where each
  attempt runs (the local GPU or CPU, or the cloud), on which agent and model,
  is recorded through the assignment's ``placement`` BEFORE the node runs there
  (issue #133, Decision 0037's 14): for the cloud that record is also the audit
  of the external send, so a node whose placement cannot be recorded (an
  assignment without ``placement``, such as a planner call, or a failed write)
  never goes to the cloud. The wall time a
  node holds a local lease is charged to the task as ``GPU_SECONDS`` (the budget's
  "max GPU time"), also when the local runtime raises or is cancelled. A node
  with no GPU time left does not start locally, and the task's local calls are
  stopped when together they have held the GPU for the time that was left
  (``NodeStopped`` on the budget in both cases), so slow calls cannot overrun
  the budget.
* :class:`ScheduledMemoryWorker` wraps a Memory Worker (PAW-041). A job runs only
  under a Background lease on the Memory Worker's model; when there is none (the
  model is unloaded, background work is stopped under pressure, an Exclusive job
  holds the GPU) it raises ``WorkerUnavailableError``, which the consolidator
  reads as "not now" and defers without counting a failure (Decision 0018). An
  observation too long for the model ever to take is a failed attempt instead
  (``ComputeUnavailableError``), so that it is not deferred for ever.
* :class:`PlacedEmbedder` wraps the GPU and the CPU copy of an embedding model
  (the retrieval's ``Embedder``, PAW-043) and uses the one the scheduler placed
  (CPU fallback).
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import math
import uuid
import weakref
from collections.abc import Callable, Coroutine, Sequence
from typing import Protocol

from paw_backend.compute.domain import (
    PERMANENT_REFUSALS,
    Placement,
    Refusal,
    ResourceClass,
)
from paw_backend.compute.errors import ComputeUnavailableError
from paw_backend.compute.limits import (
    BYTES_PER_TOKEN_ESTIMATE,
    DEFAULT_MEMORY_OUTPUT_TOKENS,
    DEFAULT_NODE_WAIT_SECONDS,
    DEFAULT_OUTPUT_TOKENS,
)
from paw_backend.compute.scheduler import (
    ComputeLease,
    ComputeRequest,
    ComputeScheduler,
    check_seconds,
)
from paw_backend.memory.journal.errors import WorkerUnavailableError
from paw_backend.memory.journal.worker import check_worker
from paw_backend.orchestrator.config import Clock, SystemClock
from paw_backend.orchestrator.domain import ExecutionPlacement
from paw_backend.orchestrator.errors import (
    InvalidOrchestratorArgumentError,
    NodeStopped,
    StopReason,
)
from paw_backend.orchestrator.result import upstream_size
from paw_backend.orchestrator.runtime import (
    AgentRuntime,
    NodeAssignment,
    NodeOutcome,
    validate_runtime,
)
from paw_backend.orchestrator.validation import check_agent_label, check_model
from paw_backend.tasks.queueing import BudgetKind
from paw_backend.tools.interfaces import require_async_method

COMPUTE_UNAVAILABLE = "ComputeUnavailable"
_MAX_ESTIMATE = 1 << 24
# How long a local runtime stopped on the GPU time left may take to end.
CANCEL_GRACE_SECONDS = 10.0
# The local runtime was stopped because the task's GPU time ran out.
_EXHAUSTED = object()
# The local runtime was stopped because the scheduler revoked the lease.
_REVOKED = object()
# The local placement could not be recorded: the node did not run.
_UNPLACED = object()

logger = logging.getLogger(__name__)


async def _cancel_and_wait(work: asyncio.Future, lease: ComputeLease) -> None:
    """Cancel ``work`` and wait for it, at most ``CANCEL_GRACE_SECONDS``. The
    lease is held until ``work`` ends *before* the wait: the wait itself can be
    cancelled again (a task stop and a shutdown at once), and work that swallowed
    its cancellation still uses the GPU whatever happens to its caller."""
    work.cancel()
    work.add_done_callback(_retrieve)
    lease.hold_until(work)
    if not work.done():
        await asyncio.wait({work}, timeout=CANCEL_GRACE_SECONDS)
    if not work.done():
        logger.error(
            "A local runtime did not stop when it was cancelled: its lease is "
            "kept until it ends"
        )


async def _until_revoked[T](
    call: Coroutine[object, object, T],
    lease: ComputeLease,
    stopped: Callable[[], BaseException],
) -> T:
    """Await ``call``; when ``lease`` is revoked first, cancel it and raise
    ``stopped()``. A call that does not stop when it is cancelled keeps the lease
    until it ends (its GPU capacity is not given away)."""
    work = asyncio.ensure_future(call)
    revoked = asyncio.ensure_future(lease.revoked.wait())
    try:
        await asyncio.wait({work, revoked}, return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return work.result()
        raise stopped()
    finally:
        revoked.cancel()
        await _cancel_and_wait(work, lease)


def _retrieve(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


def _tokens(size_bytes: int) -> int:
    return math.ceil(size_bytes / BYTES_PER_TOKEN_ESTIMATE)


def estimate_context_tokens(
    assignment: NodeAssignment, *, output_tokens: int = DEFAULT_OUTPUT_TOKENS
) -> int:
    """A conservative estimate of the context a node needs: its title, goal,
    input and upstream results in bytes / 3, plus ``output_tokens`` for the
    answer. A runtime that knows better passes its own ``estimate``."""
    size = len(assignment.title.encode("utf-8")) + len(assignment.goal.encode("utf-8"))
    size += len(
        json.dumps(assignment.input, ensure_ascii=False, default=str).encode("utf-8")
    )
    size += upstream_size(assignment.upstream)
    return min(_MAX_ESTIMATE, _tokens(size) + output_tokens)


class CloudPolicy(Protocol):
    async def allows(self, assignment: NodeAssignment) -> bool:
        """Whether this node may run on the cloud agent: the task's permission
        (the external send of its content), the user's quota, the node's
        dependencies. ``False`` keeps it local."""
        ...


class HybridRuntime:
    """Local first, the cloud when the local GPU is busy (see the module).

    ``local_model``: the model id recorded for a local attempt (default: the
    deployment's name). ``cloud_agent`` / ``cloud_model``: the cloud agent's name
    (``codex``, ``claude``) and model id, recorded for a cloud attempt and in the
    audit of the send; required with ``cloud``."""

    def __init__(
        self,
        scheduler: ComputeScheduler,
        local: AgentRuntime,
        *,
        deployment: str,
        cloud: AgentRuntime | None = None,
        cloud_policy: CloudPolicy | None = None,
        cloud_agent: str | None = None,
        cloud_model: str | None = None,
        local_model: str | None = None,
        resource_class: ResourceClass = ResourceClass.CODING,
        estimate: Callable[[NodeAssignment], int] = estimate_context_tokens,
        wait_seconds: float = DEFAULT_NODE_WAIT_SECONDS,
        cloud_after_seconds: float = 0.0,
        charge_gpu_seconds: bool = True,
        late_gpu_charge: "LateGpuCharge | None" = None,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(scheduler, ComputeScheduler):
            raise TypeError("scheduler must be a ComputeScheduler")
        validate_runtime(local, "local")
        if cloud is not None:
            validate_runtime(cloud, "cloud")
            if cloud_policy is None:
                raise TypeError("a cloud runtime needs a cloud_policy")
            if cloud_agent is None or cloud_model is None:
                raise TypeError("a cloud runtime needs a cloud_agent and cloud_model")
        if cloud_agent is not None:
            check_agent_label("cloud_agent", cloud_agent)
        if cloud_model is not None:
            check_model("cloud_model", cloud_model)
        if cloud_policy is not None:
            require_async_method(cloud_policy, "allows", 1)
        if not isinstance(resource_class, ResourceClass) or resource_class in (
            ResourceClass.EXCLUSIVE,
        ):
            raise ValueError("resource_class")
        if not callable(estimate):
            raise TypeError("estimate must be callable")
        if late_gpu_charge is not None:
            require_async_method(late_gpu_charge, "charge", 2)
        # Checked the way the scheduler checks them.
        ComputeRequest(resource_class, deployment=deployment)
        self._scheduler = scheduler
        self._scheduler.parallelism(deployment, 0, resource_class)  # a known model
        self._local = local
        self._cloud = cloud
        self._policy = cloud_policy
        self._cloud_agent = cloud_agent
        self._cloud_model = cloud_model
        self._deployment = deployment
        self._local_model = check_model(
            "local_model", deployment if local_model is None else local_model
        )
        self._class = resource_class
        self._estimate = estimate
        self._wait = check_seconds("wait_seconds", wait_seconds)
        self._cloud_after = check_seconds("cloud_after_seconds", cloud_after_seconds)
        self._charge = bool(charge_gpu_seconds)
        self._late = late_gpu_charge
        self._clock = clock or SystemClock()

    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome:
        allow_cloud = await self._cloud_allowed(assignment)
        tokens = self._estimate(assignment)
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            return NodeOutcome.failed(COMPUTE_UNAVAILABLE, retryable=False)
        request = ComputeRequest(
            self._class,
            deployment=self._deployment,
            context_tokens=min(tokens, _MAX_ESTIMATE),
            allow_cloud=allow_cloud,
        )
        try:
            lease = await self._scheduler.acquire(
                request, wait_seconds=self._wait, cloud_after_seconds=self._cloud_after
            )
        except ComputeUnavailableError:
            return NodeOutcome.failed(COMPUTE_UNAVAILABLE, retryable=True)
        seconds = 0
        outcome = None
        meter: _GpuMeter | None = None
        run = object()
        held: list[asyncio.Future] = []  # the local call, when it outlives us
        try:
            async with lease:
                if lease.placement is Placement.CLOUD:
                    # Asked again: the permission or the quota may have changed
                    # while the node waited for the local GPU.
                    if not await self._cloud_allowed(assignment):
                        return NodeOutcome.failed(COMPUTE_UNAVAILABLE, retryable=True)
                    # The placement and the audit of the send, before anything
                    # leaves the backend; not on record, not sent.
                    placed = await self._place(
                        assignment,
                        ExecutionPlacement.CLOUD,
                        self._cloud_agent,
                        self._cloud_model,
                    )
                    if placed is None:
                        return NodeOutcome.failed(COMPUTE_UNAVAILABLE, retryable=True)
                    return await self._cloud.run_node(placed)
                # The GPU time first: a node that cannot start locally (its
                # budget is spent) is not on record as having run there.
                meter = await self._join_meter(assignment, run)
                placed = await self._place(
                    assignment,
                    ExecutionPlacement(lease.placement.value),
                    assignment.agent,
                    self._local_model,
                )
                if placed is None:
                    outcome = _UNPLACED
                else:
                    started = self._clock.monotonic()
                    try:
                        outcome = await self._run_local(placed, lease, meter, held)
                    finally:
                        now = self._clock.monotonic()
                        seconds = math.ceil(max(0.0, now - started))
                        if held:
                            # Still running (it did not stop when it was cancelled):
                            # the meter keeps counting it, and the time it goes on
                            # using is charged when it ends.
                            self._meter_held(assignment, meter, held[0], now)
                        if meter is not None:
                            meter.stop(run, now)
        except BaseException:
            # The GPU time was spent although the node failed or was cancelled:
            # charged too, or failing nodes that are retried would bypass the
            # budget's max GPU time. The runtime's error is what propagates.
            with contextlib.suppress(Exception):
                await self._settle(assignment, meter, run, seconds)
            raise
        # Raises NodeStopped when the budget is now used up: it passes.
        await self._settle(assignment, meter, run, seconds)
        if outcome is _UNPLACED:
            # Its placement could not be recorded: it did not run (fail closed).
            return NodeOutcome.failed(COMPUTE_UNAVAILABLE, retryable=True)
        if outcome is _REVOKED:
            # The scheduler took the GPU back (VRAM pressure on Background work):
            # not now, the node may run again later.
            return NodeOutcome.failed(COMPUTE_UNAVAILABLE, retryable=True)
        if outcome is _EXHAUSTED:
            # The task's GPU time was spent and the local runtime was stopped:
            # the node stops on the budget (the charge may already have said so).
            raise NodeStopped(StopReason.BUDGET_EXCEEDED)
        return outcome

    async def _place(
        self,
        assignment: NodeAssignment,
        placement: ExecutionPlacement,
        agent: str,
        model: str,
    ) -> NodeAssignment | None:
        """Record where the attempt runs (issue #133) and return the assignment
        for the runtime that runs it there. ``None``: it could not be recorded,
        and the node must not run there. An assignment without ``placement`` (a
        planner call, a caller that records nothing) runs locally unrecorded; it
        never reaches the cloud (``_cloud_allowed``). ``NodeStopped`` passes.

        The returned assignment's ``placement`` is already on record: the chosen
        runtime may record the same place, agent and model again (the
        ``NodePlacement`` contract asks a runtime that sends to the cloud to
        record first), and is refused any other."""
        recorder = assignment.placement
        if recorder is None:
            return None if placement is ExecutionPlacement.CLOUD else assignment
        try:
            await recorder.record(placement, agent=agent, model=model)
        except NodeStopped:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "The placement of a node could not be recorded: it does not run"
            )
            return None
        return dataclasses.replace(
            assignment, placement=_Recorded(placement, agent, model)
        )

    async def _cloud_allowed(self, assignment: NodeAssignment) -> bool:
        if self._cloud is None or self._policy is None:
            return False
        if assignment.placement is None:
            # Nothing could record the send (Decision 0037's 14): not to the cloud.
            return False
        try:
            return await self._policy.allows(assignment) is True
        except asyncio.CancelledError:
            raise
        except Exception:
            # A policy that cannot answer keeps the node local.
            logger.warning("The cloud policy failed: the node stays local")
            return False

    async def _join_meter(
        self, assignment: NodeAssignment, run: object
    ) -> "_GpuMeter | None":
        """The task's GPU time meter with this call in it (``None``: the task's
        ``GPU_SECONDS`` is unlimited, or this runtime does not charge it). Raises
        ``NodeStopped(BUDGET_EXCEEDED)`` when nothing is left, counting what the
        task's other local calls in this process have used and not charged yet.
        The budget is read under the meter's lock, which a call also holds while
        it charges and leaves: the figure read never has a charge whose call is
        still counted by the meter."""
        if not self._charge:
            return None
        meters = _METERS.setdefault(self._scheduler, {})
        meter = meters.get(assignment.task_id)
        if meter is None:
            meter = meters[assignment.task_id] = _GpuMeter()
        meter.users += 1
        joined = False
        try:
            async with meter.lock:
                left = (await assignment.budget.remaining()).get(BudgetKind.GPU_SECONDS)
                if isinstance(left, bool) or not isinstance(left, int):
                    return None
                meter.base = left
                now = self._clock.monotonic()
                if meter.left(now) <= _EPSILON:
                    raise NodeStopped(StopReason.BUDGET_EXCEEDED)
                meter.start(run, now)
                joined = True
                return meter
        finally:
            if not joined:
                self._drop_user(assignment, meter)

    async def _settle_charge(
        self,
        assignment: NodeAssignment,
        meter: "_GpuMeter | None",
        run: object,
        seconds: int,
        *,
        late: bool = False,
    ) -> None:
        """Charge the call's GPU time and take it out of the meter, together.
        ``late``: the call ended after its node (see ``_meter_held``)."""
        if meter is None:
            if self._charge and seconds > 0:
                await self._charge_gpu(assignment, seconds, late)
            return
        try:
            async with meter.lock:
                try:
                    if seconds > 0:
                        await self._charge_gpu(assignment, seconds, late)
                finally:
                    meter.finish(run, seconds)
        finally:
            if run in meter.runs:  # the lock was never taken (cancelled)
                meter.finish(run, seconds)
            self._drop_user(assignment, meter)

    async def _settle(
        self,
        assignment: NodeAssignment,
        meter: "_GpuMeter | None",
        run: object,
        seconds: int,
    ) -> None:
        """``_settle_charge`` in a task of its own: a cancellation that reaches
        the node while the charge is being written (a task stop and a shutdown at
        once) does not interrupt it; the charge completes and the time stays
        counted by the meter until then."""
        settle = asyncio.ensure_future(
            self._settle_charge(assignment, meter, run, seconds)
        )
        _CHARGES.add(settle)
        settle.add_done_callback(_charge_done)
        await asyncio.shield(settle)

    async def _charge_gpu(
        self, assignment: NodeAssignment, seconds: int, late: bool
    ) -> None:
        if late and self._late is not None:
            # The attempt is closed by now (the orchestrator refuses its node's
            # charges once it stops waiting for it): the time is charged to the
            # task through the tracker, as work that happened.
            await self._late.charge(assignment.task_id, seconds)
            return
        try:
            await assignment.budget.charge(BudgetKind.GPU_SECONDS, seconds)
        except NodeStopped as stopped:
            # The attempt closed while the charge was on its way (the node was
            # cancelled meanwhile): the time was used all the same.
            if stopped.reason is not StopReason.ABANDONED or self._late is None:
                raise
            await self._late.charge(assignment.task_id, seconds)

    def _meter_held(
        self,
        assignment: NodeAssignment,
        meter: "_GpuMeter | None",
        work: asyncio.Future,
        since: float,
    ) -> None:
        """Keep metering ``work`` (a local call that did not stop when it was
        cancelled; it holds its lease until it ends) from ``since``, and charge
        that time when it ends: GPU time it goes on using is not lost to the
        budget, and the task's other local calls see it spent meanwhile."""
        run = object()
        if meter is not None:
            meter.users += 1
            meter.start(run, since)

        def ended(_: asyncio.Future) -> None:
            end = self._clock.monotonic()
            if meter is not None:
                meter.stop(run, end)
            seconds = math.ceil(max(0.0, end - since))
            charge = asyncio.ensure_future(
                self._settle_charge(assignment, meter, run, seconds, late=True)
            )
            _CHARGES.add(charge)
            charge.add_done_callback(_charge_done)

        work.add_done_callback(ended)

    def _drop_user(self, assignment: NodeAssignment, meter: "_GpuMeter") -> None:
        meter.users -= 1
        meters = _METERS.get(self._scheduler, {})
        if meter.users <= 0 and meters.get(assignment.task_id) is meter:
            del meters[assignment.task_id]

    async def _run_local(
        self,
        assignment: NodeAssignment,
        lease: ComputeLease,
        meter: "_GpuMeter | None",
        held: list[asyncio.Future],
    ):
        """The local runtime. It is stopped when the scheduler revokes the lease
        (``_REVOKED``: VRAM pressure on Background work) and when the task's local
        calls together have used the GPU time that was left (``_EXHAUSTED``): the
        budget is checked while a call runs, not only after it, so slow or hung
        calls (one, or several of the same task at once) cannot overrun it
        without bound. A call that does not stop when it is cancelled keeps its
        lease until it ends (its GPU capacity is not given away) and is put in
        ``held`` (its time is metered and charged until it ends)."""
        work = asyncio.ensure_future(self._local.run_node(assignment))
        try:
            while True:
                helpers = [asyncio.ensure_future(lease.revoked.wait())]
                if meter is not None:
                    left = meter.left(self._clock.monotonic())
                    if left <= _EPSILON:
                        helpers[0].cancel()
                        return _EXHAUSTED
                    helpers.append(
                        asyncio.ensure_future(
                            self._clock.sleep(left / max(1, meter.running))
                        )
                    )
                    helpers.append(asyncio.ensure_future(meter.changed.wait()))
                try:
                    await asyncio.wait(
                        {work, *helpers}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for helper in helpers:
                        helper.cancel()
                if work.done():
                    return work.result()
                if lease.revoked.is_set():
                    return _REVOKED
        finally:
            try:
                await _cancel_and_wait(work, lease)
            finally:
                if not work.done():
                    held.append(work)


class _Recorded:
    """The ``placement`` that ``HybridRuntime`` hands the runtime it chose: the
    attempt's placement is already on record. Recording the same place, agent
    and model again is accepted (it is on record); any other is refused, as the
    orchestrator refuses a second placement."""

    __slots__ = ("_placement",)

    def __init__(self, placement: ExecutionPlacement, agent: str, model: str) -> None:
        self._placement = (placement, agent, model)

    async def record(
        self, placement: ExecutionPlacement, *, agent: str, model: str
    ) -> None:
        if (placement, agent, model) != self._placement:
            raise InvalidOrchestratorArgumentError("placement")


class _GpuMeter:
    """The local calls of one task in this process that hold the GPU and whose
    time is not charged yet. ``base`` is the task's ``GPU_SECONDS`` left as the
    budget last said; ``left`` takes off what the calls have used since."""

    def __init__(self) -> None:
        self.base = 0.0
        self.runs: dict[object, list[float | None]] = {}  # run: [start, end]
        self.changed = asyncio.Event()
        self.lock = asyncio.Lock()
        self.users = 0  # the calls joining or in the meter

    @property
    def running(self) -> int:
        return sum(1 for _, end in self.runs.values() if end is None)

    def left(self, now: float) -> float:
        spent = sum(
            (now if end is None else end) - start for start, end in self.runs.values()
        )
        return self.base - spent

    def _notify(self) -> None:
        # The calls that wait re-read ``left`` and the rate at which it falls.
        self.changed.set()
        self.changed = asyncio.Event()

    def start(self, run: object, now: float) -> None:
        self.runs[run] = [now, None]
        self._notify()

    def stop(self, run: object, now: float) -> None:
        if run in self.runs and self.runs[run][1] is None:
            self.runs[run][1] = now
            self._notify()

    def finish(self, run: object, seconds: int) -> None:
        # Its time is now in the budget (or was lost with a failed charge: still
        # taken off, the safe side).
        self.runs.pop(run, None)
        self.base -= seconds
        self._notify()


# The meters, by scheduler and task (the scheduler is one per process,
# Decision 0037's 1).
_METERS: "weakref.WeakKeyDictionary[ComputeScheduler, dict[uuid.UUID, _GpuMeter]]" = (
    weakref.WeakKeyDictionary()
)
_EPSILON = 1e-6
# The GPU time charges being written (see ``_settle`` and ``_meter_held``), kept
# until they are done.
_CHARGES: "set[asyncio.Future]" = set()


def _charge_done(charge: asyncio.Future) -> None:
    _CHARGES.discard(charge)
    if charge.cancelled():
        return
    error = charge.exception()
    if error is None:
        return
    if isinstance(error, NodeStopped) and error.reason is StopReason.BUDGET_EXCEEDED:
        return  # charged, and the budget is now used up: the node has ended
    # Without a ``late_gpu_charge`` the node's budget refuses a charge once its
    # attempt is closed: the time is lost to the budget.
    logger.error(
        "The GPU time of a local call was not charged (%s)", type(error).__name__
    )


class LateGpuCharge(Protocol):
    """Charges GPU time to a task after its node's attempt has closed."""

    async def charge(self, task_id: uuid.UUID, seconds: int) -> None: ...


class TrackerLateGpuCharge:
    """A :class:`LateGpuCharge` backed by the task budget tracker.

    Not fenced by the attempt or the run (like the Broker's charge of a tool
    call that already ran): it records GPU time that was used, by a local call
    that did not stop when its node was cancelled and held its lease until it
    ended. Hiding it would let such calls use the GPU beyond the task's
    ``GPU_SECONDS`` unseen."""

    def __init__(self, tracker: object) -> None:
        require_async_method(tracker, "record", 3)
        self._tracker = tracker

    async def charge(self, task_id: uuid.UUID, seconds: int) -> None:
        await self._tracker.record(task_id, BudgetKind.GPU_SECONDS, seconds)


class ScheduledMemoryWorker:
    """A Memory Worker that runs only when the scheduler has room (see the module)."""

    def __init__(
        self,
        worker: object,
        scheduler: ComputeScheduler,
        *,
        deployment: str,
        resource_class: ResourceClass = ResourceClass.BACKGROUND,
        output_tokens: int = DEFAULT_MEMORY_OUTPUT_TOKENS,
    ) -> None:
        check_worker(worker)
        if not isinstance(scheduler, ComputeScheduler):
            raise TypeError("scheduler must be a ComputeScheduler")
        ComputeRequest(resource_class, deployment=deployment)
        scheduler.parallelism(deployment, 0, resource_class)  # a known model
        self._worker = worker
        self._scheduler = scheduler
        self._deployment = deployment
        self._class = resource_class
        self._output = output_tokens

    async def extract(self, input_text: str) -> str:
        tokens = _tokens(len(str(input_text).encode("utf-8"))) + self._output
        admission = await self._scheduler.try_acquire(
            ComputeRequest(
                self._class,
                deployment=self._deployment,
                context_tokens=min(tokens, _MAX_ESTIMATE),
            )
        )
        if admission.refusal in PERMANENT_REFUSALS:
            # Waiting cannot help: a failed attempt (it reaches the dead letter),
            # not "unavailable" (that would defer the job for ever).
            raise ComputeUnavailableError(admission.refusal)
        if admission.lease is None:
            raise WorkerUnavailableError()
        async with admission.lease as lease:
            # A revoked lease (VRAM pressure on Background work, or the model is
            # about to be unloaded) stops the job: it is deferred, not failed,
            # and the relief steps are not held up by it.
            return await _until_revoked(
                self._worker.extract(input_text), lease, WorkerUnavailableError
            )


class PlacedEmbedder:
    """The GPU or the CPU copy of one embedding model, as the scheduler placed it."""

    def __init__(
        self,
        scheduler: ComputeScheduler,
        *,
        deployment: str,
        gpu: object,
        cpu: object | None = None,
        resource_class: ResourceClass = ResourceClass.SUPPORT,
    ) -> None:
        if not isinstance(scheduler, ComputeScheduler):
            raise TypeError("scheduler must be a ComputeScheduler")
        for embedder in (gpu, cpu):
            if embedder is not None:
                require_async_method(embedder, "embed", 1)
        if cpu is not None and (
            cpu.model_id != gpu.model_id or cpu.dimensions != gpu.dimensions
        ):
            raise ValueError("the CPU copy must be the same model")
        ComputeRequest(resource_class, deployment=deployment)
        scheduler.parallelism(deployment, 0, resource_class)  # a known model
        self._scheduler = scheduler
        self._deployment = deployment
        self._gpu = gpu
        self._cpu = cpu
        self._class = resource_class

    @property
    def model_id(self) -> str:
        return self._gpu.model_id

    @property
    def dimensions(self) -> int:
        return self._gpu.dimensions

    async def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Raises :class:`ComputeUnavailableError` when neither copy may run now
        (the retrieval then degrades, Decision 0019)."""
        size = sum(len(str(text).encode("utf-8")) for text in texts)
        admission = await self._scheduler.try_acquire(
            ComputeRequest(
                self._class,
                deployment=self._deployment,
                context_tokens=min(_tokens(size), _MAX_ESTIMATE),
            )
        )
        lease = admission.lease
        if lease is None:
            raise ComputeUnavailableError(admission.refusal)
        async with lease:
            if lease.placement is Placement.LOCAL_CPU:
                if self._cpu is None:
                    raise ComputeUnavailableError(Refusal.NOT_RESIDENT)
                return await self._cpu.embed(texts)
            # The GPU copy is about to be moved to the CPU or unloaded (a relief
            # step waits for its leases): a revoked lease stops the call, and the
            # retrieval degrades (Decision 0019).
            return await _until_revoked(
                self._gpu.embed(texts),
                lease,
                lambda: ComputeUnavailableError(Refusal.NOT_RESIDENT),
            )
