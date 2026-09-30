"""The files of the Recovery Repository's checkout and of the projection it copies.

**The checkout** (``PAW_RECOVERY_REPOSITORY_DIR``) holds every user's private
memory, like the projection (Decision 0038 7), so it is handled the same way:

* **Where.** An absolute, canonical path (no symbolic link, no ``..``), not ``/``,
  an existing directory owned by this process's user, neither inside nor above a
  person's home directory (where every project checkout lives) and not
  overlapping the Memory Projection's directory. That it is the top of a git
  work tree is checked by ``git.py``.
* **Whose checkout.** Its root holds the marker ``.paw-recovery-repository``
  (committed with the backup). A checkout without it is claimed only when it has
  nothing but ``.git`` (an empty clone of the new private repository); any other
  checkout without the marker is refused, so a misconfigured path never gets a
  project repository overwritten or committed to.
* **Who can read.** The root, every directory the backup makes ``0700``, every
  file ``0600`` (``fchmod``), whatever the umask.
* **Only its own names.** Only ``format.MANAGED_ROOT_NAMES`` at the root are
  written, replaced or removed; inside a managed directory the backup owns
  everything. A symbolic link is never followed (``O_NOFOLLOW`` and ``dir_fd``):
  one in the tree is removed as a link, never written through. Files are written
  to a temporary name and renamed (atomic, never through a link).
* **One at a time.** ``open_checkout`` takes an exclusive ``flock`` on
  ``.git/paw-recovery.lock`` (a backup and a restore never overlap).

**The projection** is read under its own marker's lock, only when no incomplete
flag is present (Decision 0038 9); the caller also checks that the last recorded
projection run completed before it reads (``ProjectionReader``).

Everything here blocks: call it with ``asyncio.to_thread``.
"""

import errno
import fcntl
import os
import stat
from collections.abc import Callable, Collection, Mapping
from enum import StrEnum
from pathlib import Path

from paw_backend.memory.projection.render import (
    INDEX_FILE,
    KEYED_TOP_DIRECTORIES,
    SHARED_DIRECTORY,
    TOP_DIRECTORIES,
    is_memory_file_name,
    is_uuid_name,
)
from paw_backend.memory.projection.writer import (
    DIRECTORY_MODE,
    FILE_MODE,
    INCOMPLETE_NAME,
    _same_file,
    _write_file,
)
from paw_backend.memory.projection.writer import (
    MARKER_CONTENT as PROJECTION_MARKER_CONTENT,
)
from paw_backend.memory.projection.writer import (
    MARKER_NAME as PROJECTION_MARKER_NAME,
)
from paw_backend.recovery.format import (
    MANAGED_DIRECTORIES,
    MANAGED_ROOT_NAMES,
    MARKER_CONTENT,
    MARKER_NAME,
)

LOCK_NAME = "paw-recovery.lock"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
# A file of the checkout or of the projection larger than this is refused (a
# memory file is at most ~1 MB of text plus its front matter).
MAX_FILE_BYTES = 64 * 1024 * 1024


class CheckoutProblem(StrEnum):
    """Why a directory was refused (a closed vocabulary; never a path)."""

    NOT_ABSOLUTE = "not_absolute"
    NOT_CANONICAL = "not_canonical"
    FILESYSTEM_ROOT = "filesystem_root"
    NOT_A_DIRECTORY = "not_a_directory"
    NOT_OWNED = "not_owned"
    OVERLAPS_HOME = "overlaps_home"
    OVERLAPS_PROJECTION = "overlaps_projection"
    NOT_A_GIT_CHECKOUT = "not_a_git_checkout"
    NOT_RECOVERY_REPOSITORY = "not_recovery_repository"
    MARKER_INVALID = "marker_invalid"
    UNSAFE_ENTRY = "unsafe_entry"
    FILE_TOO_LARGE = "file_too_large"
    PROJECTION_MISSING = "projection_missing"
    PROJECTION_INCOMPLETE = "projection_incomplete"
    PROJECTION_BUSY = "projection_busy"


