"""The vocabulary of the Compute Resource Scheduler (PAW-036).

``REQUIREMENTS.md`` ("GPU / Compute Resource Scheduler", FIXED) names the five
resource classes, the model residency policy and the order in which VRAM pressure
is relieved; this module turns them into closed enums. The requirements are the
source; ``docs/decisions/0037-gpu-compute-scheduler.md`` (Approved) records the
choices they leave open.
"""

from enum import IntEnum, StrEnum


class ResourceClass(StrEnum):
    """What a request is for. Interactive and Coding work comes before Support and
    Background work; Exclusive is a job that needs the GPU to itself."""

    INTERACTIVE = "interactive"  # ordinary chat
    CODING = "coding"  # the local coding agent
    SUPPORT = "support"  # Memory Worker / Embedding / Reranker calls
    BACKGROUND = "background"  # memory consolidation, research refresh
    EXCLUSIVE = "exclusive"  # Kaggle, a Model Benchmark: the whole GPU


# Lower is served first. Exclusive is not queued with the others.
CLASS_RANK: dict[ResourceClass, int] = {
    ResourceClass.INTERACTIVE: 0,
    ResourceClass.CODING: 1,
    ResourceClass.SUPPORT: 2,
    ResourceClass.BACKGROUND: 3,
}
SHARED_CLASSES = tuple(CLASS_RANK)


class Placement(StrEnum):
    """Where admitted work runs."""

    LOCAL_GPU = "local_gpu"
    LOCAL_CPU = "local_cpu"
    CLOUD = "cloud"  # a Codex / Claude agent instead of the local model


class ModelRole(StrEnum):
    """What a deployed model is. The role fixes its place in the relief order."""

    MAIN = "main"  # the main (coding) LLM runtime, shared by every local agent
    MEMORY_WORKER = "memory_worker"
    EMBEDDING = "embedding"
    RERANKER = "reranker"


SUPPORT_ROLES = frozenset({ModelRole.EMBEDDING, ModelRole.RERANKER})
# The order in which models are loaded (and the reverse, unloaded).
ROLE_ORDER: dict[ModelRole, int] = {
    ModelRole.MAIN: 0,
    ModelRole.MEMORY_WORKER: 1,
    ModelRole.EMBEDDING: 2,
    ModelRole.RERANKER: 3,
}


class ResidencyPolicy(StrEnum):
    """``ALWAYS``: kept on the GPU (the main coding model); ``IF_ROOM``: loaded
    only while there is VRAM to spare (the Memory Worker, Embedding, Reranker)."""

    ALWAYS = "always"
    IF_ROOM = "if_room"


class DeploymentState(StrEnum):
    GPU = "gpu"
    CPU = "cpu"  # the CPU copy of an Embedding / Reranker serves requests
    UNLOADED = "unloaded"
    # An action failed: where the model is, is unknown. Its VRAM is counted as
    # still held (fail closed) and the scheduler tries again later.
    FAILED = "failed"


class Relief(IntEnum):
    """How far VRAM pressure has been relieved, in the order of the requirements.

    Each step keeps the ones before it. The steps are taken one per refresh while
    the pressure lasts and undone in reverse once there is room again.
    """

    NONE = 0
    BACKGROUND_STOPPED = 1  # 1. background GPU jobs are stopped
    MEMORY_WORKER_UNLOADED = 2  # 2. the Memory Worker is unloaded
    SUPPORT_ON_CPU = 3  # 3. Embedding / Reranker move to the CPU
    ADMISSION_SUPPRESSED = 4  # 4. new local requests are held back
    CONTEXT_REDUCED = 5  # 5. the context of new requests is reduced
    MAIN_CHANGE_NEEDED = 6  # 6. a human must change the main model's setup


class SchedulerMode(StrEnum):
    NORMAL = "normal"
    DRAINING = "draining"  # an Exclusive job waits for the GPU to empty
    EXCLUSIVE = "exclusive"  # an Exclusive job holds the GPU


class Refusal(StrEnum):
    """Why a request was not admitted now. A closed set of stable codes."""

    PROBE_UNAVAILABLE = "probe_unavailable"  # no fresh reading of the GPU
    NOT_RESIDENT = "not_resident"  # the model is not loaded (or is draining)
    BACKGROUND_PAUSED = "background_paused"
    ADMISSION_SUPPRESSED = "admission_suppressed"
    CONTEXT_TOO_LONG = "context_too_long"  # longer than the model ever takes
    CONTEXT_REDUCED = "context_reduced"  # longer than it takes under pressure
    KV_FULL = "kv_full"
    SEQUENCES_FULL = "sequences_full"
    EXCLUSIVE_MODE = "exclusive_mode"
    QUEUED_BEHIND = (
        "queued_behind"  # an earlier request of the same or a higher class waits
    )
    QUEUE_FULL = "queue_full"
    # Decision 0042: the work needs VRAM of its own (``vram_bytes``) and the
    # probe does not show that much free beyond the headroom (another workload,
    # one the scheduler does not manage, may hold it). The request waits.
    INSUFFICIENT_FREE_VRAM = "insufficient_free_vram"


# Refusals that waiting cannot change.
PERMANENT_REFUSALS = frozenset({Refusal.CONTEXT_TOO_LONG})
# Refusals for lack of capacity: a waiter refused for one of these holds back the
# waiters of the same model behind it (strict priority, FIFO within a class).
CAPACITY_REFUSALS = frozenset({Refusal.KV_FULL, Refusal.SEQUENCES_FULL})


class ExclusiveFailure(StrEnum):
    """Why an Exclusive lease was not granted."""

    BUSY = "busy"  # another Exclusive job holds or waits for the GPU
    PROBE_UNAVAILABLE = "probe_unavailable"  # the release could not be confirmed
    DRAIN_TIMEOUT = "drain_timeout"  # running local work did not end in time
    CANNOT_UNLOAD = "cannot_unload"  # no model control, or an unload failed
    NOT_FREED = "not_freed"  # the probe does not show the VRAM free
