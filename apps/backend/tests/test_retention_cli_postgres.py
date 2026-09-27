"""``python -m paw_backend.cli audit-retention-*`` on a real PostgreSQL (Issue #117).

Skipped unless ``PAW_TEST_DATABASE_URL`` is set. The command connects with
``PAW_MIGRATION_DATABASE_URL`` (the table owner: here, the test database's own
user), exactly as the systemd service of ``deploy/systemd`` runs it. The failure
cases run it as throw-away, non-owner roles, the realistic way it breaks: a
misconfigured URL that names the application's role instead of the owner.
"""

import asyncio
import io
import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from sqlalchemy import text

from paw_backend.authz.retention import (
    AuditRetentionService,
    MaintenanceAlreadyRunningError,
    RetentionAction,
)
from paw_backend.cli import retention as cli

from .identity_support import ROLE_PASSWORD, url_for_role
from .support import paw_environment
from .task_support import TEST_DATABASE_URL, migrate, new_database, requires_postgres

BACKEND_DIR = Path(__file__).resolve().parents[1]
RUN_ID = uuid.uuid4().hex[:10]


def run(argv: list[str], **environment: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with paw_environment(**environment):
        code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@requires_postgres
class RetentionCommandTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()  # head

    async def asyncSetUp(self) -> None:
        self.database = new_database()
        self.addAsyncCleanup(self.database.dispose)
        self.started = await self.scalar("SELECT clock_timestamp()")

    async def scalar(self, sql: str, **parameters):
        async with self.database.engine.connect() as connection:
            return (await connection.execute(text(sql), parameters)).scalar()

    async def outcome_rows(self) -> list[tuple[str, str]]:
        """The run-level retention rows written since this test started."""
        async with self.database.engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT action, reason FROM audit_events "
                    "WHERE action IN (:completed, :failed) AND recorded_at >= :since "
                    "ORDER BY recorded_at"
                ),
                {
                    "completed": RetentionAction.MAINTENANCE_COMPLETED.value,
                    "failed": RetentionAction.MAINTENANCE_FAILED.value,
                    "since": self.started,
                },
            )
            return [tuple(row) for row in rows]

    async def owner_run(self, argv: list[str]) -> tuple[int, str, str]:
        return await asyncio.to_thread(
            run, argv, PAW_MIGRATION_DATABASE_URL=TEST_DATABASE_URL
        )


