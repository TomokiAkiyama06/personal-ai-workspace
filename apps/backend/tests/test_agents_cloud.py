"""The order of an external send (Decision 0083, 1): ``CloudGate``, the
attempt's ``connections`` (``NodeConnectionGateway``) and ``HybridRuntime``'s
fallback path to a cloud runtime with its own gate. Fakes only.
"""

import unittest
import uuid

from paw_backend.agents.cloud import CloudGate
from paw_backend.agents.failures import ErrorClassifier, Failure, RuntimeFailure
from paw_backend.connections import (
    ConnectionKind,
    ConnectionUnavailableError,
    QuotaExceededError,
    QuotaMetric,
    QuotaPeriod,
    RefusalReason,
    SessionRequest,
    SessionResult,
    SessionStop,
    TaskBudgetError,
    TaskNotUsableError,
    UsagePurpose,
)
from paw_backend.orchestrator.domain import ExecutionPlacement, NodeRole
from paw_backend.orchestrator.errors import NodeStopped, StopReason
from paw_backend.orchestrator.gateway import AttemptFence, NodeConnectionGateway
from paw_backend.orchestrator.runtime import NodeAssignment

REQUEST = SessionRequest("gpt-5-codex", UsagePurpose.CODING, "the task", 600)
PRINCIPAL = object()
DONE = SessionResult(uuid.uuid4(), {"summary": "s"}, 10, 1)


class Policy:
    def __init__(self, allows=True):
        self.answer = allows
        self.asked = 0

    async def allows(self, assignment):
        self.asked += 1
        return self.answer


class Placement:
    def __init__(self, timeline, error=None):
        self.timeline = timeline
        self.error = error

    async def record(self, placement, *, agent, model):
        self.timeline.append(("placement", placement, agent, model))
        if self.error is not None:
            raise self.error

    async def ensure_active(self):
        return None


class Connections:
    def __init__(self, timeline, *, error=None, result=DONE):
        self.timeline = timeline
        self.error = error
        self.result = result

    async def execute_session(self, kind, request, *, gate=None):
        self.timeline.append(("admission", kind))
        if self.error is not None:
            raise self.error
        await gate()
        self.timeline.append(("adapter", kind))
        return self.result


def assignment(*, placement=None, connections=None):
    return NodeAssignment(
        task_id=uuid.uuid4(),
        node_key="a",
        role=NodeRole.WORKER,
        title="t",
        goal="g",
        input={},
        upstream={},
        agent="codex",
        attempt=1,
        approach=0,
        tools=None,
        budget=None,
        placement=placement,
        connections=connections,
    )


class CloudGateTest(unittest.IsolatedAsyncioTestCase):
    async def gate(self, policy=None, *, error=None, placement_error=None, both=True):
        timeline = []
        work = assignment(
            placement=Placement(timeline, placement_error) if both else None,
            connections=Connections(timeline, error=error) if both else None,
        )
        result = await CloudGate(policy).session(
            work,
            kind=ConnectionKind.CODEX,
            request=REQUEST,
            agent="codex",
            model="gpt-5-codex",
        )
        return result, timeline

    async def test_policy_then_admission_then_placement_then_the_adapter(self):
        result, timeline = await self.gate(Policy())
        self.assertIs(result, DONE)
        self.assertEqual(
            timeline,
            [
                ("admission", ConnectionKind.CODEX),
                ("placement", ExecutionPlacement.CLOUD, "codex", "gpt-5-codex"),
                ("adapter", ConnectionKind.CODEX),
            ],
        )

    async def refused(self, failure, *args, **kwargs):
        with self.assertRaises(RuntimeFailure) as caught:
            await self.gate(*args, **kwargs)
        self.assertIs(caught.exception.failure, failure)

    async def test_refusals_send_nothing(self):
        await self.refused(Failure.CLOUD_POLICY_DENIED, None)
        await self.refused(Failure.CLOUD_POLICY_DENIED, Policy(allows=False))
        # Fail closed: a truthy answer that is not ``True`` allows nothing.
        for answer in (1, "yes", object()):
            await self.refused(Failure.CLOUD_POLICY_DENIED, Policy(allows=answer))
        await self.refused(Failure.CLOUD_POLICY_DENIED, Policy(), both=False)
        quota = QuotaExceededError(QuotaMetric.TOKENS, QuotaPeriod.DAY, None)
        await self.refused(Failure.CONNECTION_QUOTA_EXCEEDED, Policy(), error=quota)
        floor = TaskBudgetError(RefusalReason.TASK_TOKEN_FLOOR)
        await self.refused(Failure.TOKEN_FLOOR, Policy(), error=floor)
        await self.refused(
            Failure.CONNECTION_UNAVAILABLE,
            Policy(),
            error=ConnectionUnavailableError(),
        )

    async def test_a_failed_placement_sends_nothing(self):
        timeline = []
        work = assignment(
            placement=Placement(timeline, RuntimeError("down")),
            connections=Connections(timeline),
        )
        with self.assertRaises(RuntimeError):
            await CloudGate(Policy()).session(
                work,
                kind=ConnectionKind.CODEX,
                request=REQUEST,
                agent="codex",
                model="m",
            )
        self.assertNotIn(("adapter", ConnectionKind.CODEX), timeline)

    def test_the_outcomes_of_a_refusal(self):
        own = ErrorClassifier.outcome(Failure.CONNECTION_QUOTA_EXCEEDED)
        self.assertEqual((own.error_class, own.escalate), ("ConnectionError", True))
        fallback = ErrorClassifier.outcome(
            Failure.CONNECTION_QUOTA_EXCEEDED, fallback=True
        )
        self.assertEqual(fallback.error_class, "ComputeUnavailable")
        self.assertFalse(fallback.escalate)