class RecoveryFilesError(Exception):
    """A directory or a file cannot be used (see ``problem``). No path in the text."""

    def __init__(self, problem: CheckoutProblem) -> None:
        self.problem = problem
        super().__init__(f"recovery files refused: {problem.value}")


class RecoveryBusyError(Exception):
    """Another backup or restore holds the checkout's lock. Nothing was done."""

    def __init__(self) -> None:
        super().__init__("another recovery backup or restore is in progress")


def _within(path: str, other: str) -> bool:
    return path == other or path.startswith(other.rstrip("/") + "/")


def check_directory_path(
    path: str | Path,
    protected: Collection[str],
    *,
    projection_dir: str | Path | None = None,
) -> str:
    """The canonical text of the existing directory ``path`` (or refuse)."""
    text = str(path)
    if not os.path.isabs(text):
        raise RecoveryFilesError(CheckoutProblem.NOT_ABSOLUTE)
    if os.path.normpath(text) != text or text.startswith("//"):
        raise RecoveryFilesError(CheckoutProblem.NOT_CANONICAL)
    if text == "/":
        raise RecoveryFilesError(CheckoutProblem.FILESYSTEM_ROOT)
    if os.path.realpath(text) != text:
        raise RecoveryFilesError(CheckoutProblem.NOT_CANONICAL)
    for home in protected:
        home = os.path.normpath(str(home))
        for candidate in {home, os.path.realpath(home)}:
            if _within(text, candidate) or _within(candidate, text):
                raise RecoveryFilesError(CheckoutProblem.OVERLAPS_HOME)
    if projection_dir is not None:
        other = os.path.normpath(str(projection_dir))
        for candidate in {other, os.path.realpath(other)}:
            if _within(text, candidate) or _within(candidate, text):
                raise RecoveryFilesError(CheckoutProblem.OVERLAPS_PROJECTION)
    if not os.path.isdir(text):
        raise RecoveryFilesError(CheckoutProblem.NOT_A_DIRECTORY)
    return text


