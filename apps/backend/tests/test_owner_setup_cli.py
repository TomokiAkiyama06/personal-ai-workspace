"""The server-local command ``python -m paw_backend.cli`` (PAW-021).

The first classes need no database. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the commands against a migrated database,
including as separate processes and as a restricted database role.
"""

import asyncio
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from paw_backend.authz import PostgresAuditSink
from paw_backend.cli import owner as cli
from paw_backend.db import Database
from paw_backend.identity import OwnerSetupService, SetupTokenRejectedError

from .identity_support import (
    APP_ROLE,
    ROLE_PASSWORD,
    TEST_DATABASE_URL,
    migrate,
    requires_postgres,
    sync_database_url,
    url_for_role,
)
from .support import make_settings, paw_environment

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET_URL = "postgresql://paw:hunter2-secret-pw@127.0.0.1:1/paw"


def run(argv: list[str], **environment: str) -> tuple[int, str, str]:
    """Run ``main`` in this process with only ``environment`` as PAW_* settings."""
    out, err = io.StringIO(), io.StringIO()
    with paw_environment(**environment):
        code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class ExitCodeTest(unittest.TestCase):
    def test_the_exit_codes_follow_the_repository_convention(self):
        self.assertEqual(
            (cli.EXIT_OK, cli.EXIT_REFUSED, cli.EXIT_ENVIRONMENT), (0, 1, 2)
        )


class UsageTest(unittest.TestCase):
    def test_help_lists_both_commands_and_exits_zero(self):
        code, out, err = run(["--help"])

        self.assertEqual(code, 0)
        self.assertIn("owner-setup", out)
        self.assertIn("owner-recover", out)
        self.assertEqual(err, "")

    def test_no_command_and_unknown_commands_are_refused(self):
        for argv in ([], ["owner-delete"], ["owner-setup", "--login-name"]):
            with self.subTest(argv):
                code, out, err = run(argv)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertIn("invalid arguments", err)

    def test_the_database_url_cannot_be_given_as_an_argument(self):
        for argv in (
            ["owner-setup", "--login-name", "boss", "--database-url", SECRET_URL],
            ["owner-setup", "--login-name", "boss", f"--database-url={SECRET_URL}"],
            ["owner-recover", "--confirm-owner-recovery", SECRET_URL],
        ):
            with self.subTest(argv):
                code, out, err = run(argv, PAW_DATABASE_URL="")
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertNotIn("hunter2", err)
                self.assertNotIn("127.0.0.1", err)
                self.assertIn("PAW_DATABASE_URL", err)

    def test_abbreviated_options_are_not_accepted(self):
        code, out, _ = run(
            ["owner-setup", "--login", "boss"], PAW_DATABASE_URL=SECRET_URL
        )

        self.assertEqual((code, out), (1, ""))

    def test_the_setup_command_needs_a_login_name(self):
        code, out, _ = run(["owner-setup"], PAW_DATABASE_URL=SECRET_URL)

        self.assertEqual((code, out), (1, ""))

    def test_recovery_needs_the_explicit_confirmation_before_anything_else(self):
        # Not even the environment is looked at: no PAW_DATABASE_URL here.
        code, out, err = run(["owner-recover"])

        self.assertEqual((code, out), (1, ""))
        self.assertIn("--confirm-owner-recovery", err)
        self.assertIn("revokes every outstanding", err)

    def test_there_is_no_json_output(self):
        code, out, _ = run(
            ["owner-setup", "--login-name", "boss", "--json"],
            PAW_DATABASE_URL=SECRET_URL,
        )

        self.assertEqual((code, out), (1, ""))