class Guard:
    def __init__(self):
        self.stopped = None

    async def ensure_active(self):
        if self.stopped is not None:
            raise NodeStopped(self.stopped)

    def stop(self, reason):
        if self.stopped is None:
            self.stopped = reason


class Principals:
    def __init__(self, principal=PRINCIPAL):
        self.principal = principal

    async def get_principal_by_id(self, user_id):
        return self.principal


class Service:
    def __init__(self, result, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def execute_session(self, principal, context, kind, request, *, gate=None):
        self.calls.append((principal, context, kind))
        if self.error is not None:
            raise self.error
        return self.result


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def gateway(self, result, principals=None, fence=None, error=None):
        guard = Guard()
        service = Service(result, error)
        context = object()

        async def factory():
            return context

        gateway = NodeConnectionGateway(
            guard, service, principals or Principals(), uuid.uuid4(), factory, fence
        )
        return gateway, guard, service, context

    async def test_a_session_runs_with_a_context_built_now(self):
        gateway, _, service, context = await self.gateway(DONE)
        self.assertIs(
            await gateway.execute_session(ConnectionKind.CODEX, REQUEST), DONE
        )
        self.assertIs(service.calls[0][1], context)

    async def test_a_stopped_session_stops_the_node(self):
        for stop, reason in (
            (SessionStop.BUDGET_EXCEEDED, StopReason.BUDGET_EXCEEDED),
            (SessionStop.LEASE_LOST, StopReason.LEASE_LOST),
            (SessionStop.RUN_REPLACED, StopReason.SUPERSEDED),
        ):
            stopped = SessionResult(uuid.uuid4(), None, 1, 1, None, stop)
            gateway, guard, _, _ = await self.gateway(stopped)
            with self.assertRaises(NodeStopped) as caught:
                await gateway.execute_session(ConnectionKind.CODEX, REQUEST)
            self.assertIs(caught.exception.reason, reason)

    async def test_a_creator_who_is_gone_stops_the_node(self):
        gateway, _, service, _ = await self.gateway(DONE, Principals(None))
        with self.assertRaises(NodeStopped):
            await gateway.execute_session(ConnectionKind.CODEX, REQUEST)
        self.assertEqual(service.calls, [])

    async def test_an_admission_fenced_out_stops_the_node(self):
        # The task ended, the run was replaced or the lease was lost between
        # the gateway's checks and the admission: the attempt stops (not a
        # connection failure for the runtime to retry or escalate).
        for reason, stop in (
            (RefusalReason.TASK_ENDED, StopReason.TASK_ENDED),
            (RefusalReason.TASK_NOT_FOUND, StopReason.TASK_ENDED),
            (RefusalReason.TASK_SUPERSEDED, StopReason.SUPERSEDED),
            (RefusalReason.LEASE_LOST, StopReason.LEASE_LOST),
        ):
            gateway, guard, _, _ = await self.gateway(
                DONE, error=TaskNotUsableError(reason)
            )
            with self.assertRaises(NodeStopped) as caught:
                await gateway.execute_session(ConnectionKind.CODEX, REQUEST)
            self.assertIs(caught.exception.reason, stop)
            self.assertIs(guard.stopped, stop)

    async def test_other_refusals_pass_to_the_runtime(self):
        for error in (
            TaskNotUsableError(RefusalReason.LEASE_UNAVAILABLE),
            ConnectionUnavailableError(),
            TaskBudgetError(RefusalReason.TASK_TOKEN_FLOOR),
        ):
            gateway, guard, _, _ = await self.gateway(DONE, error=error)
            with self.assertRaises(type(error)):
                await gateway.execute_session(ConnectionKind.CODEX, REQUEST)
            self.assertIsNone(guard.stopped)

    async def test_an_attempt_abandoned_during_the_lookup_sends_nothing(self):
        # The orchestrator closes the fence while the principal is looked up
        # (a runtime that ignored its cancellation): no session may start.
        fence = AttemptFence()

        class Closing(Principals):
            async def get_principal_by_id(self, user_id):
                fence.close()
                return self.principal

        gateway, _, service, _ = await self.gateway(DONE, Closing(), fence)
        with self.assertRaises(NodeStopped) as caught:
            await gateway.execute_session(ConnectionKind.CODEX, REQUEST)
        self.assertIs(caught.exception.reason, StopReason.ABANDONED)
        self.assertEqual(service.calls, [])


if __name__ == "__main__":
    unittest.main()