def _lstat(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None


def _open_directory_fd(parent_fd: int | None, name: str) -> int:
    try:
        if parent_fd is None:
            return os.open(name, _DIRECTORY_FLAGS)
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RecoveryFilesError(CheckoutProblem.UNSAFE_ENTRY) from None
        raise


def _read_regular(dir_fd: int, name: str) -> bytes:
    """A regular file below ``dir_fd``, never through a link."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO):
            raise RecoveryFilesError(CheckoutProblem.UNSAFE_ENTRY) from None
        raise
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode):
            raise RecoveryFilesError(CheckoutProblem.UNSAFE_ENTRY)
        if status.st_size > MAX_FILE_BYTES:
            raise RecoveryFilesError(CheckoutProblem.FILE_TOO_LARGE)
        chunks = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_BYTES:
            raise RecoveryFilesError(CheckoutProblem.FILE_TOO_LARGE)
        return data
    finally:
        os.close(fd)


def _remove_tree(parent_fd: int, name: str) -> int:
    """Remove ``name`` below ``parent_fd`` (a link is removed, never followed).

    Returns how many files were removed."""
    entry = _lstat(name, parent_fd)
    if entry is None:
        return 0
    if not stat.S_ISDIR(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return 1
    if entry.st_uid != os.geteuid():
        raise RecoveryFilesError(CheckoutProblem.UNSAFE_ENTRY)
    fd = _open_directory_fd(parent_fd, name)
    removed = 0
    try:
        for child in os.listdir(fd):
            removed += _remove_tree(fd, child)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)
    return removed


class RecoveryCheckout:
    """An opened, verified checkout whose lock this process holds."""

    def __init__(self, path: str, root_fd: int, lock_fd: int) -> None:
        self.path = path
        self._root_fd = root_fd
        self._lock_fd = lock_fd
        self._closed = False

    def close(self) -> None:
        """Release the lock and close the directory (idempotent)."""
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._lock_fd)
        finally:
            os.close(self._root_fd)

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("the recovery checkout is closed")

    def read_file(self, path: str) -> bytes | None:
        """A file of the checkout by its relative path (``None`` when missing)."""
        self._check_open()
        parts = path.split("/")
        fd = self._root_fd
        opened: list[int] = []
        try:
            for part in parts[:-1]:
                entry = _lstat(part, fd)
                if entry is None:
                    return None
                fd = _open_directory_fd(fd, part)
                opened.append(fd)
            if _lstat(parts[-1], fd) is None:
                return None
            return _read_regular(fd, parts[-1])
        finally:
            for descriptor in opened:
                os.close(descriptor)

    def read_managed(self) -> dict[str, bytes]:
        """Every file below the managed names (relative path -> bytes).

        A link or a special file anywhere in a managed tree is refused
        (``unsafe_entry``): a restore reads only regular files."""
        self._check_open()
        files: dict[str, bytes] = {}
        for name in MANAGED_ROOT_NAMES:
            entry = _lstat(name, self._root_fd)
            if entry is None:
                continue
            if stat.S_ISDIR(entry.st_mode):
                fd = _open_directory_fd(self._root_fd, name)
                try:
                    _read_tree(fd, name, files)
                finally:
                    os.close(fd)
            else:
                files[name] = _read_regular(self._root_fd, name)
        return files

    def sync(self, files: Mapping[str, bytes]) -> tuple[int, int]:
        """Make the managed names hold exactly ``files``. Returns (written, removed).

        The marker is never rewritten (it is checked when the checkout is
        opened); every other managed name is replaced or removed."""
        self._check_open()
        tree: dict[str, object] = {}
        for path, data in files.items():
            if path == MARKER_NAME:
                continue
            parts = path.split("/")
            if parts[0] not in MANAGED_ROOT_NAMES:
                raise ValueError("not a managed path")
            node = tree
            for part in parts[:-1]:
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ValueError("a path is both a file and a directory")
                node = child
            if parts[-1] in node:
                raise ValueError("a path is both a file and a directory")
            node[parts[-1]] = data
        counts = [0, 0]
        for name in MANAGED_ROOT_NAMES:
            if name == MARKER_NAME:
                continue
            _sync_entry(self._root_fd, name, tree.get(name), counts)
        os.fsync(self._root_fd)
        return counts[0], counts[1]


def _read_tree(fd: int, prefix: str, files: dict[str, bytes]) -> None:
    for name in sorted(os.listdir(fd)):
        entry = _lstat(name, fd)
        if entry is None:
            continue
        if stat.S_ISDIR(entry.st_mode):
            child = _open_directory_fd(fd, name)
            try:
                _read_tree(child, f"{prefix}/{name}", files)
            finally:
                os.close(child)
        elif stat.S_ISREG(entry.st_mode):
            files[f"{prefix}/{name}"] = _read_regular(fd, name)
        else:
            raise RecoveryFilesError(CheckoutProblem.UNSAFE_ENTRY)


def _sync_entry(parent_fd: int, name: str, wanted: object, counts: list[int]) -> None:
    entry = _lstat(name, parent_fd)
    if wanted is None:
        if entry is not None:
            counts[1] += _remove_tree(parent_fd, name)
        return
    if isinstance(wanted, bytes):
        if entry is not None and stat.S_ISDIR(entry.st_mode):
            counts[1] += _remove_tree(parent_fd, name)
        if _same_file(parent_fd, name, wanted):
            return
        _write_file(parent_fd, name, wanted)
        counts[0] += 1
        return
    assert isinstance(wanted, dict)
    if entry is not None and not stat.S_ISDIR(entry.st_mode):
        os.unlink(name, dir_fd=parent_fd)  # a file or a link where a directory goes
        counts[1] += 1
    try:
        os.mkdir(name, DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = _open_directory_fd(parent_fd, name)
    try:
        status = os.fstat(fd)
        if status.st_uid != os.geteuid():
            raise RecoveryFilesError(CheckoutProblem.UNSAFE_ENTRY)
        if stat.S_IMODE(status.st_mode) != DIRECTORY_MODE:
            os.fchmod(fd, DIRECTORY_MODE)
        for child in sorted(set(os.listdir(fd)) | set(wanted)):
            _sync_entry(fd, child, wanted.get(child), counts)
        os.fsync(fd)
    finally:
        os.close(fd)


def _check_marker(root_fd: int) -> bool:
    """``True`` when the marker is there and valid, ``False`` when it is missing."""
    try:
        fd = os.open(MARKER_NAME, _READ_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        return False
    except OSError:
        raise RecoveryFilesError(CheckoutProblem.MARKER_INVALID) from None
    try:
        status = os.fstat(fd)
        content = os.read(fd, len(MARKER_CONTENT) + 1)
        if not stat.S_ISREG(status.st_mode) or content != MARKER_CONTENT:
            raise RecoveryFilesError(CheckoutProblem.MARKER_INVALID)
    finally:
        os.close(fd)
    return True


def open_checkout(
    path: str | Path,
    protected: Collection[str],
    *,
    projection_dir: str | Path | None = None,
    claim: Callable[[], bool] | None = None,
) -> RecoveryCheckout:
    """Verify the checkout's directory, take its lock; with ``claim`` write the
    marker into a checkout that has nothing but ``.git`` and no history.

    ``claim`` is called (under the lock) only when the marker is missing and
    the work tree has nothing but ``.git``; it says whether the repository is
    an empty clone (no commit, no ref: ``RecoveryGit.has_no_history``). A
    project's clone made with ``--no-checkout``, or one on a new orphan branch,
    has only ``.git`` in its work tree too, but a history: it is refused.

    Raises ``RecoveryFilesError`` or ``RecoveryBusyError``. That the directory is
    the top of a git work tree is ``git.py``'s check (made before this)."""
    text = check_directory_path(path, protected, projection_dir=projection_dir)
    root_fd = _open_directory_fd(None, text)
    try:
        if os.fstat(root_fd).st_uid != os.geteuid():
            raise RecoveryFilesError(CheckoutProblem.NOT_OWNED)
        git = _lstat(".git", root_fd)
        if git is None or stat.S_ISLNK(git.st_mode):
            raise RecoveryFilesError(CheckoutProblem.NOT_A_GIT_CHECKOUT)
        if not stat.S_ISDIR(git.st_mode):
            # A linked worktree or a submodule (``.git`` file): not a clone of
            # its own, whose ``.git`` the lock file could live in.
            raise RecoveryFilesError(CheckoutProblem.NOT_A_GIT_CHECKOUT)
        git_fd = _open_directory_fd(root_fd, ".git")
        try:
            lock_fd = os.open(LOCK_NAME, _LOCK_FLAGS, FILE_MODE, dir_fd=git_fd)
        finally:
            os.close(git_fd)
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoveryBusyError() from None
            if not _check_marker(root_fd):
                others = [name for name in os.listdir(root_fd) if name != ".git"]
                if claim is None or others or not claim():
                    raise RecoveryFilesError(CheckoutProblem.NOT_RECOVERY_REPOSITORY)
                _write_file(root_fd, MARKER_NAME, MARKER_CONTENT)
                os.fsync(root_fd)
            if stat.S_IMODE(os.fstat(root_fd).st_mode) != DIRECTORY_MODE:
                os.fchmod(root_fd, DIRECTORY_MODE)
        except BaseException:
            os.close(lock_fd)
            raise
    except BaseException:
        os.close(root_fd)
        raise
    return RecoveryCheckout(text, root_fd, lock_fd)


# -- the Memory Projection ------------------------------------------------------


class ProjectionReader:
    """The projection's directory, opened and locked (Decision 0038 9).

    ``read`` refuses a tree with the incomplete flag and copies only the names
    the projection writes; ``close`` releases the lock."""

    def __init__(self, root_fd: int, marker_fd: int) -> None:
        self._root_fd = root_fd
        self._marker_fd = marker_fd
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._marker_fd)
        finally:
            os.close(self._root_fd)

    def read(self) -> dict[str, bytes]:
        """Every memory and index file, by its path below the projection's root."""
        if self._closed:
            raise RuntimeError("the projection reader is closed")
        if _lstat(INCOMPLETE_NAME, self._root_fd) is not None:
            raise RecoveryFilesError(CheckoutProblem.PROJECTION_INCOMPLETE)
        files: dict[str, bytes] = {}
        for top in sorted(TOP_DIRECTORIES.values()):
            entry = _lstat(top, self._root_fd)
            if entry is None or not stat.S_ISDIR(entry.st_mode):
                continue
            fd = _open_directory_fd(self._root_fd, top)
            try:
                if top == SHARED_DIRECTORY:
                    _read_leaf(fd, top, files)
                    continue
                assert top in KEYED_TOP_DIRECTORIES
                for name in sorted(os.listdir(fd)):
                    child_entry = _lstat(name, fd)
                    if (
                        not is_uuid_name(name)
                        or child_entry is None
                        or not stat.S_ISDIR(child_entry.st_mode)
                    ):
                        continue
                    child = _open_directory_fd(fd, name)
                    try:
                        _read_leaf(child, f"{top}/{name}", files)
                    finally:
                        os.close(child)
            finally:
                os.close(fd)
        return files


