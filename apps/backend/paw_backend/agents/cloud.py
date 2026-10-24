"""The order of an external send (Decision 0083, 1: ``CloudGate``).

A CLI runtime (a later stage) sends a node to a provider only through this gate:

1. ``CloudPolicy.allows(assignment)``: the task's permission to send its content
   out, the user's quota, the node's dependencies (no policy: nothing is sent,
   Decision 0037, 14);
2. the admission of ``ConnectionService.execute_session`` (credential, quota,
   lease, the session's token floor), through the attempt's
   ``NodeAssignment.connections``;
3. ``placement.record(CLOUD, agent=..., model=...)`` (Decision 0048: with the
   audit of the send), run by the service between the admission and the
   adapter;
4. the adapter.

The record's transaction may ask the policy again (Decision 0099, 2: the
single-user recount, ``CloudSendRefusedError``): a refusal there is
``CLOUD_POLICY_DENIED`` as well.

If any of them refuses, nothing is sent and no placement is recorded (a refused
placement closes the admitted row ``cancelled``). An attempt without a placement
or without connections is refused: fail closed. A refusal is
a :class:`~.failures.RuntimeFailure` of the closed table; the runtime turns it
into its outcome (``escalate`` on its own rung, ``ComputeUnavailable`` on
``HybridRuntime``'s fallback path: ``ErrorClassifier.outcome(failure,
fallback=...)``).
"""

from paw_backend.agents.failures import Failure, RuntimeFailure
from paw_backend.connections import (
    ConnectionBusyError,
    ConnectionKind,
    ConnectionUnavailableError,
    QuotaExceededError,
    SessionRequest,
    SessionResult,
    TaskBudgetError,
    TaskNotUsableError,
)
from paw_backend.orchestrator.domain import ExecutionPlacement
from paw_backend.orchestrator.errors import CloudSendRefusedError
from paw_backend.orchestrator.runtime import NodeAssignment
from paw_backend.tools.interfaces import require_async_method


class CloudGate:
    """See the module. ``policy``: the deployment's ``CloudPolicy`` (``None``:
    every send is refused)."""

    def __init__(self, policy: object | None = None) -> None:
        if policy is not None:
            require_async_method(policy, "allows", 1)
        self._policy = policy

    async def session(
        self,
        assignment: NodeAssignment,
        *,
        kind: ConnectionKind,
        request: SessionRequest,
        agent: str,
        model: str,
    ) -> SessionResult:
        """Run ``request`` as a session of ``kind`` for ``assignment`` through
        the four steps, or ``RuntimeFailure`` (nothing sent)."""
        # Fail closed: only an exact ``True`` allows a send (as
        # ``HybridRuntime``'s own check of the policy).
        if self._policy is None or await self._policy.allows(assignment) is not True:
            raise RuntimeFailure(Failure.CLOUD_POLICY_DENIED)
        placement = assignment.placement
        connections = assignment.connections
        if placement is None or connections is None:
            raise RuntimeFailure(Failure.CLOUD_POLICY_DENIED)

        async def record() -> None:
            await placement.record(ExecutionPlacement.CLOUD, agent=agent, model=model)

        try:
            return await connections.execute_session(kind, request, gate=record)
        except ConnectionBusyError:
            # The database did not answer the admission in time, or the
            # connection's one session slot stayed taken for the whole wait
            # (``SessionSlotBusyError``, Decision 0083, 6): nothing admitted.
            raise RuntimeFailure(Failure.CONNECTION_BUSY) from None
        except CloudSendRefusedError:
            # The record's transaction asked the policy again (Decision 0099,
            # 2: one active user, under the single-user lock) and wrote
            # nothing: refused like the policy itself.
            raise RuntimeFailure(Failure.CLOUD_POLICY_DENIED) from None
        except QuotaExceededError:
            raise RuntimeFailure(Failure.CONNECTION_QUOTA_EXCEEDED) from None
        except TaskBudgetError:
            # The floor, or a budget used up or not configured: not enough
            # tokens for a session either way.
            raise RuntimeFailure(Failure.TOKEN_FLOOR) from None
        except (ConnectionUnavailableError, TaskNotUsableError):
            raise RuntimeFailure(Failure.CONNECTION_UNAVAILABLE) from None


__all__ = ["CloudGate"]
