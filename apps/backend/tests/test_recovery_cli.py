"""``python -m paw_backend.cli recovery-*`` (PAW-047, Decision 0054).

The commands read their directories and database URLs from the environment, never
an argument; they print no path, URL or git output; their exit codes are what
systemd's ``OnFailure=`` watches. The database cases are skipped unless
``PAW_TEST_DATABASE_URL`` is set; files are written below a temporary directory
only, and the systemd unit files are read as text (nothing is installed).
"""

import configparser
import io
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from paw_backend.cli import dispatch, owner
from paw_backend.cli import memory_projection as projection_cli
from paw_backend.cli import recovery as cli

from .memory_support import requires_postgres
from .projects_support import PostgresProjectTestCase
from .recovery_support import RecoveryWorld
from .support import paw_environment
from .task_support import TEST_DATABASE_URL

BACKEND_DIR = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = BACKEND_DIR / "deploy" / "systemd"


def run(argv, *, homes=(), clock=None, module=cli, **environment):
    out, err = io.StringIO(), io.StringIO()
    with paw_environment(**environment):
        code = module.main(
            argv, stdout=out, stderr=err, protected_homes=homes, clock=clock
        )
    return code, out.getvalue(), err.getvalue()


class ArgumentTest(unittest.TestCase):
    def test_the_commands_go_to_this_module(self) -> None:
        for command in cli.COMMANDS:
            self.assertIs(dispatch.command_module([command]), cli)
        out = io.StringIO()
        with paw_environment():
            owner.main(["--help"], stdout=out, stderr=io.StringIO())
        self.assertIn("recovery-backup-run", out.getvalue())

    def test_an_argument_is_never_echoed(self) -> None:
        code, out, err = run(["recovery-restore", "--from", "/secret/place"])
        self.assertEqual(cli.EXIT_REFUSED, code)
        self.assertNotIn("/secret/place", out + err)

    def test_missing_configuration_is_an_environment_error(self) -> None:
        code, _, err = run(["recovery-backup-run"])
        self.assertEqual(cli.EXIT_ENVIRONMENT, code)
        self.assertIn("PAW_DATABASE_URL", err)
        code, _, err = run(
            ["recovery-backup-run"], PAW_DATABASE_URL="postgresql://u@h/db"
        )
        self.assertEqual(cli.EXIT_ENVIRONMENT, code)
        self.assertIn("PAW_RECOVERY_REPOSITORY_DIR", err)
        self.assertIn("PAW_MEMORY_PROJECTION_DIR", err)
        code, _, err = run(["recovery-restore"], PAW_DATABASE_URL="postgresql://u@h/db")
        self.assertEqual(cli.EXIT_ENVIRONMENT, code)
        self.assertIn("PAW_MIGRATION_DATABASE_URL", err)
        code, _, err = run(
            ["recovery-restore"], PAW_MIGRATION_DATABASE_URL="postgresql://u@h/db"
        )
        self.assertEqual(cli.EXIT_ENVIRONMENT, code)
        self.assertIn("PAW_RECOVERY_REPOSITORY_DIR", err)


@requires_postgres
class CommandTest(PostgresProjectTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE memories CASCADE"))
        self.world = RecoveryWorld(self)
        self.environment = {
            "PAW_DATABASE_URL": TEST_DATABASE_URL,
            "PAW_MIGRATION_DATABASE_URL": TEST_DATABASE_URL,
            "PAW_MEMORY_PROJECTION_DIR": str(self.world.projection),
            "PAW_RECOVERY_REPOSITORY_DIR": str(self.world.checkout),
        }

    def command(self, argv, **options):
        return run(argv, homes=self.world.homes, **self.environment, **options)

    def test_backup_check_and_restore_exit_codes(self) -> None:
        code, _, err = run(
            ["memory-projection-run"],
            homes=self.world.homes,
            module=projection_cli,
            **self.environment,
        )
        self.assertEqual(0, code, err)
        code, out, err = self.command(["recovery-backup-run"])
        self.assertEqual(cli.EXIT_OK, code, err)
        self.assertIn("Recovery backup completed", err)
        self.assertEqual("", out)
        self.assertNotIn(str(self.world.base), err)

        now = datetime.now(UTC)
        code, _, err = self.command(["recovery-backup-check"], clock=lambda: now)
        self.assertEqual(cli.EXIT_OK, code, err)
        code, _, err = self.command(
            ["recovery-backup-check", "--max-age-minutes", "5"],
            clock=lambda: now + timedelta(hours=1),
        )
        self.assertEqual(cli.EXIT_FAILED, code)

        # The checkout is the latest pushed state and the workspace is empty:
        # the dry run shows the plan and writes nothing; once a user exists, a
        # restore is refused.
        code, _, err = self.command(["recovery-restore"])
        self.assertEqual(cli.EXIT_OK, code, err)
        self.assertIn("Dry run", err)
        self.assertIn("only the recovery.restore.planned audit row", err)
        self.assertIn("owner-recover", err)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, login_name, system_role, status,"
                    " passkey_required, created_at, updated_at) VALUES"
                    " (gen_random_uuid(), 'someone', 'user', 'active', false,"
                    " now(), now())"
                )
            )
        code, _, err = self.command(["recovery-restore", "--apply"])
        self.assertEqual(cli.EXIT_REFUSED, code)
        self.assertIn("target_not_empty", err)
        self.assertIn("only the recovery.restore.refused audit row was written", err)
        self.assertNotIn(str(self.world.base), err)

    def test_a_refused_checkout_fails_the_backup(self) -> None:
        (self.world.checkout / "project.txt").write_text("not ours\n")
        run(
            ["memory-projection-run"],
            homes=self.world.homes,
            module=projection_cli,
            **self.environment,
        )
        code, _, err = self.command(["recovery-backup-run"])
        self.assertEqual(cli.EXIT_FAILED, code)
        self.assertIn("check_repository (not_recovery_repository)", err)
        code, _, err = self.command(["recovery-backup-check"])
        self.assertEqual(cli.EXIT_FAILED, code)


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string((SYSTEMD_DIR / name).read_text(encoding="utf-8"))
    return parser


class SystemdUnitTest(unittest.TestCase):
    def test_the_service_runs_the_backup_with_an_environment_file(self) -> None:
        service = unit("paw-recovery-backup.service")
        self.assertEqual("oneshot", service["Service"]["Type"])
        self.assertIn(
            "-m paw_backend.cli recovery-backup-run", service["Service"]["ExecStart"]
        )
        self.assertIn("EnvironmentFile", service["Service"])
        self.assertNotIn("Environment", service["Service"])
        self.assertEqual("true", service["Service"]["ProtectHome"])
        self.assertEqual("0077", service["Service"]["UMask"])
        self.assertEqual(
            "paw-recovery-backup-failure.service", service["Unit"]["OnFailure"]
        )

    def test_the_timer_runs_every_thirty_minutes_and_catches_up(self) -> None:
        timer = unit("paw-recovery-backup.timer")
        self.assertEqual("*:02/30", timer["Timer"]["OnCalendar"])
        self.assertEqual("true", timer["Timer"]["Persistent"])

    def test_the_failure_unit_is_loud(self) -> None:
        failure = (SYSTEMD_DIR / "paw-recovery-backup-failure.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("recovery.backup.failed", failure)
        self.assertIn("--priority=crit", failure)


if __name__ == "__main__":
    unittest.main()
