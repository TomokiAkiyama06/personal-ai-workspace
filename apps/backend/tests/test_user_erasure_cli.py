"""``python -m paw_backend.cli user-erasure-run`` (Issue #127, Decision 0043).

The first classes need no database (arguments, configuration, dispatch, the systemd
units read as text); ``RunCommandTest`` runs the command against a real PostgreSQL
as the table owner (skipped unless ``PAW_TEST_DATABASE_URL`` is set), the way the
systemd service runs it.
"""

import asyncio
import configparser
import io
import unittest
import uuid
from pathlib import Path

from paw_backend.auth.onboarding.erasure import UserErasureService
from paw_backend.cli import erasure as cli

from .auth_support import TEST_DATABASE_URL, requires_postgres
from .support import paw_environment
from .test_user_erasure import ErasureTestCase

BACKEND_DIR = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = BACKEND_DIR / "deploy" / "systemd"
SECRET_URL = "postgresql://paw:hunter2-secret-pw@127.0.0.1:1/paw"


def run(argv: list[str], **environment: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with paw_environment(**environment):
        code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class CommandLineTest(unittest.TestCase):
    def test_help_names_the_command(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("user-erasure-run", out)

    def test_without_the_migration_url_nothing_runs(self):
        code, _, err = run(["user-erasure-run"])
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertIn("PAW_MIGRATION_DATABASE_URL is not set", err)

    def test_the_application_url_alone_is_not_used(self):
        code, _, err = run(["user-erasure-run"], PAW_DATABASE_URL=SECRET_URL)
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertNotIn("hunter2", err)

    def test_an_unreachable_database_is_an_environment_error_without_secrets(self):
        code, out, err = run(
            ["user-erasure-run"], PAW_MIGRATION_DATABASE_URL=SECRET_URL
        )
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertNotIn("hunter2", out + err)

    def test_a_malformed_user_id_is_refused_without_echoing_it(self):
        code, _, err = run(["user-erasure-run", "--checkouts-removed", "hunter2"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotIn("hunter2", err)

    def test_the_checkouts_flag_repeats(self):
        first, second = uuid.uuid4(), uuid.uuid4()
        arguments = cli.build_parser().parse_args(
            [
                "user-erasure-run",
                "--checkouts-removed",
                str(first),
                "--checkouts-removed",
                str(second),
            ]
        )
        self.assertEqual(arguments.checkouts_removed, [first, second])


class DispatchTest(unittest.TestCase):
    def test_the_package_entry_point_routes_the_erasure_command(self):
        from paw_backend.cli import dispatch, owner, retention

        self.assertIs(dispatch.command_module(["user-erasure-run"]), cli)
        self.assertIs(dispatch.command_module(["audit-retention-run"]), retention)
        self.assertIs(dispatch.command_module(["owner-setup"]), owner)

    def test_the_owner_help_points_at_the_erasure_command(self):
        from paw_backend.cli import owner

        out, err = io.StringIO(), io.StringIO()
        with paw_environment():
            owner.main(["--help"], stdout=out, stderr=err)
        self.assertIn("user-erasure-run", out.getvalue())


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string((SYSTEMD_DIR / name).read_text(encoding="utf-8"))
    return parser


class SystemdUnitTest(unittest.TestCase):
    def test_the_service_runs_the_command_once_as_a_oneshot(self):
        service = unit("paw-user-erasure.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertIn(
            "-m paw_backend.cli user-erasure-run", service["Service"]["ExecStart"]
        )

    def test_the_service_keeps_the_owner_credential_away_from_the_backend(self):
        service = unit("paw-user-erasure.service")
        self.assertEqual(service["Service"]["User"], "paw-maint")
        self.assertIn("EnvironmentFile", service["Service"])
        self.assertNotIn(
            "PAW_MIGRATION_DATABASE_URL=", service["Service"].get("Environment", "")
        )
        # It never reaches into a user's home.
        self.assertEqual(service["Service"]["ProtectHome"], "true")

    def test_a_failed_run_tells_the_owner(self):
        service = unit("paw-user-erasure.service")
        self.assertEqual(
            service["Unit"]["OnFailure"], "paw-user-erasure-failure.service"
        )
        failure = unit("paw-user-erasure-failure.service")
        self.assertIn("ExecStart", failure["Service"])

    def test_the_timer_runs_daily_and_catches_up_after_downtime(self):
        timer = unit("paw-user-erasure.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "daily")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Timer"]["Unit"], "paw-user-erasure.service")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")


@requires_postgres
class RunCommandTest(ErasureTestCase):
    async def deleted_long_ago(self, name: str):
        """A user deleted 31 days ago by the database's clock (the command's)."""
        user = await self.make_user(name, status="pending_deletion")
        await self.execute(
            "INSERT INTO user_status_changes (id, user_id, old_status, new_status, "
            "changed_at, changed_by, recorded_at) VALUES (gen_random_uuid(), :u, "
            "'active', 'pending_deletion', clock_timestamp() - interval '31 days', "
            "NULL, clock_timestamp())",
            u=user.id,
        )
        return user

    async def owner_run(self, argv: list[str]) -> tuple[int, str, str]:
        return await asyncio.to_thread(
            run, argv, PAW_MIGRATION_DATABASE_URL=TEST_DATABASE_URL
        )

    async def test_a_run_erases_the_due_users(self):
        bob = await self.deleted_long_ago("bob")

        code, out, err = await self.owner_run(["user-erasure-run"])

        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(out, "")
        self.assertIn("erased=1", err)
        self.assertEqual(await self.status_of(bob.id), "deleted")
        self.assertEqual(await self.rows_left(bob.id), {})

        code, _, err = await self.owner_run(["user-erasure-run"])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("erased=0", err)

    async def test_a_user_that_cannot_be_erased_fails_the_run(self):
        bob = await self.deleted_long_ago("bob")
        await self.tasks.create_task(
            project_id=uuid.uuid4(), created_by=bob.id, title="Still running"
        )

        code, _, err = await self.owner_run(["user-erasure-run"])

        self.assertEqual(code, cli.EXIT_ERASURE_FAILED)
        self.assertIn(f"NOT ERASED: user {bob.id} (tasks_active", err)
        self.assertEqual(await self.status_of(bob.id), "pending_deletion")

    async def test_a_concurrent_run_is_refused(self):
        await self.deleted_long_ago("bob")
        async with UserErasureService(self.database).run_lock():
            code, _, err = await self.owner_run(["user-erasure-run"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("already running", err)


if __name__ == "__main__":
    unittest.main()
