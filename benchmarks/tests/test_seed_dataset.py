"""Tests of the public seed datasets (paw-seed-v1, v2) and their evaluator-side helpers."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from benchmarks import seed_check, seed_dataset
from benchmarks.seed_dataset import (
    CATEGORIES,
    DIFFICULTIES,
    MANIFEST_PATH,
    SeedDatasetError,
    check_manifest,
    dataset_summary,
    hidden_command,
    load_manifest,
    manifest_path,
    snapshot_commit,
)


def git(repository, *arguments):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        env=environment,
    ).stdout.decode()


class PublicDatasetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest, cls.entries = load_manifest()

    def test_manifest_and_tasks_are_consistent(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(check_manifest(manifest, MANIFEST_PATH.parent), [])

    def test_mix_meets_the_issue_minimums(self):
        summary = dataset_summary(self.entries)
        self.assertGreaterEqual(summary["total"], 20)
        self.assertGreaterEqual(summary["kind"]["historical"], 8)
        self.assertGreaterEqual(summary["kind"]["spec"], 6)
        self.assertGreaterEqual(summary["kind"]["injected_bug"], 6)
        for category in ("multi_file", "repo_exploration", "test_fix"):
            self.assertIn(category, summary["category"])
        self.assertEqual(set(summary["difficulty"]), set(DIFFICULTIES))

    def test_every_task_records_difficulty_and_categories(self):
        for entry in self.entries:
            with self.subTest(task=entry.task_id):
                self.assertIn(entry.difficulty, DIFFICULTIES)
                self.assertTrue(set(entry.categories) <= set(CATEGORIES))

    def test_historical_tasks_name_their_pull_request(self):
        for entry in self.entries:
            if entry.kind != "historical":
                continue
            with self.subTest(task=entry.task_id):
                self.assertIsInstance(entry.source["pull_request"], int)
                self.assertRegex(entry.source["merge_commit"], r"^[0-9a-f]{40}$")
                self.assertNotEqual(entry.source["merge_commit"], entry.starting_commit)
                self.assertTrue(entry.source["golden_tests"])

    def test_hidden_checks_carry_only_opaque_references(self):
        for entry in self.entries:
            with self.subTest(task=entry.task_id):
                for check in entry.document["hidden_checks"]:
                    self.assertEqual(set(check), {"id", "type", "reference_id"})
                    self.assertRegex(check["reference_id"], r"^seed-v1-[0-9a-f]{12}$")

    def test_public_files_do_not_name_private_material(self):
        for path in (MANIFEST_PATH.parent / "tasks").glob("*.json"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("/data/datasets", text)
                self.assertNotIn("golden.patch", text)
                document = json.loads(text)
                self.assertNotIn("known_good_commit", document["repository"])


class PublicDatasetV2Test(unittest.TestCase):
    """paw-seed-v2 (Issue #198, Decision 0074): v1 listed unchanged plus new tasks."""

    @classmethod
    def setUpClass(cls):
        cls.path = manifest_path("paw-seed-v2")
        cls.manifest, cls.entries = load_manifest(cls.path)

    def test_manifest_and_tasks_are_consistent(self):
        manifest = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(check_manifest(manifest, self.path.parent), [])

    def test_every_v1_task_is_listed_unchanged(self):
        _, v1 = load_manifest()
        listed = {e.task_id: e for e in self.entries if e.dataset == "paw-seed-v1"}
        self.assertEqual(set(listed), {e.task_id for e in v1})
        for entry in v1:
            with self.subTest(task=entry.task_id):
                self.assertEqual(listed[entry.task_id].path, entry.path)
                self.assertEqual(listed[entry.task_id].document, entry.document)

    def test_new_tasks_are_spec_or_injected_bug_of_medium_or_hard(self):
        new = [e for e in self.entries if e.dataset == "paw-seed-v2"]
        self.assertGreaterEqual(len(new), 20)
        for entry in new:
            with self.subTest(task=entry.task_id):
                self.assertIn(entry.kind, ("spec", "injected_bug"))
                self.assertIn(entry.difficulty, ("medium", "hard"))
                for check in entry.document["hidden_checks"]:
                    self.assertRegex(check["reference_id"], r"^seed-v2-[0-9a-f]{12}$")

    def test_public_files_do_not_name_private_material(self):
        for path in (self.path.parent / "tasks").glob("*.json"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("/data/datasets", text)
                self.assertNotIn("golden.patch", text)
                self.assertNotIn("known_good_commit", json.loads(text)["repository"])


class CheckManifestTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        (self.directory / "tasks").mkdir()
        self.task_id = "paw-seed-v1-spec-01-example"
        self.task = {
            "schema_version": "1.0",
            "task_id": self.task_id,
            "kind": "spec",
            "repository": {
                "locator": "paw-dataset://paw-seed-v1/" + self.task_id,
                "starting_commit": "1" * 40,
            },
            "issue_text": "Add a feature.",
            "visible_checks": [],
            "hidden_checks": [
                {
                    "id": "h",
                    "type": "acceptance",
                    "reference_id": "seed-v1-0123456789ab",
                }
            ],
        }
        self.manifest = {
            "dataset": "paw-seed-v1",
            "base_commit": "1" * 40,
            "tasks": [
                {
                    "task_id": self.task_id,
                    "file": f"tasks/{self.task_id}.json",
                    "kind": "spec",
                    "difficulty": "easy",
                    "categories": ["feature"],
                    "source": {},
                }
            ],
        }

    def errors(self):
        path = self.directory / "tasks" / f"{self.task_id}.json"
        path.write_text(json.dumps(self.task), encoding="utf-8")
        return check_manifest(self.manifest, self.directory)

    def test_valid(self):
        self.assertEqual(self.errors(), [])

    def test_known_good_commit_must_stay_private(self):
        self.task["repository"]["known_good_commit"] = "2" * 40
        self.assertIn(
            f"tasks/{self.task_id}.json: known_good_commit must stay private",
            self.errors(),
        )

    def test_hidden_reference_must_be_opaque(self):
        self.task["hidden_checks"][0]["reference_id"] = "tests/test_hidden.py"
        self.assertTrue(any("opaque" in error for error in self.errors()))

    def test_credentials_are_refused(self):
        self.task["issue_text"] = (
            "use postgresql://example:not-a-real-password@localhost/db"
        )
        errors = self.errors()
        self.assertTrue(any("credential" in error for error in errors))
        self.assertNotIn("not-a-real-password", "\n".join(errors))

    def test_unknown_category_and_difficulty(self):
        self.manifest["tasks"][0]["categories"] = ["feature", "vibes"]
        self.manifest["tasks"][0]["difficulty"] = "trivial"
        errors = self.errors()
        self.assertTrue(any("categories" in error for error in errors))
        self.assertTrue(any("difficulty" in error for error in errors))

    def test_unlisted_task_file(self):
        (self.directory / "tasks" / "stray.json").write_text("{}", encoding="utf-8")
        self.assertIn(
            "tasks/stray.json: task file is not listed in the dataset index",
            self.errors(),
        )

    def test_kind_mismatch_and_locator(self):
        self.task["kind"] = "historical"
        self.task["repository"]["locator"] = "https://example.invalid/repo.git"
        errors = self.errors()
        self.assertTrue(any("kind differs" in error for error in errors))
        self.assertTrue(any("locator" in error for error in errors))

    def test_load_manifest_raises_on_errors(self):
        self.task["hidden_checks"] = []
        path = self.directory / "tasks" / f"{self.task_id}.json"
        path.write_text(json.dumps(self.task), encoding="utf-8")
        (self.directory / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8"
        )
        with self.assertRaises(SeedDatasetError):
            load_manifest(self.directory / "manifest.json")


def _task_document(dataset, task_id, reference):
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "kind": "spec",
        "repository": {
            "locator": f"paw-dataset://{dataset}/{task_id}",
            "starting_commit": "1" * 40,
        },
        "issue_text": "Add a feature.",
        "visible_checks": [],
        "hidden_checks": [{"id": "h", "type": "acceptance", "reference_id": reference}],
    }


def _entry(task_id):
    return {
        "task_id": task_id,
        "file": f"tasks/{task_id}.json",
        "kind": "spec",
        "difficulty": "medium",
        "categories": ["feature"],
        "source": {},
    }


class EarlierVersionTasksTest(unittest.TestCase):
    """A later version lists tasks of an earlier version unchanged (Decision 0041 2)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.old_id = "paw-seed-v1-spec-01-old"
        self.new_id = "paw-seed-v2-spec-01-new"
        self.v1 = self.root / "paw-seed-v1"
        self.v2 = self.root / "paw-seed-v2"
        for directory in (self.v1, self.v2):
            (directory / "tasks").mkdir(parents=True)
        self.write(
            self.v1 / "tasks" / f"{self.old_id}.json",
            _task_document("paw-seed-v1", self.old_id, "seed-v1-0123456789ab"),
        )
        self.write(
            self.v1 / "manifest.json",
            {
                "dataset": "paw-seed-v1",
                "base_commit": "1" * 40,
                "tasks": [_entry(self.old_id)],
            },
        )
        self.new_task = _task_document(
            "paw-seed-v2", self.new_id, "seed-v2-0123456789ab"
        )
        self.manifest = {
            "dataset": "paw-seed-v2",
            "base_commit": "2" * 40,
            "tasks": [
                {**_entry(self.old_id), "dataset": "paw-seed-v1"},
                _entry(self.new_id),
            ],
        }

    def write(self, path, document):
        path.write_text(json.dumps(document), encoding="utf-8")

    def errors(self):
        self.write(self.v2 / "tasks" / f"{self.new_id}.json", self.new_task)
        return check_manifest(self.manifest, self.v2)

    def test_valid_and_loaded_from_the_owning_version(self):
        self.assertEqual(self.errors(), [])
        self.write(self.v2 / "manifest.json", self.manifest)
        _, entries = load_manifest(self.v2 / "manifest.json")
        self.assertEqual(
            [(entry.task_id, entry.dataset) for entry in entries],
            [(self.old_id, "paw-seed-v1"), (self.new_id, "paw-seed-v2")],
        )
        self.assertEqual(entries[0].path, self.v1 / "tasks" / f"{self.old_id}.json")
        self.assertEqual(summary_total(entries), 2)

    def test_an_earlier_task_must_be_listed_unchanged(self):
        self.manifest["tasks"][0]["difficulty"] = "hard"
        self.assertIn(
            "$.tasks[0]: differs from the entry of the earlier version", self.errors()
        )

    def test_an_unknown_earlier_task_is_refused(self):
        self.manifest["tasks"][0] = {
            **_entry("paw-seed-v1-spec-02-missing"),
            "dataset": "paw-seed-v1",
        }
        self.assertIn(
            "$.tasks[0]: differs from the entry of the earlier version", self.errors()
        )

    def test_only_earlier_versions_can_be_listed(self):
        for dataset in ("paw-seed-v2", "paw-seed-v3", "other"):
            with self.subTest(dataset=dataset):
                self.manifest["tasks"][0]["dataset"] = dataset
                errors = self.errors()
                if dataset == "paw-seed-v2":
                    # Listed as its own task: the v1 name does not fit the v2 rule.
                    self.assertIn(
                        "$.tasks[0].task_id: does not follow the naming rule", errors
                    )
                else:
                    self.assertIn(
                        "$.tasks[0].dataset: must name an earlier version", errors
                    )

    def test_own_tasks_follow_the_version_naming(self):
        self.new_task["hidden_checks"][0]["reference_id"] = "seed-v1-0123456789ab"
        self.new_task["repository"]["locator"] = "paw-dataset://paw-seed-v1/x"
        errors = self.errors()
        self.assertIn(
            f"tasks/{self.new_id}.json: hidden reference_id must be opaque", errors
        )
        self.assertTrue(any("locator must be" in error for error in errors))

    def test_dataset_name_is_checked(self):
        self.manifest["dataset"] = "seed-two"
        self.assertEqual(self.errors(), ["$.dataset: must be named paw-seed-v<N>"])

    def test_files_of_the_earlier_version_are_not_unlisted_files_here(self):
        self.write(self.v2 / "tasks" / "stray.json", {})
        self.assertEqual(
            self.errors(),
            ["tasks/stray.json: task file is not listed in the dataset index"],
        )


def summary_total(entries):
    return dataset_summary(entries)["total"]


class HiddenCommandTest(unittest.TestCase):
    def test_unittest_command_uses_the_evaluator_helper(self):
        private = Path("/private/root")
        command = hidden_command(
            {
                "mode": "unittest",
                "overlay": "overlays/x",
                "workdir": "apps/backend",
                "postgres": True,
                "tests": ["tests.test_a"],
            },
            private,
            "example/postgres:tag",
        )
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(Path(command[1]), seed_dataset.SEED_CHECK)
        self.assertIn("--overlay", command)
        self.assertIn(str(private / "overlays/x"), command)
        index = command.index("--postgres-image")
        self.assertEqual(command[index + 1], "example/postgres:tag")
        self.assertEqual(command[-3:], ("--expect", "pass", "tests.test_a"))

    def test_postgres_check_needs_an_image(self):
        with self.assertRaises(SeedDatasetError):
            hidden_command(
                {"mode": "unittest", "postgres": True, "tests": ["t"]}, Path("/p"), None
            )

    def test_forbidden_changes_command(self):
        command = hidden_command(
            {"mode": "forbidden-changes", "base": "abc", "allow": ["a/", "b.py"]},
            Path("/p"),
            None,
        )
        self.assertEqual(
            command[2:],
            ("forbidden-changes", "--base", "abc", "--allow", "a/", "--allow", "b.py"),
        )


class SnapshotCommitTest(unittest.TestCase):
    def test_snapshot_is_parentless_and_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            git(repo, "init", "-q")
            (repo / "a.py").write_text("x = 1\n")
            git(repo, "add", "a.py")
            git(repo, "commit", "-q", "-m", "base")
            base = git(repo, "rev-parse", "HEAD").strip()
            (repo / "a.py").write_text("x = 2\n")
            patch = Path(directory) / "bug.patch"
            patch.write_text(git(repo, "diff"))
            git(repo, "checkout", "-q", "a.py")
            first = snapshot_commit(repo, base, patch, "paw-seed-v1-bug-01-x")
            second = snapshot_commit(repo, base, patch, "paw-seed-v1-bug-01-x")
            self.assertEqual(first, second)
            self.assertEqual(
                git(repo, "rev-list", "--parents", "-n1", first).split(), [first]
            )
            self.assertEqual(git(repo, "show", f"{first}:a.py"), "x = 2\n")
            self.assertEqual((repo / "a.py").read_text(), "x = 1\n")
            later = snapshot_commit(
                repo,
                base,
                patch,
                "paw-seed-v2-bug-01-x",
                "paw-seed-v2",
                "2026-10-05T00:00:00+00:00",
            )
            self.assertNotEqual(later, first)
            self.assertEqual(
                git(repo, "log", "-1", "--format=%an %at %s", later).strip(),
                "paw-seed-v2 1791158400 "  # 2026-10-05T00:00:00+00:00
                "paw-seed-v2 starting state of paw-seed-v2-bug-01-x",
            )


class SeedCheckTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.tree = self.directory / "tree"
        (self.tree / "pkg").mkdir(parents=True)
        (self.tree / "pkg" / "__init__.py").write_text("")
        (self.tree / "pkg" / "code.py").write_text("def value():\n    return 1\n")
        self.overlay = self.directory / "overlay"
        (self.overlay / "pkg").mkdir(parents=True)

    def write_test(self, body):
        (self.overlay / "pkg" / "test_hidden.py").write_text(
            "import unittest\nfrom pkg.code import value\n\n"
            "class T(unittest.TestCase):\n" + body
        )

    def run_check(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = seed_check.main(
                [
                    "unittest",
                    "--worktree",
                    str(self.tree),
                    "--overlay",
                    str(self.overlay),
                    *extra,
                    "pkg.test_hidden",
                ]
            )
        return code, out.getvalue()

    def test_passing_hidden_test(self):
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 1)\n"
        )
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertFalse((self.tree / "pkg" / "test_hidden.py").exists())

    def test_failing_hidden_test(self):
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 2)\n"
        )
        self.assertEqual(self.run_check()[0], 1)

    def test_expect_fail_inverts_the_verdict(self):
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 2)\n"
        )
        self.assertEqual(self.run_check("--expect", "fail")[0], 0)

    def test_skipped_tests_fail_unless_allowed(self):
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 1)\n"
            "    @unittest.skip('needs a database')\n"
            "    def test_skipped(self):\n        pass\n"
        )
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("skipped", output)
        self.assertEqual(self.run_check("--allow-skips")[0], 0)

    def test_no_test_is_a_failure(self):
        self.write_test("    pass\n")
        self.assertEqual(self.run_check()[0], 1)

    def test_import_error_is_a_failure(self):
        (self.overlay / "pkg" / "test_hidden.py").write_text(
            "from pkg.code import missing\n"
        )
        self.assertEqual(self.run_check()[0], 1)

    def test_overlay_replaces_candidate_files(self):
        # A candidate cannot keep a weakened copy of a hidden test.
        (self.tree / "pkg" / "test_hidden.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n"
            "    def test_value(self):\n        pass\n"
        )
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 2)\n"
        )
        self.assertEqual(self.run_check()[0], 1)

    def test_expect_fail_needs_an_assertion_failure(self):
        # A crash (ERROR) against the known bug is not "the test detects the bug".
        self.write_test(
            "    def test_value(self):\n        raise RuntimeError('crash')\n"
        )
        code, output = self.run_check("--expect", "fail")
        self.assertEqual(code, 1, output)
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 2)\n"
            "    def test_crash(self):\n        raise RuntimeError('crash')\n"
        )
        self.assertEqual(self.run_check("--expect", "fail")[0], 1)

    def test_expect_fail_refuses_a_skipped_test(self):
        # Skipping the test against the known bug is not "detecting the bug".
        self.write_test(
            "    @unittest.skipIf(value() == 1, 'hide the bug')\n"
            "    def test_value(self):\n        self.assertEqual(value(), 2)\n"
            "    def test_other(self):\n        self.assertEqual(value(), 2)\n"
        )
        code, output = self.run_check("--expect", "fail")
        self.assertEqual(code, 1, output)
        self.assertIn("skips are not allowed", output)

    def test_overlay_does_not_follow_a_symlinked_parent(self):
        # A candidate symlink in place of an overlay directory must not carry the
        # hidden files outside the private copy.
        outside = self.directory / "outside"
        outside.mkdir()
        shutil.rmtree(self.tree / "pkg")
        (self.tree / "pkg").symlink_to(outside, target_is_directory=True)
        (outside / "__init__.py").write_text("")
        (outside / "code.py").write_text("def value():\n    return 1\n")
        (self.overlay / "pkg" / "__init__.py").write_text("")
        (self.overlay / "pkg" / "code.py").write_text("def value():\n    return 1\n")
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 1)\n"
        )
        code, output = self.run_check()
        self.assertEqual(code, 0, output)
        self.assertFalse((outside / "test_hidden.py").exists())
        self.assertEqual(
            sorted(p.name for p in outside.iterdir()), ["__init__.py", "code.py"]
        )

    def test_real_directory_replaces_links_and_files(self):
        root = self.directory / "copy"
        (root / "a").mkdir(parents=True)
        (root / "a" / "b").write_text("file in the way")
        (root / "c").symlink_to(self.directory, target_is_directory=True)
        self.assertTrue(seed_check._real_directory(root, Path("a/b/x")).is_dir())
        made = seed_check._real_directory(root, Path("c/d"))
        self.assertFalse((root / "c").is_symlink())
        self.assertEqual(made, root / "c" / "d")
        self.assertFalse((self.directory / "d").exists())

    def test_large_output_is_bounded_and_still_judged(self):
        self.write_test(
            "    def test_value(self):\n"
            "        import sys\n"
            "        for _ in range(40):\n"
            "            sys.stderr.write('x' * 100_000 + '\\n')\n"
            "        self.assertEqual(value(), 1)\n"
        )
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertLessEqual(len(output.encode()), seed_check._MAX_ECHO + 4096)

    def test_run_bounded_keeps_only_the_tail(self):
        script = "import sys\nfor i in range(30):\n    print(str(i) * 100_000)\nprint('END')\n"
        code, output = seed_check._run_bounded(
            [sys.executable, "-c", script], self.directory, dict(os.environ)
        )
        self.assertEqual(code, 0)
        self.assertLessEqual(len(output), seed_check._MAX_CAPTURE)
        self.assertTrue(output.endswith(b"END\n"))
        self.assertFalse(output.startswith(b"0"))

    def test_teardown_failure_fails_the_check(self):
        self.write_test(
            "    def test_value(self):\n        self.assertEqual(value(), 1)\n"
        )
        with (
            unittest.mock.patch.object(
                seed_check,
                "_start_cluster",
                return_value=seed_check._Cluster(
                    "paw-seed-x", "postgresql://postgres:secret@127.0.0.1:1/postgres"
                ),
            ),
            unittest.mock.patch.object(
                seed_check, "_docker", return_value=unittest.mock.Mock(returncode=1)
            ),
        ):
            code, output = self.run_check("--postgres-image", "example/pg")
        self.assertEqual(code, 1, output)
        self.assertIn("could not remove the PostgreSQL container", output)
        self.assertNotIn("secret", output)

    def test_each_check_gets_its_own_container_without_the_password_in_argv(self):
        calls = []

        def docker(*arguments, timeout=120.0):
            calls.append(arguments)
            if arguments[0] == "run":
                env_file = Path(arguments[arguments.index("--env-file") + 1])
                self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                calls.append(("env", env_file.read_text()))
            stdout = b"127.0.0.1:54321\n" if arguments[0] == "port" else b""
            return unittest.mock.Mock(returncode=0, stdout=stdout)

        with (
            unittest.mock.patch.object(seed_check, "_docker", side_effect=docker),
            unittest.mock.patch.object(seed_check, "_wait_until_ready"),
        ):
            first = seed_check._start_cluster("example/pg")
            second = seed_check._start_cluster("example/pg")
            self.assertTrue(seed_check._stop_cluster(first))
        self.assertNotEqual(first.container, second.container)
        self.assertNotEqual(first.test_url, second.test_url)
        password = first.test_url.split(":")[2].split("@")[0]
        runs = [c for c in calls if c[0] == "run"]
        self.assertEqual(len(runs), 2)
        for run in runs:
            self.assertNotIn(password, " ".join(run))
            self.assertIn("127.0.0.1::5432", run)
            self.assertIn("--rm", run)
        self.assertIn(("env", f"POSTGRES_PASSWORD={password}\n"), calls)
        self.assertIn(("rm", "--force", "--volumes", first.container), calls)
        self.assertTrue(first.test_url.startswith("postgresql://postgres:"))
        self.assertTrue(first.test_url.endswith("@127.0.0.1:54321/postgres"))

    def test_a_container_that_fails_to_start_is_removed(self):
        for failure in ("timeout", "returncode", "not-ready"):
            with self.subTest(failure=failure):
                calls = []

                def docker(*arguments, timeout=120.0, failure=failure, calls=calls):
                    calls.append(arguments)
                    if arguments[0] == "run" and failure == "timeout":
                        raise subprocess.TimeoutExpired("docker", timeout)
                    code = 1 if arguments[0] == "run" and failure == "returncode" else 0
                    stdout = b"127.0.0.1:54321\n" if arguments[0] == "port" else b""
                    return unittest.mock.Mock(returncode=code, stdout=stdout)

                ready = unittest.mock.Mock(side_effect=RuntimeError("not ready"))
                with (
                    unittest.mock.patch.object(
                        seed_check, "_docker", side_effect=docker
                    ),
                    unittest.mock.patch.object(seed_check, "_wait_until_ready", ready),
                    self.assertRaises((subprocess.TimeoutExpired, RuntimeError)),
                ):
                    seed_check._start_cluster("example/pg")
                name = calls[0][calls[0].index("--name") + 1]
                self.assertEqual(calls[-1], ("rm", "--force", "--volumes", name))


@unittest.skipUnless(
    os.environ.get("PAW_TEST_POSTGRES_IMAGE"), "PAW_TEST_POSTGRES_IMAGE is not set"
)
class ThrowawayClusterTest(unittest.TestCase):
    """What the tests of one check do to PostgreSQL does not outlive the check."""

    def test_changes_to_the_cluster_leave_with_its_container(self):
        import psycopg

        image = os.environ["PAW_TEST_POSTGRES_IMAGE"]
        cluster = seed_check._start_cluster(image)
        stopped = False
        try:
            with psycopg.connect(cluster.test_url, autocommit=True) as connection:
                connection.execute("CREATE ROLE paw_seed_leftover LOGIN SUPERUSER")
                connection.execute("CREATE DATABASE paw_seed_leftover")
            stopped = seed_check._stop_cluster(cluster)
            self.assertTrue(stopped)
            self.assertNotEqual(
                seed_check._docker("inspect", cluster.container).returncode, 0
            )
            fresh = seed_check._start_cluster(image)
            try:
                with psycopg.connect(fresh.test_url, autocommit=True) as connection:
                    for query in (
                        "SELECT count(*) FROM pg_roles WHERE rolname = 'paw_seed_leftover'",
                        "SELECT count(*) FROM pg_database WHERE datname = 'paw_seed_leftover'",
                    ):
                        self.assertEqual(connection.execute(query).fetchone()[0], 0)
            finally:
                seed_check._stop_cluster(fresh)
        finally:
            if not stopped:
                seed_check._stop_cluster(cluster)


class ForbiddenChangesTest(unittest.TestCase):
    def test_allowances_match_whole_paths_or_directories(self):
        self.assertTrue(seed_check._allowed("tests/test_a.py", ["tests/test_a.py"]))
        self.assertFalse(seed_check._allowed("tests/test_a.pyx", ["tests/test_a.py"]))
        self.assertTrue(seed_check._allowed("tests/x/y.py", ["tests/"]))
        self.assertFalse(seed_check._allowed("tests2/y.py", ["tests/"]))

    def test_changes_outside_the_allowed_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-q")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_a.py").write_text("a\n")
            (repo / "code.py").write_text("c\n")
            git(repo, "add", ".")
            git(repo, "commit", "-q", "-m", "base")
            base = git(repo, "rev-parse", "HEAD").strip()

            def run():
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    return seed_check.main(
                        [
                            "forbidden-changes",
                            "--worktree",
                            str(repo),
                            "--base",
                            base,
                            "--allow",
                            "tests/test_a.py",
                        ]
                    )

            self.assertEqual(run(), 0)
            (repo / "tests" / "test_a.py").write_text("b\n")
            self.assertEqual(run(), 0)
            (repo / "new.py").write_text("n\n")
            self.assertEqual(run(), 1)
            (repo / "new.py").unlink()
            (repo / "code.py").write_text("changed\n")
            git(repo, "commit", "-q", "-am", "candidate commit")
            self.assertEqual(run(), 1)
            git(repo, "reset", "-q", "--hard", base)
            self.assertEqual(run(), 0)
            # Index flags set by the candidate must not hide a change.
            git(repo, "update-index", "--assume-unchanged", "code.py")
            (repo / "code.py").write_text("hidden\n")
            self.assertEqual(run(), 1)
            git(repo, "update-index", "--no-assume-unchanged", "code.py")
            git(repo, "checkout", "-q", "--", "code.py")
            git(repo, "update-index", "--skip-worktree", "code.py")
            (repo / "code.py").write_text("hidden\n")
            self.assertEqual(run(), 1)
            git(repo, "update-index", "--no-skip-worktree", "code.py")
            git(repo, "checkout", "-q", "--", "code.py")
            # Nor may ignore rules the candidate writes into .git/info/exclude.
            (repo / ".git" / "info").mkdir(exist_ok=True)
            (repo / ".git" / "info" / "exclude").write_text("sneaky.py\n")
            (repo / "sneaky.py").write_text("s\n")
            self.assertEqual(run(), 1)
            (repo / "sneaky.py").unlink()
            # A replace ref must not swap the base commit for the candidate's commit.
            (repo / "code.py").write_text("replaced\n")
            git(repo, "commit", "-q", "-am", "replacement")
            git(repo, "replace", base, "HEAD")
            self.assertEqual(run(), 1)
            git(repo, "replace", "-d", base)
            git(repo, "reset", "-q", "--hard", base)
            # A linked worktree (whose .git is a file) is compared the same way.
            elsewhere = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, elsewhere, True)
            linked = elsewhere / "linked"
            git(repo, "worktree", "add", "-q", "--detach", str(linked), base)
            with contextlib.redirect_stdout(io.StringIO()):
                code = seed_check.main(
                    ["forbidden-changes", "--worktree", str(linked), "--base", base]
                )
            self.assertEqual(code, 0)
            # A mode change (the executable bit) is a change too.
            (repo / "code.py").chmod(0o755)
            self.assertEqual(run(), 1)
            (repo / "code.py").chmod(0o644)
            self.assertEqual(run(), 0)
            # Git takes the executable bit from the owner bit only: group / other
            # execute bits are no change, and 0o100 alone is one.
            (repo / "code.py").chmod(0o655)
            self.assertEqual(run(), 0)
            (repo / "code.py").chmod(0o700)
            self.assertEqual(run(), 1)
            (repo / "code.py").chmod(0o644)
            # An allowed file does not allow its siblings with a longer name.
            (repo / "tests" / "test_a.py.backup").write_text("x\n")
            self.assertEqual(run(), 1)
            (repo / "tests" / "test_a.py.backup").unlink()
            # Evaluator caches are not changes.
            (repo / "__pycache__").mkdir()
            (repo / "__pycache__" / "code.cpython-313.pyc").write_bytes(b"x")
            self.assertEqual(run(), 0)
            # Selectors of a calling Git process (a hook) must not redirect the check.
            other = Path(directory) / "other"
            other.mkdir()
            git(other, "init", "-q")
            with unittest.mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}):
                self.assertEqual(run(), 1)


if __name__ == "__main__":
    unittest.main()
