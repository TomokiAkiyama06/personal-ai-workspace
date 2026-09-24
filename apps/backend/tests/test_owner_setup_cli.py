"""The server-local command ``python -m paw_backend.cli`` (PAW-021).

The first classes need no database. The PostgreSQL classes (skipped unless
``PAW_TEST_DATABASE_URL`` is set) run the commands against a migrated database,
including as separate processes and as a restricted database role.
"""

import asyncio
import contextlib
import io
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from paw_backend.authz import PostgresAuditSink
from paw_backend.cli import owner as cli
from paw_backend.db import Database
from paw_backend.identity import SetupTokenRejectedError, TokenRedeemer

from .identity_support import (
    ROLE_PASSWORD,
    TEST_DATABASE_URL,
    migrate,
    requires_postgres,
    role_name,
    sync_database_url,
    url_for_role,
)
from .support import make_settings, paw_environment

BACKEND_DIR = Path(__file__).resolve().parents[1]
SECRET_URL = "postgresql://paw:hunter2-secret-pw@127.0.0.1:1/paw"


def run(
    argv: list[str], *, euid: int | None = None, **environment: str
) -> tuple[int, str, str]:
    """Run ``main`` in this process with only ``environment`` as PAW_* settings.

    ``owner-recover`` runs as root (uid 0), as it must under sudo, unless ``euid``
    says otherwise.
    """
    if euid is None and argv[:1] == ["owner-recover"]:
        euid = 0
    out, err = io.StringIO(), io.StringIO()
    as_user = (
        contextlib.nullcontext()
        if euid is None
        else patch("os.geteuid", return_value=euid)
    )
    with paw_environment(**environment), as_user:
        code = cli.main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class ExitCodeTest(unittest.TestCase):
    def test_the_exit_codes_follow_the_repository_convention(self):
        self.assertEqual(
            (cli.EXIT_OK, cli.EXIT_REFUSED, cli.EXIT_ENVIRONMENT), (0, 1, 2)
        )

    def test_a_token_that_could_not_be_delivered_has_its_own_exit_code(self):
        self.assertEqual(cli.EXIT_TOKEN_NOT_DELIVERED, 3)


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
        self.assertIn("PAW_OPERATOR_DATABASE_URL (or PAW_DATABASE_URL) is not set", err)

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
        async def broken(settings, arguments, operator):
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
                service = TokenRedeemer(database, PostgresAuditSink(database))
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

    def test_a_process_that_is_not_root_is_refused_even_with_a_sudo_uid(self):
        # Ubuntu's sudo runs the command as root. SUDO_UID is an environment
        # variable that anybody can set, so it authorises nothing.
        setup_token = self.set_up_owner()

        for euid, extra in ((1000, {}), (1000, {"SUDO_UID": "0"}), (33, {})):
            with self.subTest(euid=euid, extra=extra):
                code, out, err = run(
                    ["owner-recover", "--confirm-owner-recovery"],
                    euid=euid,
                    **self.environment,
                    **extra,
                )

                self.assertEqual((code, out), (1, ""))
                self.assertIn("must be run as root", err)
                self.assertNotIn(TEST_DATABASE_URL, err)
        # Nothing was issued or revoked: the setup token is still the only one.
        self.assertEqual(self.scalar("SELECT count(*) FROM setup_tokens"), 1)
        self.assertEqual(self.redeem(setup_token).purpose.value, "setup")

    def test_root_recovers_and_the_sudo_user_is_recorded(self):
        self.set_up_owner()

        code, out, err = run(
            ["owner-recover", "--confirm-owner-recovery"],
            euid=0,
            SUDO_UID="1000",
            **self.environment,
        )

        self.assertEqual(code, 0, err)
        self.token_of(out)
        self.assertIn("operator uid=0 sudo_uid=1000", err)

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


# ``python -m paw_backend.cli`` as a process that is root, as under sudo (the test
# cannot become root): the same entry point, with the uid it reports replaced.
AS_ROOT = (
    "import os, sys; os.geteuid = lambda: 0; "
    "from paw_backend.cli.owner import main; sys.exit(main())"
)


