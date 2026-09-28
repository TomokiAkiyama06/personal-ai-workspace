"""The ``placement`` of a ``NodeAssignment``: where an attempt runs (issue #133).

Decision 0037's 14 (approved 2026-09-28): the orchestrator's record must say
where each node actually ran (the local GPU or CPU, or a cloud agent) and on
which agent and model, and a node sent to the cloud must leave an audit of the
external send, before any ``CloudPolicy`` is injected. The runtime knows where
it runs the node (``HybridRuntime`` holds the scheduler's lease); the
orchestrator owns the record. :class:`NodePlacementHandle` is the seam:

* the runtime calls ``record(placement, agent=, model=)`` once, BEFORE the node
  runs there;
* the handle writes it to the attempt's row (``DagStore.record_placement``),
  fenced like the attempt's outcome (the epoch, the task's run, the attempt);
* for ``CLOUD`` it also computes, from what the orchestrator handed the node
  (never from the runtime), the SHA-256 and size of the content, and the store
  appends the send's ``audit_events`` row in the same transaction
  (``orchestrator/audit.py``). When ``record`` raises, the runtime must not
  send (fail closed).

An abandoned attempt (``AttemptFence``) and a stopped run record nothing.
"""

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from paw_backend.orchestrator.audit import CloudSend
from paw_backend.orchestrator.domain import ExecutionPlacement, NodeRole
from paw_backend.orchestrator.errors import (
    InvalidOrchestratorArgumentError,
    NodeStopped,
)
from paw_backend.orchestrator.gateway import AttemptFence, RunGuard
from paw_backend.orchestrator.result import NodeResult
from paw_backend.orchestrator.store import DagStore
from paw_backend.tasks import StaleRunError


def content_digest(
    *,
    node_key: str,
    role: NodeRole,
    title: str,
    goal: str,
    input: Mapping[str, object],
    upstream: Mapping[str, NodeResult | None],
) -> tuple[str, int]:
    """The ``sha256:`` fingerprint and the UTF-8 size of a node's content: its
    key, role, title, goal, input and the results of its dependencies, as
    compact JSON with sorted keys (the same content always gives the same
    fingerprint)."""
    content = {
        "node_key": node_key,
        "role": role.value,
        "title": title,
        "goal": goal,
        "input": input,
        "upstream": {
            key: None if result is None else result.to_json()
            for key, result in upstream.items()
        },
    }
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest(), len(encoded)


class NodePlacementHandle:
    """The ``placement`` of one attempt of one DAG node (see the module)."""

    def __init__(
        self,
        guard: RunGuard,
        store: DagStore,
        *,
        dag_id: uuid.UUID,
        epoch: int,
        node_key: str,
        attempt: int,
        agent_id: uuid.UUID,
        content: tuple[str, int],
        fence: AttemptFence | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._guard = guard
        self._store = store
        self._dag_id = dag_id
        self._epoch = epoch
        self._key = node_key
        self._attempt = attempt
        self._agent_id = agent_id
        self._content = content
        self._fence = fence or AttemptFence()
        self._now = now or (lambda: datetime.now(UTC))
        self._recorded = False

    async def record(
        self, placement: ExecutionPlacement, *, agent: str, model: str
    ) -> None:
        self._fence.ensure_open()
        if self._guard.stop_reason is not None:
            raise NodeStopped(self._guard.stop_reason)
        if self._recorded:
            raise InvalidOrchestratorArgumentError("placement")
        cloud = None
        if placement is ExecutionPlacement.CLOUD:
            fingerprint, size = self._content
            cloud = CloudSend(
                content_fingerprint=fingerprint,
                content_bytes=size,
                agent_id=self._agent_id,
                occurred_at=self._now(),
            )
        try:
            await self._store.record_placement(
                self._dag_id,
                self._epoch,
                self._key,
                self._attempt,
                placement=placement,
                agent=agent,
                model=model,
                cloud=cloud,
            )
        except StaleRunError:
            await self._guard.refused()
        self._recorded = True
        # The attempt may have been abandoned while the write was on its way:
        # the record stays (the node was placed), but nothing more may act.
        self._fence.ensure_open()
