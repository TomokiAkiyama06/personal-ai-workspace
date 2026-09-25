"""The statements of the admission use their indexes (planned as prepared statements).

An index is only useful if the statement, planned WITHOUT the values (the generic plan
of a prepared statement, which a driver may cache) and WITH them, can use it. The
history is long (20,000 rows of many users) and one user's window is short: a Seq
Scan, or a scan that reads rows only to throw them away, would make every admission
read the whole history under the quota lock.
"""

import unittest
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from paw_backend.connections import store

from .task_support import PostgresTaskTestCase, requires_postgres

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


@requires_postgres
class AdmissionPlanTest(PostgresTaskTestCase):
    HISTORY = 20_000

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.task = await self.create_task()
        # Calls are spread over 2,000 tasks (a task has a handful of calls), as the
        # statistics of a real table are: the planner must not think that one task
        # owns every row.
        async with self.database.engine.begin() as connection:
            await connection.execute(text("TRUNCATE connection_usage"))
            await connection.execute(
                text(
                    "INSERT INTO tasks (id, project_id, created_by, title, input,"
                    " state, attempt, retry_count, version, created_at, updated_at)"
                    " SELECT gen_random_uuid(), gen_random_uuid(), :u, 'T',"
                    " CAST('{}' AS jsonb), 'running', 1, 0, 1, now(), now()"
                    " FROM generate_series(1, 2000)"
                ),
                {"u": self.user_id},
            )
            # Other users' calls, half of them of the other kind.
            await connection.execute(
                text(
                    "WITH t AS (SELECT id, row_number() OVER () AS n FROM tasks)"
                    " INSERT INTO connection_usage (user_id, task_id, project_id,"
                    " kind, model, purpose, status, input_tokens, started_at,"
                    " finished_at, duration_ms) SELECT gen_random_uuid(), t.id,"
                    " gen_random_uuid(), (ARRAY['codex', 'claude'])[1 + g % 2],"
                    " 'seeded', 'coding', 'succeeded', 5,"
                    " :now - g * interval '1 minute', :now - g * interval '1 minute',"
                    " 0 FROM generate_series(1, :n) g JOIN t ON t.n = 1 + g % 2000"
                ),
                {"now": NOW, "n": self.HISTORY},
            )
            # This user: 24 calls in the window, then 500 before it, one kind.
            await connection.execute(
                text(
                    "INSERT INTO connection_usage (user_id, task_id, project_id,"
                    " kind, model, purpose, status, input_tokens, started_at,"
                    " finished_at, duration_ms) SELECT :u, :t, gen_random_uuid(),"
                    " 'codex', 'seeded', 'coding', 'succeeded', 5,"
                    " :now - g * interval '1 hour', :now - g * interval '1 hour', 0"
                    " FROM generate_series(1, 524) g"
                ),
                {"u": self.user_id, "t": self.task, "now": NOW},
            )
            await connection.execute(text("ANALYZE connection_usage"))
            await connection.execute(text("ANALYZE tasks"))

    async def asyncTearDown(self):
        async with self.database.engine.begin() as connection:
            await connection.execute(text("TRUNCATE connection_usage"))

    async def assert_uses_only(self, sql: str, parameters: dict, index: str, rows=None):
        for mode in ("force_custom_plan", "force_generic_plan"):
            with self.subTest(plan_cache_mode=mode, statement=sql[:60]):
                (explained,) = await self.plan(sql, parameters, mode, analyze=True)
                nodes = list(self.plan_nodes(explained["Plan"]))
                types = {node["Node Type"] for node in nodes}
                self.assertNotIn("Seq Scan", types)
                (scan,) = (
                    n for n in nodes if n.get("Relation Name") == "connection_usage"
                )
                self.assertEqual(scan["Index Name"], index, scan)
                self.assertEqual(scan.get("Rows Removed by Filter", 0), 0, scan)
                if rows is not None:
                    self.assertEqual(scan["Actual Rows"], rows, scan)

    async def test_the_window_sums_read_only_the_rows_of_the_window(self):
        # 524 calls of this user, one an hour back; the window is 24 hours long:
        # started_at >= now - 24h holds for g = 1..24, and only those are read.
        await self.assert_uses_only(
            store.SUMS_SQL,
            {
                "user": self.user_id,
                "kind": "codex",
                "since": (NOW - timedelta(hours=24)).isoformat(),
            },
            "ix_connection_usage_user_id_kind_started_at",
            rows=24,
        )

    async def test_the_running_task_lookup_uses_the_task_index(self):
        await self.assert_uses_only(
            store.CONTINUING_SQL,
            {"task": str(uuid.uuid4()), "kind": "codex"},
            "ix_connection_usage_task_id_kind",
        )


if __name__ == "__main__":
    unittest.main()
