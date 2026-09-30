"""The Compute Resource Scheduler in the application (issue #165, Decision 0058).

* ``create_app(compute=ComputeSetup(...))`` builds the process's scheduler with
  the application's VRAM warning sink; the lifespan runs its refresh loop and,
  with a database, Kaggle / Full GPU Mode, and stops both. Without ``compute``
  there is no scheduler.
* ``local_runtimes`` reach the orchestrator as ``HybridRuntime`` on that
  scheduler, with the task budget's late GPU charge.
* ``/api/v1/admin/compute/full-gpu``: ``GET`` / ``POST`` / ``DELETE``, guarded by
  ``admin.compute.full_gpu`` (and ``FullGpuMode`` authorizes again); the start
  runs in the background; ``503`` without a scheduler or a database.

Nothing here touches a GPU: the probe and the model control are the fakes of
``compute_support`` (the models "load" and "unload" in memory), and the time is
a manual clock.
"""

import asyncio
import contextlib
import unittest
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import httpx

from paw_backend.app import create_app
from paw_backend.authz import (
    Authorizer,
    Capability,
    InMemoryAuditSink,
    SystemRole,
)
from paw_backend.compute import (
    ComputeConfig,
    ComputeRequest,
    DeferredWork,
    DeploymentState,
    FullGpuState,
    HybridRuntime,
    Placement,
    ResourceClass,
    SchedulerMode,
    TrackerLateGpuCharge,
    VramDeferral,
)
from paw_backend.compute.wiring import (
    ComputeSetup,
    FullGpuController,
    LocalRuntime,
    RecentVramWarnings,
    build_compute,
)
from paw_backend.orchestrator import OrchestratorConfig
from paw_backend.orchestrator.composition import build_task_execution

from .authz_support import StaticProvider, principal, uid
from .compute_support import (
    GIB,
    FakeControl,
    FakeProbe,
    ManualClock,
    default_specs,
    settle,
)
from .orchestrator_support import FakeRuntime
from .support import FakeDatabase, make_settings
from .test_compute_full_gpu import FakeHolds
from .test_scratch_janitor_lifespan import configured

OWNER = principal(SystemRole.OWNER, uid(2))
ADMIN = principal(SystemRole.ADMIN, uid(1))
USER = principal(SystemRole.USER, uid(3))
URL = "/api/v1/admin/compute/full-gpu"
QUIET = dict(
    scratch_purge_interval_seconds=0,
    project_task_stop_interval_seconds=0,
    connection_reap_interval_seconds=0,
    freshness_job_interval_seconds=0,
)


def fake_gpu(*, control=True, stopped=()):
    """A setup over the fake GPU, the resident models already on it (but the
    ``stopped`` ones: configured on the GPU, their runtime is not running)."""
    specs = default_specs()
    probe = FakeProbe()
    fake = FakeControl(probe, specs)
    for spec in specs:
        if spec.initial is DeploymentState.GPU and spec.name not in stopped:
            fake.start_on_gpu(spec.name)
    clock = ManualClock()
    setup = ComputeSetup(
        ComputeConfig(deployments=specs),
        probe,
        control=fake if control else None,
        clock=clock,
    )
    return setup, probe, fake, clock


