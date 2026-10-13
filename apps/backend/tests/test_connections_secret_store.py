"""The secret store of the shared connections (Decision 0087, 1): files only,
no database (the service's use of it is ``test_connections_credential_store``).

* a value is stored encrypted under a new handle and read back; no file holds
  the value, nothing prints it;
* the handle, the version and the kind are authenticated: a file moved under
  another handle or version does not decrypt, and ``verify`` checks the kind;
* ``rotate`` is a compare-and-swap on the version and keeps the kind;
* the handle's lock excludes a second holder;
* ``collect`` deletes the old unreferenced handles only (asking about each
  one under its lock) and the replaced versions after the grace period;
* the directory and the key are checked when the store is opened.
"""

import asyncio
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from paw_backend.connections import ConnectionKind
from paw_backend.connections.secret_store import (
    MASTER_KEY_BYTES,
    FileSecretStore,
    SecretStoreError,
    StaleCredentialVersionError,
    check_root,
    load_master_key,
)

CODEX = ConnectionKind.CODEX
CLAUDE = ConnectionKind.CLAUDE
# Test values built from pieces: no literal a secret scanner would flag.
VALUE = "paw-store-canary-" + "41d7" + "-first-value"
OTHER = "paw-store-canary-" + "9be2" + "-second-value"
KEY = bytes(range(MASTER_KEY_BYTES))


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class StoreCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.chmod(self.root, 0o700)
        self.clock = Clock(1_000_000.0)
        self.store = FileSecretStore(self.root, KEY, clock=self.clock)

    def files(self) -> list[Path]:
        return [path for path in self.root.rglob("*.bin")]


class PutAndReadTest(StoreCase):
    async def test_a_value_is_stored_encrypted_and_read_back(self):
        handle = await self.store.put(CLAUDE, VALUE)
        self.assertRegex(handle, r"^cred_[0-9a-f]{32}$")
        stored = await self.store.read(handle)
        self.assertEqual(stored.secret.reveal(), VALUE)
        self.assertIs(stored.kind, CLAUDE)
        self.assertEqual(stored.version, 1)
        self.assertEqual((await self.store.resolve(handle)).reveal(), VALUE)
        for path in self.files():
            self.assertNotIn(VALUE.encode(), path.read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / handle).stat().st_mode), 0o700)

    async def test_nothing_prints_the_value(self):
        handle = await self.store.put(CLAUDE, VALUE)
        stored = await self.store.read(handle)
        for text in (repr(stored), str(stored), repr(self.store)):
            self.assertNotIn(VALUE, text)

    async def test_an_unknown_handle_is_a_lookup_error(self):
        with self.assertRaises(LookupError):
            await self.store.read("cred_" + "0" * 32)

    async def test_the_kind_is_verified(self):
        handle = await self.store.put(CODEX, VALUE)
        self.assertTrue(await self.store.verify(handle, CODEX))
        self.assertFalse(await self.store.verify(handle, CLAUDE))
        self.assertFalse(await self.store.verify("cred_" + "0" * 32, CODEX))

    async def test_a_file_moved_under_another_handle_does_not_decrypt(self):
        first = await self.store.put(CODEX, VALUE)
        second = await self.store.put(CODEX, OTHER)
        shutil.copy(self.root / first / "v1.bin", self.root / second / "v1.bin")
        with self.assertRaises(SecretStoreError):
            await self.store.read(second)
        self.assertFalse(await self.store.verify(second, CODEX))

    async def test_a_file_renamed_to_another_version_does_not_decrypt(self):
        handle = await self.store.put(CODEX, VALUE)
        os.rename(self.root / handle / "v1.bin", self.root / handle / "v2.bin")
        with self.assertRaises(SecretStoreError):
            await self.store.read(handle)

    async def test_another_key_does_not_decrypt(self):
        handle = await self.store.put(CODEX, VALUE)
        other = FileSecretStore(self.root, bytes(32), clock=self.clock)
        with self.assertRaises(SecretStoreError):
            await other.read(handle)


class RotateTest(StoreCase):
    async def test_rotate_writes_the_next_version_only_from_the_current(self):
        handle = await self.store.put(CODEX, VALUE)
        self.assertEqual(await self.store.rotate(handle, CODEX, 1, OTHER), 2)
        stored = await self.store.read(handle)
        self.assertEqual((stored.version, stored.secret.reveal()), (2, OTHER))
        with self.assertRaises(StaleCredentialVersionError):
            await self.store.rotate(handle, CODEX, 1, VALUE)
        self.assertEqual((await self.store.read(handle)).version, 2)

    async def test_rotate_keeps_the_kind(self):
        handle = await self.store.put(CODEX, VALUE)
        with self.assertRaises(SecretStoreError):
            await self.store.rotate(handle, CLAUDE, 1, OTHER)
        self.assertEqual((await self.store.read(handle)).version, 1)