class EnvironmentErrorTest(unittest.TestCase):
    def test_a_missing_database_url_is_an_environment_error(self):
        code, out, err = run(["owner-recover", "--confirm-owner-recovery"])

        self.assertEqual((code, out), (2, ""))
        self.assertIn("PAW_DATABASE_URL is not set", err)

    def test_an_invalid_setting_is_named_but_its_value_is_not_shown(self):
        for name, value in (
            ("PAW_DATABASE_URL", "mysql://u:hunter2-pw@h/d"),
            ("PAW_SETUP_TOKEN_TTL_SECONDS", "hunter2-not-a-number"),
            ("PAW_SETUP_TOKEN_MAX_ATTEMPTS", "999"),
        ):
            with self.subTest(name):
                environment = {"PAW_DATABASE_URL": SECRET_URL, name: value}
                code, out, err = run(
                    ["owner-recover", "--confirm-owner-recovery"], **environment
                )

                self.assertEqual((code, out), (2, ""))
                self.assertIn(name, err)
                self.assertNotIn("hunter2", err)
                self.assertNotIn("999", err)
                self.assertNotIn("mysql", err)

    def test_an_unreachable_database_reports_the_error_type_only(self):
        code, out, err = run(
            ["owner-recover", "--confirm-owner-recovery"], PAW_DATABASE_URL=SECRET_URL
        )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("OperationalError", err)
        self.assertNotIn("hunter2", err)
        self.assertNotIn("127.0.0.1", err)
        self.assertNotIn("Connection refused", err)

    def test_an_invalid_login_name_is_refused_without_echoing_it(self):
        code, out, err = run(
            ["owner-setup", "--login-name", "not valid: hunter2"],
            PAW_DATABASE_URL=SECRET_URL,
        )

        self.assertEqual((code, out), (1, ""))
        self.assertIn("Invalid login name", err)
        self.assertNotIn("hunter2", err)

    def test_an_unexpected_error_shows_its_type_and_nothing_else(self):
        async def broken(settings, arguments):
            raise RuntimeError("hunter2-internal-detail")

        with patch.object(cli, "_issue", broken):
            code, out, err = run(
                ["owner-recover", "--confirm-owner-recovery"],
                PAW_DATABASE_URL=SECRET_URL,
            )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("RuntimeError", err)
        self.assertNotIn("hunter2", err)
        self.assertNotIn("Traceback", err)


