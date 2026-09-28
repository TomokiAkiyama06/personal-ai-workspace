"""The web checks of `run_ci.py`: required in CI, skipped with a warning locally."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

import run_ci


class WebCommandsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "apps/web").mkdir(parents=True)
        (self.root / "apps/web/package.json").write_text("{}")

    def commands(self, environ, npm):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = run_ci.web_commands(self.root, environ, lambda name: npm)
        return result, stderr.getvalue()

    def test_installs_from_the_lockfile_then_runs_the_web_ci_script(self):
        result, _ = self.commands({}, "/usr/bin/npm")
        self.assertEqual(
            result,
            [["/usr/bin/npm", "ci", "--no-audit", "--no-fund"], ["/usr/bin/npm", "run", "ci"]],
        )

    def test_a_developer_without_node_gets_a_warning_and_the_other_checks(self):
        result, stderr = self.commands({}, None)
        self.assertEqual(result, [])
        self.assertIn("SKIPPED", stderr)

    def test_github_actions_cannot_skip_them(self):
        result, stderr = self.commands({"GITHUB_ACTIONS": "true"}, None)
        self.assertIsNone(result)
        self.assertIn("required", stderr)

    def test_the_environment_variable_makes_them_required_locally(self):
        result, _ = self.commands({run_ci.REQUIRE_WEB_ENV: "1"}, None)
        self.assertIsNone(result)

    def test_nothing_to_run_without_the_web_app(self):
        (self.root / "apps/web/package.json").unlink()
        result, stderr = self.commands({"GITHUB_ACTIONS": "true"}, None)
        self.assertEqual(result, [])
        self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
