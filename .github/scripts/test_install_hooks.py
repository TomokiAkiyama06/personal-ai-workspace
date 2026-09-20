"""Protect existing hook setups and keep failed first-time installs retryable."""

from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_hooks


RUN = subprocess.run


class InstallHooksTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        # Git hooks export repository-local GIT_* variables. Never let them
        # redirect fixture commands back into the developer's repository.
        self.git_env = {key: value for key, value in os.environ.items()
                        if not key.startswith("GIT_")}
        self.git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        self.git("init", "-q")
        self.hooks = self.root / ".git/hooks"
        self.commands = []

    def git(self, *arguments):
        return RUN(["git", *arguments], cwd=self.root, env=self.git_env,
                   check=True, capture_output=True)

    def install(self, prepare_exit=0, during_prepare=None):
        def run(command, **kwargs):
            if command[:3] != [sys.executable, "-m", "pre_commit"]:
                return RUN(command, env=self.git_env, **kwargs)
            operation = command[3:]
            self.commands.append(operation)
            if operation == ["install-hooks"]:
                if during_prepare:
                    during_prepare()
                return subprocess.CompletedProcess(command, prepare_exit)
            self.assertEqual(operation, ["install", "--hook-type", "pre-commit"])
            (self.hooks / "pre-commit").write_text("installed hook\n")
            return subprocess.CompletedProcess(command, 0)

        with patch.object(install_hooks.subprocess, "run", side_effect=run):
            with redirect_stderr(io.StringIO()):
                return install_hooks.install(self.root)

    def test_dependency_failure_leaves_no_hook_and_retry_succeeds(self):
        self.assertEqual(self.install(prepare_exit=7), 7)
        self.assertFalse((self.hooks / "pre-commit").exists())
        self.assertEqual(self.commands, [["install-hooks"]])
        self.commands.clear()
        self.assertEqual(self.install(), 0)
        self.assertEqual(self.commands, [
            ["install-hooks"], ["install", "--hook-type", "pre-commit"],
        ])
        self.assertEqual((self.hooks / "pre-commit").read_text(), "installed hook\n")

    def test_existing_hooks_and_dangling_symlinks_are_preserved(self):
        for name in ("pre-commit", "pre-commit.legacy"):
            for symlink in (False, True):
                with self.subTest(name=name, symlink=symlink):
                    hook = self.hooks / name
                    if symlink:
                        hook.symlink_to("missing-original-hook")
                    else:
                        hook.write_text("existing hook\n")
                    self.assertEqual(self.install(), 1)
                    self.assertEqual(self.commands, [])
                    if symlink:
                        self.assertEqual(hook.readlink(), Path("missing-original-hook"))
                    else:
                        self.assertEqual(hook.read_text(), "existing hook\n")
                    hook.unlink()

    def test_configured_hooks_path_is_preserved(self):
        for value in ("custom-hooks", ""):
            with self.subTest(value=value):
                self.git("config", "core.hooksPath", value)
                self.assertEqual(self.install(), 1)
                self.assertEqual(self.commands, [])
                self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout,
                                 (value + "\n").encode())
                self.git("config", "--unset", "core.hooksPath")

    def test_hook_added_during_dependency_setup_is_preserved(self):
        hook = self.hooks / "pre-commit"
        result = self.install(during_prepare=lambda: hook.write_text("new existing hook\n"))
        self.assertEqual(result, 1)
        self.assertEqual(self.commands, [["install-hooks"]])
        self.assertEqual(hook.read_text(), "new existing hook\n")

    def test_hooks_path_added_during_dependency_setup_is_preserved(self):
        result = self.install(during_prepare=lambda: self.git("config", "core.hooksPath", "new-hooks"))
        self.assertEqual(result, 1)
        self.assertEqual(self.commands, [["install-hooks"]])
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout, b"new-hooks\n")
        self.assertFalse((self.hooks / "pre-commit").exists())


if __name__ == "__main__":
    unittest.main()
