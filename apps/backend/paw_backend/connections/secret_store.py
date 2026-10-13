"""The secret store of the shared connections (Decision 0087, 1; #208).

An encrypted file store that the backend owns. No new product: one directory,
one sub-directory per credential handle, one file per version.

Layout and format
-----------------
``<root>/<handle>/v<N>.bin`` (``N`` = 1, 2, ...; mode 0600, the directories
0700) holds version ``N`` of the credential::

    b"PAWS1" | len(kind) (1 byte) | kind (ASCII) | nonce (12 bytes)
            | AES-256-GCM ciphertext

The additional authenticated data is ``paw-secret|<handle>|<N>|<kind>``: a file
moved under another handle, renamed to another version or given another kind
does not decrypt. The kind in the header is therefore authenticated too, and
:meth:`FileSecretStore.verify` reads the kind a handle was stored for.

The master key (32 bytes) is not in any environment file: systemd hands it to
the backend's unit (``LoadCredentialEncrypted=paw-secret-key:...``, encrypted
with the host key and the TPM2 when there is one) and to the one-shot unit of
the management command; :func:`load_master_key` reads it from
``$CREDENTIALS_DIRECTORY``.

Writing
-------
A version is written to a temporary name, ``fsync``-ed, renamed into place,
and then the handle's directory (and, for a new handle, the root) is
``fsync``-ed: only after that does :meth:`put` return the handle or
:meth:`rotate` its new version, so a database row that refers to it after a
crash still finds the file. Versions only grow. :meth:`rotate` writes version
``N + 1`` only while ``N`` is the current one (compare-and-swap).

Locks
-----
:meth:`lock` takes the handle's ``flock`` (``<root>/.locks/<handle>``): it holds
across processes. The service takes it around "check the file, then commit the
database row" (registration) and "check the database, then delete the file"
(deletion), and :meth:`collect` takes it for each handle it looks at, so a
handle can never be deleted between a registration's check and its commit.

Cleanup (:meth:`collect`)
-------------------------
A handle that no connection refers to and that is older than the grace period
(24 hours, provisional) is deleted (a ``put`` whose registration never came, a
leftover of a deletion that failed half-way); the version before the current
one is deleted once the current one is older than the grace period. Each under
the handle's lock, the reference read again right before the deletion.

Not in backups, not in the recovery repository: a credential is registered
again after a restore (REQUIREMENTS, Decision 0054), and so is every credential
when the master key is lost.
"""

import asyncio
import contextlib
import fcntl
import os
import re
import secrets
import stat
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from paw_backend.connections.domain import ConnectionKind
from paw_backend.connections.errors import ConnectionsError
from paw_backend.connections.secret import Secret
from paw_backend.connections.validation import validate_enum, validate_handle

#: The name of the systemd credential that holds the master key.
MASTER_KEY_CREDENTIAL: Final = "paw-secret-key"
MASTER_KEY_BYTES: Final = 32
#: How long an unreferenced handle or a replaced version is kept (provisional).
GRACE_SECONDS: Final = 24 * 3600

_MAGIC: Final = b"PAWS1"
_NONCE_BYTES: Final = 12
_VERSION_FILE = re.compile(r"v([1-9][0-9]{0,8})\.bin")
_LOCKS: Final = ".locks"


class SecretStoreError(ConnectionsError):
    """The secret store cannot be used (configuration, a file that does not
    decrypt). The message is a fixed text: never a path or a value."""

    code = "secret_store_error"


class StaleCredentialVersionError(ConnectionsError):
    """A rotation's expected version is not the current one any more."""

    code = "stale_credential_version"

    def __init__(self) -> None:
        super().__init__("The credential was changed in the meantime")


@dataclass(frozen=True, slots=True)
class StoredCredential:
    """One version of a stored credential. ``secret`` is the value (a
    :class:`Secret`: it does not print)."""

    handle: str
    kind: ConnectionKind
    version: int
    secret: Secret


class CredentialStore(Protocol):
    """What the connection service needs of a secret store besides resolving
    (:class:`FileSecretStore` in production)."""

    def lock(self, handle: str) -> contextlib.AbstractAsyncContextManager[None]: ...

    async def verify(self, handle: str, kind: ConnectionKind) -> bool: ...

    async def delete(self, handle: str) -> None: ...

    async def rotate(
        self, handle: str, kind: ConnectionKind, expected_version: int, value: str
    ) -> int: ...


def load_master_key(
    directory: Path | str | None = None, name: str = MASTER_KEY_CREDENTIAL
) -> bytes:
    """The master key as systemd handed it over (``$CREDENTIALS_DIRECTORY``):
    exactly 32 bytes, or ``SecretStoreError``."""
    base = directory
    if base is None:
        base = os.environ.get("CREDENTIALS_DIRECTORY")
    if not base:
        raise SecretStoreError("The secret store's key was not handed over")
    path = Path(base) / name
    try:
        key = path.read_bytes()
    except OSError:
        raise SecretStoreError("The secret store's key cannot be read") from None
    if len(key) != MASTER_KEY_BYTES:
        raise SecretStoreError("The secret store's key has the wrong length")
    return key


