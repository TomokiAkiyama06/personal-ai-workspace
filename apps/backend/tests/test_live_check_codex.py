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
MODEL = "gpt-5.5"
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
        self.auth = self.base / "auth.json"
        self.auth.write_text(LOGIN)

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

    def test_a_refreshed_login_is_carried_in_memory_only(self):
        node = [{"refresh": REFRESHED}, *NODE_OK]
        code, result = self.check(node=node)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["credential_refreshed"])
        self.assertEqual(self.auth.read_text(), LOGIN)
        self.assertEqual(self.left(), [])
        self.assert_clean_json(result)


class FailTest(LiveCheckCase):
    def test_a_refused_stream_fails_and_cleans_up(self):
        code, result = self.check(node=[{"item": {"type": "commandExecution"}}])
        self.assertEqual(code, 1)
        self.assertFalse(result["passed"])
        self.assertTrue(result["probe"]["passed"])
        self.assertTrue(result["node"]["stream_refused"])
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

    def test_an_unverified_version_is_refused(self):
        code, result = self.check(arguments=self.arguments(cli_version="9.9.9"))
        self.assert_refused(code, result, "version_not_verified")

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
