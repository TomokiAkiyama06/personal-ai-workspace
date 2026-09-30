"""Full GPU Mode over HTTP on PostgreSQL (issue #165, Decision 0058).

The application's own composition: ``create_app(compute=...)`` builds
``FullGpuMode`` in the lifespan over ``PostgresTaskHolds`` of the application's
task service and queue. A running task whose node holds a local GPU lease is
held (``waiting`` for a resource, by the policy) when Full GPU Mode is started
through ``POST``, and resumed (unblocked, with a new queue entry) after ``DELETE``, once
the main LLM is back. Only the GPU is fake (``compute_support``); the Project
state gate is the always-active one of the task tests (the projects of these
tasks are not stored).
"""

import uuid
from unittest.mock import patch

import httpx

from paw_backend.app import create_app
from paw_backend.authz import Authorizer, InMemoryAuditSink
from paw_backend.compute import (
    HOLD_REASON,
    RESUME_REASON,
    ComputeRequest,
    FullGpuState,
    ResourceClass,
)
from paw_backend.tasks import TaskCommand

from .authz_support import StaticProvider
from .compute_support import settle
from .gate_support import ALWAYS_ACTIVE
from .queueing_support import PostgresQueueingTestCase, requires_postgres
from .support import make_settings
from .task_support import TEST_DATABASE_URL
from .test_compute_app import OWNER, QUIET, URL, fake_gpu


@requires_postgres
class FullGpuOverHttpTest(PostgresQueueingTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.owner_sql("TRUNCATE tasks CASCADE")
        setup, self.probe, self.fake, self.clock = fake_gpu()
        patcher = patch(
            "paw_backend.orchestrator.composition.ProjectStateGate",
            lambda: ALWAYS_ACTIVE,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        settings = make_settings(database_url=TEST_DATABASE_URL, **QUIET)
        self.app = create_app(settings, compute=setup)
        self.app.state.principal_provider = StaticProvider(OWNER)
        self.app.state.authorizer = Authorizer(InMemoryAuditSink())
        self.scheduler = self.app.state.compute.scheduler
        lifespan = self.app.router.lifespan_context(self.app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost"
        )
        self.addAsyncCleanup(self.client.aclose)
        await settle()

    async def running_task(self) -> uuid.UUID:
        task_id = await self.create_task()
        await self.queue.enqueue(task_id)
        entry = await self.queue.claim_next("worker-1")
        self.entry = entry
        await self.service.execute(task_id, TaskCommand.START, actor=self.system)
        return task_id

    async def last_event(self, task_id):
        return (
            await self.rows(
                "SELECT command, actor_kind, reason FROM task_events "
                "WHERE task_id = :t ORDER BY seq DESC LIMIT 1",
                t=task_id,
            )
        )[0]

    async def state(self, task_id) -> str:
        return (await self.rows("SELECT state FROM tasks WHERE id = :t", t=task_id))[0][
            "state"
        ]

    async def advance_until(self, predicate, *, limit=200):
        for _ in range(limit):
            if await predicate():
                return True
            await self.clock.advance(1)
        return await predicate()

    def mode_state(self):
        return self.app.state.compute.full_gpu.status().state

    async def test_a_task_is_held_through_http_and_resumed_after_the_end(self):
        task_id = await self.running_task()
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
        self.assertIsNotNone(lease)

        with self.assertLogs("paw_backend.compute", level="WARNING"):
            response = await self.client.post(URL, json={"drain_seconds": 120})
            self.assertEqual(response.status_code, 202)

            async def held():
                return await self.state(task_id) == "waiting"

            self.assertTrue(await self.advance_until(held))
            self.assertEqual(
                await self.last_event(task_id),
                {"command": "wait", "actor_kind": "policy", "reason": HOLD_REASON},
            )
            # The node that was running finishes; its worker completes the entry.
            await lease.release()
            await self.queue.complete(self.entry.id, "worker-1", self.entry.claim_count)

            async def on():
                return self.mode_state() is FullGpuState.ON

            self.assertTrue(await self.advance_until(on))
            self.assertEqual((await self.client.get(URL)).json()["held_tasks"], 1)

            self.assertEqual((await self.client.delete(URL)).status_code, 200)

            async def resumed():
                return self.mode_state() is FullGpuState.OFF

            self.assertTrue(await self.advance_until(resumed))
        self.assertEqual(
            await self.last_event(task_id),
            {"command": "unblock", "actor_kind": "policy", "reason": RESUME_REASON},
        )
        # Unblocked (running again) and back in the queue for a worker.
        self.assertEqual(await self.state(task_id), "running")
        entries = await self.rows(
            "SELECT status FROM queue_entries WHERE task_id = :t ORDER BY id",
            t=task_id,
        )
        self.assertEqual(entries[-1]["status"], "queued")