class SetupTest(unittest.TestCase):
    def test_it_checks_what_it_is_given(self):
        setup, probe, *_ = fake_gpu()
        with self.assertRaises(TypeError):
            ComputeSetup(object(), probe)
        with self.assertRaises(TypeError):
            ComputeSetup(setup.config, object())  # no async sample()
        for refresh in (0, -1, float("nan"), 86_401):
            with self.subTest(refresh=refresh), self.assertRaises(ValueError):
                ComputeSetup(setup.config, probe, refresh_seconds=refresh)
        for kept in (0, 1_001, True, 2.0):
            with self.subTest(kept=kept), self.assertRaises(ValueError):
                ComputeSetup(setup.config, probe, kept_vram_warnings=kept)
        with self.assertRaises(TypeError):
            build_compute(object())

    def test_a_local_runtime_must_be_a_runtime(self):
        with self.assertRaises(TypeError):
            LocalRuntime(object(), deployment="main")
        with self.assertRaises(TypeError):
            LocalRuntime(None, deployment="main")

    def test_local_runtimes_need_compute_and_a_database(self):
        setup, *_ = fake_gpu()
        runtimes = {"local": LocalRuntime(FakeRuntime(), deployment="main")}
        config = OrchestratorConfig.uniform(("local",))
        settings, database = configured()
        with self.assertRaises(TypeError):
            create_app(
                settings,
                database=database,
                local_runtimes=runtimes,
                orchestrator_config=config,
            )
        with self.assertRaises(TypeError):
            create_app(
                make_settings(),
                database=FakeDatabase(),
                compute=setup,
                local_runtimes=runtimes,
                orchestrator_config=config,
            )


class RecentVramWarningsTest(unittest.IsolatedAsyncioTestCase):
    def event(self, requested=1):
        return VramDeferral(
            DeferredWork.REQUEST,
            ResourceClass.CODING,
            requested_bytes=requested,
            observed_free_bytes=2,
            external_bytes=3,
            headroom_bytes=4,
        )

    def test_it_keeps_the_latest_ones_and_counts_them_all(self):
        moment = datetime(2026, 9, 30, tzinfo=UTC)
        sink = RecentVramWarnings(kept=2, now=lambda: moment)
        for requested in (1, 2, 3):
            sink.vram_deferred(self.event(requested))
        self.assertEqual(sink.total, 3)
        self.assertEqual([item.event.requested_bytes for item in sink.recent()], [2, 3])
        self.assertEqual({item.occurred_at for item in sink.recent()}, {moment})

    async def test_the_scheduler_reports_to_the_sink_of_the_application(self):
        setup, probe, _, _ = fake_gpu()
        scheduler, sink = build_compute(setup)
        probe.external = 10 * GIB  # another workload: 1.2 GiB left
        await scheduler.refresh()
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            waiting = asyncio.create_task(
                scheduler.acquire(
                    ComputeRequest(
                        ResourceClass.CODING, deployment="main", vram_bytes=2 * GIB
                    ),
                    wait_seconds=600,
                )
            )
            await settle()
        [recorded] = sink.recent()
        self.assertEqual(recorded.event.work, DeferredWork.REQUEST)
        self.assertEqual(recorded.event.external_bytes, 10 * GIB)
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting


class CompositionTest(unittest.TestCase):
    def test_local_runtimes_run_through_the_scheduler(self):
        setup, *_ = fake_gpu()
        runtime = FakeRuntime()
        cloud_free = FakeRuntime()
        settings, database = configured()
        app = create_app(
            settings,
            database=database,
            compute=setup,
            agent_runtimes={"planner": cloud_free},
            local_runtimes={
                "local": LocalRuntime(
                    runtime,
                    deployment="main",
                    local_model="qwen3-coder",
                    resource_class=ResourceClass.INTERACTIVE,
                    wait_seconds=30,
                )
            },
            orchestrator_config=OrchestratorConfig.uniform(("local", "planner")),
        )
        scheduler = app.state.compute.scheduler
        execution = app.state.task_execution
        runtimes = execution.orchestrator._runtimes
        self.assertEqual(set(runtimes), {"local", "planner"})
        self.assertIs(runtimes["planner"], cloud_free)
        hybrid = runtimes["local"]
        self.assertIsInstance(hybrid, HybridRuntime)
        self.assertIs(hybrid._scheduler, scheduler)
        self.assertIs(hybrid._local, runtime)
        self.assertEqual(hybrid._deployment, "main")
        self.assertEqual(hybrid._local_model, "qwen3-coder")
        self.assertIs(hybrid._class, ResourceClass.INTERACTIVE)
        self.assertEqual(hybrid._wait, 30)
        # Nothing goes to the cloud (Decision 0037, 14).
        self.assertIsNone(hybrid._cloud)
        self.assertIsNone(hybrid._policy)
        # GPU time a node used after its attempt closed reaches the task budget.
        self.assertIsInstance(hybrid._late, TrackerLateGpuCharge)
        self.assertIs(hybrid._late._tracker, execution.budget)
        # The warnings of that scheduler go to the application's sink.
        self.assertIs(scheduler._warnings._sink, app.state.compute.warnings)

    def test_without_compute_there_is_no_scheduler(self):
        settings, database = configured()
        app = create_app(settings, database=database)
        self.assertIsNone(app.state.compute)

    def test_the_arguments_are_checked(self):
        setup, *_ = fake_gpu()
        scheduler, _ = build_compute(setup)
        settings, database = configured()
        authorizer = Authorizer(InMemoryAuditSink())
        config = OrchestratorConfig.uniform(("local",))
        local = {"local": LocalRuntime(FakeRuntime(), deployment="main")}
        cases = (
            dict(local_runtimes=local, orchestrator_config=config),  # no scheduler
            dict(scheduler=scheduler, local_runtimes=local),  # no config
            dict(
                scheduler=object(),
                local_runtimes=local,
                orchestrator_config=config,
            ),
            dict(
                scheduler=scheduler,
                local_runtimes={"local": FakeRuntime()},  # not a LocalRuntime
                orchestrator_config=config,
            ),
            dict(  # the same label twice
                scheduler=scheduler,
                runtimes={"local": FakeRuntime()},
                local_runtimes=local,
                orchestrator_config=config,
            ),
        )
        for options in cases:
            with self.subTest(options=sorted(options)), self.assertRaises(TypeError):
                build_task_execution(settings, database, authorizer, **options)
        # A deployment the scheduler does not know.
        with self.assertRaises(ValueError):
            build_task_execution(
                settings,
                database,
                authorizer,
                scheduler=scheduler,
                local_runtimes={
                    "local": LocalRuntime(FakeRuntime(), deployment="nope")
                },
                orchestrator_config=config,
            )


class ComputeAppTestCase(unittest.IsolatedAsyncioTestCase):
    """An application with the fake GPU, a database that nothing listens on and
    fake task holds; ``who`` is the authenticated principal."""

    who = OWNER
    control = True
    with_database = True
    settings_overrides = {}
    stopped = ()
    held_before = ()  # tasks an earlier process held

    async def asyncSetUp(self):
        self.setup, self.probe, self.fake, self.clock = fake_gpu(
            control=self.control, stopped=self.stopped
        )
        self.holds = FakeHolds()
        self.holds.held.extend(self.held_before)
        patcher = patch("paw_backend.app.PostgresTaskHolds", lambda *_: self.holds)
        patcher.start()
        self.addCleanup(patcher.stop)
        if self.with_database:
            settings, database = configured(**QUIET, **self.settings_overrides)
        else:
            settings = make_settings(**QUIET, **self.settings_overrides)
            database = FakeDatabase()
        self.app = create_app(settings, database=database, compute=self.setup)
        self.audit = InMemoryAuditSink()
        self.app.state.principal_provider = StaticProvider(self.who)
        self.app.state.authorizer = Authorizer(self.audit)
        self.scheduler = self.app.state.compute.scheduler
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.stop_app)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)
        await settle()

    async def stop_app(self):
        if self.lifespan is not None:
            lifespan, self.lifespan = self.lifespan, None
            await lifespan.__aexit__(None, None, None)

    async def advance_until(self, predicate, *, step=1.0, limit=200):
        for _ in range(limit):
            if predicate():
                return True
            await self.clock.advance(step)
        return predicate()

    def mode_state(self):
        return self.app.state.compute.full_gpu.status().state

    def decisions(self):
        return [
            event
            for event in self.audit.events
            if event.action == Capability.ADMIN_COMPUTE_FULL_GPU.value
        ]


