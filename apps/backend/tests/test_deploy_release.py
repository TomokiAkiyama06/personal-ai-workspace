"""The release tool (``deploy/release/paw_release.py``; Issue #54, Decision 0079).

It runs nothing on the host but what its configuration and the releases give
it: here a fake "world" stands for the backend commands of a release (the
database: schema revision, maintenance, restore points), the services and the
health check. The releases are built from a real git repository.

* build: a release per commit, its manifest (schema head, migration chain and
  compatibility), immutable (no rebuild, a changed file is found);
* install / update / rollback: the order of the steps; a migration takes and
  verifies a restore point first; a failed update goes back to the known-good
  release (and restores the point) and resumes the tasks; a failed rollback
  stays in maintenance and notifies;
* the pre-update check refuses (nothing changes) on a failed database check,
  an operation in progress, too little disk, an incompatible schema;
* the drain timing out aborts (the current release runs on) unless --stop-now;
* backward compatibility: a rollback over ``expand`` migrations needs no
  restore point, over any other one it does.
"""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

TOOL_PATH = (
    Path(__file__).resolve().parents[1] / "deploy" / "release" / "paw_release.py"
)


def load_tool():
    spec = importlib.util.spec_from_file_location("paw_release", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses look their module up while the class is made.
    sys.modules.setdefault("paw_release", module)
    spec.loader.exec_module(module)
    return sys.modules["paw_release"]


paw_release = load_tool()
Result = paw_release.Result

# Not the GIT_DIR / GIT_INDEX_FILE of a calling git (the pre-commit hook runs
# these tests), nor the user's configuration.
GIT_ENV = {
    **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}

requires_git = unittest.skipUnless(shutil.which("git"), "git is not installed")

MIGRATION = '''"""A test migration."""
revision: str = "{revision}"
down_revision: str | None = {down}
{extra}

def upgrade() -> None:
    pass
'''


class World:
    """The database, the services and the health check, as the commands see
    them. ``fail`` names steps that fail (``deploy-drain``, ``alembic``,
    ``health:<version>``, ``deploy-restore-point-restore``, ``check:<name>``,
    ``start:<version>``)."""

    def __init__(self, root: Path, points: Path) -> None:
        self.root = root
        self.points = points
        self.revision: str | None = None
        self.maintenance = False
        self.running = False
        self.fail: set[str] = set()
        self.calls: list[str] = []
        # The stop commands (counted from 1) that fail.
        self.stops = 0
        self.failing_stops: set[int] = set()

    def release_of(self, argv) -> str:
        python = Path(argv[argv.index("-m") - 1])
        return python.parents[2].name

    def head_of(self, version: str) -> str | None:
        manifest = json.loads(
            (self.root / "releases" / version / "release.json").read_text()
        )
        return manifest["schema_head"]

    def __call__(self, argv, cwd, env, timeout):
        argv = list(argv)
        if argv[0] == "git":
            return paw_release.subprocess_runner(argv, cwd, env, timeout)
        if "-m" in argv:
            version = self.release_of(argv)
            module, args = argv[argv.index("-m") + 1], argv[argv.index("-m") + 2 :]
            name = "alembic" if module == "alembic" else args[0]
            self.calls.append(f"{version}:{name}")
            if name == "deploy-maintenance-begin" and "begin:after-row" in self.fail:
                self.maintenance = True  # the row committed, holding failed
                return Result(3, "", "")
            if name in self.fail:
                return Result(3, "", "")
            return self.backend(version, name, args)
        name = argv[0]
        current = (
            os.readlink(self.root / "current").split("/")[-1]
            if (self.root / "current").is_symlink()
            else None
        )
        self.calls.append(name)
        if name == "svc-stop":
            self.stops += 1
            if self.stops in self.failing_stops:
                return Result(1)
            self.running = False
        elif name == "svc-start":
            if f"start:{current}" in self.fail:
                return Result(1)
            self.running = True
        elif name == "health":
            if not self.running or f"health:{current}" in self.fail:
                return Result(7)
        elif name in self.fail:
            return Result(1)
        return Result(0)

    def backend(self, version: str, name: str, args: list[str]) -> Result:
        if name == "alembic":
            self.revision = self.head_of(version)
        elif name == "deploy-precheck":
            checks = [
                {"name": check, "ok": f"check:{check}" not in self.fail, "detail": "x"}
                for check in ("audit_retention", "recovery_backup")
            ]
            report = {"ok": True, "checks": checks, "status": self.status()}
            return Result(0, json.dumps(report))
        elif name == "deploy-status":
            return Result(0, json.dumps(self.status()))
        elif name == "deploy-maintenance-begin":
            self.maintenance = True
        elif name == "deploy-maintenance-end":
            self.maintenance = False
        elif name == "deploy-restore-point-create":
            label = args[args.index("--label") + 1]
            self.points.mkdir(mode=0o700, exist_ok=True)
            (self.points / f"{label}.json").write_text(
                json.dumps({"revision": self.revision, "verified": None})
            )
        elif name == "deploy-restore-point-verify":
            label = args[args.index("--label") + 1]
            path = self.points / f"{label}.json"
            point = json.loads(path.read_text())
            point["verified"] = {"revision": point["revision"]}
            path.write_text(json.dumps(point))
        elif name == "deploy-restore-point-restore":
            label = args[args.index("--label") + 1]
            point = json.loads((self.points / f"{label}.json").read_text())
            self.revision = point["revision"]
            self.maintenance = True  # taken during the update's maintenance
        return Result(0)

    def status(self) -> dict:
        return {
            "schema_revision": self.revision,
            "maintenance": {"to_release": "x"} if self.maintenance else None,
        }


@requires_git
class ReleaseToolTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "opt"
        self.points = self.tmp / "points"
        self.repo = self.tmp / "repo"
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.tmp)
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "Test")
        env_file = self.tmp / "deploy.env"
        env_file.write_text("PAW_MIGRATION_DATABASE_URL='postgresql://o:p@h/d'\n")
        os.chmod(env_file, 0o600)
        self.config = self.tmp / "release.toml"
        self.config.write_text(
            f"""
root = "{self.root}"
state_dir = "{self.tmp / "state"}"
restore_point_dir = "{self.points}"
env_file = "{env_file}"
min_free_gib = 0
drain_timeout_seconds = 60
health_timeout_seconds = 10
health_interval_seconds = 5

[commands]
stop = [["svc-stop"]]
start = [["svc-start"]]
health = [["health"]]
post_restore = [["user-erasure-run"]]
notify = [["notify", "{{message}}"]]
precheck = [["timer-check"]]
"""
        )
        self.world = World(self.root, self.points)
        self.clock = [0.0]

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", *args],
            cwd=cwd or self.repo,
            env=GIT_ENV,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit_migration(self, revision, down, extra="") -> str:
        versions = self.repo / "apps" / "backend" / "migrations" / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        (versions / f"{revision}_m.py").write_text(
            MIGRATION.format(revision=revision, down=repr(down), extra=extra)
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", revision)
        return self.git("rev-parse", "HEAD")

    def run_tool(self, *argv) -> tuple[int, str]:
        out = io.StringIO()

        def sleep(seconds):
            self.clock[0] += seconds

        code = paw_release.main(
            ["--config", str(self.config), *argv],
            runner=self.world,
            out=out,
            sleep=sleep,
            monotonic=lambda: self.clock[0],
        )
        return code, out.getvalue()

    def build(self, version, revision, down, extra="") -> None:
        commit = self.commit_migration(revision, down, extra)
        code, out = self.run_tool(
            "build", "--repo", str(self.repo), "--commit", commit, "--version", version
        )
        self.assertEqual(code, 0, out)

    def current(self) -> str:
        return os.readlink(self.root / "current").split("/")[-1]

    def state(self) -> dict:
        return json.loads((self.tmp / "state" / "state.json").read_text())

    def installed(self) -> None:
        """r1 (0001) installed; r2 (0002, expand) and r3 (0003, contract) built."""
        self.build("r1", "0001", None)
        self.build("r2", "0002", "0001", 'paw_compatibility: str = "expand"')
        self.build("r3", "0003", "0002")
        code, out = self.run_tool("install", "r1")
        self.assertEqual(code, 0, out)
        self.world.calls.clear()


class BuildTest(ReleaseToolTestCase):
    def test_a_release_records_its_commit_and_schema(self):
        self.build("r1", "0001", None)
        self.build("r2", "0002", "0001", 'paw_compatibility: str = "expand"')
        manifest = json.loads(
            (self.root / "releases" / "r2" / "release.json").read_text()
        )
        self.assertEqual(manifest["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(manifest["schema_head"], "0002")
        self.assertEqual(
            manifest["migrations"],
            [
                {"revision": "0001", "down_revision": None, "compatibility": None},
                {
                    "revision": "0002",
                    "down_revision": "0001",
                    "compatibility": "expand",
                },
            ],
        )
        self.assertTrue(
            (
                self.root / "releases" / "r2" / "apps/backend/migrations/versions"
            ).is_dir()
        )
        code, out = self.run_tool("list")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["r1", "r2"])

    def test_releases_are_immutable(self):
        self.build("r1", "0001", None)
        commit = self.git("rev-parse", "HEAD")
        code, out = self.run_tool(
            "build", "--repo", str(self.repo), "--commit", commit, "--version", "r1"
        )
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("immutable", out)
        # A file changed after the build is found before it is installed.
        migration = next((self.root / "releases/r1").rglob("0001_m.py"))
        migration.write_text(migration.read_text() + "# changed\n")
        code, out = self.run_tool("install", "r1")
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("source digest", out)
        self.assertFalse((self.root / "current").exists())

    def test_the_default_version_is_the_commit_date_and_sha(self):
        commit = self.commit_migration("0001", None)
        code, out = self.run_tool("build", "--repo", str(self.repo), "--commit", commit)
        self.assertEqual(code, 0, out)
        (version,) = os.listdir(self.root / "releases")
        self.assertRegex(version, rf"^\d{{8}}-{commit[:12]}$")

    def test_a_branched_or_invalid_chain_is_refused(self):
        self.commit_migration("0001", None)
        commit = self.commit_migration("0002", None)  # a second head
        code, out = self.run_tool(
            "build", "--repo", str(self.repo), "--commit", commit, "--version", "r1"
        )
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("2 heads", out)
        self.assertEqual(os.listdir(self.root / "releases"), [])


class UpdateTest(ReleaseToolTestCase):
    def test_install_migrates_starts_and_marks_known_good(self):
        self.build("r1", "0001", None)
        code, out = self.run_tool("install", "r1")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.current(), "r1")
        self.assertEqual(self.world.revision, "0001")
        self.assertEqual(self.state()["known_good"], ["r1"])
        code, out = self.run_tool("install", "r1")
        self.assertEqual(code, paw_release.EXIT_REFUSED)

    def test_an_update_with_a_migration(self):
        self.installed()
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, 0, out)
        self.assertEqual(
            self.world.calls,
            [
                "r1:deploy-precheck",
                "timer-check",
                "r1:deploy-maintenance-begin",
                "r1:deploy-drain",
                "r1:recovery-backup-run",
                "svc-stop",
                "r1:deploy-restore-point-create",
                "r1:deploy-restore-point-verify",
                "r2:alembic",
                "svc-start",
                "r2:deploy-status",
                "health",
                "r2:deploy-maintenance-end",
            ],
        )
        self.assertEqual((self.current(), self.world.revision), ("r2", "0002"))
        self.assertFalse(self.world.maintenance)
        self.assertEqual(self.state()["known_good"], ["r1", "r2"])
        self.assertIsNone(self.state()["in_progress"])
        (point,) = self.points.glob("*.json")
        self.assertTrue(point.name.startswith("r1-to-r2-"))

    def test_an_application_only_update_takes_no_restore_point(self):
        self.build("r1", "0001", None)
        self.git("commit", "-q", "--allow-empty", "-m", "app")
        code, out = self.run_tool(
            "build",
            "--repo",
            str(self.repo),
            "--commit",
            "HEAD",
            "--version",
            "r1b",
        )
        self.assertEqual(code, 0, out)
        self.assertEqual(self.run_tool("install", "r1")[0], 0)
        self.world.calls.clear()
        code, out = self.run_tool("update", "r1b")
        self.assertEqual(code, 0, out)
        self.assertNotIn("r1:deploy-restore-point-create", self.world.calls)
        self.assertNotIn("r1b:alembic", self.world.calls)
        self.assertIn("application only", out)

    def test_a_failed_health_check_rolls_back_and_restores(self):
        self.installed()
        self.world.fail.add("health:r2")
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_ROLLED_BACK, out)
        self.assertEqual((self.current(), self.world.revision), ("r1", "0001"))
        self.assertFalse(self.world.maintenance)
        self.assertTrue(self.world.running)
        calls = self.world.calls
        tail = calls[calls.index("r2:alembic") :]
        self.assertEqual(
            tail[-6:-3],
            ["r1:deploy-restore-point-restore", "r1:user-erasure-run", "svc-start"],
        )
        self.assertEqual(tail[-1], "r1:deploy-maintenance-end")
        self.assertEqual(self.state()["known_good"], ["r1"])
        self.assertIsNone(self.state()["in_progress"])
        # The health check was retried until its timeout (10 s, every 5 s).
        self.assertEqual(calls.count("r2:deploy-status"), 3)

    def test_a_failed_migration_restores_the_point(self):
        self.installed()
        self.world.fail.add("alembic")
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_ROLLED_BACK, out)
        self.assertIn("r1:deploy-restore-point-restore", self.world.calls)
        self.assertEqual(self.current(), "r1")

    def test_a_failed_restore_stays_in_maintenance_and_notifies(self):
        self.installed()
        self.world.fail.update({"health:r2", "deploy-restore-point-restore"})
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_MAINTENANCE, out)
        self.assertTrue(self.world.maintenance)
        self.assertFalse(self.world.running)
        self.assertEqual(self.world.calls[-1], "notify")
        self.assertIsNotNone(self.state()["in_progress"])
        # Nothing else starts until it is resolved.
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("no_operation_in_progress", out)

    def test_a_rollback_whose_stop_fails_stays_in_maintenance(self):
        # Codex review #204: the new release may still run (and write).
        self.installed()
        self.world.fail.add("health:r2")
        self.world.failing_stops = {2}  # the update's stop works, the rollback's not
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_MAINTENANCE, out)
        self.assertNotIn("r1:deploy-restore-point-restore", self.world.calls)
        self.assertEqual(self.current(), "r2")
        self.assertTrue(self.world.maintenance)
        self.assertEqual(self.world.calls[-1], "notify")

    def test_a_begin_that_failed_after_its_row_ends_the_maintenance(self):
        # Codex review #204 (ad3c59c): the row may be committed although the
        # command failed (holding a task, the audit row): the queue stays gated.
        self.installed()
        self.world.fail.add("begin:after-row")
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_ABORTED, out)
        self.assertFalse(self.world.maintenance)
        self.assertEqual(self.world.calls[-1], "r1:deploy-maintenance-end")
        self.assertIsNone(self.state()["in_progress"])

    def test_a_drain_timeout_aborts_unless_stop_now(self):
        self.installed()
        self.world.fail.add("deploy-drain")
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_ABORTED, out)
        self.assertEqual(self.current(), "r1")
        self.assertNotIn("svc-stop", self.world.calls)
        self.assertFalse(self.world.maintenance)
        self.assertIsNone(self.state()["in_progress"])
        code, out = self.run_tool("update", "r2", "--stop-now")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.current(), "r2")

    def test_a_failed_restore_point_aborts_before_the_migration(self):
        self.installed()
        self.world.fail.add("deploy-restore-point-verify")
        code, out = self.run_tool("update", "r2")
        self.assertEqual(code, paw_release.EXIT_ABORTED, out)
        self.assertNotIn("r2:alembic", self.world.calls)
        self.assertEqual(
            self.world.calls[-2:], ["svc-start", "r1:deploy-maintenance-end"]
        )
        self.assertEqual((self.current(), self.world.revision), ("r1", "0001"))

    def test_a_failed_precheck_changes_nothing(self):
        self.installed()
        for failing in ("check:audit_retention", "timer-check"):
            with self.subTest(failing=failing):
                self.world.fail = {failing}
                self.world.calls.clear()
                code, out = self.run_tool("update", "r2")
                self.assertEqual(code, paw_release.EXIT_REFUSED, out)
                self.assertIn("FAIL", out)
                self.assertNotIn("r1:deploy-maintenance-begin", self.world.calls)
        code, out = self.run_tool("precheck", "r2")
        self.assertEqual(code, paw_release.EXIT_REFUSED)

    def test_too_little_disk_is_refused(self):
        self.installed()
        out = io.StringIO()
        self.config.write_text(
            self.config.read_text().replace("min_free_gib = 0", "min_free_gib = 1")
        )
        code = paw_release.main(
            ["--config", str(self.config), "update", "r2"],
            runner=self.world,
            out=out,
            free_bytes=lambda path: 0,
        )
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("disk_free_restore_points", out.getvalue())

    def test_one_operation_at_a_time(self):
        self.installed()
        state = paw_release.State(paw_release.load_config(self.config))
        entered, release = threading.Event(), threading.Event()

        def hold():
            with state.lock():
                entered.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        entered.wait(5)
        try:
            code, out = self.run_tool("update", "r2")
        finally:
            release.set()
            holder.join()
        self.assertEqual(code, paw_release.EXIT_ENVIRONMENT)
        self.assertIn("another paw-release operation", out)


