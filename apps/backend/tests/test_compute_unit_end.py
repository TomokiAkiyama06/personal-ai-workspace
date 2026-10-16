"""How a model server's systemd unit ended (Decision 0092, 1 and 2; issue #251).

``CommandModelControl.unit_state`` reads ``systemctl show`` of the unit a
deployment names (``DeploymentCommands.unit``); the scheduler reads it only when
the unit's processes are gone, before it starts the unit again, and when a
runtime asks (``model_server_oom_killed``), and keeps, per deployment, the last
end it saw and the ``oom-kill`` ends of the last hour. Fakes only: nothing runs
``systemctl``, no model is loaded.
"""

import asyncio
import unittest

from paw_backend.compute import (
    CommandModelControl,
    DeploymentCommands,
    DeploymentState,
    InvalidComputeArgumentError,
    ModelControlError,
    UnitState,
)
from paw_backend.compute.limits import (
    MAX_MODEL_SERVER_OOM_ENDS,
    MODEL_SERVER_OOM_WINDOW_SECONDS,
    UNIT_END_POLL_SECONDS,
    UNIT_END_WAIT_SECONDS,
)
from paw_backend.compute.probe import CommandResult
from tests.compute_support import build, main_spec, memory_spec, settle

ID_1 = "0123456789abcdef0123456789abcdef"
ID_2 = "fedcba9876543210fedcba9876543210"
SHOW = (
    "/usr/bin/systemctl",
    "show",
    "-p",
    "Result",
    "-p",
    "ActiveState",
    "-p",
    "InvocationID",
    "--",
    "paw-llm-main.service",
)


class Runner:
    def __init__(self, stdout="", code=0):
        self.calls = []
        self.result = CommandResult(code, stdout)
        self.error = None

    async def run(self, argv, *, timeout_seconds):
        self.calls.append((tuple(argv), timeout_seconds))
        if self.error is not None:
            raise self.error
        return self.result


def show(active="failed", result="oom-kill", invocation=ID_1):
    return f"Result={result}\nActiveState={active}\nInvocationID={invocation}\n"


def control(stdout, **commands):
    runner = Runner(stdout)
    values = {"unit": "paw-llm-main.service"} | commands
    return (
        CommandModelControl(
            {"main": DeploymentCommands(**values)},
            runner=runner,
            host_memory=lambda: 1 << 40,
        ),
        runner,
    )