class LifespanTest(ComputeAppTestCase):
    async def test_the_scheduler_and_full_gpu_mode_run_with_the_app(self):
        # The first reading was made at start-up.
        self.assertGreaterEqual(self.probe.calls, 1)
        self.assertTrue(self.scheduler.status().probe_ok)
        self.assertIsInstance(self.app.state.compute.full_gpu, FullGpuController)
        # Nothing was held by an earlier process: the first tick found nothing.
        self.assertTrue(
            await self.advance_until(lambda: self.mode_state() is FullGpuState.OFF)
        )
        calls = self.probe.calls
        await self.clock.advance(5)
        self.assertGreater(self.probe.calls, calls)  # refreshed every 5 s

        await self.stop_app()
        self.assertIsNone(self.app.state.compute.full_gpu)
        calls = self.probe.calls
        await self.clock.advance(60)
        self.assertEqual(self.probe.calls, calls)  # the loop has stopped


class WithoutDatabaseTest(ComputeAppTestCase):
    with_database = False

    async def test_the_scheduler_runs_but_full_gpu_mode_is_unavailable(self):
        self.assertTrue(self.scheduler.status().probe_ok)
        self.assertIsNone(self.app.state.compute.full_gpu)
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method):
                response = await self.client.request(method, URL)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.json()["error"]["code"], "compute_not_configured"
                )


class WithoutComputeTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_routes_answer_503(self):
        settings, database = configured(**QUIET)
        app = create_app(settings, database=database)
        app.state.principal_provider = StaticProvider(OWNER)
        app.state.authorizer = Authorizer(InMemoryAuditSink())
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client,
        ):
            for method in ("GET", "POST", "DELETE"):
                with self.subTest(method):
                    response = await client.request(method, URL)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(
                        response.json()["error"]["code"], "compute_not_configured"
                    )