def shell(argv: list[str], redirect: str, **environment: str):
    """Run the real module through ``bash`` with ``redirect`` on the command.

    ``owner-recover`` runs as root (see ``AS_ROOT``), which its refusal of other
    users requires.
    """
    variables = {k: v for k, v in os.environ.items() if not k.startswith("PAW_")}
    variables["PYTHONPATH"] = str(BACKEND_DIR)
    variables.update(environment)
    entry = (
        ["-c", AS_ROOT] if argv[:1] == ["owner-recover"] else ["-m", "paw_backend.cli"]
    )
    command = " ".join(
        [shlex.quote(sys.executable)]
        + [shlex.quote(a) for a in entry]
        + [shlex.quote(a) for a in argv]
        + [redirect]
    )
    if "|" in redirect:
        command += "; exit ${PIPESTATUS[0]}"
    return subprocess.run(
        ["bash", "-c", command],
        cwd=BACKEND_DIR,
        env=variables,
        capture_output=True,
        text=True,
        timeout=120,
    )


@requires_postgres
class UndeliveredTokenTest(CliDatabaseTestCase):
    """The change is committed before the token is written: say so when it fails."""

    ENVIRONMENT = {"PAW_DATABASE_URL": TEST_DATABASE_URL}

    def assert_reported(self, code: int, err: str) -> None:
        self.assertEqual(code, 3, err)
        self.assertIn("could not be written to stdout", err)
        self.assertIn("owner-recover --confirm-owner-recovery", err)
        self.assertNotIn("pawst1.", err)
        self.assertNotIn("Traceback", err)
        # Not the interpreter's own complaint at exit (which also changes the code).
        self.assertNotIn("Exception ignored", err)
        self.assertNotIn("Unexpected error", err)

    def test_a_full_disk_a_closed_stdout_and_a_closed_pipe_all_exit_with_3(self):
        for name, redirect in (
            ("disk full", ">/dev/full"),
            ("closed stdout", ">&-"),
            ("closed pipe", "| head -c0"),
        ):
            with self.subTest(name):
                self.execute("TRUNCATE users CASCADE")
                self.started_at = self.scalar("SELECT clock_timestamp()")

                result = shell(
                    ["owner-setup", "--login-name", "boss"],
                    redirect,
                    **self.ENVIRONMENT,
                )

                self.assert_reported(result.returncode, result.stderr)
                self.assertIn("Owner created, but", result.stderr)
                # The change WAS made and audited, as the message says ...
                self.assertEqual(self.scalar("SELECT count(*) FROM users"), 1)
                self.assertIn(("owner.create", "allow", "created"), self.audit())
                # ... and recovery is the documented way to get a token.
                code, out, err = run(
                    ["owner-recover", "--confirm-owner-recovery"], **self.ENVIRONMENT
                )
                self.assertEqual(code, 0, err)
                self.assertEqual(
                    self.redeem(self.token_of(out)).purpose.value, "recovery"
                )

    def test_recovery_reports_a_lost_token_too(self):
        run(["owner-setup", "--login-name", "boss"], **self.ENVIRONMENT)

        result = shell(
            ["owner-recover", "--confirm-owner-recovery"],
            ">/dev/full",
            **self.ENVIRONMENT,
        )

        self.assert_reported(result.returncode, result.stderr)
        self.assertIn("Recovery token issued, but", result.stderr)

    def test_an_unwritable_stdout_object_is_reported_in_process_too(self):
        class Broken(io.StringIO):
            def write(self, text):
                raise BrokenPipeError

        closed = io.StringIO()
        closed.close()
        for name, stream in (("broken", Broken()), ("closed", closed)):
            with self.subTest(name):
                self.execute("TRUNCATE users CASCADE")
                err = io.StringIO()
                with paw_environment(**self.ENVIRONMENT):
                    code = cli.main(
                        ["owner-setup", "--login-name", "boss"],
                        stdout=stream,
                        stderr=err,
                    )
                self.assert_reported(code, err.getvalue())

    def test_a_missing_stdout_and_stderr_do_not_crash_the_command(self):
        closed = io.StringIO()
        closed.close()
        with paw_environment(**self.ENVIRONMENT):
            with patch.object(sys, "stdout", None):
                code = cli.main(
                    ["owner-setup", "--login-name", "boss"], stdout=None, stderr=closed
                )

        self.assertEqual(code, 3)

    def test_a_working_stdout_is_still_exit_0(self):
        result = shell(["owner-setup", "--login-name", "boss"], "", **self.ENVIRONMENT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.token_of(result.stdout)


@requires_postgres
class NonLiveOwnerCommandTest(CliDatabaseTestCase):
    ENVIRONMENT = {"PAW_DATABASE_URL": TEST_DATABASE_URL}

    def setUp(self):
        super().setUp()
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"], **self.ENVIRONMENT
        )
        self.assertEqual(code, 0, err)
        self.old_token = out.strip()

    def test_setup_says_what_the_state_is_and_how_to_replace_the_account(self):
        for status in ("pending_deletion", "deleted"):
            with self.subTest(status):
                self.execute("UPDATE users SET status = :s", s=status)

                code, out, err = run(
                    ["owner-setup", "--login-name", "second"], **self.ENVIRONMENT
                )

                self.assertEqual((code, out), (1, ""))
                self.assertIn(status, err)
                self.assertIn("--replace-non-live-owner", err)
                self.assertIn("owner-recover cannot help", err)
                self.assertNotIn("run owner-recover on this server", err)
                self.assertEqual(self.scalar("SELECT count(*) FROM users"), 1)

    def test_recovery_says_the_account_is_not_live_and_points_at_the_flag(self):
        self.execute("UPDATE users SET status = 'deleted'")

        code, out, err = run(
            ["owner-recover", "--confirm-owner-recovery"], **self.ENVIRONMENT
        )

        self.assertEqual((code, out), (1, ""))
        self.assertIn("deleted", err)
        self.assertIn("--replace-non-live-owner", err)
        self.assertNotIn("Run owner-setup first", err)

    def test_the_flag_replaces_the_account_and_hands_over_a_new_token(self):
        self.execute("UPDATE users SET status = 'deleted'")

        code, out, err = run(
            ["owner-setup", "--login-name", "second", "--replace-non-live-owner"],
            **self.ENVIRONMENT,
        )

        self.assertEqual(code, 0, err)
        token = self.token_of(out)
        self.assertEqual(
            [
                tuple(r)
                for r in self.rows(
                    "SELECT login_name, system_role, status FROM users ORDER BY 1"
                )
            ],
            [("boss", "user", "deleted"), ("second", "owner", "invited")],
        )
        self.assertEqual(self.redeem(token).purpose.value, "setup")
        with self.assertRaises(SetupTokenRejectedError):
            self.redeem(self.old_token)
        self.assertIn(("owner.replace", "allow", "replaced"), self.audit())

    def test_the_flag_never_replaces_a_live_owner(self):
        for status in ("invited", "active"):
            with self.subTest(status):
                self.execute("UPDATE users SET status = :s", s=status)

                code, out, err = run(
                    [
                        "owner-setup",
                        "--login-name",
                        "second",
                        "--replace-non-live-owner",
                    ],
                    **self.ENVIRONMENT,
                )

                self.assertEqual((code, out), (1, ""))
                self.assertIn("an Owner already exists", err)
                self.assertEqual(
                    self.rows("SELECT login_name, system_role FROM users"),
                    [("boss", "owner")],
                )

    def test_the_flag_is_not_available_on_recovery(self):
        code, out, _ = run(
            ["owner-recover", "--confirm-owner-recovery", "--replace-non-live-owner"],
            **self.ENVIRONMENT,
        )

        self.assertEqual((code, out), (1, ""))