class LockTest(StoreCase):
    async def test_the_lock_excludes_a_second_holder(self):
        handle = await self.store.put(CODEX, VALUE)
        order = []

        async def hold(name: str, wait: float) -> None:
            async with self.store.lock(handle):
                order.append(f"{name} in")
                await asyncio.sleep(wait)
                order.append(f"{name} out")

        first = asyncio.create_task(hold("a", 0.2))
        await asyncio.sleep(0.05)
        await asyncio.gather(first, hold("b", 0))
        self.assertEqual(order, ["a in", "a out", "b in", "b out"])

    async def test_a_cancelled_waiter_leaves_the_lock_usable(self):
        handle = await self.store.put(CODEX, VALUE)
        async with self.store.lock(handle):
            waiter = asyncio.create_task(self._enter(handle))
            await asyncio.sleep(0.05)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
        async with asyncio.timeout(2):
            await self._enter(handle)  # free again

    async def _enter(self, handle: str) -> None:
        async with self.store.lock(handle):
            pass


class CollectTest(StoreCase):
    async def test_old_unreferenced_handles_go_and_the_rest_stay(self):
        kept = await self.store.put(CODEX, VALUE)
        gone = await self.store.put(CODEX, VALUE)
        young = await self.store.put(CODEX, VALUE)
        old = self.clock.now - 2 * 24 * 3600
        for handle in (kept, gone):
            os.utime(self.root / handle / "v1.bin", (old, old))
        asked = []

        async def referenced(handle: str) -> bool:
            asked.append(handle)
            return handle == kept

        self.assertEqual(await self.store.collect(referenced), 1)
        self.assertTrue(await self.store.verify(kept, CODEX))
        self.assertFalse(await self.store.verify(gone, CODEX))
        self.assertTrue(await self.store.verify(young, CODEX))
        self.assertNotIn(young, asked)  # too young: not even asked about

    async def test_a_replaced_version_stays_a_day(self):
        handle = await self.store.put(CODEX, VALUE)
        await self.store.rotate(handle, CODEX, 1, OTHER)
        await self.store.rotate(handle, CODEX, 2, VALUE)

        async def referenced(handle: str) -> bool:
            return True

        await self.store.collect(referenced)
        names = sorted(path.name for path in (self.root / handle).glob("*.bin"))
        self.assertEqual(names, ["v2.bin", "v3.bin"])  # one replaced version stays
        old = self.clock.now - 2 * 24 * 3600
        os.utime(self.root / handle / "v3.bin", (old, old))
        await self.store.collect(referenced)
        names = sorted(path.name for path in (self.root / handle).glob("*.bin"))
        self.assertEqual(names, ["v3.bin"])
        self.assertEqual((await self.store.read(handle)).version, 3)


class HealthTest(StoreCase):
    async def test_the_check_reads_every_handle_and_the_directory(self):
        handle = await self.store.put(CODEX, VALUE)
        self.assertIsNone(await self.store.check())
        (self.root / handle / "v1.bin").write_bytes(b"PAWS1damaged")
        self.assertEqual(await self.store.check(), "secret_store_unavailable")
        os.unlink(self.root / handle / "v1.bin")
        self.assertIsNone(await self.store.check())
        os.chmod(self.root, 0o755)
        try:
            self.assertEqual(await self.store.check(), "secret_store_unavailable")
        finally:
            os.chmod(self.root, 0o700)


class OpeningTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.chmod(self.root, 0o700)

    def test_the_directory_is_checked(self):
        check_root(self.root)
        with self.assertRaises(SecretStoreError):
            check_root(Path("relative/dir"))
        with self.assertRaises(SecretStoreError):
            check_root(self.root / "missing")
        os.chmod(self.root, 0o750)
        with self.assertRaises(SecretStoreError):
            check_root(self.root)
        os.chmod(self.root, 0o700)
        for forbidden in (self.root, self.root.parent, self.root / "inside"):
            with self.subTest(forbidden=forbidden):
                with self.assertRaises(SecretStoreError):
                    check_root(self.root, [forbidden])
        check_root(self.root, [self.root.parent / "elsewhere-entirely"])

    def test_the_restore_points_are_forbidden_too(self):
        from paw_backend.connections.wiring import (
            DEFAULT_RESTORE_POINT_DIR,
            restore_point_dirs,
        )

        config = self.root / "release.toml"
        self.assertEqual(restore_point_dirs(config), [DEFAULT_RESTORE_POINT_DIR])
        config.write_text('restore_point_dir = "/srv/paw/restore"\n')
        self.assertEqual(
            restore_point_dirs(config),
            [DEFAULT_RESTORE_POINT_DIR, Path("/srv/paw/restore")],
        )
        config.write_text("not toml [")
        self.assertEqual(restore_point_dirs(config), [DEFAULT_RESTORE_POINT_DIR])

    def test_the_key_is_checked(self):
        with self.assertRaises(SecretStoreError):
            FileSecretStore(self.root, b"short")
        credentials = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, credentials, ignore_errors=True)
        (credentials / "paw-secret-key").write_bytes(KEY)
        self.assertEqual(load_master_key(credentials), KEY)
        (credentials / "paw-secret-key").write_bytes(KEY + b"\n")
        with self.assertRaises(SecretStoreError):
            load_master_key(credentials)
        with self.assertRaises(SecretStoreError):
            load_master_key(credentials / "missing")
        saved = os.environ.pop("CREDENTIALS_DIRECTORY", None)
        try:
            with self.assertRaises(SecretStoreError):
                load_master_key()
        finally:
            if saved is not None:
                os.environ["CREDENTIALS_DIRECTORY"] = saved


if __name__ == "__main__":
    unittest.main()