def check_root(root: Path, forbidden: Iterable[Path] = ()) -> Path:
    """``root`` as the store accepts it: absolute, an existing directory of this
    process's user with no group or other permission, and neither inside nor
    above one of ``forbidden`` (the release directory, the recovery repository,
    the restore points)."""
    if not isinstance(root, Path) or not root.is_absolute():
        raise SecretStoreError("The secret store's directory must be absolute")
    try:
        info = root.lstat()
    except OSError:
        raise SecretStoreError("The secret store's directory does not exist") from None
    if not stat.S_ISDIR(info.st_mode):
        raise SecretStoreError("The secret store's directory is not a directory")
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise SecretStoreError(
            "The secret store's directory must be the backend's own, mode 0700"
        )
    resolved = root.resolve()
    for other in forbidden:
        if other is None:
            continue
        other = Path(other).resolve()
        if resolved == other or other in resolved.parents or resolved in other.parents:
            raise SecretStoreError(
                "The secret store's directory overlaps a directory it must not"
            )
    return resolved


class FileSecretStore:
    """See the module. Also the :class:`SecretResolver` of the service."""

    def __init__(
        self,
        root: Path,
        key: bytes,
        *,
        forbidden: Iterable[Path] = (),
        grace_seconds: float = GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(key) is not bytes or len(key) != MASTER_KEY_BYTES:
            raise SecretStoreError("The secret store's key has the wrong length")
        self._root = check_root(root, forbidden)
        self._aead = AESGCM(key)
        self._grace = float(grace_seconds)
        self._clock = clock
        locks = self._root / _LOCKS
        locks.mkdir(mode=0o700, exist_ok=True)

    def __repr__(self) -> str:
        return "FileSecretStore(<redacted>)"

    # --- the resolver -------------------------------------------------------

    async def resolve(self, handle: str) -> Secret:
        """The current version's value (``SecretResolver``)."""
        return (await self.read(handle)).secret

    async def read(self, handle: str) -> StoredCredential:
        """The current version of ``handle`` (``LookupError`` when there is
        none, ``SecretStoreError`` when it does not decrypt)."""
        handle = validate_handle("handle", handle)
        return await asyncio.to_thread(self._read_current, handle)

    async def verify(self, handle: str, kind: ConnectionKind) -> bool:
        """Whether ``handle`` holds a credential stored for ``kind`` (its
        current version decrypts, with that kind authenticated)."""
        kind = validate_enum("kind", kind, ConnectionKind)
        try:
            stored = await self.read(handle)
        except (LookupError, SecretStoreError):
            return False
        return stored.kind is kind

    # --- writing ------------------------------------------------------------

    async def put(self, kind: ConnectionKind, value: str) -> str:
        """Store ``value`` for ``kind`` under a new handle (version 1) and return
        the handle once the file and its directories are on disk."""
        kind = validate_enum("kind", kind, ConnectionKind)
        secret = Secret(value)  # the same checks as everywhere a value is held
        handle = "cred_" + secrets.token_hex(16)
        async with self.lock(handle):
            await asyncio.to_thread(self._create, handle, kind, secret)
        return handle

    async def rotate(
        self, handle: str, kind: ConnectionKind, expected_version: int, value: str
    ) -> int:
        """Write version ``expected_version + 1`` of ``handle`` while
        ``expected_version`` is the current one (``StaleCredentialVersionError``
        otherwise); the kind stays. Returns the new version."""
        handle = validate_handle("handle", handle)
        kind = validate_enum("kind", kind, ConnectionKind)
        secret = Secret(value)
        async with self.lock(handle):
            return await asyncio.to_thread(
                self._rotate, handle, kind, expected_version, secret
            )

    async def delete(self, handle: str) -> None:
        """Remove every version of ``handle``. The caller holds its lock and
        has made sure that nothing refers to it."""
        handle = validate_handle("handle", handle)
        await asyncio.to_thread(self._delete, handle)

    @contextlib.asynccontextmanager
    async def lock(self, handle: str) -> AsyncIterator[None]:
        """The handle's ``flock`` (across processes), held while the block runs."""
        handle = validate_handle("handle", handle)
        path = self._root / _LOCKS / handle
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            # Polled without blocking (no thread left waiting on the file
            # descriptor when the waiter is cancelled).
            delay = 0.005
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 0.1)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # --- cleanup ------------------------------------------------------------

    async def collect(self, referenced: Callable[[str], Awaitable[bool]]) -> int:
        """Delete the handles nothing refers to (older than the grace period)
        and the replaced versions (once the version after them is older than
        the grace period). Returns how many handles were deleted."""
        deleted = 0
        for handle in await asyncio.to_thread(self._handles):
            async with self.lock(handle):
                created = await asyncio.to_thread(self._created_at, handle)
                if created is None:
                    continue
                old = self._clock() - created >= self._grace
                if old and not await referenced(handle):
                    await asyncio.to_thread(self._delete, handle)
                    deleted += 1
                else:
                    await asyncio.to_thread(self._prune_versions, handle)
        return deleted

    # --- the files (in a worker thread) ---------------------------------------

    def _dir(self, handle: str) -> Path:
        return self._root / handle

    def _versions(self, handle: str) -> list[int]:
        try:
            names = os.listdir(self._dir(handle))
        except FileNotFoundError:
            return []
        return sorted(
            int(match.group(1))
            for name in names
            if (match := _VERSION_FILE.fullmatch(name))
        )

    def _aad(self, handle: str, version: int, kind: ConnectionKind) -> bytes:
        return f"paw-secret|{handle}|{version}|{kind.value}".encode()

    def _encrypt(
        self, handle: str, version: int, kind: ConnectionKind, secret: Secret
    ) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        kind_bytes = kind.value.encode()
        sealed = self._aead.encrypt(
            nonce, secret.reveal().encode(), self._aad(handle, version, kind)
        )
        return _MAGIC + bytes([len(kind_bytes)]) + kind_bytes + nonce + sealed

    def _decrypt(self, handle: str, version: int, data: bytes) -> StoredCredential:
        if not data.startswith(_MAGIC) or len(data) < len(_MAGIC) + 1:
            raise SecretStoreError("A stored credential is damaged")
        size = data[len(_MAGIC)]
        start = len(_MAGIC) + 1
        try:
            kind = ConnectionKind(data[start : start + size].decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            raise SecretStoreError("A stored credential is damaged") from None
        nonce = data[start + size : start + size + _NONCE_BYTES]
        sealed = data[start + size + _NONCE_BYTES :]
        try:
            plain = self._aead.decrypt(nonce, sealed, self._aad(handle, version, kind))
        except (InvalidTag, ValueError):
            raise SecretStoreError("A stored credential does not decrypt") from None
        return StoredCredential(handle, kind, version, Secret(plain.decode()))

    def _read_current(self, handle: str) -> StoredCredential:
        versions = self._versions(handle)
        if not versions:
            raise LookupError("no such credential")
        version = versions[-1]
        data = (self._dir(handle) / f"v{version}.bin").read_bytes()
        return self._decrypt(handle, version, data)

    def _write_version(
        self, handle: str, version: int, kind: ConnectionKind, secret: Secret
    ) -> None:
        directory = self._dir(handle)
        final = directory / f"v{version}.bin"
        temporary = directory / f".v{version}.{secrets.token_hex(4)}.tmp"
        data = self._encrypt(handle, version, kind, secret)
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary, final)
        _fsync_dir(directory)

    def _create(self, handle: str, kind: ConnectionKind, secret: Secret) -> None:
        directory = self._dir(handle)
        directory.mkdir(mode=0o700)
        _fsync_dir(self._root)
        self._write_version(handle, 1, kind, secret)

    def _rotate(
        self, handle: str, kind: ConnectionKind, expected: int, secret: Secret
    ) -> int:
        current = self._read_current(handle)
        if current.version != expected:
            raise StaleCredentialVersionError()
        if current.kind is not kind:
            raise SecretStoreError("A credential keeps the kind it was stored for")
        self._write_version(handle, expected + 1, kind, secret)
        return expected + 1

    def _delete(self, handle: str) -> None:
        directory = self._dir(handle)
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return
        for name in names:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(directory / name)
        with contextlib.suppress(FileNotFoundError):
            os.rmdir(directory)
        _fsync_dir(self._root)

    def _handles(self) -> list[str]:
        handles = []
        for name in os.listdir(self._root):
            if name.startswith("."):
                continue
            try:
                handles.append(validate_handle("handle", name))
            except ConnectionsError:
                continue
        return sorted(handles)

    def _created_at(self, handle: str) -> float | None:
        versions = self._versions(handle)
        if not versions:
            # A directory without a version (a put that failed half-way).
            try:
                return self._dir(handle).stat().st_mtime
            except FileNotFoundError:
                return None
        return (self._dir(handle) / f"v{versions[0]}.bin").stat().st_mtime

    def _prune_versions(self, handle: str) -> None:
        versions = self._versions(handle)
        if len(versions) < 2:
            return
        current = self._dir(handle) / f"v{versions[-1]}.bin"
        keep = 1
        if self._clock() - current.stat().st_mtime < self._grace:
            keep = 2  # the version before it stays a day (a failed write-back)
        stale = versions[:-keep]
        if not stale:
            return
        for version in stale:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self._dir(handle) / f"v{version}.bin")
        _fsync_dir(self._dir(handle))


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


__all__ = [
    "CredentialStore",
    "GRACE_SECONDS",
    "MASTER_KEY_BYTES",
    "MASTER_KEY_CREDENTIAL",
    "FileSecretStore",
    "SecretStoreError",
    "StaleCredentialVersionError",
    "StoredCredential",
    "check_root",
    "load_master_key",
]
