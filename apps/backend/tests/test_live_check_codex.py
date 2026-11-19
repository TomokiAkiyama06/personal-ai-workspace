"""The live check of a Codex pair (Decision 0104, 3 A; issue #341), run with a
fake ``codex`` (``fake_codex_cli.py``): nothing is sent anywhere. The check
itself sends to OpenAI and is a Human's step
(``deploy/cli-session/live_check_codex.py``)."""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from paw_backend.agent_models import verified
from paw_backend.agents import cli_wiring
from paw_backend.agents.claude_probe import PROBE_PROMPT, PROBE_SYSTEM_PROMPT
from paw_backend.connections import ConnectionKind

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_codex_cli.py"
SCRIPT = HERE.parent / "deploy" / "cli-session" / "live_check_codex.py"
VERSION = "0.156.1"
# Decision 0105: the catalog model (the logged-in catalog has no gpt-5.5).
MODEL = "gpt-6-sol"
# A login built from pieces (no literal a scanner would flag).
LOGIN = '{"tokens": {"access_token": "paw-live-check-' + "c4na" + 'ry"}}'
REFRESHED = '{"tokens": {"access_token": "paw-live-check-' + "r3fr" + 'esh"}}'
PROBE_OK = [
    {"answer": "PAW_PROBE_OK."},
    {"usage": [120, 4]},
    {"complete": "completed"},
]
REVIEW = "Off by one: page 1 now starts at index size (pager.py line 6)."
NODE_OK = [
    {"usage": [100, 10]},
    {
        "call": "submit",
        "arguments": {"summary": REVIEW, "discovered_facts": ["start is wrong"]},
    },
    {"sleep": 30},
]


