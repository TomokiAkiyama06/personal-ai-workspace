"""System Health on PostgreSQL (PAW-066): the store and the database-backed sources.

* samples land in the bucket of the interval, once per bucket;
* the roll-up moves old rows to the next resolution, merging with what is
  there, counting every sample once; the purge removes what is past retention;
* an event is recorded when a component's severity changes, not otherwise;
* the series is aggregated to the requested step; the events come newest first;
* the SQL of the task, Memory Worker, connection and scheduled-job sources runs
  and counts what it should; the in-flight index exists and is partial.

Skipped unless ``PAW_TEST_DATABASE_URL`` is set.
"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from paw_backend.db import Database
from paw_backend.health.domain import Component, ComponentHealth, Severity, Status
from paw_backend.health.sources import (
    ConnectionSource,
    MemoryWorkerSource,
    ScheduledJobSource,
    TaskQueueSource,
)
from paw_backend.health.store import HealthStore
from paw_backend.health.wiring import SCHEDULED_JOBS
from paw_backend.recovery.audit import RecoveryAction, record_recovery_outcome

from .memory_support import sync_database_url
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate, requires_postgres


def health(component: Component, severity: Severity) -> ComponentHealth:
    return ComponentHealth(component, severity, Status.OK, ("a", "b"))


@requires_postgres
class HealthPostgresTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate("head")
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.clean()
        cls.engine.dispose()

    @classmethod
    def clean(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE health_metric_samples, health_events, connection_usage,"
                    " shared_connections, loop_failure_signatures, task_events,"
                    " tasks CASCADE"
                )
            )

    def database_url(self) -> str:
        return TEST_DATABASE_URL

    async def asyncSetUp(self) -> None:
        self.clean()
        self.database = Database(make_settings(database_url=self.database_url()))
        self.addAsyncCleanup(self.database.dispose)
        self.store = HealthStore(self.database)

    def sql(self, statement: str, **params):
        with self.engine.begin() as connection:
            result = connection.execute(text(statement), params)
            return result.fetchall() if result.returns_rows else []

    def insert_sample(self, metric, resolution, at, count, total, low, high):
        self.sql(
            "INSERT INTO health_metric_samples VALUES (:m, :r, :at, :c, :s, :lo, :hi)",
            m=metric,
            r=resolution,
            at=at,
            c=count,
            s=total,
            lo=low,
            hi=high,
        )

    def rows(self):
        return self.sql(
            "SELECT metric, resolution_seconds, bucket_start, sample_count,"
            " value_sum, value_min, value_max FROM health_metric_samples"
            " ORDER BY metric, resolution_seconds, bucket_start"
        )

    # -- samples ---------------------------------------------------------------

    async def test_one_sample_per_bucket(self):
        await self.store.add_samples(
            {"compute.x": 1.0, "compute.y": 2, "bad.inf": float("inf")},
            interval_seconds=30,
        )
        await self.store.add_samples({"compute.x": 5.0}, interval_seconds=30)
        rows = self.rows()
        self.assertEqual([r[0] for r in rows][:2], ["compute.x", "compute.y"])
        by = {r[0]: r for r in rows}
        self.assertNotIn("bad.inf", by)
        (now,) = self.sql("SELECT now()")[0]
        x = by["compute.x"]
        self.assertEqual(x[1], 0)
        self.assertLessEqual(x[2], now)
        self.assertEqual(x[2].second % 30, 0)
        self.assertEqual(x[2].microsecond, 0)
        # The second sample in the same bucket did not replace the first (unless
        # the bucket changed between the two calls, which a 30 s bucket rarely does).
        if (
            len(
                self.sql(
                    "SELECT 1 FROM health_metric_samples WHERE metric = 'compute.x'"
                )
            )
            == 1
        ):
            self.assertEqual(x[4], 1.0)

    async def test_a_name_the_schema_refuses_is_left_out(self):
        await self.store.add_samples(
            {"Not A Name": 1.0, "fine.name": 2.0}, interval_seconds=30
        )
        self.assertEqual([r[0] for r in self.rows()], ["fine.name"])
        with self.assertRaises(IntegrityError):
            self.insert_sample("Not A Name", 0, datetime.now(UTC), 1, 1.0, 1.0, 1.0)

    # -- roll-up -------------------------------------------------------------------

    async def test_roll_up_moves_old_rows_once_and_merges(self):
        (now,) = self.sql("SELECT now()")[0]
        old = (now - timedelta(days=2)).replace(second=0, microsecond=0)
        self.insert_sample("a.b", 0, old, 1, 1.0, 1.0, 1.0)
        self.insert_sample("a.b", 0, old + timedelta(seconds=30), 1, 5.0, 5.0, 5.0)
        # An existing minute row of the same bucket is merged with, not replaced.
        self.insert_sample("a.b", 60, old, 2, 4.0, 0.5, 3.5)
        recent = now - timedelta(hours=1)
        self.insert_sample("a.b", 0, recent, 1, 9.0, 9.0, 9.0)
        # A minute row older than 7 days goes to 5 minutes, a 5-minute row older
        # than 30 days to an hour, an hour older than the retention away.
        week = (now - timedelta(days=8)).replace(minute=0, second=0, microsecond=0)
        self.insert_sample("a.b", 60, week, 1, 2.0, 2.0, 2.0)
        self.insert_sample("a.b", 60, week + timedelta(minutes=1), 1, 4.0, 4.0, 4.0)
        month = (now - timedelta(days=31)).replace(minute=0, second=0, microsecond=0)
        self.insert_sample("a.b", 300, month, 3, 3.0, 1.0, 1.0)
        ancient = (now - timedelta(days=500)).replace(minute=0, second=0, microsecond=0)
        self.insert_sample("a.b", 3600, ancient, 1, 1.0, 1.0, 1.0)
        self.sql(
            "INSERT INTO health_events (occurred_at, component, severity, status)"
            " VALUES (now() - interval '500 days', 'database', 'info', 'ok'),"
            " (now(), 'database', 'warning', 'degraded')"
        )

        result = await self.store.roll_up(retention_days=400)

        self.assertEqual(result.moved, {0: 2, 60: 2, 300: 1})
        self.assertEqual((result.purged_samples, result.purged_events), (1, 1))
        rows = {(r[1], r[2]): r[3:] for r in self.rows()}
        self.assertEqual(rows[(60, old)], (4, 10.0, 0.5, 5.0))
        self.assertEqual(rows[(0, recent)], (1, 9.0, 9.0, 9.0))
        self.assertEqual(rows[(300, week)], (2, 6.0, 2.0, 4.0))
        self.assertEqual(rows[(3600, month)], (3, 3.0, 1.0, 1.0))
        self.assertNotIn((3600, ancient), rows)
        self.assertEqual(len(rows), 4)
        # Nothing moves twice.
        again = await self.store.roll_up(retention_days=400)
        self.assertEqual(again.moved, {0: 0, 60: 0, 300: 0})
        self.assertEqual(sum(r[3] for r in self.rows()), 10)

    # -- events --------------------------------------------------------------------

    async def test_events_on_severity_change_only(self):
        first = [
            health(Component.DATABASE, Severity.INFO),
            health(Component.COMPUTE, Severity.INFO),
        ]
        self.assertEqual(await self.store.record_changes(first), 2)
        self.assertEqual(await self.store.record_changes(first), 0)
        changed = [
            health(Component.DATABASE, Severity.CRITICAL),
            health(Component.COMPUTE, Severity.INFO),
        ]
        self.assertEqual(await self.store.record_changes(changed), 1)
        since = datetime.now(UTC) - timedelta(hours=1)
        events = await self.store.events(since=since, limit=10)
        self.assertEqual(
            [(e.component, e.severity, e.previous_severity) for e in events],
            [
                ("database", "critical", "info"),
                ("compute", "info", None),
                ("database", "info", None),
            ],
        )
        self.assertEqual(events[0].reasons, ("a", "b"))
        self.assertEqual(len(await self.store.events(since=since, limit=1)), 1)

    # -- series --------------------------------------------------------------------

    async def test_series_aggregates_across_resolutions(self):
        base = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        self.insert_sample("a.b", 0, base, 1, 1.0, 1.0, 1.0)
        self.insert_sample("a.b", 0, base + timedelta(seconds=30), 1, 3.0, 3.0, 3.0)
        self.insert_sample("a.b", 60, base + timedelta(minutes=5), 2, 10.0, 4.0, 6.0)
        self.insert_sample("other.metric", 0, base, 1, 99.0, 99.0, 99.0)
        points = await self.store.series(
            "a.b",
            since=base,
            until=base + timedelta(hours=1),
            step_seconds=300,
            limit=10,
        )
        self.assertEqual(
            [(p.bucket_start, p.count, p.mean, p.minimum, p.maximum) for p in points],
            [
                (base, 2, 2.0, 1.0, 3.0),
                (base + timedelta(minutes=5), 2, 5.0, 4.0, 6.0),
            ],
        )

    # -- the sources -----------------------------------------------------------------

    def insert_task(self, state, *, wait=None, age=timedelta()):
        task_id = uuid.uuid4()
        self.sql(
            "INSERT INTO tasks (id, project_id, created_by, title, input, state,"
            " wait_reason, attempt, retry_count, version, created_at, updated_at)"
            " VALUES (:id, :p, :u, 't', '{}'::jsonb, :s, :w, 1, 0, 1,"
            " now() - :age, now() - :age)",
            id=task_id,
            p=uuid.uuid4(),
            u=uuid.uuid4(),
            s=state,
            w=wait,
            age=age,
        )
        return task_id

    def insert_event(self, task, command, from_state, to_state, age):
        self.sql(
            "INSERT INTO task_events (task_id, attempt, retry_count, command,"
            " from_state, to_state, actor_kind, task_version, created_at)"
            " VALUES (:t, 1, 1, :c, :f, :to, 'system', 2, now() - :age)",
            t=task,
            c=command,
            f=from_state,
            to=to_state,
            age=age,
        )

    async def test_task_counts(self):
        self.insert_task("queued")
        self.insert_task("running")
        self.insert_task("waiting", wait="resource")
        for age in (timedelta(minutes=5), timedelta(hours=5), timedelta(days=5)):
            failed = self.insert_task("failed", age=age)
            self.insert_event(failed, "fail", "running", "failed", age)
        self.insert_task("completed", age=timedelta(hours=1))
        self.insert_task("completed", age=timedelta(days=2))
        result = await TaskQueueSource(self.database).check()
        metrics = result.metrics
        self.assertEqual(
            (metrics["queued"], metrics["running"], metrics["waiting_resource"]),
            (1, 1, 1),
        )
        self.assertEqual(
            (metrics["failed_last_hour"], metrics["failed_last_day"]), (1, 2)
        )
        self.assertEqual(metrics["completed_last_day"], 1)
        self.assertIs(result.severity, Severity.WARNING)

    async def test_failures_of_a_retried_task_are_each_counted(self):
        # Five failures in the last hour, each retried: the task is running again,
        # yet the failures reach the error threshold (Codex P1 on PR #170).
        task = self.insert_task("running")
        for minutes in range(5):
            age = timedelta(minutes=10 + minutes)
            self.insert_event(task, "fail", "running", "failed", age)
            self.insert_event(task, "retry", "failed", "queued", age)
        result = await TaskQueueSource(self.database).check()
        self.assertEqual(
            (result.metrics["failed_last_hour"], result.metrics["retries_last_hour"]),
            (5, 5),
        )
        self.assertIs(result.severity, Severity.ERROR)
        self.assertIn("task_failures", result.reasons)

    async def test_retries_and_loops(self):
        task = self.insert_task("running")
        for age in ("5 minutes", "2 hours"):
            self.sql(
                "INSERT INTO task_events (task_id, attempt, retry_count, command,"
                " from_state, to_state, actor_kind, task_version, created_at)"
                " VALUES (:t, 1, 1, 'retry', 'failed', 'queued', 'system', 2,"
                f" now() - interval '{age}')",
                t=task,
            )
        signature = "a" * 64
        # Three times the same failure (a loop), once another one.
        for sig in (signature, signature, signature, "b" * 64):
            self.sql(
                "INSERT INTO loop_failure_signatures (task_id, attempt, approach,"
                " signature) VALUES (:t, 1, 0, :s)",
                t=task,
                s=sig,
            )
        result = await TaskQueueSource(self.database).check()
        self.assertEqual(
            (result.metrics["retries_last_hour"], result.metrics["loops_last_hour"]),
            (1, 1),
        )
        self.assertEqual(result.reasons, ("loops_detected", "task_retries"))

    async def test_a_loop_whose_earlier_failures_are_old_is_counted(self):
        # The detector has no time cutoff: the third same failure now is a loop
        # even when the first two were hours ago (Codex P1 on PR #170).
        task = self.insert_task("running")
        signature = "c" * 64
        for age in ("3 hours", "2 hours", "5 minutes"):
            self.sql(
                "INSERT INTO loop_failure_signatures (task_id, attempt, approach,"
                " signature, created_at) VALUES (:t, 1, 0, :s,"
                f" now() - interval '{age}')",
                t=task,
                s=signature,
            )
        # A loop detected hours ago, nothing since: not one of the last hour.
        old = self.insert_task("running")
        for age in ("5 hours", "4 hours", "3 hours"):
            self.sql(
                "INSERT INTO loop_failure_signatures (task_id, attempt, approach,"
                " signature, created_at) VALUES (:t, 1, 0, :s,"
                f" now() - interval '{age}')",
                t=old,
                s=signature,
            )
        result = await TaskQueueSource(self.database).check()
        self.assertEqual(result.metrics["loops_last_hour"], 1)

    async def test_a_late_event_keeps_the_time_it_was_seen(self):
        seen = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
        await self.store.record_changes(
            [health(Component.DATABASE, Severity.CRITICAL)], occurred_at=seen
        )
        (event,) = await self.store.events(since=seen, limit=5)
        self.assertEqual(event.occurred_at, seen)

    async def test_a_change_older_than_the_last_event_is_not_recorded(self):
        # Two processes saw the same outage; A recorded critical then info, B's
        # older critical comes afterwards and must not open a second outage.
        base = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
        for severity, minute in ((Severity.CRITICAL, 1), (Severity.INFO, 3)):
            await self.store.record_changes(
                [health(Component.DATABASE, severity)],
                occurred_at=base + timedelta(minutes=minute),
            )
        for severity, minute in ((Severity.CRITICAL, 2), (Severity.INFO, 4)):
            recorded = await self.store.record_changes(
                [health(Component.DATABASE, severity)],
                occurred_at=base + timedelta(minutes=minute),
            )
            self.assertEqual(recorded, 0)
        events = await self.store.events(since=base, limit=10)
        self.assertEqual(
            [(e.severity, e.occurred_at) for e in events],
            [
                ("info", base + timedelta(minutes=3)),
                ("critical", base + timedelta(minutes=1)),
            ],
        )
        # A change after the last event is still recorded.
        later = base + timedelta(minutes=5)
        recorded = await self.store.record_changes(
            [health(Component.DATABASE, Severity.CRITICAL)], occurred_at=later
        )
        self.assertEqual(recorded, 1)

    async def test_a_time_ahead_of_the_database_is_not_a_barrier(self):
        # A host whose clock runs ahead must not make the next changes of the
        # other processes look stale (Codex P2 on PR #170).
        ahead = datetime.now(UTC) + timedelta(hours=1)
        await self.store.record_changes(
            [health(Component.DATABASE, Severity.CRITICAL)], occurred_at=ahead
        )
        (event,) = await self.store.events(since=ahead - timedelta(hours=2), limit=5)
        self.assertLess(event.occurred_at, ahead)
        recorded = await self.store.record_changes(
            [health(Component.DATABASE, Severity.INFO)],
            occurred_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        self.assertEqual(recorded, 1)

    async def test_connections(self):
        self.sql(
            "INSERT INTO shared_connections (kind, secret_handle, status, enabled,"
            " checked_at, created_at, updated_at) VALUES ('claude', :h, 'connected',"
            " true, now() - interval '10 seconds', now(), now())",
            h="cred_" + "0" * 32,
        )
        task_id = self.insert_task("running")
        self.sql(
            "INSERT INTO connection_usage (user_id, task_id, project_id, kind, model,"
            " purpose, status, started_at) VALUES (:u, :t, :p, 'claude', 'm',"
            " 'coding', 'in_flight', now() - interval '1 minute')",
            u=uuid.uuid4(),
            t=task_id,
            p=uuid.uuid4(),
        )
        result = await ConnectionSource(self.database).check()
        codex, claude = result.parts
        self.assertEqual((codex["configured"], claude["available"]), (False, True))
        self.assertEqual(claude["in_flight"], 1)
        self.assertGreaterEqual(claude["oldest_in_flight_seconds"], 59)
        self.assertGreaterEqual(claude["checked_seconds_ago"], 9)

    async def test_memory_worker_on_an_empty_queue(self):
        result = await MemoryWorkerSource(self.database).check()
        self.assertEqual(
            (result.metrics["pending"], result.metrics["dead_last_day"]), (0, 0)
        )
        self.assertEqual(
            (result.metrics["waiting_for_worker"], result.metrics["expired_leases"]),
            (0, 0),
        )

    async def test_scheduled_job_counts_failures_since_the_last_success(self):
        (backup,) = [
            j for j in SCHEDULED_JOBS if j.component is Component.RECOVERY_BACKUP
        ]
        source = ScheduledJobSource(self.database, backup)
        at = datetime.now(UTC)
        await record_recovery_outcome(
            self.database, RecoveryAction.BACKUP_FAILED, "commit:early", occurred_at=at
        )
        await record_recovery_outcome(
            self.database, RecoveryAction.BACKUP_COMPLETED, "files=1", occurred_at=at
        )
        for _ in range(3):
            await record_recovery_outcome(
                self.database,
                RecoveryAction.BACKUP_FAILED,
                "push:rejected",
                occurred_at=at,
            )
        result = await source.check()
        self.assertEqual(result.metrics["consecutive_failures"], 3)
        self.assertEqual(result.metrics["last_run"], "failed")
        self.assertEqual(result.metrics["last_failure"], "push:rejected")
        self.assertIs(result.severity, Severity.ERROR)
        await record_recovery_outcome(
            self.database, RecoveryAction.BACKUP_COMPLETED, "files=1", occurred_at=at
        )
        result = await source.check()
        self.assertEqual(
            (result.severity, result.metrics["consecutive_failures"]),
            (Severity.INFO, 0),
        )

    def test_the_indexes_of_the_sources(self):
        indexes = dict(
            self.sql(
                "SELECT indexname, indexdef FROM pg_indexes WHERE indexname IN"
                " ('ix_task_events_retry_fail_created_at',"
                " 'ix_loop_failure_signatures_created_at',"
                " 'ix_tasks_active_state', 'ix_tasks_ended_updated_at')"
            )
        )
        self.assertIn(
            "WHERE ((command)::text = ANY ((ARRAY['retry'::character varying,"
            " 'fail'::character varying])::text[]))",
            indexes["ix_task_events_retry_fail_created_at"],
        )
        self.assertIn("(created_at)", indexes["ix_loop_failure_signatures_created_at"])
        self.assertIn("(state) WHERE", indexes["ix_tasks_active_state"])
        self.assertIn("(updated_at) WHERE", indexes["ix_tasks_ended_updated_at"])

    def test_the_task_counts_use_the_partial_indexes(self):
        # The sample runs every 10-30 seconds: it must not read every task that
        # ever ended, nor every task event (Codex P2 on PR #170).
        from paw_backend.health.sources import _TASK_EVENTS, _TASKS

        with self.engine.begin() as connection:
            connection.execute(text("SET LOCAL enable_seqscan = off"))
            plans = {
                sql: "\n".join(
                    row[0] for row in connection.execute(text(f"EXPLAIN {sql}"))
                )
                for sql in (_TASKS, _TASK_EVENTS)
            }
        # With sequential scans off, one is planned only when no index serves.
        for plan in plans.values():
            self.assertNotIn("Seq Scan", plan)
        self.assertIn("ix_task_events_retry_fail_created_at", plans[_TASK_EVENTS])

    def test_the_in_flight_index_is_partial(self):
        ((definition,),) = self.sql(
            "SELECT indexdef FROM pg_indexes WHERE indexname ="
            " 'ix_connection_usage_in_flight_started_at'"
        )
        self.assertIn("(started_at)", definition)
        self.assertIn("WHERE (status = 'in_flight'::text)", definition)