class RollbackTest(ReleaseToolTestCase):
    def test_over_expand_migrations_no_restore_point_is_needed(self):
        self.installed()
        self.assertEqual(self.run_tool("update", "r2")[0], 0)
        self.world.calls.clear()
        code, out = self.run_tool("rollback")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.current(), self.world.revision), ("r1", "0002"))
        self.assertNotIn("r1:deploy-restore-point-restore", self.world.calls)
        self.assertFalse(self.world.maintenance)

    def test_over_another_migration_a_restore_point_is_required(self):
        self.installed()
        self.assertEqual(self.run_tool("update", "r2")[0], 0)
        self.assertEqual(self.run_tool("update", "r3")[0], 0)
        code, out = self.run_tool("rollback")
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("not backward compatible", out)
        (point,) = [p.stem for p in self.points.glob("r2-to-r3-*.json")]
        code, out = self.run_tool("rollback", "--to", "r1", "--restore-point", point)
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("needs 0001", out)
        self.world.calls.clear()
        code, out = self.run_tool("rollback", "--restore-point", point)
        self.assertEqual(code, 0, out)
        self.assertEqual((self.current(), self.world.revision), ("r2", "0002"))
        self.assertIn("r2:deploy-restore-point-restore", self.world.calls)
        self.assertIn("r2:user-erasure-run", self.world.calls)
        self.assertFalse(self.world.maintenance)

    def test_a_failed_stop_changes_nothing_else(self):
        # Codex review #204: no restore and no switch while the release may run.
        self.installed()
        self.assertEqual(self.run_tool("update", "r2")[0], 0)
        self.assertEqual(self.run_tool("update", "r3")[0], 0)
        (point,) = [p.stem for p in self.points.glob("r2-to-r3-*.json")]
        self.world.failing_stops = {self.world.stops + 1}
        self.world.calls.clear()
        code, out = self.run_tool("rollback", "--restore-point", point)
        self.assertEqual(code, paw_release.EXIT_MAINTENANCE, out)
        self.assertNotIn("r2:deploy-restore-point-restore", self.world.calls)
        self.assertEqual((self.current(), self.world.revision), ("r3", "0003"))
        self.assertEqual(self.world.calls[-1], "notify")

    def test_the_default_is_the_known_good_release_before_the_current_one(self):
        # Codex review #204 (ad3c59c): r1 -> r2 -> r3, back to r2, then the
        # default rollback goes to r1, not forward to r3.
        self.installed()
        self.assertEqual(self.run_tool("update", "r2")[0], 0)
        self.assertEqual(self.run_tool("update", "r3")[0], 0)
        (point,) = [p.stem for p in self.points.glob("r2-to-r3-*.json")]
        code, out = self.run_tool("rollback", "--to", "r2", "--restore-point", point)
        self.assertEqual(code, 0, out)
        code, out = self.run_tool("rollback")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.current(), "r1")

    def test_only_to_a_known_good_release(self):
        self.installed()
        code, out = self.run_tool("rollback", "--to", "r3")
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("not a known-good release", out)
        code, out = self.run_tool("rollback")
        self.assertEqual(code, paw_release.EXIT_REFUSED)
        self.assertIn("no earlier known-good release", out)

    def test_end_maintenance_after_a_manual_fix(self):
        self.installed()
        self.world.fail.update({"health:r2", "deploy-restore-point-restore"})
        self.assertEqual(self.run_tool("update", "r2")[0], paw_release.EXIT_MAINTENANCE)
        self.world.fail.clear()
        code, out = self.run_tool("end-maintenance")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.world.maintenance)
        self.assertIsNone(self.state()["in_progress"])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def config(self, text: str):
        path = self.tmp / "release.toml"
        path.write_text(text)
        return paw_release.load_config(path)

    def test_paths_must_be_absolute_and_commands_argv_lists(self):
        base = (
            'root = "/opt/paw"\nstate_dir = "/var/lib/paw"\nrestore_point_dir = "/r"\n'
        )
        self.assertEqual(self.config(base).python, "venv/bin/python")
        for text in (
            base.replace('"/opt/paw"', '"opt"'),
            base + '[commands]\nstop = "systemctl stop paw"\n',
            base + '[commands]\nreboot = [["x"]]\n',
            base + 'python = "/usr/bin/python3"\n',
        ):
            with self.subTest(text=text), self.assertRaises(paw_release.ReleaseError):
                self.config(text)

    def test_the_env_file_must_be_private(self):
        path = self.tmp / "deploy.env"
        path.write_text("# comment\nA=1\nB='two words'\n")
        os.chmod(path, 0o644)
        with self.assertRaises(paw_release.ReleaseError):
            paw_release.read_env_file(path)
        os.chmod(path, 0o600)
        self.assertEqual(paw_release.read_env_file(path), {"A": "1", "B": "two words"})

    def test_the_subprocess_runner(self):
        result = paw_release.subprocess_runner(
            ["sh", "-c", "echo hi; exit 3"], None, {}, 10
        )
        self.assertEqual((result.code, result.stdout), (3, "hi\n"))
        self.assertEqual(
            paw_release.subprocess_runner(["/nonexistent/x"], None, {}, 10).code, 127
        )


