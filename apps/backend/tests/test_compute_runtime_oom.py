"""``HybridRuntime`` turns a local node's lost model server into
``AgentOutOfMemory`` when the server's unit was ended by the OOM killer after the
node's lease began (Decision 0092, 3; issue #251). Fakes only: no model server,
no ``systemctl``."""

import asyncio
import unittest

from paw_backend.compute import HybridRuntime, ModelControlError, UnitState
from paw_backend.compute.limits import UNIT_END_POLL_SECONDS, UNIT_END_WAIT_SECONDS
from paw_backend.orchestrator import NodeOutcome, NodeResult
from paw_backend.orchestrator.errors import AGENT_OUT_OF_MEMORY
from tests.compute_support import build, main_spec, settle
from tests.test_compute_runtimes import Policy, Recorder, assignment, fill_main

ID_1 = "0123456789abcdef0123456789abcdef"
ID_2 = "fedcba9876543210fedcba9876543210"


class FakeUnits:
    def __init__(self, fake) -> None:
        self.states: dict[str, UnitState] = {}
        self.reads = 0
        self.fail = False
        fake.unit_state = self.unit_state

    async def unit_state(self, deployment):
        self.reads += 1
        if self.fail:
            raise ModelControlError()
        return self.states.get(deployment)


class LostServer:
    """A local runtime whose model server went away during the call: the
    adapter's ``SERVER_UNAVAILABLE`` row (``ConnectionError``)."""

    def __init__(self, units=None, state=None, error_class="ConnectionError"):
        self.units = units
        self.state = state
        self.error_class = error_class
        self.calls = 0

    async def run_node(self, assignment):
        self.calls += 1
        if self.units is not None and self.state is not None:
            self.units.states["main"] = self.state  # the kill, during the call
        return NodeOutcome.failed(self.error_class, "server_unavailable")


class HybridOomTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build((main_spec(),))
        self.units = FakeUnits(self.control)
        self.units.states["main"] = UnitState("active", "success", ID_1)
        await self.scheduler.refresh()

    def runtime(self, local, **options):
        values = dict(deployment="main", clock=self.clock, wait_seconds=60)
        values.update(options)
        return HybridRuntime(self.scheduler, local, **values)

    async def test_an_oom_killed_server_is_agent_out_of_memory(self):
        for active in ("deactivating", "failed", "inactive"):
            with self.subTest(active=active):
                self.units.states["main"] = UnitState("active", "success", ID_1)
                local = LostServer(
                    self.units, UnitState(active, "oom-kill", f"{active:0>32}"[-32:])
                )
                outcome = await self.runtime(local).run_node(assignment())
                self.assertEqual(outcome.error_class, AGENT_OUT_OF_MEMORY)
                # The fixed text of the table, no escalation (Decision 0083, 4).
                self.assertEqual(outcome.message, "out_of_memory")
                self.assertFalse(outcome.escalate)
                self.assertTrue(outcome.retryable)

    async def test_another_end_stays_a_lost_connection(self):
        local = LostServer(self.units, UnitState("failed", "exit-code", ID_1))
        outcome = await self.runtime(local).run_node(assignment())
        self.assertEqual(outcome.error_class, "ConnectionError")
        self.assertEqual(outcome.message, "server_unavailable")

    async def test_it_waits_up_to_ten_seconds_for_systemd_to_notice(self):
        local = LostServer()  # the unit is still active when the call fails
        run = asyncio.create_task(self.runtime(local).run_node(assignment()))
        await settle()
        self.assertFalse(run.done())
        await self.clock.advance(UNIT_END_POLL_SECONDS)
        self.units.states["main"] = UnitState("deactivating", "oom-kill", ID_1)
        await self.clock.advance(UNIT_END_POLL_SECONDS)
        outcome = await run
        self.assertEqual(outcome.error_class, AGENT_OUT_OF_MEMORY)

    async def test_a_unit_active_for_ten_seconds_is_not_an_oom_kill(self):
        run = asyncio.create_task(self.runtime(LostServer()).run_node(assignment()))
        await settle()
        polls = round(UNIT_END_WAIT_SECONDS / UNIT_END_POLL_SECONDS)
        for _ in range(polls):
            self.assertFalse(run.done())
            await self.clock.advance(UNIT_END_POLL_SECONDS)
        outcome = await run
        self.assertEqual(outcome.error_class, "ConnectionError")

    async def test_an_oom_kill_before_the_lease_is_not_this_nodes(self):
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        self.assertTrue(
            await self.scheduler.model_server_oom_killed("main", since=0.0)
        )  # seen at 0
        await self.clock.advance(30)
        await self.scheduler.refresh()  # a fresh reading admits the node
        # The unit was started again and runs; the node's call then fails.
        self.units.states["main"] = UnitState("failed", "exit-code", ID_2)
        outcome = await self.runtime(LostServer()).run_node(assignment())
        self.assertEqual(outcome.error_class, "ConnectionError")

    async def test_other_outcomes_are_not_asked_about(self):
        for local in (
            LostServer(error_class="TimeoutError"),
            LostServer(error_class="RuntimeError"),
            Recorder("local"),
        ):
            with self.subTest(local=local):
                self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
                reads = self.units.reads
                outcome = await self.runtime(local).run_node(assignment())
                self.assertNotEqual(outcome.error_class, AGENT_OUT_OF_MEMORY)
                self.assertEqual(self.units.reads, reads)

    async def test_the_cloud_path_is_not_asked_about(self):
        cloud = LostServer()
        runtime = self.runtime(
            Recorder("local"),
            cloud=cloud,
            cloud_policy=Policy(),
            cloud_agent="codex",
            cloud_model="gpt-5-codex",
        )
        # The main model is busy: fill its pool so the node goes to the cloud.
        leases = await fill_main(self.scheduler)
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        reads = self.units.reads
        outcome = await runtime.run_node(assignment())
        self.assertEqual(cloud.calls, 1)
        self.assertEqual(outcome.error_class, "ConnectionError")
        self.assertEqual(self.units.reads, reads)
        for lease in leases:
            await lease.release()

    async def test_an_unreadable_unit_keeps_the_lost_connection(self):
        self.units.fail = True
        outcome = await self.runtime(LostServer()).run_node(assignment())
        self.assertEqual(outcome.error_class, "ConnectionError")

    async def test_nodes_running_together_are_each_out_of_memory(self):
        gate = asyncio.Event()

        class Together:
            async def run_node(inner, assignment):
                await gate.wait()
                return NodeOutcome.failed("ConnectionError", "server_unavailable")

        runtime = self.runtime(Together())
        runs = [asyncio.create_task(runtime.run_node(assignment())) for _ in range(2)]
        await settle()
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        gate.set()
        outcomes = await asyncio.gather(*runs)
        self.assertEqual(
            [outcome.error_class for outcome in outcomes],
            [AGENT_OUT_OF_MEMORY, AGENT_OUT_OF_MEMORY],
        )

    def test_the_literals_agree_with_the_adapters_table(self):
        from paw_backend.agents.failures import FAILURE_TABLE, Failure
        from paw_backend.compute import runtimes

        self.assertEqual(
            runtimes.SERVER_UNAVAILABLE_CLASS,
            FAILURE_TABLE[Failure.SERVER_UNAVAILABLE].error_class,
        )
        self.assertEqual(runtimes.OUT_OF_MEMORY_TEXT, Failure.OUT_OF_MEMORY.value)
        row = FAILURE_TABLE[Failure.OUT_OF_MEMORY]
        self.assertEqual(row.error_class, AGENT_OUT_OF_MEMORY)
        self.assertFalse(row.escalate)

    async def test_a_success_is_untouched(self):
        outcome = await self.runtime(Recorder("local")).run_node(assignment())
        self.assertEqual(outcome.result, NodeResult(summary="done by local"))


if __name__ == "__main__":
    unittest.main()