class ProcessTest(unittest.TestCase):
    """The real entry point: ``python -m paw_backend.cli``."""

    def run_module(self, *argv: str, **environment: str):
        variables = {k: v for k, v in os.environ.items() if not k.startswith("PAW_")}
        variables["PYTHONPATH"] = str(BACKEND_DIR)
        variables.update(environment)
        return subprocess.run(
            [sys.executable, "-m", "paw_backend.cli", *argv],
            cwd=BACKEND_DIR,
            env=variables,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_the_module_can_be_run_and_reports_the_exit_code(self):
        helped = self.run_module("--help")
        missing = self.run_module("owner-recover", "--confirm-owner-recovery")
        refused = self.run_module("owner-recover")

        self.assertEqual(
            (helped.returncode, missing.returncode, refused.returncode), (0, 2, 1)
        )
        self.assertIn("owner-setup", helped.stdout)
        self.assertEqual((missing.stdout, refused.stdout), ("", ""))


class CliDatabaseTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        migrate("downgrade", "base")
        migrate("upgrade", "head")
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()
        migrate("downgrade", "base")

    def setUp(self):
        self.execute("TRUNCATE users CASCADE")
        self.started_at = self.scalar("SELECT clock_timestamp()")

    def execute(self, sql: str, **params) -> None:
        with self.engine.begin() as connection:
            connection.execute(text(sql), params)

    def rows(self, sql: str, **params) -> list:
        with self.engine.connect() as connection:
            return list(connection.execute(text(sql), params).all())

    def scalar(self, sql: str, **params):
        return self.rows(sql, **params)[0][0]

    def audit(self) -> list[tuple]:
        return [
            (row.action, row.decision, row.reason)
            for row in self.rows(
                "SELECT action, decision, reason FROM audit_events "
                "WHERE recorded_at >= :since ORDER BY recorded_at",
                since=self.started_at,
            )
        ]

    def redeem(self, token: str):
        """Redeem ``token`` through the service, as the web flow will."""

        async def go():
            database = Database(make_settings(database_url=TEST_DATABASE_URL))
            try:
                service = OwnerSetupService(database, PostgresAuditSink(database))
                return await service.redeem(token)
            finally:
                await database.dispose()

        return asyncio.run(go())

    def token_of(self, stdout: str) -> str:
        self.assertRegex(stdout, r"\Apawst1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{43}\n\Z")
        return stdout.strip()


@requires_postgres
class OwnerSetupCommandTest(CliDatabaseTestCase):
    def setup_owner(self, name: str = "Tomoki", **environment: str):
        environment.setdefault("PAW_DATABASE_URL", TEST_DATABASE_URL)
        return run(["owner-setup", "--login-name", name], **environment)

    def test_creates_the_owner_and_prints_the_token_once_on_stdout(self):
        code, out, err = self.setup_owner()

        self.assertEqual(code, 0, err)
        token = self.token_of(out)  # stdout is exactly the token, on one line
        self.assertNotIn(token, err)
        self.assertNotIn(token.split(".")[2], err)
        self.assertIn("login_name=tomoki", err)
        self.assertIn("works once", err)
        (user,) = self.rows("SELECT * FROM users")
        self.assertEqual(
            (user.login_name, user.system_role, user.status, user.passkey_required),
            ("tomoki", "owner", "invited", True),
        )
        self.assertIn(str(user.id), err)
        self.assertEqual(self.redeem(token).user_id, user.id)

    def test_the_setup_is_audited(self):
        self.setup_owner()

        self.assertEqual(
            sorted(self.audit()),
            [
                ("owner.create", "allow", "created"),
                ("owner.setup_token.issue", "allow", "issued"),
            ],
        )

    def test_a_second_setup_is_refused_with_exit_code_1(self):
        self.setup_owner()

        code, out, err = self.setup_owner("someone-else")

        self.assertEqual((code, out), (1, ""))
        self.assertIn("an Owner already exists", err)
        self.assertIn("owner-recover", err)
        self.assertEqual(self.scalar("SELECT count(*) FROM users"), 1)
        self.assertIn(("owner.create", "deny", "owner_exists"), self.audit())

    def test_the_token_and_the_database_url_appear_nowhere_but_stdout(self):
        code, out, err = self.setup_owner()

        secret = out.strip().split(".")[2]
        self.assertEqual(code, 0)
        self.assertEqual(out.count(secret), 1)
        self.assertNotIn(secret, err)
        self.assertNotIn(ROLE_PASSWORD, out + err)
        stored = "".join(
            row[0]
            for row in self.rows(
                "SELECT t::text FROM setup_tokens t UNION ALL "
                "SELECT t::text FROM users t UNION ALL "
                "SELECT t::text FROM audit_events t"
            )
        )
        self.assertNotIn(secret, stored)

    def test_the_ttl_setting_is_used(self):
        code, out, err = self.setup_owner(PAW_SETUP_TOKEN_TTL_SECONDS="120")

        self.assertEqual(code, 0)
        (row,) = self.rows("SELECT expires_at - created_at AS ttl FROM setup_tokens")
        self.assertEqual(row.ttl.total_seconds(), 120)

    def test_separate_processes_racing_to_set_up_produce_one_owner(self):
        variables = {k: v for k, v in os.environ.items() if not k.startswith("PAW_")}
        variables.update(
            PYTHONPATH=str(BACKEND_DIR), PAW_DATABASE_URL=TEST_DATABASE_URL
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "paw_backend.cli",
                    "owner-setup",
                    "--login-name",
                    f"owner{index}",
                ],
                cwd=BACKEND_DIR,
                env=variables,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(4)
        ]
        results = []
        try:
            for process in processes:
                out, err = process.communicate(timeout=120)
                results.append((process.returncode, out, err))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

        self.assertEqual(sorted(code for code, _, _ in results), [0, 1, 1, 1], results)
        printed = [out for code, out, _ in results if out]
        self.assertEqual(len(printed), 1)
        self.token_of(printed[0])
        self.assertEqual(
            self.scalar("SELECT count(*) FROM users WHERE system_role = 'owner'"), 1
        )
        self.assertEqual(self.scalar("SELECT count(*) FROM setup_tokens"), 1)


