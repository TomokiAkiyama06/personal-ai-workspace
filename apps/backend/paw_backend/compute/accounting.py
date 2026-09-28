"""VRAM accounting (PAW-036): pure functions, no I/O.

The requirements ("VRAM accounting") say the decision is never made on the model
weights alone: the weights, the KV cache, CUDA graphs and runtime buffers, the
temporary workspace, the Memory Worker, the Embedding / Reranker models and a
safety reserve all count. Two quantities are kept apart:

* **actual**: what the probe sees used on the GPU (every process, the
  workspace's and everyone else's);
* **reserved**: what the scheduler has promised: the full footprint of every
  model it placed on the GPU (a model that is still loading holds its
  reservation before its memory shows up), plus an Exclusive job's reservation.

``committed`` is what admission counts: for each of the workspace's models the
larger of its reservation and what its processes actually use, plus everything
the probe sees that is not the workspace's (``external``: another user's
workload, a model that was unloaded but whose memory lingers). A model whose
processes are not known (no model control is configured, or it could not say)
is assumed to hold its reservation out of what the probe sees, the rest being
external; so is an Exclusive job (its processes are nobody's model). So is a
model whose known processes hold nothing on the GPU: the pids
given are then not the ones that hold its memory (the pids command named only
the parent of a runtime whose GPU memory sits in a child process, as vLLM and
SGLang do; or the model is still starting), and counting its reservation *and*
the memory the probe sees as external would count the model twice and start the
relief steps for pressure that is not there. A pid set that covers only part of
the model's processes cannot be told apart from another workload: the pids
command must list every process of the runtime (the unit's ``cgroup.procs``).
A model some of whose processes report no usage (``[N/A]``) holds what is left
of its reservation out of the rest in the same way.

So ``committed`` is never below the actual use nor below the
reservations: the scheduler cannot promise memory that is in use, and memory it
promised is not given away because it does not show yet.

``available = total - headroom - committed``; below zero the GPU is under
pressure and the relief steps start.
"""

import math
from collections.abc import Iterable
from dataclasses import dataclass

from paw_backend.compute.probe import GpuDevice, GpuProcess


@dataclass(frozen=True, slots=True)
class DeploymentUsage:
    """One of the workspace's models on the GPU: its reservation and the pids of
    its processes (``None``: not known)."""

    reserved_bytes: int
    pids: frozenset[int] | None


@dataclass(frozen=True, slots=True)
class VramView:
    total: int
    actual: int  # what the probe sees used
    reserved: int  # what the scheduler promised
    own_actual: int  # what the workspace's known processes use
    external: int  # what is used and is not the workspace's
    committed: int
    headroom: int
    available: int  # total - headroom - committed; negative under pressure
    # What the VRAM leases (an Exclusive job's, or the shared ones with
    # ``vram_bytes``) use, absorbed by their reservations (see account).
    extra_use: int = 0

    @property
    def under_pressure(self) -> bool:
        return self.available < 0

    @property
    def observed_free(self) -> int:
        """What the probe sees free on the GPU (Decision 0042)."""
        return self.total - self.actual

    @property
    def unseen_reserved(self) -> int:
        """What the scheduler promised that the probe does not show used yet (a
        model still loading, work that has not allocated its VRAM)."""
        return max(0, self.committed - self.actual)


def free_vram_admits(view: VramView, need: int) -> bool:
    """Decision 0042: work that allocates ``need`` bytes of VRAM of its own starts
    only when the probe shows that much free beyond the safety headroom, after
    what the scheduler already promised and the probe does not show yet.

    Only the observed free VRAM is used, never the GPU utilisation. The
    workspace's resident models are not counted twice: a runtime that allocated
    its whole footprint when it loaded (vLLM / SGLang reserve their KV cache pool)
    shows it as used, and work inside that footprint (``need`` 0) is not held back
    here (the relief steps of Decision 0037 handle pressure). As ``committed`` is
    never below what the probe sees used, this is ``available >= need``."""
    if need <= 0:
        return True
    return view.observed_free - view.unseen_reserved >= need + view.headroom


def free_vram_after_emptying(view: VramView) -> int:
    """What an Exclusive job could get once every model of the workspace is off
    the GPU: the GPU without the headroom and without what another workload
    holds (``external``). Decision 0042: when this is below what the job needs,
    unloading the models cannot help and the job waits instead."""
    return view.total - view.headroom - view.external


def headroom_bytes(total: int, *, minimum_bytes: int, fraction: float) -> int:
    """The safety headroom: the larger of ``minimum_bytes`` and ``fraction`` of the
    GPU, never more than the GPU."""
    return min(total, max(minimum_bytes, math.floor(total * fraction)))


def account(
    device: GpuDevice,
    processes: Iterable[GpuProcess],
    deployments: Iterable[DeploymentUsage],
    *,
    headroom: int,
    extra_reserved: int = 0,
    extra_baseline: int = 0,
) -> VramView:
    """The VRAM view of ``device`` (see the module)."""
    used_by: dict[int, int] = {}
    unknown_use: set[int] = set()  # on the GPU, but the usage is "[N/A]"
    for process in processes:
        if process.gpu_uuid != device.uuid:
            continue
        if process.used_bytes is None:
            unknown_use.add(process.pid)
        else:
            used_by[process.pid] = used_by.get(process.pid, 0) + process.used_bytes
    reserved = extra_reserved
    committed_own = extra_reserved
    own_actual = 0
    unknown_reserved = 0
    for usage in deployments:
        reserved += usage.reserved_bytes
        actual = (
            0 if usage.pids is None else sum(used_by.get(pid, 0) for pid in usage.pids)
        )
        if actual == 0:
            # Not known, or the known pids hold nothing: see the module.
            unknown_reserved += usage.reserved_bytes
            committed_own += usage.reserved_bytes
            continue
        own_actual += actual
        committed_own += max(usage.reserved_bytes, actual)
        if usage.pids & unknown_use:
            # Some of its processes report no figure: what they use shows in
            # the rest and is the model's, up to its reservation.
            unknown_reserved += max(0, usage.reserved_bytes - actual)
    rest = max(0, device.used_bytes - own_actual - unknown_reserved)
    # An Exclusive job's processes are no model's: what it uses shows in the
    # rest and is absorbed by its reservation, not counted a second time. Only
    # what grew over ``extra_baseline`` (what was external when the lease was
    # granted) is the job's: another workload that was already there stays
    # external beside the reservation.
    exclusive_use = min(extra_reserved, max(0, rest - max(0, extra_baseline)))
    external = rest - exclusive_use
    committed = committed_own + external
    return VramView(
        total=device.total_bytes,
        actual=device.used_bytes,
        reserved=reserved,
        own_actual=own_actual,
        external=external,
        committed=committed,
        headroom=headroom,
        available=device.total_bytes - headroom - committed,
        extra_use=exclusive_use,
    )