class FullGpuHttpTest(ComputeAppTestCase):
    async def test_start_status_and_end(self):
        response = await self.client.get(URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"], "off")
        self.assertFalse(body["start_pending"])
        self.assertEqual(body["gpu"]["mode"], "normal")
        self.assertTrue(body["gpu"]["probe_ok"])
        self.assertEqual(body["gpu"]["vram"]["total_bytes"], 96 * GIB)
        self.assertEqual(body["gpu"]["vram"]["external_bytes"], 0)
        self.assertEqual(body["vram_warnings"], [])

        with self.assertLogs("paw_backend.compute", level="WARNING"):
            response = await self.client.post(URL, json={"drain_seconds": 60})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["state"], "starting")
            self.assertTrue(response.json()["start_pending"])
            self.assertTrue(
                await self.advance_until(lambda: self.mode_state() is FullGpuState.ON)
            )
        body = (await self.client.get(URL)).json()
        self.assertEqual(body["state"], "on")
        self.assertFalse(body["start_pending"])
        self.assertEqual(body["gpu"]["mode"], SchedulerMode.EXCLUSIVE.value)
        self.assertIsNone(body["last_failure"])
        # Every model left the GPU (through the fake control only).
        self.assertEqual(self.probe.resident, {})

        # A second start is refused while the mode is on.
        response = await self.client.post(URL, json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "full_gpu_mode_state")

        with self.assertLogs("paw_backend.compute", level="WARNING"):
            response = await self.client.delete(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "resuming")
        # The models come back and the mode is off again.
        self.assertTrue(
            await self.advance_until(lambda: self.mode_state() is FullGpuState.OFF)
        )
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertIs(
            self.scheduler.status().deployment("main").state, DeploymentState.GPU
        )
        # Nothing to end any more.
        response = await self.client.delete(URL)
        self.assertEqual(response.status_code, 409)

        # The route's guard and FullGpuMode's own decision, both allowed and
        # audited, for the Owner.
        decisions = self.decisions()
        self.assertTrue(decisions)
        self.assertTrue(all(event.decision == "allow" for event in decisions))
        self.assertEqual({event.actor_id for event in decisions}, {OWNER.user_id})

    async def test_the_start_holds_the_tasks_of_the_running_gpu_work(self):
        task_id = uuid.uuid4()
        self.holds.running.add(task_id)
        lease = (
            await self.scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING,
                    deployment="main",
                    context_tokens=1_000,
                    task_id=task_id,
                )
            )
        ).lease
        self.assertEqual(lease.placement, Placement.LOCAL_GPU)
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            response = await self.client.post(URL, json={})
            self.assertEqual(response.status_code, 202)
            await settle()
            self.assertEqual(self.holds.held, [task_id])
            body = (await self.client.get(URL)).json()
            self.assertEqual(body["state"], "starting")
            self.assertEqual(body["held_tasks"], 1)
            # The drain waits for the work; it ends and the GPU is emptied.
            await lease.release()
            self.assertTrue(
                await self.advance_until(lambda: self.mode_state() is FullGpuState.ON)
            )

    async def test_ending_a_pending_start_abandons_it(self):
        lease = (
            await self.scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING, deployment="main", context_tokens=1_000
                )
            )
        ).lease
        with self.assertLogs("paw_backend.compute", level="WARNING") as logs:
            self.assertEqual((await self.client.post(URL, json={})).status_code, 202)
            await self.clock.advance(5)
            self.assertEqual(self.scheduler.status().mode, SchedulerMode.DRAINING)
            # A second start is refused while one is pending.
            response = await self.client.post(URL, json={})
            self.assertEqual(response.status_code, 409)
            response = await self.client.delete(URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["start_pending"])
        self.assertIn(body["state"], ("resuming", "off"))
        self.assertIn("start abandoned", "".join(logs.output))
        # Back to normal: nothing was unloaded, the running work goes on.
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertFalse(lease.released)
        self.assertNotIn(("unload", "main"), self.fake.actions)
        await lease.release()

    async def test_a_start_that_is_pending_at_shutdown_is_abandoned(self):
        lease = (
            await self.scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING, deployment="main", context_tokens=1_000
                )
            )
        ).lease
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            self.assertEqual((await self.client.post(URL, json={})).status_code, 202)
            await self.clock.advance(5)
            self.assertEqual(self.scheduler.status().mode, SchedulerMode.DRAINING)
            await self.stop_app()
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertNotIn(("unload", "main"), self.fake.actions)
        await lease.release()

    async def test_a_start_stopped_at_shutdown_after_an_unload_is_recovered(self):
        # Codex review #168 (P1): the shutdown comes after the start emptied the
        # GPU, while it waits for the probe to show the memory freed (the main
        # LLM's runtime is slow to exit). The start is abandoned and, before the
        # loops stop, the main LLM is loaded again and the held task resumes.
        task_id = uuid.uuid4()
        self.holds.running.add(task_id)
        lease = (
            await self.scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING,
                    deployment="main",
                    context_tokens=1_000,
                    task_id=task_id,
                )
            )
        ).lease
        main_pid = self.fake.pids["main"]
        self.fake.linger.add("main")
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            self.assertEqual((await self.client.post(URL, json={})).status_code, 202)
            await settle()
            self.assertEqual(self.holds.held, [task_id])
            await lease.release()
            self.assertTrue(
                await self.advance_until(
                    lambda: ("unload", "main") in self.fake.actions
                )
            )
            await self.clock.advance(1)
            self.assertIs(self.mode_state(), FullGpuState.STARTING)
            self.assertIs(
                self.scheduler.status().deployment("main").state,
                DeploymentState.UNLOADED,
            )
            # The runtime has exited now; the process shuts down.
            self.fake.linger.clear()
            self.probe.resident.pop(main_pid)
            await self.stop_app()
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertIs(
            self.scheduler.status().deployment("main").state, DeploymentState.GPU
        )
        self.assertEqual(self.holds.resumed, [task_id])
        self.assertEqual(self.holds.held, [])

    async def test_a_start_ended_by_delete_is_recovered_at_shutdown(self):
        # Codex review #168 (round 2, P1): the start emptied the GPU, then a
        # DELETE abandoned it (Full GPU Mode ``resuming``, no start pending);
        # the shutdown comes before the loops brought the main LLM back.
        task_id = uuid.uuid4()
        self.holds.running.add(task_id)
        lease = (
            await self.scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING,
                    deployment="main",
                    context_tokens=1_000,
                    task_id=task_id,
                )
            )
        ).lease
        main_pid = self.fake.pids["main"]
        self.fake.linger.add("main")
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            self.assertEqual((await self.client.post(URL, json={})).status_code, 202)
            await settle()
            await lease.release()
            self.assertTrue(
                await self.advance_until(
                    lambda: ("unload", "main") in self.fake.actions
                )
            )
            self.assertEqual((await self.client.delete(URL)).status_code, 200)
            self.assertIs(self.mode_state(), FullGpuState.RESUMING)
            self.fake.linger.clear()
            self.probe.resident.pop(main_pid)
            await self.stop_app()
        self.assertIs(
            self.scheduler.status().deployment("main").state, DeploymentState.GPU
        )
        self.assertEqual(self.holds.resumed, [task_id])

    async def test_the_body_is_checked(self):
        for body in (
            {"preempt": "yes"},
            {"drain_seconds": -1},
            {"drain_seconds": 86_401},
            {"vram_bytes": 0},
            {"vram_bytes": 1.5},
            {"unknown": True},
        ):
            with self.subTest(body=body):
                response = await self.client.post(URL, json=body)
                self.assertEqual(response.status_code, 422)
        self.assertIs(self.mode_state(), FullGpuState.OFF)
        self.assertFalse(self.app.state.compute.full_gpu.start_pending)

    async def test_the_vram_warnings_are_in_the_status(self):
        self.probe.external = 10 * GIB
        await self.clock.advance(5)  # the next reading sees the other workload
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            waiting = asyncio.create_task(
                self.scheduler.acquire(
                    ComputeRequest(
                        ResourceClass.CODING, deployment="main", vram_bytes=2 * GIB
                    ),
                    wait_seconds=600,
                )
            )
            await settle()
        body = (await self.client.get(URL)).json()
        self.assertEqual(body["vram_warnings_total"], 1)
        [warning] = body["vram_warnings"]
        self.assertEqual(warning["work"], "request")
        self.assertEqual(warning["resource_class"], "coding")
        self.assertEqual(warning["requested_bytes"], 2 * GIB)
        self.assertEqual(warning["external_bytes"], 10 * GIB)
        self.assertFalse(warning["gave_up"])
        self.assertEqual(body["gpu"]["vram_waiting"], 1)
        # Byte counts and kinds only: no pid of the other workload.
        self.assertNotIn("9999", str(body))
        waiting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiting


class FailedStartTest(ComputeAppTestCase):
    control = False  # the models cannot be unloaded

    async def test_the_failure_is_in_the_status(self):
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            self.assertEqual((await self.client.post(URL, json={})).status_code, 202)
            self.assertTrue(
                await self.advance_until(
                    lambda: not self.app.state.compute.full_gpu.start_pending
                )
            )
        body = (await self.client.get(URL)).json()
        self.assertEqual(body["last_failure"], "cannot_unload")
        self.assertNotEqual(body["state"], "on")
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)


class UserTest(ComputeAppTestCase):
    who = USER

    async def test_a_user_may_not_see_start_or_end_it(self):
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method):
                response = await self.client.request(method, URL)
                self.assertEqual(response.status_code, 403)
        self.assertIs(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertFalse(self.app.state.compute.full_gpu.start_pending)
        denials = self.decisions()
        self.assertEqual(len(denials), 3)
        self.assertFalse(any(event.decision == "allow" for event in denials))


class AnonymousTest(ComputeAppTestCase):
    who = None

    async def test_nobody_is_refused(self):
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method):
                response = await self.client.request(method, URL)
                self.assertEqual(response.status_code, 401)


class AdminTest(ComputeAppTestCase):
    who = ADMIN

    async def test_an_admin_may_start_and_end_it(self):
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            self.assertEqual(
                (await self.client.post(URL, json={"preempt": True})).status_code,
                202,
            )
            self.assertTrue(
                await self.advance_until(lambda: self.mode_state() is FullGpuState.ON)
            )
            self.assertEqual((await self.client.delete(URL)).status_code, 200)