class UnitStateReadTest(unittest.IsolatedAsyncioTestCase):
    async def test_it_runs_systemctl_show_without_a_shell_and_parses_three_lines(self):
        model_control, runner = control(show())
        state = await model_control.unit_state("main")
        self.assertEqual(state, UnitState("failed", "oom-kill", ID_1))
        self.assertTrue(state.ended)
        self.assertTrue(state.oom_killed)
        self.assertEqual([argv for argv, _ in runner.calls], [SHOW])
        # A short timeout of its own, not the 900 s of a load.
        self.assertLessEqual(runner.calls[0][1], 30)

    async def test_the_order_of_the_lines_does_not_matter(self):
        model_control, _ = control(
            f"ActiveState=active\nInvocationID={ID_2}\nResult=success\n"
        )
        state = await model_control.unit_state("main")
        self.assertEqual(state, UnitState("active", "success", ID_2))
        self.assertFalse(state.ended)
        self.assertFalse(state.oom_killed)

    async def test_a_unit_that_never_ran_has_no_invocation_id(self):
        model_control, _ = control(show("inactive", "success", ""))
        self.assertEqual(
            await model_control.unit_state("main"),
            UnitState("inactive", "success", ""),
        )

    async def test_ending_states(self):
        for active, ended in (
            ("active", False),
            ("activating", False),
            ("reloading", False),
            ("deactivating", True),
            ("failed", True),
            ("inactive", True),
        ):
            with self.subTest(active=active):
                self.assertIs(UnitState(active, "success", ID_1).ended, ended)

    async def test_without_a_unit_there_is_nothing_to_read(self):
        runner = Runner(show())
        model_control = CommandModelControl(
            {"main": DeploymentCommands(gpu=("true",))}, runner=runner
        )
        self.assertIsNone(await model_control.unit_state("main"))
        self.assertIsNone(await model_control.unit_state("other"))
        self.assertEqual(runner.calls, [])

    async def test_anything_else_is_refused_without_its_output(self):
        for stdout in (
            "",
            "Result=oom-kill\nActiveState=failed\n",  # a line missing
            show() + "MainPID=12\n",  # a line too many
            show() + "Result=success\n",  # twice
            "Result=oom-kill\nActiveState=failed\nInvocationID=secret-ish\n",
            show(active="Failed"),
            show(active="sleeping"),
            show(result="OOM kill"),
            show(result="x" * 65),
            "Result oom-kill\nActiveState=failed\nInvocationID=\n",
        ):
            with self.subTest(stdout=stdout):
                model_control, _ = control(stdout)
                with self.assertRaises(ModelControlError) as raised:
                    await model_control.unit_state("main")
                self.assertNotIn("secret", str(raised.exception))
        model_control, runner = control(show())
        runner.result = CommandResult(1, show())
        with self.assertRaises(ModelControlError):
            await model_control.unit_state("main")
        runner.error = TimeoutError()
        with self.assertRaises(ModelControlError):
            await model_control.unit_state("main")

    def test_the_unit_name_is_checked(self):
        for name in (
            "paw-llm-main.service",
            "paw-cli-session@abc.service",
            "a_b.c-d.service",
        ):
            self.assertEqual(DeploymentCommands(unit=name).unit, name)
        for bad in (
            "",
            "paw-llm-main",
            "paw-llm-main.socket",
            "../x.service",
            "a b.service",
            "-p.service",
            "x\x00.service",
            ".service",
            "a" * 250 + ".service",
            ("paw-llm-main.service",),
            1,
        ):
            with self.subTest(bad=bad), self.assertRaises(InvalidComputeArgumentError):
                DeploymentCommands(unit=bad)
        self.assertIsNone(DeploymentCommands().unit)


class FakeUnits:
    """What ``systemctl show`` would say for each deployment of a fake control."""

    def __init__(self, fake) -> None:
        self.states: dict[str, UnitState] = {}
        self.reads: list[str] = []
        self.fail = False
        fake.unit_state = self.unit_state

    async def unit_state(self, deployment):
        self.reads.append(deployment)
        if self.fail:
            raise ModelControlError()
        return self.states.get(deployment)


def kill(control, probe, name="main"):
    """The OOM killer ended the runtime's processes."""
    probe.resident.pop(control.pids.pop(name))


class SchedulerUnitEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build(
            (main_spec(),), failed_retry_seconds=60
        )
        self.units = FakeUnits(self.control)
        self.units.states["main"] = UnitState("active", "success", ID_1)
        await self.scheduler.refresh()

    def kills(self, name="main"):
        return self.scheduler.status().deployment(name).oom_kills_last_hour

    async def test_a_running_unit_is_not_read(self):
        for _ in range(3):
            await self.clock.advance(5)
            await self.scheduler.refresh()
        self.assertEqual(self.units.reads, [])
        self.assertEqual(self.kills(), 0)
        self.assertEqual(self.scheduler.status().model_server_oom_kills_last_hour, 0)

    async def test_the_unit_is_read_when_its_processes_are_gone(self):
        # Held by a lease: the deployment stays ``gpu`` but the end is read.
        lease = (await self.scheduler.try_acquire(_request())).lease
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        kill(self.control, self.probe)
        await self.scheduler.refresh()
        self.assertEqual(self.units.reads, ["main"])
        self.assertEqual(self.kills(), 1)
        self.assertEqual(self.scheduler.status().model_server_oom_kills_last_hour, 1)
        # Read again while the lease holds it there: the same end, counted once.
        await self.clock.advance(5)
        await self.scheduler.refresh()
        self.assertEqual(self.kills(), 1)
        await lease.release()

    async def test_the_end_is_read_again_before_the_unit_is_started(self):
        self.units.states["main"] = UnitState("deactivating", "oom-kill", ID_1)
        kill(self.control, self.probe)
        # Read when the processes are gone (it then counts as unloaded), and
        # again before the same refresh starts it.
        await self.scheduler.refresh()
        self.assertEqual(self.units.reads, ["main", "main"])
        self.assertIn(("place:local_gpu", "main"), self.control.actions)
        self.assertEqual(self.kills(), 1)  # the same InvocationID: once

    async def test_an_end_first_seen_just_before_the_start_is_kept(self):
        # A deployment the scheduler has to load: the start would erase the
        # unit's last result, so it is read first.
        scheduler, _, fake, _ = build((main_spec(initial=DeploymentState.UNLOADED),))
        units = FakeUnits(fake)
        units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        await scheduler.refresh()
        self.assertEqual(units.reads, ["main"])
        self.assertEqual(fake.actions, [("place:local_gpu", "main")])
        self.assertEqual(scheduler.status().deployment("main").oom_kills_last_hour, 1)

    async def test_a_normal_end_is_not_an_oom_kill_and_does_not_erase_one(self):
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        kill(self.control, self.probe)
        await self.scheduler.refresh()
        await self.clock.advance(5)
        await self.scheduler.refresh()  # started again (ID_2 from here)
        self.units.states["main"] = UnitState("inactive", "success", ID_2)
        kill(self.control, self.probe)
        await self.clock.advance(5)
        await self.scheduler.refresh()
        self.assertEqual(self.kills(), 1)  # the later normal end kept it

    async def test_kills_older_than_an_hour_are_not_counted(self):
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        kill(self.control, self.probe)
        await self.scheduler.refresh()
        self.assertEqual(self.kills(), 1)
        await self.clock.advance(MODEL_SERVER_OOM_WINDOW_SECONDS + 1)
        self.assertEqual(self.kills(), 0)
        self.assertEqual(self.scheduler.status().model_server_oom_kills_last_hour, 0)

    async def test_at_most_64_kills_are_kept(self):
        scheduler = self.scheduler
        entry = scheduler._deployments["main"]  # the bookkeeping, not the probe
        for number in range(MAX_MODEL_SERVER_OOM_ENDS + 10):
            scheduler._note_unit_end(
                entry, UnitState("failed", "oom-kill", f"{number:032x}")
            )
        self.assertEqual(self.kills(), MAX_MODEL_SERVER_OOM_ENDS)

    async def test_a_unit_that_cannot_be_read_changes_nothing(self):
        self.units.fail = True
        kill(self.control, self.probe)
        await self.scheduler.refresh()
        self.assertEqual(self.kills(), 0)
        self.assertIn(("place:local_gpu", "main"), self.control.actions)

    async def test_a_control_without_unit_state_works_as_before(self):
        scheduler, probe, fake, _ = build((main_spec(),))
        self.assertFalse(hasattr(fake, "unit_state"))
        await scheduler.refresh()
        kill(fake, probe)
        await scheduler.refresh()
        self.assertEqual(scheduler.status().deployment("main").oom_kills_last_hour, 0)
        self.assertFalse(await scheduler.model_server_oom_killed("main", since=0.0))

    async def test_kills_are_counted_per_deployment(self):
        scheduler, probe, fake, clock = build((main_spec(), memory_spec()))
        units = FakeUnits(fake)
        await scheduler.refresh()
        units.states["memory"] = UnitState("failed", "oom-kill", ID_2)
        kill(fake, probe, "memory")
        await scheduler.refresh()
        status = scheduler.status()
        self.assertEqual(status.deployment("memory").oom_kills_last_hour, 1)
        self.assertEqual(status.deployment("main").oom_kills_last_hour, 0)
        self.assertEqual(status.model_server_oom_kills_last_hour, 1)
        self.assertEqual(set(units.reads), {"memory"})


