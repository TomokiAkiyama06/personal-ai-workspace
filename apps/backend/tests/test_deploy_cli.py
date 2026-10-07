"""``python -m paw_backend.cli deploy-*`` (Issue #54, Decision 0079).

* the dispatcher hands every ``deploy-*`` command to ``cli.deploy``;
* without ``PAW_MIGRATION_DATABASE_URL`` nothing runs (exit 2), and no URL is
  ever printed;
* ``deploy-precheck`` reports each check (the audit partitions to the end of
  next month, the last Recovery backup, the Recovery Repository, no maintenance)
  as JSON and exits 3 when one failed;
* begin / drain / end change the maintenance, are audited, and the drain exits
  3 when it times out;
* the restore point commands create, verify and restore (the fakes of
  ``deploy_support``), need ``PAW_DEPLOY_ADMIN_DATABASE_URL`` for verify /
  restore and refuse an unverified point (exit 1).
"""

import asyncio
import io
import json
import os
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from paw_backend.cli import deploy as cli
from paw_backend.cli import dispatch
from paw_backend.cli import retention as retention_cli
from paw_backend.recovery.audit import RecoveryAction, record_recovery_outcome
from paw_backend.tasks import TaskService
from paw_backend.tasks.queueing import TaskQueue

from .deploy_support import write_fake_pg_tools
from .gate_support import ALWAYS_ACTIVE
from .support import paw_environment
from .task_support import (
    TEST_DATABASE_URL,
    migrate,
    new_database,
    requires_postgres,
    single_target,
)


def run(argv: list[str], **environment: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with paw_environment(**environment):
        code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class DispatchTest(unittest.TestCase):
    def test_every_deploy_command_goes_to_the_deploy_module(self):
        for name in cli.COMMANDS:
            with self.subTest(command=name):
                self.assertIs(dispatch.command_module([name]), cli)

    def test_nothing_runs_without_the_migration_url(self):
        code, out, err = run(["deploy-status"], PAW_DATABASE_URL="postgresql://a:b@h/d")
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertEqual(out, "")
        self.assertIn("PAW_MIGRATION_DATABASE_URL is not set", err)

    def test_usage_errors_are_refusals(self):
        for argv in (
            ["deploy-drain"],
            ["deploy-drain", "--timeout-seconds", "0"],
            ["deploy-restore-point-create", "--dir", "relative", "--label", "x"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(run(argv)[0], cli.EXIT_REFUSED)


@requires_postgres
class DeployCommandTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()

    async def asyncSetUp(self):
        self.database = new_database()
        self.addAsyncCleanup(self.database.dispose)
        await self.sql("DELETE FROM deploy_maintenance")
        self.addAsyncCleanup(self.sql, "DELETE FROM deploy_maintenance")
        await self.sql("TRUNCATE tasks CASCADE")
        self.started = await self.scalar("SELECT clock_timestamp()")

    async def sql(self, statement: str, **parameters) -> None:
        async with self.database.engine.begin() as connection:
            await connection.execute(text(statement), parameters)

    async def scalar(self, statement: str, **parameters):
        async with self.database.engine.connect() as connection:
            return (await connection.execute(text(statement), parameters)).scalar()

    async def deploy_rows(self) -> list[tuple[str, str]]:
        async with self.database.engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT action, reason FROM audit_events WHERE resource_kind ="
                    " 'deploy_update' AND recorded_at >= :since ORDER BY recorded_at"
                ),
                {"since": self.started},
            )
            return [tuple(row) for row in rows]

    async def owner_run(self, argv: list[str], **environment: str):
        return await asyncio.to_thread(
            run, argv, PAW_MIGRATION_DATABASE_URL=TEST_DATABASE_URL, **environment
        )

    async def test_status_is_json(self):
        code, out, err = await self.owner_run(["deploy-status"])
        self.assertEqual(code, cli.EXIT_OK, err)
        status = json.loads(out)
        self.assertRegex(status["schema_revision"], r"^\d{4}$")
        self.assertIsNone(status["maintenance"])
        self.assertEqual(status["tasks"]["running"], 0)
        self.assertTrue(status["drain"]["drained"])

    async def test_the_precheck_reports_each_check(self):
        recovery = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, recovery)
        # The backup timer's last run failed; the partitions are made below.
        await record_recovery_outcome(
            self.database,
            RecoveryAction.BACKUP_FAILED,
            "push:timeout",
            occurred_at=datetime.now(UTC),
        )
        code, out, err = await self.owner_run(["deploy-precheck"])
        self.assertEqual(code, cli.EXIT_FAILED, err)
        report = json.loads(out)
        checks = {check["name"]: check["ok"] for check in report["checks"]}
        self.assertEqual(
            set(checks),
            {
                "database",
                "maintenance_off",
                "audit_retention",
                "recovery_backup",
                "recovery_repository",
            },
        )
        self.assertFalse(checks["recovery_backup"])
        self.assertFalse(checks["recovery_repository"])
        self.assertFalse(report["ok"])

        def retention_run() -> int:
            with paw_environment(PAW_MIGRATION_DATABASE_URL=TEST_DATABASE_URL):
                return retention_cli.main(
                    ["audit-retention-run"], stdout=io.StringIO(), stderr=io.StringIO()
                )

        self.assertEqual(await asyncio.to_thread(retention_run), 0)
        await record_recovery_outcome(
            self.database,
            RecoveryAction.BACKUP_COMPLETED,
            "files=1",
            occurred_at=datetime.now(UTC),
        )
        code, out, err = await self.owner_run(
            ["deploy-precheck"], PAW_RECOVERY_REPOSITORY_DIR=recovery
        )
        report = json.loads(out)
        self.assertEqual(code, cli.EXIT_OK, report)
        self.assertTrue(all(check["ok"] for check in report["checks"]))

        await self.sql("INSERT INTO deploy_maintenance (to_release) VALUES ('r2')")
        code, out, _ = await self.owner_run(
            ["deploy-precheck"], PAW_RECOVERY_REPOSITORY_DIR=recovery
        )
        self.assertEqual(code, cli.EXIT_FAILED)
        checks = {c["name"]: c["ok"] for c in json.loads(out)["checks"]}
        self.assertFalse(checks["maintenance_off"])

    async def test_begin_drain_and_end(self):
        code, _, err = await self.owner_run(["deploy-drain", "--timeout-seconds", "1"])
        self.assertEqual(code, cli.EXIT_REFUSED, err)
        code, _, err = await self.owner_run(
            ["deploy-maintenance-begin", "--from-release", "r1", "--to-release", "r2"]
        )
        self.assertEqual(code, cli.EXIT_OK, err)
        status = json.loads((await self.owner_run(["deploy-status"]))[1])
        self.assertEqual(status["maintenance"]["to_release"], "r2")
        code, _, err = await self.owner_run(["deploy-drain", "--timeout-seconds", "1"])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("Drained", err)
        code, _, err = await self.owner_run(["deploy-maintenance-end"])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIsNone(await self.scalar("SELECT id FROM deploy_maintenance"))
        self.assertEqual(
            await self.deploy_rows(),
            [
                ("deploy.maintenance.started", "from=r1 to=r2 held=0"),
                ("deploy.maintenance.ended", "resumed=0 remaining=0"),
            ],
        )
        code, _, err = await self.owner_run(
            ["deploy-maintenance-begin", "--to-release", "../x"]
        )
        self.assertEqual(code, cli.EXIT_REFUSED, err)

    async def test_the_drain_times_out_on_a_live_claim(self):
        gate = ALWAYS_ACTIVE
        tasks = TaskService(self.database, project_gate=gate)
        queue = TaskQueue(self.database, project_gate=gate)
        event = await tasks.create_task(
            project_id=uuid.uuid4(),
            created_by=uuid.uuid4(),
            title="Fix the parser",
            repositories=single_target(uuid.uuid4()),
        )
        await queue.enqueue(event.task_id)
        # A worker holds the entry with a live lease (it is running a node).
        self.assertIsNotNone(await queue.claim_next("worker-1"))
        await self.owner_run(["deploy-maintenance-begin"])
        code, _, err = await self.owner_run(
            ["deploy-drain", "--timeout-seconds", "1", "--poll-seconds", "1"]
        )
        self.assertEqual(code, cli.EXIT_FAILED, err)
        self.assertIn("active_claims=1", err)
        self.assertIn("The maintenance stays on", err)


