"""The statements the authentication code runs can use their indexes (PostgreSQL).

The statements are the ones the code really sends (captured from the driver), each
planned as a prepared statement in ``force_generic_plan`` mode: a partial index
is only usable by such a plan when its condition is written into the statement
and not passed as a parameter. Sequential and bitmap scans are switched off, so
the test answers "can the index serve this statement", which does not depend on
the statistics of a small table.
"""

import contextlib
import json
import re
import uuid
from datetime import timedelta

from sqlalchemy import event

from paw_backend.auth import tokens
from paw_backend.auth.models import ThrottleScope

from .auth_support import T0, PostgresAuthTestCase, requires_postgres


def nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", ()):
        yield from nodes(child)


@requires_postgres
class PlanTest(PostgresAuthTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.alice = await self.make_user("alice")
        await self.execute(
            """
            INSERT INTO auth_sessions (id, user_id, token_hash, remember_me,
                auth_method, created_at, last_used_at, idle_timeout_seconds,
                idle_expires_at, absolute_expires_at, revoked_at, revoked_reason)
            SELECT gen_random_uuid(), :u, sha256(i::text::bytea), false, 'password',
                :t, :t, 60, :t + interval '1 hour', :t + interval '2 hours',
                CASE WHEN i % 2 = 0 THEN :t + interval '30 minutes' END,
                CASE WHEN i % 2 = 0 THEN 'logout' END
            FROM generate_series(1, 3000) AS i
            """,
            u=self.alice.id,
            t=T0,
        )
        await self.execute(
            """
            INSERT INTO auth_throttles (scope, key_hash, attempts, last_attempt_at)
            SELECT 'login_account', sha256(i::text::bytea), 1, :t
            FROM generate_series(1, 3000) AS i
            """,
            t=T0 - timedelta(days=5),
        )

    @contextlib.contextmanager
    def captured(self):
        """The statements the services send, on either kind of engine they use."""
        captured: list[tuple[str, dict]] = []

        def capture(connection, cursor, statement, parameters, context, many):
            captured.append((statement, dict(parameters)))

        database = self.service_database
        engines = [database.engine.sync_engine]
        if database._abortable_engine is not None:
            engines.append(database._abortable_engine.sync_engine)
        for engine in engines:
            event.listen(engine, "before_cursor_execute", capture)
        try:
            yield captured
        finally:
            for engine in engines:
                event.remove(engine, "before_cursor_execute", capture)

    async def plan_of(self, sql: str, parameters: dict) -> dict:
        names = list(dict.fromkeys(re.findall(r"%\((\w+)\)s", sql)))
        numbered = re.sub(
            r"%\((\w+)\)s", lambda m: f"${names.index(m.group(1)) + 1}", sql
        )

        def literal(value) -> str:
            if value is None:
                return "NULL"
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, int):
                return str(value)
            if isinstance(value, bytes):
                return "'\\x" + value.hex() + "'"
            if isinstance(value, list):
                return "'{" + ",".join(str(v) for v in value) + "}'"
            return "'" + str(value).replace("'", "''") + "'"

        arguments = ", ".join(literal(parameters[name]) for name in names)
        async with self.database.engine.connect() as connection:
            for statement in (
                "SET plan_cache_mode = force_generic_plan",
                "SET enable_seqscan = off",
                "SET enable_bitmapscan = off",
                f"PREPARE checked AS {numbered}",
            ):
                await connection.exec_driver_sql(statement)
            try:
                result = await connection.exec_driver_sql(
                    f"EXPLAIN (FORMAT JSON) EXECUTE checked({arguments})"
                )
                return result.scalar()[0]["Plan"]
            finally:
                await connection.exec_driver_sql("DEALLOCATE checked")
                await connection.exec_driver_sql("RESET plan_cache_mode")
                await connection.exec_driver_sql("RESET enable_seqscan")
                await connection.exec_driver_sql("RESET enable_bitmapscan")

    async def warm_up(self) -> None:
        """Use the engine of the abortable transactions once, so that it exists."""
        await self.services.throttle.reset(
            ThrottleScope.LOGIN_ACCOUNT, tokens.account_key("warm-up")
        )

    def statements(self, captured, marker: str) -> list[tuple[str, dict]]:
        return [(s, p) for s, p in captured if marker in s]

    def indexes_used(self, plan: dict) -> set[str]:
        return {node["Index Name"] for node in nodes(plan) if "Index Name" in node}

    async def test_a_session_is_found_by_the_hash_of_its_id_through_the_unique_index(
        self,
    ):
        async with self.service_database.session() as session:
            issued = await self.services.sessions.create(
                session, self.alice.id, remember_me=False
            )
            await session.commit()
        token = issued.token
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.services.sessions.authenticate(session, token)
                await session.commit()
        ((sql, params),) = self.statements(captured, "SELECT s.id, s.user_id")
        plan = await self.plan_of(sql, params)
        self.assertIn(
            "uq_auth_sessions_token_hash", self.indexes_used(plan), json.dumps(plan)
        )

    async def test_the_device_list_uses_the_partial_index_of_active_sessions(self):
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.services.sessions.list_active(session, self.alice.id)
                await session.commit()
        ((sql, params),) = self.statements(captured, "ORDER BY s.last_used_at DESC")
        plan = await self.plan_of(sql, params)
        self.assertIn(
            "ix_auth_sessions_user_id_active", self.indexes_used(plan), json.dumps(plan)
        )

    async def test_each_purge_of_old_sessions_is_served_by_an_index(self):
        with self.captured() as captured:
            async with self.service_database.session() as session:
                await self.services.sessions.purge(session)
                await session.commit()
        deletes = self.statements(captured, "DELETE FROM auth_sessions")
        self.assertEqual(len(deletes), 2)
        used = [
            self.indexes_used(await self.plan_of(sql, params))
            for sql, params in deletes
        ]
        self.assertIn("ix_auth_sessions_absolute_expires_at", used[0])
        self.assertIn("ix_auth_sessions_revoked_at", used[1])

    async def test_the_purge_of_old_counters_uses_the_index_of_the_last_attempt(self):
        await self.warm_up()
        with self.captured() as captured:
            await self.services.throttle.reserve(
                ThrottleScope.LOGIN_ACCOUNT, tokens.account_key(f"new-{uuid.uuid4()}")
            )
        ((sql, params),) = self.statements(
            captured, "DELETE FROM auth_throttles WHERE (scope"
        )
        plan = await self.plan_of(sql, params)
        self.assertIn(
            "ix_auth_throttles_last_attempt_at",
            self.indexes_used(plan),
            json.dumps(plan),
        )

    async def test_a_reservation_conflicts_on_the_primary_key(self):
        await self.warm_up()
        with self.captured() as captured:
            await self.services.throttle.reserve(
                ThrottleScope.LOGIN_ACCOUNT, tokens.account_key("alice")
            )
        ((sql, params),) = self.statements(captured, "INSERT INTO auth_throttles AS t")
        plan = await self.plan_of(sql, params)
        arbiters = [
            index
            for node in nodes(plan)
            for index in node.get("Conflict Arbiter Indexes", ())
        ]
        self.assertEqual(arbiters, ["pk_auth_throttles"])

    async def test_the_purge_deletes_what_it_should_and_no_more(self):
        async with self.service_database.session() as session:
            self.services.sessions._clock = lambda: T0 + timedelta(days=400)
            deleted = await self.services.sessions.purge(session)
            await session.commit()
        # 3000 sessions: 1500 revoked long ago, and all of them past their
        # absolute limit by more than the retention: one batch of 50 goes.
        self.assertEqual(deleted, 50)
        self.assertEqual(
            await self.scalar("SELECT count(*) FROM auth_sessions"), 3000 - 50
        )