@requires_postgres
class OwnerRecoverCommandTest(CliDatabaseTestCase):
    environment = {"PAW_DATABASE_URL": TEST_DATABASE_URL}

    def recover(self, *flags: str):
        return run(["owner-recover", *flags], **self.environment)

    def set_up_owner(self) -> str:
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"], **self.environment
        )
        self.assertEqual(code, 0, err)
        return out.strip()

    def test_without_the_confirmation_nothing_is_issued(self):
        setup_token = self.set_up_owner()

        code, out, err = self.recover()

        self.assertEqual((code, out), (1, ""))
        self.assertIn("--confirm-owner-recovery", err)
        self.assertEqual(self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertEqual(self.redeem(setup_token).purpose.value, "setup")

    def test_recovery_prints_a_new_token_and_invalidates_the_old_one(self):
        setup_token = self.set_up_owner()

        code, out, err = self.recover("--confirm-owner-recovery")

        self.assertEqual(code, 0, err)
        recovery_token = self.token_of(out)
        self.assertNotEqual(recovery_token, setup_token)
        self.assertNotIn(recovery_token.split(".")[2], err)
        self.assertIn("Recovery token issued", err)
        with self.assertRaises(SetupTokenRejectedError):
            self.redeem(setup_token)
        self.assertEqual(self.redeem(recovery_token).purpose.value, "recovery")

    def test_recovery_is_audited(self):
        self.set_up_owner()

        self.recover("--confirm-owner-recovery")

        audit = self.audit()
        self.assertIn(("owner.token.revoke", "allow", "superseded"), audit)
        self.assertIn(("owner.recovery_token.issue", "allow", "issued"), audit)

    def test_without_an_owner_there_is_nothing_to_recover(self):
        code, out, err = self.recover("--confirm-owner-recovery")

        self.assertEqual((code, out), (1, ""))
        self.assertIn("no Owner to recover", err)
        self.assertEqual(self.scalar("SELECT count(*) FROM setup_tokens"), 0)
        self.assertIn(
            ("owner.recovery_token.issue", "deny", "owner_missing"), self.audit()
        )

    def test_repeating_the_recovery_supersedes_the_previous_token(self):
        self.set_up_owner()
        first = self.token_of(self.recover("--confirm-owner-recovery")[1])
        second = self.token_of(self.recover("--confirm-owner-recovery")[1])

        with self.assertRaises(SetupTokenRejectedError):
            self.redeem(first)
        self.assertEqual(self.redeem(second).purpose.value, "recovery")


@requires_postgres
class RestrictedRoleTest(CliDatabaseTestCase):
    """The command works with PAW_DATABASE_URL under a restricted role."""

    def setUp(self):
        self.drop_role()
        self.addCleanup(self.drop_role)
        self.execute(
            f"CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            f"PASSWORD '{ROLE_PASSWORD}'"
        )
        super().setUp()

    def drop_role(self):
        if self.scalar("SELECT count(*) FROM pg_roles WHERE rolname = :r", r=APP_ROLE):
            self.execute(f"DROP OWNED BY {APP_ROLE}")
            self.execute(f"DROP ROLE {APP_ROLE}")

    def regrant(self, **environment: str) -> None:
        """Re-run the migrations so that they grant to the application role."""
        migrate("downgrade", "base")
        migrate("upgrade", "head", **environment)

    def tearDown(self):
        # The class-level tearDown drops the schema; restore a plain one first.
        migrate("downgrade", "base")
        migrate("upgrade", "head")

    def test_setup_and_recovery_work_as_the_application_role(self):
        self.regrant(PAW_APP_DATABASE_ROLE=APP_ROLE)
        self.started_at = self.scalar("SELECT clock_timestamp()")
        environment = {
            "PAW_DATABASE_URL": url_for_role(APP_ROLE),
            # The command never uses the migration role.
            "PAW_MIGRATION_DATABASE_URL": "postgresql://nobody:x@127.0.0.1:1/none",
        }

        code, out, err = run(["owner-setup", "--login-name", "boss"], **environment)
        self.assertEqual(code, 0, err)
        setup_token = self.token_of(out)
        code, out, err = run(
            ["owner-recover", "--confirm-owner-recovery"], **environment
        )
        self.assertEqual(code, 0, err)
        recovery_token = self.token_of(out)

        self.assertEqual(self.redeem(recovery_token).purpose.value, "recovery")
        with self.assertRaises(SetupTokenRejectedError):
            self.redeem(setup_token)
        self.assertGreaterEqual(len(self.audit()), 5)
        self.assertNotIn(ROLE_PASSWORD, out + err)

    def test_the_command_refuses_when_the_role_cannot_write_the_audit_trail(self):
        # The tables are writable, the audit table is not: nothing may happen.
        self.regrant()
        self.execute(
            f"GRANT SELECT, INSERT, UPDATE ON users, setup_tokens TO {APP_ROLE}"
        )
        self.started_at = self.scalar("SELECT clock_timestamp()")

        code, out, err = run(
            ["owner-setup", "--login-name", "boss"],
            PAW_DATABASE_URL=url_for_role(APP_ROLE),
        )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("audit trail could not be written", err)
        self.assertEqual(self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM setup_tokens"), 0)

    def test_a_role_without_access_to_the_tables_is_an_environment_error(self):
        self.regrant()

        code, out, err = run(
            ["owner-setup", "--login-name", "boss"],
            PAW_DATABASE_URL=url_for_role(APP_ROLE),
        )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("Database error (ProgrammingError)", err)
        self.assertNotIn(ROLE_PASSWORD, err)
        self.assertNotIn("permission denied", err)


if __name__ == "__main__":
    unittest.main()
