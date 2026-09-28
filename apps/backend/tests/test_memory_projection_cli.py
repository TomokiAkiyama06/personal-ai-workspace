"""``python -m paw_backend.cli memory-projection-*`` (PAW-045).

The command reads ``PAW_DATABASE_URL`` and ``PAW_MEMORY_PROJECTION_DIR`` from
the environment, never an argument; it prints no path, URL or memory text; its
exit code is what systemd's ``OnFailure=`` (the failure notification) watches.
The database cases are skipped unless ``PAW_TEST_DATABASE_URL`` is set. Files are
written below a temporary directory only; the systemd unit files are read as
text (nothing is installed).
"""

import asyncio
import configparser
import io
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from paw_backend.cli import dispatch, owner
from paw_backend.cli import memory_projection as cli
from paw_backend.memory.projection import MARKER_NAME, open_target

from .memory_support import requires_postgres, sync_database_url
from .projection_support import TemporaryRoot, tree
from .support import paw_environment
from .task_support import TEST_DATABASE_URL, migrate

BACKEND_DIR = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = BACKEND_DIR / "deploy" / "systemd"


def run(argv, *, homes=(), clock=None, **environment) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with paw_environment(**environment):
        code = cli.main(
            argv, stdout=out, stderr=err, protected_homes=homes, clock=clock
        )
    return code, out.getvalue(), err.getvalue()