def load():
    spec = importlib.util.spec_from_file_location("live_check_codex", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveCheckCase(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.base = Path(tempfile.mkdtemp(prefix="pawlct"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.report = self.base / "report.json"
        self.scenario = self.base / "scenario.json"
        self.sessions = self.base / "s"
        # The dedicated login's directory (``CODEX_HOME=<dir> codex login``).
        self.dedicated = self.base / "dedicated"
        self.dedicated.mkdir(mode=0o700)
        self.auth = self.dedicated / "auth.json"
        self.auth.write_text(LOGIN)
        os.chmod(self.auth, 0o600)

    def arguments(self, **changes):
        values = {
            "codex": "codex",
            "cli_version": VERSION,
            "model": MODEL,
            "auth": self.auth,
            "out": self.base / "out.json",
            "sessions": self.sessions,
            "node_seconds": 120.0,
        }
        values.update(changes)
        return argparse.Namespace(**values)

    def check(self, *, node=NODE_OK, probe=PROBE_OK, version=VERSION, **run):
        self.scenario.write_text(
            json.dumps({"version": version, "actions": node, "probe_actions": probe})
        )
        executable = (
            sys.executable,
            "-I",
            str(FAKE),
            "--scenario",
            str(self.scenario),
            "--report",
            str(self.report),
        )
        arguments = run.pop("arguments", None) or self.arguments()
        run.setdefault("users", {"developer"})
        run.setdefault("euid", 1000)
        run.setdefault("environ", {})
        return self.module.run(arguments, executable=executable, **run)

    def left(self):
        """What is left in the sessions' base (the temporary directory with
        every CODEX_HOME and copy of the login is in it while it runs)."""
        return sorted(p.name for p in self.sessions.iterdir())

    def assert_clean_json(self, result):
        text = json.dumps(result)
        for secret in (LOGIN, REFRESHED, "c4na", "r3fr"):
            self.assertNotIn(secret, text)
        # No prompt text: the node's goal, the fixture, the probe's prompts.
        self.assertNotIn(self.module.GOAL, text)
        self.assertNotIn("items[start", text)
        self.assertNotIn(PROBE_PROMPT, text)
        self.assertNotIn(PROBE_SYSTEM_PROMPT, text)


class PassTest(LiveCheckCase):
    def test_both_checks_pass_and_the_widening_stays_in_the_run(self):
        before = dict(verified.VERIFIED_MODELS)
        mapping = verified.VERIFIED_MODELS
        code, result = self.check()
        self.assertEqual(code, 0, result)
        self.assertTrue(result["passed"])
        self.assertEqual(
            (result["cli_version"], result["codex_version"], result["model"]),
            (VERSION, VERSION, MODEL),
        )
        self.assertEqual(result["probe"]["outcome"], "connected")
        self.assertTrue(result["probe"]["passed"])
        node = result["node"]
        self.assertTrue(node["tool_check_passed"])
        self.assertTrue(node["submitted"])
        self.assertEqual(node["review"]["summary"], REVIEW)
        self.assertEqual(node["review"]["discovered_facts"], ["start is wrong"])
        self.assertEqual(node["session"]["mcp_servers"], ["paw"])
        self.assertEqual(node["placement_recorded"], ["cloud", "codex", MODEL])
        self.assertEqual(
            result["fixture"]["path"], "deploy/cli-session/live_check_codex_review.diff"
        )
        self.assertRegex(result["fixture"]["sha256"], "^[0-9a-f]{64}$")
        self.assertTrue(result["started_at"] and result["finished_at"])
        self.assertFalse(result["credential_refreshed"])
        # The login was placed in the session's CODEX_HOME ...
        sent = json.loads(self.report.read_text())
        self.assertEqual(sent["auth_length"], len(LOGIN))
        self.assertEqual(sent["requests"]["thread/start"]["model"], MODEL)
        # The backend's own catalog override (Decision 0104, 2 A): the
        # session's model_catalog_json in its CODEX_HOME, and the result says
        # which repository file and which values.
        self.assertEqual(sent["config"]["model_catalog_json"], sent["catalog"]["path"])
        self.assertEqual(Path(sent["catalog"]["path"]).name, "models.json")
        expected = {
            "tool_mode": "direct",
            "multi_agent_version": None,
            "experimental_supported_tools": [],
            "use_responses_lite": False,
            "supports_search_tool": False,
        }
        entry = dict(sent["catalog"]["entries"][MODEL])
        entry.pop("base_instructions")
        self.assertEqual(entry, expected)
        self.assertEqual(
            result["catalog"]["file"],
            "paw_backend/agents/codex_catalogs/codex-models-0.156.1.json",
        )
        self.assertRegex(result["catalog"]["sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(result["catalog"]["entry"], expected)
        # ... and every copy of it went with the temporary directory.
        self.assertTrue(result["cleaned"])
        self.assertEqual(self.left(), [])
        self.assert_clean_json(result)
        # The repository's pairs and production's wiring are as they were.
        self.assertIs(verified.VERIFIED_MODELS, mapping)
        self.assertEqual(dict(verified.VERIFIED_MODELS), before)
        self.assertEqual(verified.verified_pairs(ConnectionKind.CODEX), frozenset())
        self.assertEqual(cli_wiring.verified_pairs(ConnectionKind.CODEX), frozenset())
        self.assertNotIn(MODEL, cli_wiring.verified_models(ConnectionKind.CODEX))
        self.assertFalse(verified.is_verified(ConnectionKind.CODEX, VERSION, MODEL))
        # The given file is untouched.
        self.assertEqual(self.auth.read_text(), LOGIN)

    def test_an_unrefreshed_login_is_not_written(self):
        before = os.stat(self.auth)
        code, result = self.check()
        self.assertEqual(code, 0, result)
        self.assertEqual(
            (result["credential_refreshed"], result["credential_written_back"]),
            (False, False),
        )
        after = os.stat(self.auth)
        self.assertEqual(
            (after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns)
        )
        self.assertEqual(self.auth.read_text(), LOGIN)


class WriteBackTest(LiveCheckCase):
    def assert_written_back(self, result, before):
        self.assertTrue(result["credential_refreshed"])
        self.assertTrue(result["credential_written_back"])
        self.assertNotIn("credential_write_back_error", result)
        self.assertEqual(self.auth.read_text(), REFRESHED)
        after = os.stat(self.auth)
        # Renamed over (a new file), with the mode and owner kept.
        self.assertNotEqual(after.st_ino, before.st_ino)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
        # Nothing left aside next to it.
        self.assertEqual(
            sorted(p.name for p in self.dedicated.iterdir()), ["auth.json"]
        )
        self.assertEqual(self.left(), [])
        self.assert_clean_json(result)

    def test_a_refreshed_login_is_written_back_and_carried_on(self):
        before = os.stat(self.auth)
        node = [{"refresh": REFRESHED}, *NODE_OK]
        code, result = self.check(node=node)
        self.assertEqual(code, 0, result)
        self.assert_written_back(result, before)

    def test_the_probes_refresh_reaches_the_node_and_the_file(self):
        before = os.stat(self.auth)
        code, result = self.check(probe=[{"refresh": REFRESHED}, *PROBE_OK])
        self.assertEqual(code, 0, result)
        self.assert_written_back(result, before)
        # The node's session was given the refreshed login.
        sent = json.loads(self.report.read_text())
        self.assertEqual(sent["auth_length"], len(REFRESHED))

    def test_it_is_written_back_when_a_later_check_fails(self):
        before = os.stat(self.auth)
        code, result = self.check(
            probe=[{"refresh": REFRESHED}, *PROBE_OK],
            node=[{"item": {"type": "commandExecution"}}],
        )
        self.assertEqual(code, 1)
        self.assertFalse(result["passed"])
        self.assert_written_back(result, before)

    def test_it_is_written_back_after_an_exception(self):
        before = os.stat(self.auth)

        def broken(*args, **kwargs):
            raise RuntimeError("boom")

        self.module.run_node = broken
        code, result = self.check(probe=[{"refresh": REFRESHED}, *PROBE_OK])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "RuntimeError")
        self.assert_written_back(result, before)

    def test_a_failed_write_back_is_tried_again_and_fails_the_run(self):
        calls = []

        def refuse(path, value):
            calls.append(path)
            raise PermissionError(value)

        self.module.write_file_back = refuse
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code, result = self.check(node=[{"refresh": REFRESHED}, *NODE_OK])
        self.assertEqual(code, 3)
        self.assertFalse(result["passed"])
        self.assertTrue(result["credential_refreshed"])
        self.assertFalse(result["credential_written_back"])
        self.assertEqual(result["credential_write_back_error"], "PermissionError")
        self.assertEqual(len(calls), 2)  # at once, and again before the cleanup
        self.assertIn("may now be stale", stderr.getvalue())
        self.assertIn("PermissionError", stderr.getvalue())
        self.assertNotIn("r3fr", stderr.getvalue())
        self.assertEqual(self.auth.read_text(), LOGIN)
        self.assertEqual(self.left(), [])
        self.assert_clean_json(result)

    def test_write_file_back_keeps_another_mode(self):
        os.chmod(self.auth, 0o640)
        self.module.write_file_back(self.auth, REFRESHED)
        self.assertEqual(self.auth.read_text(), REFRESHED)
        self.assertEqual(stat.S_IMODE(os.stat(self.auth).st_mode), 0o640)
        self.assertEqual(
            sorted(p.name for p in self.dedicated.iterdir()), ["auth.json"]
        )


class CodexPathTest(LiveCheckCase):
    """``--codex`` is resolved once to an absolute path: the binary whose
    version is checked is the one the sessions (in their own directory, with
    another ``PATH``) launch."""

    def wrapper(self):
        """A ``codex`` that runs the fake with this test's scenario."""
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" -I "{FAKE}" --scenario "{self.scenario}"'
            f' --report "{self.report}" "$@"\n'
        )
        os.chmod(codex, 0o755)
        return codex

    def run_with(self, codex, environ):
        self.scenario.write_text(
            json.dumps(
                {"version": VERSION, "actions": NODE_OK, "probe_actions": PROBE_OK}
            )
        )
        return self.module.run(
            self.arguments(codex=codex),
            users={"developer"},
            euid=1000,
            environ=environ,
        )

    def test_a_relative_path_is_resolved_against_the_invoking_directory(self):
        codex = self.wrapper()
        cwd = os.getcwd()
        os.chdir(self.base)
        self.addCleanup(os.chdir, cwd)
        code, result = self.run_with("./bin/codex", {"PATH": "/nonexistent"})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["codex_executable"], str(codex))
        self.assertEqual(result["probe"]["outcome"], "connected")
        self.assertTrue(result["node"]["passed"])

    def test_a_bare_name_is_looked_up_on_the_invoking_path(self):
        codex = self.wrapper()
        code, result = self.run_with("codex", {"PATH": str(codex.parent)})
        self.assertEqual(code, 0, result)
        self.assertEqual(result["codex_executable"], str(codex))
        self.assertTrue(result["passed"])

    def test_a_codex_that_cannot_be_resolved_is_refused(self):
        for value, environ in (
            ("codex", {"PATH": str(self.base / "empty")}),
            ("./no/such/codex", {}),
            (str(self.auth), {}),  # a file, but not executable
        ):
            with self.subTest(value=value):
                code, result = self.run_with(value, environ)
                self.assertEqual(code, 2)
                self.assertEqual(result["refused"], "codex_not_found")
                self.assertFalse(Path(str(self.report) + ".probes").exists())
                self.assertEqual(self.left() if self.sessions.exists() else [], [])


class FailTest(LiveCheckCase):
    def test_a_refused_stream_fails_and_cleans_up(self):
        code, result = self.check(node=[{"item": {"type": "commandExecution"}}])
        self.assertEqual(code, 1)
        self.assertFalse(result["passed"])
        self.assertTrue(result["probe"]["passed"])
        self.assertTrue(result["node"]["stream_refused"])
        self.assertEqual(result["node"]["refused_items"], ["commandExecution"])
        self.assertFalse(result["node"]["tool_check_passed"])
        self.assertIsNone(result["node"]["review"])
        self.assertEqual(self.left(), [])
        self.assert_clean_json(result)

    def test_without_a_submit_it_fails(self):
        code, result = self.check(node=[{"complete": "completed"}])
        self.assertEqual(code, 1)
        self.assertFalse(result["node"]["submitted"])
        self.assertEqual(self.left(), [])

    def test_a_probe_not_connected_skips_the_node(self):
        code, result = self.check(
            probe=[{"answer": "something else"}, {"complete": "completed"}]
        )
        self.assertEqual(code, 1)
        self.assertNotEqual(result["probe"]["outcome"], "connected")
        self.assertEqual(result["node"]["skipped"], "probe_not_connected")
        self.assertEqual(self.left(), [])

    def test_an_error_is_its_class_only_and_cleans_up(self):
        def broken(*args, **kwargs):
            raise RuntimeError(LOGIN)

        self.module.run_node = broken
        code, result = self.check()
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "RuntimeError")
        self.assertEqual(self.left(), [])
        self.assert_clean_json(result)

    def test_an_interruption_cleans_up(self):
        async def interrupted(*args, **kwargs):
            raise KeyboardInterrupt

        self.module.run_probe = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.check()
        self.assertEqual(self.left(), [])


class RefusalTest(LiveCheckCase):
    def assert_refused(self, code, result, why):
        self.assertEqual(code, 2)
        self.assertEqual(result["refused"], why)
        self.assertFalse(result["passed"])
        # Nothing ran: no app-server, no temporary directory left.
        self.assertFalse(self.report.exists())
        self.assertEqual(self.left() if self.sessions.exists() else [], [])
        self.assert_clean_json(result)

    def test_the_backend_user_is_refused(self):
        code, result = self.check(users={"developer", "paw"})
        self.assert_refused(code, result, "backend_user")
        self.assertFalse(Path(str(self.report) + ".probes").exists())

    def test_root_is_refused(self):
        code, result = self.check(users={"root"}, euid=0)
        self.assert_refused(code, result, "root")

    def test_no_credential_is_refused(self):
        code, result = self.check(arguments=self.arguments(auth=None))
        self.assert_refused(code, result, "no_credential")
        code, result = self.check(arguments=self.arguments(auth=self.base / "none"))
        self.assert_refused(code, result, "no_credential")

    def test_a_credential_that_is_not_a_login_is_refused(self):
        self.auth.write_text("[1, 2]")
        code, result = self.check()
        self.assert_refused(code, result, "credential_not_a_login")

    def test_the_secret_store_is_refused(self):
        store = self.base / "store"
        (store / "cred_x").mkdir(parents=True)
        inside = store / "cred_x" / "auth.json"
        inside.write_text(LOGIN)
        environ = {"PAW_SECRET_STORE_DIR": str(store)}
        code, result = self.check(
            arguments=self.arguments(auth=inside), environ=environ
        )
        self.assert_refused(code, result, "secret_store_path")
        # Through a link that points into it, too.
        link = self.base / "link.json"
        os.symlink(inside, link)
        code, result = self.check(arguments=self.arguments(auth=link), environ=environ)
        self.assert_refused(code, result, "secret_store_path")
        # The default store's path without the variable (not looked at).
        with self.assertRaises(self.module.Refused) as refused:
            self.module.check_auth_path(
                Path("/var/lib/paw/secrets/cred_x/auth.json"), {}
            )
        self.assertEqual(refused.exception.code, "secret_store_path")

    def test_missing_backend_dependencies_are_refused(self):
        self.module.BACKEND_MODULES = (
            *self.module.BACKEND_MODULES,
            "paw_backend.no_such_module_for_this_test",
        )
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code, result = self.check()
        self.assert_refused(code, result, "backend_deps_missing")
        self.assertFalse(Path(str(self.report) + ".probes").exists())
        self.assertIn(sys.executable, stderr.getvalue())
        self.assertIn("venv", stderr.getvalue())
        self.assertEqual(self.auth.read_text(), LOGIN)

    def test_the_users_own_codex_login_is_refused(self):
        home = self.base / "home"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        own = codex / "auth.json"
        own.write_text(LOGIN)
        environ = {"HOME": str(home)}
        code, result = self.check(arguments=self.arguments(auth=own), environ=environ)
        self.assert_refused(code, result, "personal_codex_login")
        # Through a link that points to it, too.
        link = self.base / "own-link.json"
        os.symlink(own, link)
        code, result = self.check(arguments=self.arguments(auth=link), environ=environ)
        self.assert_refused(code, result, "personal_codex_login")
        # The passwd entry's home counts without HOME as well.
        with self.assertRaises(self.module.Refused) as refused:
            self.module.check_auth_path(Path.home() / ".codex" / "auth.json", {})
        self.assertEqual(refused.exception.code, "personal_codex_login")

    def test_an_unverified_version_is_refused(self):
        code, result = self.check(arguments=self.arguments(cli_version="9.9.9"))
        self.assert_refused(code, result, "version_not_verified")

    def test_a_model_without_a_session_catalog_is_refused(self):
        # Decision 0104, 2 A: the same check as the adapters', before
        # anything runs (not even ``codex --version``).
        for model, why in (("gpt-5.5", "catalog_model_not_in_catalog"),):
            with self.subTest(model):
                code, result = self.check(arguments=self.arguments(model=model))
                self.assert_refused(code, result, why)
                self.assertFalse(Path(str(self.report) + ".probes").exists())

    def test_another_cli_version_is_refused_before_any_login(self):
        code, result = self.check(version="0.162.1")
        self.assert_refused(code, result, "version_mismatch")
        self.assertEqual(result["codex_version"], "0.162.1")
        # Only ``codex --version`` ran.
        self.assertTrue(Path(str(self.report) + ".probes").exists())

    def test_main_writes_the_refusal(self):
        out = self.base / "out.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = self.module.main(
                [
                    "--codex",
                    str(self.base / "no-codex"),
                    "--cli-version",
                    VERSION,
                    "--model",
                    MODEL,
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.read_text())["refused"], "no_credential")


if __name__ == "__main__":
    unittest.main()