HELD_BEFORE = uuid.uuid4()


class StoppedMainAtStartupTest(ComputeAppTestCase):
    # Codex review #168 (round 2, P1): an earlier process held a task and ended
    # while the main LLM was off the GPU. The configuration says it starts on
    # the GPU, but its runtime is not running: the task is not resumed until
    # the main LLM's processes are seen on the GPU.
    stopped = ("main",)
    held_before = (HELD_BEFORE,)

    async def test_held_tasks_wait_for_the_main_llm_to_be_seen(self):
        await self.clock.advance(30)
        self.assertEqual(self.holds.resumed, [])
        self.assertIs(self.mode_state(), FullGpuState.RESUMING)
        # The main LLM's runtime is started (by hand, here).
        self.fake.start_on_gpu("main")
        self.assertTrue(
            await self.advance_until(lambda: self.mode_state() is FullGpuState.OFF)
        )
        self.assertEqual(self.holds.resumed, [HELD_BEFORE])


class ShutdownTimeoutTest(ComputeAppTestCase):
    settings_overrides = {"shutdown_timeout_seconds": 1}

    async def test_a_recovery_that_cannot_finish_is_bounded(self):
        # The shutdown cancels the main LLM's unload: it failed, and is tried
        # again only after ``failed_retry_seconds``. The recovery gives up at
        # the shutdown timeout; the held task stays held for the next process.
        task_id = uuid.uuid4()
        self.holds.running.add(task_id)
        lease = (
            await self.scheduler.try_acquire(
                ComputeRequest(
                    ResourceClass.CODING,
                    deployment="main",
                    context_tokens=1_000,
                    task_id=task_id,
                )
            )
        ).lease
        unloading_main = asyncio.Event()
        never = asyncio.Event()
        unload = self.fake.unload

        async def stuck_main_unload(deployment):
            if deployment == "main":
                unloading_main.set()
                await never.wait()
            await unload(deployment)

        self.fake.unload = stuck_main_unload
        with self.assertLogs("paw_backend.compute", level="WARNING"):
            self.assertEqual((await self.client.post(URL, json={})).status_code, 202)
            await settle()
            await lease.release()
            self.assertTrue(await self.advance_until(unloading_main.is_set))
            with self.assertLogs("paw_backend.app", level="WARNING") as logs:
                started = asyncio.get_running_loop().time()
                await self.stop_app()
                elapsed = asyncio.get_running_loop().time() - started
        self.assertIn("next process resumes the held tasks", "".join(logs.output))
        self.assertLess(elapsed, 3)
        self.assertEqual(self.scheduler.status().mode, SchedulerMode.NORMAL)
        self.assertIs(
            self.scheduler.status().deployment("main").state, DeploymentState.FAILED
        )
        self.assertEqual(self.holds.held, [task_id])
        self.assertIsNone(self.app.state.compute.full_gpu)


if __name__ == "__main__":
    unittest.main()
