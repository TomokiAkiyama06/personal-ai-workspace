"""GPU / Compute Resource Scheduler (PAW-036).

Admission to the local models by resource class and KV cache, actual / reserved
VRAM with a safety headroom, model residency with the relief steps under VRAM
pressure (Memory Worker unload, Embedding / Reranker CPU fallback), the Exclusive
class, and Local / Cloud hybrid placement. See ``scheduler.py`` and
``docs/decisions/0037-gpu-compute-scheduler.md`` (Proposed).

GPU safety: the probe only reads (``nvidia-smi --query-*``); models are loaded
and unloaded only through an injected :class:`ModelControl`, whose real adapter
runs the commands an administrator configured. Nothing here kills a process or
changes the GPU's clocks, persistence, power limits or MIG.
"""

from paw_backend.compute.accounting import VramView
from paw_backend.compute.config import ComputeConfig, DeploymentSpec
from paw_backend.compute.control import (
    CommandModelControl,
    DeploymentCommands,
    ModelControl,
)
from paw_backend.compute.domain import (
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
    ComputeError,
    ComputeUnavailableError,
    ExclusiveUnavailableError,
    InvalidComputeArgumentError,
    ModelControlError,
    ProbeUnavailableError,
)
from paw_backend.compute.probe import (
    GpuDevice,
    GpuProbe,
    GpuProcess,
    GpuSample,
    NvidiaSmiProbe,
)
from paw_backend.compute.runtimes import (
    CloudPolicy,
    HybridRuntime,
    PlacedEmbedder,
    ScheduledMemoryWorker,
    estimate_context_tokens,
)
from paw_backend.compute.scheduler import (
    Admission,
    ComputeLease,
    ComputeRequest,
    ComputeScheduler,
    ComputeStatus,
    DeploymentStatus,
)

__all__ = [
    "Admission",
    "CloudPolicy",
    "CommandModelControl",
    "ComputeConfig",
    "ComputeError",
    "ComputeLease",
    "ComputeRequest",
    "ComputeScheduler",
    "ComputeStatus",
    "ComputeUnavailableError",
    "DeploymentCommands",
    "DeploymentSpec",
    "DeploymentState",
    "DeploymentStatus",
    "ExclusiveFailure",
    "ExclusiveUnavailableError",
    "GpuDevice",
    "GpuProbe",
    "GpuProcess",
    "GpuSample",
    "HybridRuntime",
    "InvalidComputeArgumentError",
    "ModelControl",
    "ModelControlError",
    "ModelRole",
    "NvidiaSmiProbe",
    "Placement",
    "PlacedEmbedder",
    "ProbeUnavailableError",
    "Refusal",
    "Relief",
    "ResidencyPolicy",
    "ResourceClass",
    "ScheduledMemoryWorker",
    "SchedulerMode",
    "VramView",
    "estimate_context_tokens",
]