@requires_postgres
class OperatorRecordCommandTest(CliDatabaseTestCase):
    ENVIRONMENT = {"PAW_DATABASE_URL": TEST_DATABASE_URL}

    def test_the_uids_are_shown_and_stored_without_being_trusted(self):
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"], SUDO_UID="1001", **self.ENVIRONMENT
        )

        self.assertEqual(code, 0, err)
        self.assertIn(f"operator uid={os.geteuid()} sudo_uid=1001", err)
        self.assertEqual(
            [
                tuple(r)
                for r in self.rows(
                    "SELECT issued_by_uid, issued_by_sudo_uid FROM setup_tokens"
                )
            ],
            [(os.geteuid(), 1001)],
        )

    def test_a_malformed_sudo_uid_is_neither_stored_nor_shown(self):
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"],
            SUDO_UID="1001; DROP TABLE users",
            **self.ENVIRONMENT,
        )

        self.assertEqual(code, 0, err)
        self.assertNotIn("sudo_uid", err)
        self.assertNotIn("DROP", err)
        self.assertEqual(
            [
                tuple(r)
                for r in self.rows(
                    "SELECT issued_by_uid, issued_by_sudo_uid FROM setup_tokens"
                )
            ],
            [(os.geteuid(), None)],
        )

    def test_recovery_records_who_ran_it_as_well(self):
        run(["owner-setup", "--login-name", "boss"], **self.ENVIRONMENT)

        code, out, err = run(
            ["owner-recover", "--confirm-owner-recovery"],
            SUDO_UID="1002",
            **self.ENVIRONMENT,
        )

        self.assertEqual(code, 0, err)
        self.assertEqual(
            [
                tuple(r)
                for r in self.rows(
                    "SELECT issued_by_sudo_uid FROM setup_tokens "
                    "WHERE purpose = 'recovery'"
                )
            ],
            [(1002,)],
        )