class OomQueryTest(unittest.IsolatedAsyncioTestCase):
    """``model_server_oom_killed(deployment, since=...)`` (Decision 0092, 3)."""

    async def asyncSetUp(self):
        self.scheduler, self.probe, self.control, self.clock = build((main_spec(),))
        self.units = FakeUnits(self.control)
        self.units.states["main"] = UnitState("active", "success", ID_1)
        await self.scheduler.refresh()

    async def ask(self, since=0.0):
        return await self.scheduler.model_server_oom_killed("main", since=since)

    async def test_an_oom_kill_after_the_lease_is_reported_at_once(self):
        for active in ("deactivating", "failed", "inactive"):
            with self.subTest(active=active):
                self.units.states["main"] = UnitState(active, "oom-kill", ID_1)
                self.assertTrue(await self.ask())

    async def test_another_end_is_not_an_oom_kill(self):
        self.units.states["main"] = UnitState("failed", "exit-code", ID_1)
        self.assertFalse(await self.ask())
        self.assertEqual(self.units.reads, ["main"])  # no waiting

    async def test_an_end_seen_before_the_lease_does_not_count(self):
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        self.assertTrue(await self.ask(since=0.0))
        await self.clock.advance(30)
        # Seen at 0, the lease began at 30: not this lease's end.
        self.assertFalse(await self.ask(since=30.0))

    async def test_a_unit_still_active_is_read_again_for_up_to_ten_seconds(self):
        task = asyncio.create_task(self.ask())
        await settle()
        self.assertEqual(len(self.units.reads), 1)
        await self.clock.advance(UNIT_END_POLL_SECONDS)
        self.assertEqual(len(self.units.reads), 2)
        # systemd notices the kill and stops the unit.
        self.units.states["main"] = UnitState("deactivating", "oom-kill", ID_1)
        await self.clock.advance(UNIT_END_POLL_SECONDS)
        self.assertTrue(await task)

    async def test_after_ten_seconds_active_it_is_not_an_oom_kill(self):
        task = asyncio.create_task(self.ask())
        await settle()
        polls = round(UNIT_END_WAIT_SECONDS / UNIT_END_POLL_SECONDS)
        for _ in range(polls):
            self.assertFalse(task.done())
            await self.clock.advance(UNIT_END_POLL_SECONDS)
        self.assertFalse(await task)
        self.assertEqual(len(self.units.reads), polls + 1)

    async def test_a_unit_already_started_again_keeps_the_recorded_kill(self):
        self.units.states["main"] = UnitState("failed", "oom-kill", ID_1)
        kill(self.control, self.probe)
        await self.scheduler.refresh()
        await self.clock.advance(5)
        await self.scheduler.refresh()  # started again
        self.units.states["main"] = UnitState("active", "success", ID_2)
        self.assertTrue(await self.ask(since=0.0))

    async def test_an_unreadable_unit_is_not_an_oom_kill(self):
        self.units.fail = True
        self.assertFalse(await self.ask())
        self.units.fail = False
        self.units.states.pop("main")  # no unit configured
        self.assertFalse(await self.ask())

    async def test_an_unknown_deployment_is_refused(self):
        with self.assertRaises(InvalidComputeArgumentError):
            await self.scheduler.model_server_oom_killed("other", since=0.0)
        for since in (float("nan"), -1.0, "0", True):
            with (
                self.subTest(since=since),
                self.assertRaises(InvalidComputeArgumentError),
            ):
                await self.scheduler.model_server_oom_killed("main", since=since)


def _request():
    from paw_backend.compute import ComputeRequest, ResourceClass

    return ComputeRequest(ResourceClass.CODING, deployment="main", context_tokens=1_000)


if __name__ == "__main__":
    unittest.main()
