"""The connection service with the deployment's secret store (Decision 0087, 1):
real PostgreSQL, the real file store in a temporary directory.

* a handle is registered only while the store holds it for that kind;
* a replaced or disconnected credential that nothing refers to any more is
  deleted, one still referred to is kept;
* ``rotate_credential`` writes the next version of the same handle only from
  the current one, and is audited as the system's;
* the store's cleanup asks the database whether a handle is referred to;
* the credential's status (stored, version, revision; never the value or the
  handle) and the compare-and-swap of a replacement (issue #277).
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from paw_backend.connections import (
    ConnectionKind,
    ConnectionNotFoundError,
    ConnectionPermissionDeniedError,
    CredentialNotStoredError,
    FileSecretStore,
    StaleConnectionCredentialError,
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


class CredentialStatusTest(CredentialStoreCase):
    """Issue #277: what an Owner / Admin sees of the attached credential."""

    async def status(self, who=None):
        return await self.service.credential_status(self.principal(who or self.admin))

    async def test_the_status_holds_kind_state_and_version_never_the_secret(self):
        stored = await self.store.put(CODEX, VALUE)
        await self.connect(CODEX, stored)
        await self.service.rotate_credential(CODEX, stored, 1, OTHER)

        (status,) = await self.status()

        self.assertIs(status.connection.kind, CODEX)
        self.assertEqual(status.connection.status.value, "unavailable")
        self.assertIs(status.stored, True)
        self.assertEqual(status.version, 2)
        self.assertNotIn(stored, repr(status))
        self.assertNotIn(stored, status.revision)
        for secret in (VALUE, OTHER):
            self.assertNotIn(secret, repr(status))

    async def test_a_credential_gone_from_the_store_is_not_stored(self):
        stored = await self.store.put(CLAUDE, VALUE)
        await self.connect(CLAUDE, stored)
        shutil.rmtree(self.store._root / stored)

        (status,) = await self.status()

        self.assertEqual((status.stored, status.version), (False, None))

    async def test_without_a_store_nothing_is_said_of_the_credential(self):
        service = self.new_service()
        await service.connect(self.principal(self.admin), CODEX, handle(1))

        (status,) = await service.credential_status(self.principal(self.owner))

        self.assertEqual((status.stored, status.version), (None, None))

    async def test_only_an_owner_or_admin_sees_it(self):
        with self.assertRaises(ConnectionPermissionDeniedError):
            await self.status(self.user)
        self.assertEqual(await self.status(self.owner), ())

    async def test_the_revision_changes_with_a_replacement_not_a_rotation(self):
        first = await self.store.put(CODEX, VALUE)
        second = await self.store.put(CODEX, OTHER)
        await self.connect(CODEX, first)
        (before,) = await self.status()
        await self.service.rotate_credential(CODEX, first, 1, OTHER)
        (rotated,) = await self.status()
        await self.service.replace_credential(
            self.principal(self.admin), CODEX, second, expected_revision=before.revision
        )
        (replaced,) = await self.status()

        self.assertEqual(rotated.revision, before.revision)
        self.assertNotEqual(replaced.revision, before.revision)
        self.assertEqual(replaced.version, 1)


class ReplacementCompareAndSwapTest(CredentialStoreCase):
    """Issue #277: a replacement names the credential it replaces."""

    async def test_a_stale_revision_changes_nothing_and_is_audited(self):
        first = await self.store.put(CODEX, VALUE)
        second = await self.store.put(CODEX, OTHER)
        third = await self.store.put(CODEX, VALUE + "-third")
        await self.connect(CODEX, first)
        (seen,) = await self.service.credential_status(self.principal(self.admin))
        # Somebody else replaces it first.
        await self.service.replace_credential(
            self.principal(self.owner), CODEX, second, expected_revision=seen.revision
        )

        with self.assertRaises(StaleConnectionCredentialError):
            await self.service.replace_credential(
                self.principal(self.admin),
                CODEX,
                third,
                expected_revision=seen.revision,
            )

        self.assertEqual(
            self.scalar("SELECT secret_handle FROM shared_connections"), second
        )
        self.assertTrue(await self.store.verify(second, CODEX))
        self.assertIn(
            ("connection.replace", "deny", "stale_connection_credential"),
            self.own_audit(),
        )
        self.assertNotIn(third, repr(self.sink.events))

    async def test_a_revision_of_a_connection_made_again_is_stale(self):
        first = await self.store.put(CODEX, VALUE)
        second = await self.store.put(CODEX, OTHER)
        await self.connect(CODEX, first)
        (seen,) = await self.service.credential_status(self.principal(self.admin))
        await self.service.disconnect(self.principal(self.admin), CODEX)
        again = await self.store.put(CODEX, VALUE)
        await self.connect(CODEX, again)

        with self.assertRaises(StaleConnectionCredentialError):
            await self.service.replace_credential(
                self.principal(self.admin),
                CODEX,
                second,
                expected_revision=seen.revision,
            )

    async def test_a_revision_that_is_not_one_is_stale(self):
        first = await self.store.put(CODEX, VALUE)
        second = await self.store.put(CODEX, OTHER)
        await self.connect(CODEX, first)

        for revision in ("", "0" * 32, first, "ä" * 32, "\ud800"):
            with self.assertRaises(StaleConnectionCredentialError):
                await self.service.replace_credential(
                    self.principal(self.admin),
                    CODEX,
                    second,
                    expected_revision=revision,
                )

    async def test_the_guard_names_the_connection_not_only_the_handle(self):
        # Codex review (P2): a connection removed and made again with the same
        # handle between the revision check and the UPDATE is another
        # connection; the statement's guard holds the id too.
        stored = await self.store.put(CODEX, VALUE)
        other = await self.store.put(CODEX, OTHER)
        old = await self.connect(CODEX, stored)
        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM shared_connections"))
        new = await self.connect(CODEX, stored)

        changed = await self.service._store.replace_handle(
            CODEX, other, expected=(old.id, stored)
        )

        self.assertIsNone(changed)
        self.assertEqual(
            self.scalar("SELECT secret_handle FROM shared_connections"), stored
        )
        current = await self.service._store.replace_handle(
            CODEX, other, expected=(new.id, stored)
        )
        self.assertEqual(current.id, new.id)

    async def test_the_status_is_one_snapshot_of_the_connection(self):
        # Codex review (P2): the id, the handle and the state are one read, so
        # a replacement in between cannot mix the old credential with the new
        # row: the revision describes the connection that is shown.
        first = await self.store.put(CODEX, VALUE)
        second = await self.store.put(CODEX, OTHER)
        await self.connect(CODEX, first)
        (before,) = await self.service.credential_status(self.principal(self.admin))
        reads = []
        original = self.service._store.list_credentials

        async def replaced_meanwhile():
            found = await original()
            reads.append(found)
            await self.service.replace_credential(
                self.principal(self.owner), CODEX, second,
                expected_revision=before.revision,
            )  # fmt: skip
            return found

        self.service._store.list_credentials = replaced_meanwhile
        (during,) = await self.service.credential_status(self.principal(self.admin))

        self.assertEqual(len(reads), 1)
        self.assertEqual(during.revision, before.revision)
        self.assertEqual(during.connection.updated_at, before.connection.updated_at)

    async def test_a_replacement_without_a_connection_is_not_found(self):
        stored = await self.store.put(CODEX, VALUE)
        with self.assertRaises(ConnectionNotFoundError):
            await self.service.replace_credential(
                self.principal(self.admin), CODEX, stored, expected_revision="0" * 32
            )


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