class RunCommandTest(RetentionCommandTestCase):
    async def test_a_run_as_the_owner_succeeds_and_is_audited(self):
        code, out, err = await self.owner_run(["audit-retention-run"])

        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(out, "")
        self.assertIn("completed", err)
        rows = await self.outcome_rows()
        self.assertEqual(len(rows), 1)
        action, reason = rows[0]
        self.assertEqual(action, RetentionAction.MAINTENANCE_COMPLETED.value)
        self.assertRegex(reason, r"^created=\d+ archived=\d+ purged=\d+$")

    async def test_a_second_run_is_idempotent_and_still_audited(self):
        first, _, err = await self.owner_run(["audit-retention-run"])
        self.assertEqual(first, cli.EXIT_OK, err)
        second, _, err = await self.owner_run(["audit-retention-run"])
        self.assertEqual(second, cli.EXIT_OK, err)
        rows = await self.outcome_rows()
        self.assertEqual(
            rows[-1],
            (
                RetentionAction.MAINTENANCE_COMPLETED.value,
                "created=0 archived=0 purged=0",
            ),
        )

    async def test_after_a_run_the_next_three_months_are_covered(self):
        code, _, err = await self.owner_run(["audit-retention-run"])
        self.assertEqual(code, cli.EXIT_OK, err)
        code, _, err = await self.owner_run(
            ["audit-retention-check", "--months-ahead", "3"]
        )
        self.assertEqual(code, cli.EXIT_OK, err)

    async def test_the_check_reports_a_gap_beyond_the_horizon(self):
        code, _, err = await self.owner_run(["audit-retention-run"])
        self.assertEqual(code, cli.EXIT_OK, err)
        code, _, err = await self.owner_run(
            ["audit-retention-check", "--months-ahead", "12"]
        )
        self.assertEqual(code, cli.EXIT_MAINTENANCE_FAILED)
        self.assertIn("not covered", err)

    async def test_a_concurrent_run_is_refused_while_the_lock_is_held(self):
        service = AuditRetentionService(self.database)
        async with service.maintenance_lock():
            code, _, err = await self.owner_run(["audit-retention-run"])
            self.assertEqual(code, cli.EXIT_REFUSED)
            self.assertIn("already running", err)
            with self.assertRaises(MaintenanceAlreadyRunningError):
                async with AuditRetentionService(new_database()).maintenance_lock():
                    pass
        # Released: the next run goes ahead.
        code, _, err = await self.owner_run(["audit-retention-run"])
        self.assertEqual(code, cli.EXIT_OK, err)

    async def test_the_module_entry_point_runs_as_a_separate_process(self):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("PAW_")}
        environment["PAW_MIGRATION_DATABASE_URL"] = TEST_DATABASE_URL
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "paw_backend.cli", "audit-retention-run"],
            cwd=BACKEND_DIR,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class RunAsAnotherRoleTest(RetentionCommandTestCase):
    """Misconfigured: ``PAW_MIGRATION_DATABASE_URL`` names a role that is not

    the owner. The steps fail with a permission error; the command exits non-zero
    and — when the role may at least INSERT into ``audit_events``, as the
    application's role may — records the failure.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.writer = f"paw_retention_writer_{RUN_ID}"
        cls.nobody = f"paw_retention_nobody_{RUN_ID}"
        asyncio.run(cls._create_roles())

    @classmethod
    def tearDownClass(cls) -> None:
        asyncio.run(cls._drop_roles())
        super().tearDownClass()

    @classmethod
    async def _create_roles(cls) -> None:
        database = new_database()
        try:
            async with database.engine.begin() as connection:
                for role in (cls.writer, cls.nobody):
                    await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
                    await connection.execute(
                        text(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{ROLE_PASSWORD}'")
                    )
                    await connection.execute(
                        text(f'GRANT USAGE ON SCHEMA public TO "{role}"')
                    )
                    database_name = await connection.scalar(
                        text("SELECT current_database()")
                    )
                    await connection.execute(
                        text(f'GRANT CONNECT ON DATABASE "{database_name}" TO "{role}"')
                    )
                # What PAW_APP_DATABASE_ROLE has (Migration 0086): SELECT too,
                # since the INSERT returns the server-side primary key column.
                await connection.execute(
                    text(f'GRANT INSERT, SELECT ON audit_events TO "{cls.writer}"')
                )
        finally:
            await database.dispose()

    @classmethod
    async def _drop_roles(cls) -> None:
        database = new_database()
        try:
            async with database.engine.begin() as connection:
                for role in (cls.writer, cls.nobody):
                    await connection.execute(text(f'DROP OWNED BY "{role}"'))
                    await connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        finally:
            await database.dispose()

    async def test_a_failed_run_exits_non_zero_and_is_audited(self):
        code, out, err = await asyncio.to_thread(
            run,
            ["audit-retention-run"],
            PAW_MIGRATION_DATABASE_URL=url_for_role(self.writer),
        )
        self.assertEqual(code, cli.EXIT_MAINTENANCE_FAILED)
        self.assertIn("ensure_partitions", err)
        self.assertIn("was recorded", err)
        self.assertNotIn(ROLE_PASSWORD, out + err)
        rows = await self.outcome_rows()
        self.assertEqual(
            rows,
            [
                (
                    RetentionAction.MAINTENANCE_FAILED.value,
                    "ensure_partitions:ProgrammingError",
                )
            ],
        )

    async def test_a_failure_that_cannot_even_be_audited_still_exits_non_zero(self):
        code, _, err = await asyncio.to_thread(
            run,
            ["audit-retention-run"],
            PAW_MIGRATION_DATABASE_URL=url_for_role(self.nobody),
        )
        self.assertEqual(code, cli.EXIT_MAINTENANCE_FAILED)
        self.assertIn("could not be recorded", err)
        self.assertEqual(await self.outcome_rows(), [])

    async def test_the_check_as_another_role_is_an_environment_error(self):
        code, _, err = await asyncio.to_thread(
            run,
            ["audit-retention-check"],
            PAW_MIGRATION_DATABASE_URL=url_for_role(self.nobody),
        )
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertNotIn(ROLE_PASSWORD, err)


if __name__ == "__main__":
    unittest.main()
