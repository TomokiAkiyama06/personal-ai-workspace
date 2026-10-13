"""The connection service with the deployment's secret store (Decision 0087, 1):
real PostgreSQL, the real file store in a temporary directory.

* a handle is registered only while the store holds it for that kind;
* a replaced or disconnected credential that nothing refers to any more is
  deleted, one still referred to is kept;
* ``rotate_credential`` writes the next version of the same handle only from
  the current one, and is audited as the system's;
* the store's cleanup asks the database whether a handle is referred to.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from paw_backend.connections import (
    ConnectionKind,
    ConnectionNotFoundError,
    CredentialNotStoredError,
    FileSecretStore,
    StaleCredentialVersionError,
)

from .connections_fakes import handle
from .connections_support import PostgresConnectionTestCase, requires_postgres

CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE
VALUE = "paw-store-canary-" + "41d7" + "-first-value"
OTHER = "paw-store-canary-" + "9be2" + "-second-value"
KEY = bytes(range(32))


@requires_postgres
class CredentialStoreCase(PostgresConnectionTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.chmod(root, 0o700)
        self.store = FileSecretStore(root, KEY)
        self.service = self.new_service(credential_store=self.store)

    async def connect(self, kind, handle_):
        return await self.service.connect(self.principal(self.admin), kind, handle_)


class RegistrationTest(CredentialStoreCase):
    async def test_a_handle_the_store_does_not_hold_is_refused(self):
        with self.assertRaises(CredentialNotStoredError):
            await self.connect(CODEX, handle(1))
        self.assertEqual(self.rows("SELECT id FROM shared_connections"), [])

    async def test_a_handle_stored_for_the_other_kind_is_refused(self):
        stored = await self.store.put(CLAUDE, VALUE)
        with self.assertRaises(CredentialNotStoredError):
            await self.connect(CODEX, stored)
        info = await self.connect(CLAUDE, stored)
        self.assertIs(info.kind, CLAUDE)

    async def test_a_replacement_is_checked_and_the_old_file_goes(self):
        first = await self.store.put(CODEX, VALUE)
        second = await self.store.put(CODEX, OTHER)
        await self.connect(CODEX, first)
        with self.assertRaises(CredentialNotStoredError):
            await self.service.replace_credential(
                self.principal(self.admin), CODEX, handle(9)
            )
        await self.service.replace_credential(self.principal(self.admin), CODEX, second)
        self.assertFalse(await self.store.verify(first, CODEX))
        self.assertTrue(await self.store.verify(second, CODEX))

    async def test_a_handle_still_referred_to_is_kept(self):
        # A deletion reads the references under the handle's lock: a handle
        # registered again in the meantime (here: still registered) stays.
        stored = await self.store.put(CODEX, VALUE)
        await self.connect(CODEX, stored)
        await self.service._discard_credential(stored)
        self.assertTrue(await self.store.verify(stored, CODEX))

    async def test_a_disconnection_deletes_the_file(self):
        stored = await self.store.put(CODEX, VALUE)
        await self.connect(CODEX, stored)
        await self.service.disconnect(self.principal(self.admin), CODEX)
        self.assertFalse(await self.store.verify(stored, CODEX))


class RotationTest(CredentialStoreCase):
    async def test_the_same_handle_gets_the_next_version_only_from_the_current(self):
        stored = await self.store.put(CODEX, VALUE)
        await self.connect(CODEX, stored)
        self.assertEqual(
            await self.service.rotate_credential(CODEX, stored, 1, OTHER), 2
        )
        self.assertEqual((await self.store.read(stored)).secret.reveal(), OTHER)
        with self.assertRaises(StaleCredentialVersionError):
            await self.service.rotate_credential(CODEX, stored, 1, VALUE)
        actions = [event.action for event in self.sink.events]
        self.assertIn("connection.rotate", actions)
        rotate = [e for e in self.sink.events if e.action == "connection.rotate"][0]
        self.assertIsNone(rotate.actor_id)
        self.assertEqual(rotate.actor_role, "system")

    async def test_only_the_connections_own_handle_is_rotated(self):
        stored = await self.store.put(CODEX, VALUE)
        stranger = await self.store.put(CODEX, OTHER)
        await self.connect(CODEX, stored)
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.rotate_credential(CODEX, stranger, 1, VALUE)
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.rotate_credential(CLAUDE, stored, 1, VALUE)
        self.assertEqual((await self.store.read(stranger)).version, 1)


class CollectTest(CredentialStoreCase):
    async def test_the_cleanup_keeps_what_a_connection_refers_to(self):
        kept = await self.store.put(CODEX, VALUE)
        gone = await self.store.put(CLAUDE, VALUE)
        await self.connect(CODEX, kept)
        old = 0
        for stored in (kept, gone):
            os.utime(self.store._root / stored / "v1.bin", (old, old))
        self.assertEqual(await self.service.collect_credentials(), 1)
        self.assertTrue(await self.store.verify(kept, CODEX))
        self.assertFalse(await self.store.verify(gone, CLAUDE))


if __name__ == "__main__":
    unittest.main()
