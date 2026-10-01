"""Provisional values of the Compute Resource Scheduler (PAW-036).

The requirements leave the numbers to the Model / Runtime Benchmark ("Safety
Headroomの具体的なGB / %は要件定義段階では固定せず、Model Benchmark / Runtime
Benchmark後に決定する"). These are **provisional** values that only set the order
of magnitude for one 96 GB GPU; ``docs/decisions/0037-gpu-compute-scheduler.md``
(Approved) lists them, and ``0039-compute-scheduler-calibration.md`` (Approved)
checked them against the PAW-017 measurements. Every one of them is a field of
``ComputeConfig`` and can be changed there without touching this module; nothing
is written to a database.
"""

from types import MappingProxyType

from paw_backend.compute.domain import ResourceClass

GIB = 1024**3

# -- VRAM ----------------------------------------------------------------------
# The safety headroom is the larger of a fixed amount and a share of the GPU.
DEFAULT_HEADROOM_MIN_BYTES = 4 * GIB
DEFAULT_HEADROOM_FRACTION = 0.05
MAX_HEADROOM_FRACTION = 0.5

# -- KV cache ------------------------------------------------------------------
# The share of a model's KV pool that requests may reserve at all ...
DEFAULT_KV_SAFETY = 0.9
# ... and the share of that each class may fill: lower classes leave the rest
# to higher ones (Interactive / Coding before Support / Background).
DEFAULT_CLASS_KV_CEILINGS: dict[ResourceClass, float] = {
    ResourceClass.INTERACTIVE: 1.0,
    ResourceClass.CODING: 0.95,
    ResourceClass.SUPPORT: 0.85,
    ResourceClass.BACKGROUND: 0.70,
}
# Relief step 5: the longest context a new request may have, as a share of the
# model's maximum.
DEFAULT_PRESSURE_CONTEXT_FRACTION = 0.5

# -- the probe and the waiting line --------------------------------------------
DEFAULT_PROBE_MAX_AGE_SECONDS = 15.0  # an older reading admits no GPU work
DEFAULT_REFRESH_SECONDS = 5.0
DEFAULT_MAX_WAITERS = 256
DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0
MAX_COMMAND_TIMEOUT_SECONDS = 600.0  # the probe's commands

# Decision 0042: a warning that work waits for VRAM another workload
# holds is repeated at most this often for the same kind of work.
DEFAULT_VRAM_WARNING_INTERVAL_SECONDS = 300.0

# -- models --------------------------------------------------------------------
DEFAULT_FAILED_RETRY_SECONDS = 60.0  # a failed load is tried again after this
# One load / unload command. Decision 0039, 2 (Approved): a first load from the
# HDD took 412 s (gpt-oss-120b), so 900 s, about twice the longest measured.
DEFAULT_CONTROL_TIMEOUT_SECONDS = 900.0
# The ceiling of that setting (Codex P2 on #179: 900 s must still validate),
# twice the default; the probe's commands keep ``MAX_COMMAND_TIMEOUT_SECONDS``.
MAX_CONTROL_TIMEOUT_SECONDS = 1_800.0
DEFAULT_MAX_CONTEXT_TOKENS = 32_768

# -- Exclusive -----------------------------------------------------------------
DEFAULT_VERIFY_TIMEOUT_SECONDS = 60.0  # to see the VRAM free after the unloads
DEFAULT_VERIFY_POLL_SECONDS = 2.0

# -- Kaggle / Full GPU Mode (PAW-037, Decision 0055) ---------------------------
# How long running local GPU work may take to end before Full GPU Mode gives up
# (or, with ``preempt``, asks it to stop) ...
DEFAULT_FULL_GPU_DRAIN_SECONDS = 600.0
# ... how long the preempted work then has to stop and release ...
DEFAULT_FULL_GPU_PREEMPT_SECONDS = 60.0
# ... and how long the main LLM may take to come back after the end before a
# human is asked to look (the held tasks keep waiting for it).
DEFAULT_FULL_GPU_RELOAD_SECONDS = 900.0

# -- runtimes ------------------------------------------------------------------
# A node's context is estimated as its input in bytes / 3 (a conservative
# tokens-per-byte ratio for code and mixed Japanese / English text) plus a
# reserve for the answer.
BYTES_PER_TOKEN_ESTIMATE = 3
DEFAULT_OUTPUT_TOKENS = 8_192
DEFAULT_MEMORY_OUTPUT_TOKENS = 2_048
DEFAULT_NODE_WAIT_SECONDS = 600.0  # a node waits this long for local capacity

# -- the host (issue #182, Decision 0039, 4; the values: Decision 0072, Proposed)
# A GPU runtime is not started while the host's ``MemAvailable`` is below this:
# its first load may build JIT kernels (FlashInfer) with several GiB of host RAM
# per compiler process. On 2026-09-30 an unbounded build (27 ``cicc``, about
# 75 GiB) exhausted the host's memory and the machine rebooted.
DEFAULT_MIN_HOST_AVAILABLE_BYTES = 32 * GIB
# What the model commands run with: at most 4 JIT build jobs (each ``cicc`` took
# up to 4.4 GiB) and one ``nvcc`` thread per FlashInfer job.
DEFAULT_JIT_BUILD_ENV = MappingProxyType(
    {"MAX_JOBS": "4", "FLASHINFER_NVCC_THREADS": "1"}
)
