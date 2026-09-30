"""The lease seam: does the worker that asks still hold its queue lease?

Issue #126, Decision 0046 (the Human decided it on 2026-09-28). A worker of the
orchestrator runs a task only while it holds the task's queue lease
(``paw_backend.tasks.queueing.TaskQueue``). Its heartbeats notice a lost lease only
at the next heartbeat, and a take-over keeps the task's run the same, so the
run check (``task_state.py``) cannot tell the stale worker from its replacement.
So a tool call carries the worker's lease (``TaskContext.lease``: the queue
entry, the worker id and the claim generation, ``QueueLease``) and the broker
asks, **for every call**, whether that lease is still valid, before it allows the
call or opens / uses an approval for it. A worker whose lease was lost, expired
or taken over is refused (``lease_lost``), and so is every call whose answer
cannot be had (``lease_unavailable``: no verifier, an error, a timeout, an answer
that is not a ``LeaseStatus``): fail closed.

The check is a read at one instant (the database clock): it tells whether the
lease was valid when the call was decided, not that it stays valid while the
executor runs. The orchestrator's heartbeats give a lease up before it can
expire and then cancel the running nodes (Decision 0021, section 8).

The default verifier knows no lease, so every call is refused until a real one
(``orchestrator.gateway.QueueLeaseVerifier``) is installed, as with
``BudgetProvider`` and ``TaskActivityProvider``.
"""

import uuid
from enum import StrEnum
from typing import Protocol

from paw_backend.tasks.queueing import QueueLease


class LeaseStatus(StrEnum):
    HELD = "held"  # the lease is valid now: the worker may act for the task
    LOST = "lost"  # expired, released, completed, claimed again or never this
    UNKNOWN = "unknown"  # cannot be said (no verifier): treated as a refusal


class LeaseVerifier(Protocol):
    async def check(self, task_id: uuid.UUID, lease: QueueLease) -> LeaseStatus:
        """Is ``lease`` a valid lease on an entry of ``task_id`` NOW? Must read
        the current state (never a cached answer) and change nothing."""
        ...


class FailClosedLeaseVerifier:
    """The default: no lease is known, so no call may run."""

    async def check(self, task_id: uuid.UUID, lease: QueueLease) -> LeaseStatus:
        return LeaseStatus.UNKNOWN