def _read_leaf(fd: int, prefix: str, files: dict[str, bytes]) -> None:
    for name in sorted(os.listdir(fd)):
        if name != INDEX_FILE and not is_memory_file_name(name):
            continue  # unmanaged, or the writer's temporary file: never copied
        entry = _lstat(name, fd)
        if entry is None or not stat.S_ISREG(entry.st_mode):
            continue
        files[f"{prefix}/{name}"] = _read_regular(fd, name)


def open_projection(path: str | Path) -> ProjectionReader:
    """Open the projection's root and take its marker's lock (``projection_busy``
    at once while a projection run holds it: the caller waits and retries)."""
    text = str(path)
    if not os.path.isabs(text) or os.path.normpath(text) != text:
        raise RecoveryFilesError(CheckoutProblem.NOT_CANONICAL)
    try:
        root_fd = _open_directory_fd(None, text)
    except FileNotFoundError:
        raise RecoveryFilesError(CheckoutProblem.PROJECTION_MISSING) from None
    try:
        if os.fstat(root_fd).st_uid != os.geteuid():
            raise RecoveryFilesError(CheckoutProblem.NOT_OWNED)
        try:
            marker_fd = os.open(PROJECTION_MARKER_NAME, _READ_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            raise RecoveryFilesError(CheckoutProblem.PROJECTION_MISSING) from None
        except OSError:
            raise RecoveryFilesError(CheckoutProblem.MARKER_INVALID) from None
        try:
            status = os.fstat(marker_fd)
            content = os.read(marker_fd, len(PROJECTION_MARKER_CONTENT) + 1)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or content != PROJECTION_MARKER_CONTENT
            ):
                raise RecoveryFilesError(CheckoutProblem.MARKER_INVALID)
            try:
                fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RecoveryFilesError(CheckoutProblem.PROJECTION_BUSY) from None
        except BaseException:
            os.close(marker_fd)
            raise
    except BaseException:
        os.close(root_fd)
        raise
    return ProjectionReader(root_fd, marker_fd)


__all__ = [
    "LOCK_NAME",
    "MANAGED_DIRECTORIES",
    "MAX_FILE_BYTES",
    "CheckoutProblem",
    "ProjectionReader",
    "RecoveryBusyError",
    "RecoveryCheckout",
    "RecoveryFilesError",
    "check_directory_path",
    "open_checkout",
    "open_projection",
]