@requires_postgres
class OperatorUrlTest(CliDatabaseTestCase):
    UNREACHABLE = "postgresql://nobody:hunter2-pw@127.0.0.1:1/none"

    def test_the_operator_url_wins_and_needs_no_warning(self):
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"],
            PAW_OPERATOR_DATABASE_URL=TEST_DATABASE_URL,
            PAW_DATABASE_URL=self.UNREACHABLE,  # what the web application uses
        )

        self.assertEqual(code, 0, err)
        self.token_of(out)
        self.assertNotIn("Warning", err)

    def test_the_operator_url_is_the_only_one_used_when_both_are_set(self):
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"],
            PAW_OPERATOR_DATABASE_URL=self.UNREACHABLE,
            PAW_DATABASE_URL=TEST_DATABASE_URL,
        )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("Database error (OperationalError)", err)
        self.assertNotIn("hunter2", err)
        self.assertEqual(self.scalar("SELECT count(*) FROM users"), 0)

    def test_without_an_operator_url_the_database_url_is_used_with_a_warning(self):
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"], PAW_DATABASE_URL=TEST_DATABASE_URL
        )

        self.assertEqual(code, 0, err)
        self.token_of(out)  # the warning is not on stdout
        self.assertIn("Warning: PAW_OPERATOR_DATABASE_URL is not set", err)
        self.assertIn("can create Owner tokens", err)
        self.assertNotIn(ROLE_PASSWORD, err)
        self.assertNotIn("postgresql://", err)

    def test_an_invalid_operator_url_is_named_but_not_shown(self):
        code, out, err = run(
            ["owner-recover", "--confirm-owner-recovery"],
            PAW_OPERATOR_DATABASE_URL="mysql://u:hunter2-pw@h/d",
            PAW_DATABASE_URL=TEST_DATABASE_URL,
        )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("PAW_OPERATOR_DATABASE_URL", err)
        self.assertNotIn("hunter2", err)


