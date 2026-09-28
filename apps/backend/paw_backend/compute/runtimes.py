"""What connects the scheduler to its callers (PAW-036).

* :class:`HybridRuntime` is an orchestrator ``AgentRuntime`` (PAW-034). It asks
  the scheduler for a lease before a node runs on the local model, so the number
  of local nodes that run at once follows the KV cache and not the static
  ``max_parallel_nodes`` alone (that stays the upper bound, Decision 0021). When
  the local GPU is busy and the injected :class:`CloudPolicy` allows it for the
  node, the node runs on the cloud runtime instead (Local / Cloud hybrid). The
  policy is the caller's: the requirements allow the cloud only "within the
  task's dependency / permission / quota", which the backend knows and the
  scheduler does not. Without a policy nothing goes to the cloud. The wall time a
  node holds a local lease is charged to the task as ``GPU_SECONDS`` (the budget's
  "max GPU time").
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

import json
import math
from collections.abc import Callable, Sequence
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
    ComputeRequest,
    ComputeScheduler,
    check_seconds,
)
from paw_backend.memory.journal.errors import WorkerUnavailableError
from paw_backend.memory.journal.worker import check_worker
from paw_backend.orchestrator.config import Clock, SystemClock
from paw_backend.orchestrator.result import upstream_size
from paw_backend.orchestrator.runtime import (
    AgentRuntime,
    NodeAssignment,
    NodeOutcome,
    validate_runtime,
)
from paw_backend.tasks.queueing import BudgetKind
from paw_backend.tools.interfaces import require_async_method

COMPUTE_UNAVAILABLE = "ComputeUnavailable"
_MAX_ESTIMATE = 1 << 24


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
    """Local first, the cloud when the local GPU is busy (see the module)."""

    def __init__(
        self,
        scheduler: ComputeScheduler,
        local: AgentRuntime,
        *,
        deployment: str,
        cloud: AgentRuntime | None = None,
        cloud_policy: CloudPolicy | None = None,
        resource_class: ResourceClass = ResourceClass.CODING,
        estimate: Callable[[NodeAssignment], int] = estimate_context_tokens,
        wait_seconds: float = DEFAULT_NODE_WAIT_SECONDS,
        cloud_after_seconds: float = 0.0,
        charge_gpu_seconds: bool = True,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(scheduler, ComputeScheduler):
            raise TypeError("scheduler must be a ComputeScheduler")
        validate_runtime(local, "local")
        if cloud is not None:
            validate_runtime(cloud, "cloud")
            if cloud_policy is None:
                raise TypeError("a cloud runtime needs a cloud_policy")
        if cloud_policy is not None:
            require_async_method(cloud_policy, "allows", 1)
        if not isinstance(resource_class, ResourceClass) or resource_class in (
            ResourceClass.EXCLUSIVE,
        ):
            raise ValueError("resource_class")
        if not callable(estimate):
            raise TypeError("estimate must be callable")
        # Checked the way the scheduler checks them.
        ComputeRequest(resource_class, deployment=deployment)
        self._scheduler = scheduler
        self._scheduler.parallelism(deployment, 0, resource_class)  # a known model
        self._local = local
        self._cloud = cloud
        self._policy = cloud_policy
        self._deployment = deployment
        self._class = resource_class
        self._estimate = estimate
        self._wait = check_seconds("wait_seconds", wait_seconds)
        self._cloud_after = check_seconds("cloud_after_seconds", cloud_after_seconds)
        self._charge = bool(charge_gpu_seconds)
        self._clock = clock or SystemClock()

    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome:
        allow_cloud = False
        if self._cloud is not None and self._policy is not None:
            allow_cloud = await self._policy.allows(assignment) is True
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
        async with lease:
            if lease.placement is Placement.CLOUD:
                return await self._cloud.run_node(assignment)
            started = self._clock.monotonic()
            outcome = await self._local.run_node(assignment)
            seconds = math.ceil(max(0.0, self._clock.monotonic() - started))
        if self._charge and seconds > 0:
            # Raises NodeStopped when the budget is now used up: it passes.
            await assignment.budget.charge(BudgetKind.GPU_SECONDS, seconds)
        return outcome


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
        async with admission.lease:
            return await self._worker.extract(input_text)


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
            return await self._gpu.embed(texts)