@requires_postgres
class RestorePointCommandTest(unittest.TestCase):
    def setUp(self):
        admin = make_url(TEST_DATABASE_URL).set(drivername="postgresql+psycopg")
        self.name = f"paw_rpc_{uuid.uuid4().hex[:8]}"
        self.admin = create_engine(admin, isolation_level="AUTOCOMMIT")
        self.addCleanup(self.admin.dispose)
        with self.admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{self.name}"'))
        self.addCleanup(self.drop_databases)
        url = admin.set(database=self.name)
        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num varchar(32))")
            )
            connection.execute(text("INSERT INTO alembic_version VALUES ('0188')"))
        engine.dispose()
        self.url = url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: os.system(f"rm -rf '{self.root}'"))
        self.pg_dump, self.pg_restore = write_fake_pg_tools(self.root)

    def drop_databases(self):
        with self.admin.connect() as connection:
            names = [
                row[0]
                for row in connection.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE :p"),
                    {"p": f"{self.name}%"},
                )
            ]
            for name in names:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))

    def command(self, name: str, **environment: str) -> tuple[int, str, str]:
        argv = [
            name,
            "--dir",
            str(self.root / "points"),
            "--label",
            "before-r2",
            "--pg-dump",
            self.pg_dump,
            "--pg-restore",
            self.pg_restore,
        ]
        return run(argv, PAW_MIGRATION_DATABASE_URL=self.url, **environment)

    def test_create_verify_restore(self):
        code, out, err = self.command("deploy-restore-point-create")
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(json.loads(out)["revision"], "0188")
        self.assertNotIn("pawtest", out + err)
        code, _, err = self.command("deploy-restore-point-restore")
        self.assertEqual(code, cli.EXIT_ENVIRONMENT, err)
        self.assertIn("PAW_DEPLOY_ADMIN_DATABASE_URL is not set", err)
        admin = {"PAW_DEPLOY_ADMIN_DATABASE_URL": TEST_DATABASE_URL}
        code, _, err = self.command("deploy-restore-point-restore", **admin)
        self.assertEqual(code, cli.EXIT_REFUSED, err)
        self.assertIn("not_verified", err)
        code, out, err = self.command("deploy-restore-point-verify", **admin)
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(json.loads(out)["verified"]["revision"], "0188")
        code, out, err = self.command("deploy-restore-point-restore", **admin)
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("The replaced database is kept as", err)
        # This scratch workspace has no audit_events: said, not fatal.
        self.assertIn("could not be recorded", err)
