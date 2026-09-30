"""GPU / Compute Resource Scheduler (PAW-036).

Admission to the local models by resource class and KV cache, actual / reserved
VRAM with a safety headroom, model residency with the relief steps under VRAM
pressure (Memory Worker unload, Embedding / Reranker CPU fallback), the Exclusive
class, and Local / Cloud hybrid placement. See ``scheduler.py``,
``docs/decisions/0037-gpu-compute-scheduler.md`` (Approved) and, for the check
on the observed free VRAM, ``docs/decisions/0042-gpu-free-vram-admission.md``
(Proposed). Kaggle / Full GPU Mode (PAW-037) holds the local GPU tasks around an
Exclusive lease: see ``full_gpu.py``, ``holds.py`` and
``docs/decisions/0055-kaggle-full-gpu-mode.md``
(Proposed).

GPU safety: the probe only reads (``nvidia-smi --query-*``); models are loaded
and unloaded only through an injected :class:`ModelControl`, whose real adapter
runs the commands an administrator configured. Nothing here kills a process or
changes the GPU's clocks, persistence, power limits or MIG.
"""

from paw_backend.compute.accounting import VramView
from paw_backend.compute.alerts import DeferredWork, VramDeferral, VramWarningSink
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
    FullGpuModeStateError,
    FullGpuPermissionDeniedError,
    InvalidComputeArgumentError,
    ModelControlError,
    ProbeUnavailableError,
)
from paw_backend.compute.full_gpu import (
    HOLD_REASON,
    RESUME_REASON,
    FullGpuMode,
    FullGpuState,
    FullGpuStatus,
    ResumeReport,
    TaskHolds,
)
from paw_backend.compute.holds import PostgresTaskHolds
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
    LateGpuCharge,
    PlacedEmbedder,
    ScheduledMemoryWorker,
    TrackerLateGpuCharge,
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
    "HOLD_REASON",
    "RESUME_REASON",
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
    "DeferredWork",
    "DeploymentCommands",
    "DeploymentSpec",
    "DeploymentState",
    "DeploymentStatus",
    "ExclusiveFailure",
    "ExclusiveUnavailableError",
    "FullGpuMode",
    "FullGpuModeStateError",
    "FullGpuPermissionDeniedError",
    "FullGpuState",
    "FullGpuStatus",
    "GpuDevice",
    "GpuProbe",
    "GpuProcess",
    "GpuSample",
    "HybridRuntime",
    "InvalidComputeArgumentError",
    "LateGpuCharge",
    "ModelControl",
    "ModelControlError",
    "ModelRole",
    "NvidiaSmiProbe",
    "Placement",
    "PlacedEmbedder",
    "PostgresTaskHolds",
    "ProbeUnavailableError",
    "Refusal",
    "Relief",
    "ResidencyPolicy",
    "ResourceClass",
    "ResumeReport",
    "ScheduledMemoryWorker",
    "SchedulerMode",
    "TaskHolds",
    "TrackerLateGpuCharge",
    "VramDeferral",
    "VramView",
    "VramWarningSink",
    "estimate_context_tokens",
]
