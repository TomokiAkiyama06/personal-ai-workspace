"""``python -m paw_backend.cli connection-credential put`` (Decision 0087, 2):
the value comes from standard input only, the handle is the only output, the
store's directory and key are those of the deployment, and the store is
audited when a database is configured (never the value or the handle)."""

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import create_engine, text

from paw_backend.cli import credentials
from paw_backend.cli.dispatch import command_module
from paw_backend.connections import ConnectionKind, FileSecretStore

from .identity_support import sync_database_url
from .support import make_settings
from .task_support import TEST_DATABASE_URL, migrate

VALUE = "paw-store-canary-" + "41d7" + "-first-value"
KEY = bytes(range(32))


class PutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.chmod(self.root, 0o700)
        keys = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, keys, ignore_errors=True)
        (keys / "paw-secret-key").write_bytes(KEY)
        patcher = mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(keys)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, stdin: str, *args: str, **settings):
        out, err = io.StringIO(), io.StringIO()
        code = credentials.main(
            ["connection-credential", "put", *args],
            stdin=io.StringIO(stdin),
            stdout=out,
            stderr=err,
            settings=make_settings(**settings),
        )
        return code, out.getvalue(), err.getvalue()

    def test_the_value_is_stored_and_only_the_handle_is_printed(self):
        code, out, err = self.run_cli(
            VALUE + "\n", "--kind", "claude", secret_store_dir=self.root
        )
        self.assertEqual(code, credentials.EXIT_OK)
        handle = out.strip()
        self.assertRegex(handle, r"^cred_[0-9a-f]{32}$")
        self.assertNotIn(VALUE, out + err)
        store = FileSecretStore(self.root, KEY)
        import asyncio

        stored = asyncio.run(store.read(handle))
        self.assertEqual(stored.secret.reveal(), VALUE)  # the line break dropped
        self.assertIs(stored.kind, ConnectionKind.CLAUDE)

    def test_empty_or_oversized_input_is_refused(self):
        for value in ("", "\n", "x" * 20_000):
            with self.subTest(size=len(value)):
                code, out, _ = self.run_cli(
                    value, "--kind", "codex", secret_store_dir=self.root
                )
                self.assertEqual(code, credentials.EXIT_REFUSED)
                self.assertEqual(out, "")
        self.assertEqual(list(self.root.glob("cred_*")), [])

    def test_without_a_store_or_a_key_nothing_is_stored(self):
        code, _, err = self.run_cli(VALUE, "--kind", "codex")
        self.assertEqual(code, credentials.EXIT_ENVIRONMENT)
        self.assertNotIn(VALUE, err)
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": "/nonexistent"}):
            code, _, _ = self.run_cli(
                VALUE, "--kind", "codex", secret_store_dir=self.root
            )
        self.assertEqual(code, credentials.EXIT_ENVIRONMENT)
        self.assertEqual(list(self.root.glob("cred_*")), [])

    def test_the_kind_is_required_and_nothing_else_is_accepted(self):
        for args in ([], ["--kind", "gemini"], ["--kind", "codex", "--value", "x"]):
            with self.subTest(args=args):
                code, _, _ = self.run_cli(VALUE, *args, secret_store_dir=self.root)
                self.assertEqual(code, credentials.EXIT_REFUSED)

    def test_the_dispatcher_knows_the_command(self):
        self.assertIs(command_module(["connection-credential"]), credentials)


@unittest.skipUnless(TEST_DATABASE_URL, "PAW_TEST_DATABASE_URL is not set")
class AuditTest(PutTest):
    @classmethod
    def setUpClass(cls) -> None:
        migrate()

    def test_the_store_is_audited_with_the_handle_but_not_the_value(self):
        engine = create_engine(sync_database_url())
        self.addCleanup(engine.dispose)
        code, out, _ = self.run_cli(
            VALUE,
            "--kind",
            "codex",
            secret_store_dir=self.root,
            database_url=TEST_DATABASE_URL,
        )
        self.assertEqual(code, credentials.EXIT_OK)
        handle = out.strip()
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT actor_role, reason FROM audit_events"
                    " WHERE action = 'connection.credential_store'"
                    " ORDER BY occurred_at DESC LIMIT 1"
                )
            ).all()
        self.assertEqual(len(rows), 1)
        # Decision 0087, 2: the kind, the version and the handle; never the value.
        self.assertEqual(
            (rows[0].actor_role, rows[0].reason), ("system", f"codex:v1:{handle}")
        )
        self.assertNotIn(VALUE, repr(rows))


if __name__ == "__main__":
    unittest.main()