class ArgumentTest(unittest.TestCase):
    def test_the_commands_go_to_this_module(self):
        for command in ("memory-projection-run", "memory-projection-check"):
            self.assertIs(dispatch.command_module([command]), cli)
        self.assertIs(dispatch.command_module(["owner-setup"]), owner)

    def test_the_owner_help_points_at_the_projection_commands(self):
        out = io.StringIO()
        with paw_environment():
            owner.main(["--help"], stdout=out, stderr=io.StringIO())
        self.assertIn("memory-projection-run", out.getvalue())

    def test_an_argument_is_never_echoed(self):
        code, out, err = run(["memory-projection-run", "--dir", "/secret/place"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotIn("/secret/place", out + err)

    def test_without_a_database_url_nothing_runs(self):
        tmp = TemporaryRoot(self)
        code, _, err = run(
            ["memory-projection-run"], PAW_MEMORY_PROJECTION_DIR=str(tmp.root)
        )
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertIn("PAW_DATABASE_URL is not set", err)
        self.assertFalse(tmp.root.exists())

    def test_without_a_directory_nothing_runs(self):
        code, _, err = run(
            ["memory-projection-run"],
            PAW_DATABASE_URL="postgresql://user:pw@127.0.0.1:1/none",
        )
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertIn("PAW_MEMORY_PROJECTION_DIR is not set", err)
        self.assertNotIn("pw@", err)

    def test_an_unreachable_database_is_an_environment_error(self):
        tmp = TemporaryRoot(self)
        code, _, err = run(
            ["memory-projection-check"],
            PAW_DATABASE_URL="postgresql://user:hunter2@127.0.0.1:1/none",
            PAW_DATABASE_TIMEOUT_SECONDS="1",
            PAW_MEMORY_PROJECTION_DIR=str(tmp.root),
        )
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertNotIn("hunter2", err)

    def test_a_run_against_an_unreachable_database_is_an_environment_error(self):
        # Decision 0038 6: the database cannot be reached -> 2, not 3.
        tmp = TemporaryRoot(self)
        code, _, err = run(
            ["memory-projection-run"],
            PAW_DATABASE_URL="postgresql://user:hunter2@127.0.0.1:1/none",
            PAW_DATABASE_TIMEOUT_SECONDS="1",
            PAW_MEMORY_PROJECTION_DIR=str(tmp.root),
        )
        self.assertEqual(code, cli.EXIT_ENVIRONMENT)
        self.assertIn("Database error", err)
        self.assertNotIn("hunter2", err)
        self.assertNotIn(str(tmp.root), err)


@requires_postgres
class CommandTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()  # head
        cls.engine = create_engine(sync_database_url())

    @classmethod
    def tearDownClass(cls) -> None:
        with cls.engine.begin() as connection:
            connection.execute(text("TRUNCATE memories CASCADE"))
        cls.engine.dispose()

    def setUp(self) -> None:
        self.tmp = TemporaryRoot(self)
        with self.engine.begin() as connection:
            connection.execute(text("TRUNCATE memories CASCADE"))
            latest = connection.execute(
                text(
                    "SELECT max(occurred_at) FROM audit_events"
                    " WHERE resource_kind = 'memory_projection_run'"
                )
            ).scalar()
        # Later than every projection row earlier tests left in audit_events.
        now = datetime.now(UTC)
        self.now = max(now, latest + timedelta(days=1)) if latest else now

    def environment(self, root: Path | None = None) -> dict[str, str]:
        return {
            "PAW_DATABASE_URL": TEST_DATABASE_URL,
            "PAW_MEMORY_PROJECTION_DIR": str(root or self.tmp.root),
        }

    def command(self, argv, *, root=None, at=None):
        moment = at or self.now
        return run(
            argv, homes=self.tmp.homes, clock=lambda: moment, **self.environment(root)
        )

    def seed_user_memory(self, title: str, content: str) -> tuple:
        owner_id = uuid4()
        with self.engine.begin() as connection:
            memory_id = connection.execute(
                text("INSERT INTO memories DEFAULT VALUES RETURNING id")
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO memory_versions (memory_id, version_number, scope,"
                    " owner_user_id, memory_type, title, content, status,"
                    " confirmation_state, freshness_policy, actor_type) VALUES"
                    " (:m, 1, 'user', :o, 'note', :t, :c, 'active', 'confirmed',"
                    " 'permanent', 'system')"
                ),
                {"m": memory_id, "o": owner_id, "t": title, "c": content},
            )
        return owner_id, memory_id

    def test_a_run_writes_the_projection_and_the_check_passes(self):
        owner_id, memory_id = self.seed_user_memory("tabs", "private text")
        code, out, err = self.command(["memory-projection-run"])
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertEqual(out, "")
        self.assertIn("Memory projection completed: memories=1 written=2", err)
        # stderr names no path and no memory text.
        self.assertNotIn(str(self.tmp.base), err)
        self.assertNotIn("private text", err)
        files = tree(self.tmp.root)
        self.assertIn(f"users/{owner_id}/{memory_id}.md", files)
        code, _, err = self.command(
            ["memory-projection-check"], at=self.now + timedelta(minutes=10)
        )
        self.assertEqual(code, cli.EXIT_OK, err)
        self.assertIn("OK", err)

    def test_the_check_fails_when_no_run_completed_recently(self):
        code, _, err = self.command(["memory-projection-run"])
        self.assertEqual(code, cli.EXIT_OK, err)
        code, _, err = self.command(
            ["memory-projection-check", "--max-age-minutes", "30"],
            at=self.now + timedelta(minutes=31),
        )
        self.assertEqual(code, cli.EXIT_PROJECTION_FAILED)
        self.assertIn("no memory projection run completed", err)

    def test_a_refused_directory_fails_is_audited_and_the_check_fails(self):
        checkout = self.tmp.base / "checkout"
        (checkout / ".git").mkdir(parents=True)
        (checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        code, _, err = self.command(["memory-projection-run"], root=checkout / "memory")
        self.assertEqual(code, cli.EXIT_PROJECTION_FAILED)
        self.assertIn("FAILED at check_target (inside_git_work_tree)", err)
        self.assertIn("was recorded", err)
        self.assertNotIn(str(checkout), err)
        self.assertFalse((checkout / "memory").exists())
        code, _, err = self.command(["memory-projection-check"])
        self.assertEqual(code, cli.EXIT_PROJECTION_FAILED)
        self.assertIn("check_target:inside_git_work_tree", err)

    def test_a_directory_that_is_not_the_projections_is_left_alone(self):
        self.tmp.root.mkdir()
        (self.tmp.root / "notes.md").write_text("mine")
        code, _, err = self.command(["memory-projection-run"])
        self.assertEqual(code, cli.EXIT_PROJECTION_FAILED)
        self.assertIn("not_empty", err)
        self.assertEqual(sorted(p.name for p in self.tmp.root.iterdir()), ["notes.md"])

    def test_a_concurrent_run_is_refused(self):
        held = open_target(self.tmp.root, self.tmp.homes)
        self.addCleanup(held.close)
        code, _, err = self.command(["memory-projection-run"])
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("in progress", err)
        self.assertEqual([p.name for p in self.tmp.root.iterdir()], [MARKER_NAME])

    def test_the_command_runs_in_its_own_event_loop(self):
        # ``main`` owns its loop (asyncio.run): a caller in a thread works.
        code, _, err = asyncio.run(
            asyncio.to_thread(self.command, ["memory-projection-run"])
        )
        self.assertEqual(code, cli.EXIT_OK, err)


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd keys are case-sensitive
    parser.read_string((SYSTEMD_DIR / name).read_text(encoding="utf-8"))
    return parser


class SystemdUnitTest(unittest.TestCase):
    def test_the_service_runs_the_command_once_as_a_oneshot(self):
        service = unit("paw-memory-projection.service")
        self.assertEqual(service["Service"]["Type"], "oneshot")
        self.assertIn(
            "-m paw_backend.cli memory-projection-run",
            service["Service"]["ExecStart"],
        )

    def test_the_credentials_come_from_an_environment_file(self):
        service = unit("paw-memory-projection.service")
        self.assertIn("EnvironmentFile", service["Service"])
        self.assertNotIn("PAW_DATABASE_URL=", service["Service"].get("Environment", ""))

    def test_the_service_cannot_write_into_homes_or_elsewhere(self):
        service = unit("paw-memory-projection.service")["Service"]
        self.assertEqual(service["ProtectHome"], "true")
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["ReadWritePaths"], "/srv/personal-ai/memory")
        self.assertEqual(service["UMask"], "0077")

    def test_a_failed_run_triggers_the_failure_unit(self):
        service = unit("paw-memory-projection.service")
        self.assertEqual(
            service["Unit"]["OnFailure"], "paw-memory-projection-failure.service"
        )
        failure = (SYSTEMD_DIR / "paw-memory-projection-failure.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("memory.projection.failed", failure)
        self.assertIn("--priority=crit", failure)

    def test_a_terminated_run_has_time_to_record_its_failure(self):
        service = unit("paw-memory-projection.service")
        self.assertEqual(service["Service"]["KillSignal"], "SIGTERM")
        self.assertIn("TimeoutStopSec", service["Service"])

    def test_the_timer_runs_every_five_minutes_and_catches_up(self):
        timer = unit("paw-memory-projection.timer")
        self.assertEqual(timer["Timer"]["OnCalendar"], "*:0/5")
        self.assertEqual(timer["Timer"]["Persistent"], "true")
        self.assertEqual(timer["Timer"]["Unit"], "paw-memory-projection.service")
        self.assertEqual(timer["Install"]["WantedBy"], "timers.target")


if __name__ == "__main__":
    unittest.main()