class RepositoryTest(unittest.TestCase):
    """What the tool reads of this repository."""

    def test_the_example_configuration_loads(self):
        config = paw_release.load_config(TOOL_PATH.parent / "release.example.toml")
        self.assertEqual(config.root, Path("/opt/paw"))
        self.assertEqual(config.commands["post_restore"], (("user-erasure-run",),))

    def test_the_migrations_of_this_repository_form_one_chain(self):
        versions = Path(__file__).resolve().parents[1] / "migrations" / "versions"
        migrations = paw_release.read_migrations(versions)
        head = paw_release.schema_head(migrations)
        self.assertEqual(migrations["0191"].compatibility, "expand")
        release = paw_release.Release("x", versions, "c", head, migrations, "d")
        self.assertEqual(len(release.chain()), len(migrations))

    def test_the_example_stops_the_services_of_the_timers_it_stops(self):
        # Codex review #204: stopping a timer does not stop its running job.
        config = paw_release.load_config(TOOL_PATH.parent / "release.example.toml")
        stopped = {unit for argv in config.commands["stop"] for unit in argv[2:]}
        for unit in sorted(stopped):
            if unit.endswith(".timer"):
                with self.subTest(unit=unit):
                    self.assertIn(unit.removesuffix(".timer") + ".service", stopped)

    def test_the_backend_unit_can_be_enabled_and_write_its_paths(self):
        # Codex review #204 (80d3189): `enable` needs an [Install] target, and
        # the backend creates checkouts under the users' homes.
        unit = (TOOL_PATH.parents[1] / "systemd" / "paw-backend.service").read_text()
        settings = [
            line.split("=", 1)
            for line in unit.splitlines()
            if "=" in line and not line.startswith("#")
        ]
        values = {key.strip(): value.strip() for key, value in settings}
        self.assertIn("[Install]", unit)
        self.assertEqual(values.get("WantedBy"), "multi-user.target")
        protect = values.get("ProtectSystem")
        self.assertNotEqual(protect, "strict")
        self.assertNotEqual(values.get("ProtectHome"), "true")

    def test_the_deployment_document_follows_the_decisions_status(self):
        # Codex review #204 (ad3c59c): no "until it is approved" once it is.
        docs = Path(__file__).resolve().parents[3] / "docs"
        decision = (docs / "decisions" / "0079-deploy-update-rollback.md").read_text()
        document = (docs / "DEPLOYMENT_UPDATE.md").read_text()
        if "- Status: Approved" in decision:
            for obsolete in ("proposes the mechanism", "until it is"):
                with self.subTest(obsolete=obsolete):
                    self.assertNotIn(obsolete, document)


class PruneTest(ReleaseToolTestCase):
    def point(self, label: str, created_at: str) -> None:
        self.points.mkdir(mode=0o700, exist_ok=True)
        (self.points / f"{label}.json").write_text(
            json.dumps({"revision": "0001", "created_at": created_at})
        )
        (self.points / f"{label}.dump").write_text("dump")

    def test_old_restore_points_go_after_an_update_and_on_demand(self):
        self.installed()
        self.point("old", "2020-01-01T00:00:00+00:00")
        self.assertEqual(self.run_tool("update", "r2")[0], 0)
        names = sorted(p.name for p in self.points.iterdir())
        self.assertNotIn("old.json", names)
        self.assertNotIn("old.dump", names)
        self.assertEqual(len(names), 1)  # the update's own point (fake: no dump)
        self.point("older", "2019-01-01T00:00:00+00:00")
        self.point("fresh", "2999-01-01T00:00:00+00:00")
        code, out = self.run_tool("prune-restore-points")
        self.assertEqual(code, 0, out)
        self.assertIn("Deleted 1", out)
        self.assertTrue((self.points / "fresh.dump").exists())