@requires_postgres
class OperatorRoleCommandTest(CliDatabaseTestCase):
    """The command as the operator's role; the web role cannot run it at all."""

    WEB_ROLE = role_name("cliweb")
    OPERATOR_ROLE = role_name("cliop")

    def roles(self):
        return (self.WEB_ROLE, self.OPERATOR_ROLE)

    def drop_roles(self):
        for role in self.roles():
            if self.scalar("SELECT count(*) FROM pg_roles WHERE rolname = :r", r=role):
                self.execute(f"DROP OWNED BY {role}")
                self.execute(f"DROP ROLE {role}")

    def setUp(self):
        self.drop_roles()
        self.addCleanup(self.drop_roles)
        for role in self.roles():
            self.execute(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                f"PASSWORD '{ROLE_PASSWORD}'"
            )
        migrate("downgrade", "base")
        migrate(
            "upgrade",
            "head",
            PAW_APP_DATABASE_ROLE=self.WEB_ROLE,
            PAW_OPERATOR_DATABASE_ROLE=self.OPERATOR_ROLE,
        )
        super().setUp()

    def tearDown(self):
        # The class-level tearDown drops the schema; restore a plain one first.
        migrate("downgrade", "base")
        migrate("upgrade", "head")

    def environment(self, **extra: str) -> dict[str, str]:
        return {
            "PAW_DATABASE_URL": url_for_role(self.WEB_ROLE),
            "PAW_OPERATOR_DATABASE_URL": url_for_role(self.OPERATOR_ROLE),
            # The command never uses the migration role.
            "PAW_MIGRATION_DATABASE_URL": "postgresql://nobody:x@127.0.0.1:1/none",
            **extra,
        }

    def redeem_as_web(self, token: str):
        async def go():
            database = Database(make_settings(database_url=url_for_role(self.WEB_ROLE)))
            try:
                return await TokenRedeemer(
                    database, PostgresAuditSink(database)
                ).redeem(token)
            finally:
                await database.dispose()

        return asyncio.run(go())

    def test_setup_recovery_and_replacement_work_as_the_operator_role(self):
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"], **self.environment()
        )
        self.assertEqual(code, 0, err)
        setup_token = self.token_of(out)
        code, out, err = run(
            ["owner-recover", "--confirm-owner-recovery"], **self.environment()
        )
        self.assertEqual(code, 0, err)
        recovery_token = self.token_of(out)

        # The web role spends what the operator role issued.
        self.assertEqual(self.redeem_as_web(recovery_token).purpose.value, "recovery")
        with self.assertRaises(SetupTokenRejectedError):
            self.redeem_as_web(setup_token)
        self.execute("UPDATE users SET status = 'deleted'")
        code, out, err = run(
            ["owner-setup", "--login-name", "second", "--replace-non-live-owner"],
            **self.environment(),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.redeem_as_web(self.token_of(out)).purpose.value, "setup")
        self.assertGreaterEqual(len(self.audit()), 8)
        self.assertNotIn(ROLE_PASSWORD, out + err)

    def test_the_web_role_cannot_run_the_command(self):
        # Only PAW_DATABASE_URL (the web role): the fallback, and it is refused.
        code, out, err = run(
            ["owner-setup", "--login-name", "boss"],
            PAW_DATABASE_URL=url_for_role(self.WEB_ROLE),
        )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("Warning: PAW_OPERATOR_DATABASE_URL is not set", err)
        self.assertIn("Database error (ProgrammingError)", err)
        self.assertNotIn(ROLE_PASSWORD, err)
        self.assertNotIn("permission denied", err)
        self.assertEqual(self.scalar("SELECT count(*) FROM users"), 0)

    def test_the_command_refuses_when_the_role_cannot_write_the_audit_trail(self):
        # The tables are writable, the audit table is not: nothing may happen.
        self.execute(f"REVOKE INSERT ON audit_events FROM {self.OPERATOR_ROLE}")

        code, out, err = run(
            ["owner-setup", "--login-name", "boss"], **self.environment()
        )

        self.assertEqual((code, out), (2, ""))
        self.assertIn("audit trail could not be written", err)
        self.assertEqual(self.scalar("SELECT count(*) FROM users"), 0)
        self.assertEqual(self.scalar("SELECT count(*) FROM setup_tokens"), 0)


if __name__ == "__main__":
    unittest.main()
