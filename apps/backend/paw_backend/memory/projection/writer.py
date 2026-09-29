"""Writing the Memory Projection to its directory (PAW-045, Decision 0038).

The projection directory (``PAW_MEMORY_PROJECTION_DIR``, on the HDD in the
deployment) holds **every user's private memory**, so this module is careful about
where it writes and who can read what it wrote:

* **Where.** The root must be an absolute, canonical path (no symbolic link, no
  ``..``), not ``/``, not inside a git work tree (no ``.git`` in it or in any
  directory above it, as git would find it: the projection is never written into
  a repository checkout,
  REQUIREMENTS.md "Project Repoへ勝手にMarkdownをcommitしない"), and neither inside
  nor above a person's home directory (where every checkout lives). A root that
  exists must be a directory owned by this process's user.
* **Whose directory.** The first run puts a marker file
  (``.paw-memory-projection``) into an **empty** (or new) root. A non-empty root
  without the marker is refused: a misconfigured path never gets memories written
  into, or files deleted from, a directory that is not the projection's.
* **Who can read.** The root and every directory are ``0700``, every file
  ``0600``, owned by this process's user (the user the job runs as). Group and
  other users of the server cannot list or read any of it; the modes are set with
  ``fchmod`` on the opened descriptor, whatever the umask.
* **No link is followed.** Every directory is opened with ``O_NOFOLLOW`` relative
  to its already-opened parent (``dir_fd``), so a symbolic link placed in the tree
  cannot redirect a write elsewhere; one found where a directory of the projection
  belongs fails the run (``unsafe_entry``). Files are never written in place: a new
  file is written to a temporary name (``O_EXCL``), ``fsync``-ed and renamed over
  the old name, which replaces a link or hard link instead of writing through it.
* **Only its own files.** Only names the renderer can produce are replaced or
  deleted (``<uuid>.md``, ``INDEX.md``, the top directories, ``<uuid>``
  directories, the writer's own temporary files: ``.tmp-`` and exactly 16 hex
  digits). Anything else is counted as
  ``unmanaged`` and left alone, never read.
* **One run at a time.** ``open_target`` takes an exclusive ``flock`` on the
  marker (non-blocking): a second run gets ``ProjectionBusyError`` and does
  nothing. The runner holds it from before the database read to after the write,
  so an older snapshot can never overwrite a newer one.

* **Checked before changing.** ``sync`` first checks every directory and file
  name the plan needs (no link, no file where a directory belongs, no directory
  where a file belongs, owned by this user, no git repository (``.git``) in the
  root or in any managed directory), so a tree the run could not finish, or a
  checkout made inside it, fails before anything is written or deleted.
* **An unfinished write is visible.** Before its first change ``sync`` writes
  ``.paw-memory-projection-incomplete`` into the root; only ``mark_complete``
  (called by the runner after the ``completed`` outcome is recorded) removes it.
  A write that fails midway (a full disk, a kill) or whose outcome could not be
  recorded leaves the flag, so an older ``completed`` row never vouches for a
  mixed tree: a reader (PAW-047) copies only with the lock held, the flag absent
  and the last run ``completed`` (Decision 0038 9).

Unchanged files are not rewritten (their modification time stays), so a run
without changes rewrites no file of the projection and a Git commit of the
directory shows only what changed. Everything here blocks: call it with
``asyncio.to_thread``.
"""

import errno
import fcntl
import os
import re
import secrets
import stat
from collections.abc import Collection, Iterable, Mapping
from enum import StrEnum
from pathlib import Path

from paw_backend.memory.projection.records import (
    DirectoryKey,
    ProjectionPlan,
    WriteReport,
)
from paw_backend.memory.projection.render import (
    FORMAT_VERSION,
    INDEX_FILE,
    KEYED_TOP_DIRECTORIES,
    SHARED_DIRECTORY,
    TOP_DIRECTORIES,
    is_memory_file_name,
    is_uuid_name,
)

MARKER_NAME = ".paw-memory-projection"
INCOMPLETE_NAME = ".paw-memory-projection-incomplete"
MARKER_CONTENT = (
    f"Personal AI Workspace memory projection, format {FORMAT_VERSION}.\n"
    "Generated from PostgreSQL; do not edit. See Decision 0038.\n"
).encode()
INCOMPLETE_CONTENT = (
    b"A write of the projection did not finish or was not recorded; do not copy.\n"
)
_ROOT_FILES = frozenset({MARKER_NAME, INCOMPLETE_NAME})
TEMPORARY_PREFIX = ".tmp-"
_TEMPORARY_HEX_BYTES = 8
_TEMPORARY_NAME = re.compile(
    re.escape(TEMPORARY_PREFIX) + f"[0-9a-f]{{{2 * _TEMPORARY_HEX_BYTES}}}"
)
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_ALL_TOP_DIRECTORIES = tuple(sorted(TOP_DIRECTORIES.values()))


class TargetProblem(StrEnum):
    """Why a projection directory was refused (a closed vocabulary)."""

    NOT_ABSOLUTE = "not_absolute"
    NOT_CANONICAL = "not_canonical"
    FILESYSTEM_ROOT = "filesystem_root"
    PARENT_MISSING = "parent_missing"
    NOT_A_DIRECTORY = "not_a_directory"
    NOT_OWNED = "not_owned"
    INSIDE_GIT_WORK_TREE = "inside_git_work_tree"
    OVERLAPS_HOME = "overlaps_home"
    NOT_EMPTY = "not_empty"
    MARKER_INVALID = "marker_invalid"
    UNSAFE_ENTRY = "unsafe_entry"


class ProjectionTargetError(Exception):
    """The directory cannot be used (see ``problem``). The text names no path."""

    def __init__(self, problem: TargetProblem) -> None:
        self.problem = problem
        super().__init__(f"projection directory refused: {problem.value}")


class ProjectionBusyError(Exception):
    """Another run holds the projection's lock. Nothing was done."""

    def __init__(self) -> None:
        super().__init__("another memory projection run is in progress")


def system_home_directories(min_uid: int = 1000) -> tuple[str, ...]:
    """``/home`` and the home directory of every account with ``uid >= min_uid``."""
    import pwd

    homes = {"/home"}
    for entry in pwd.getpwall():
        if entry.pw_uid >= min_uid and os.path.isabs(entry.pw_dir):
            homes.add(os.path.normpath(entry.pw_dir))
    return tuple(sorted(homes))


def _canonical_homes(homes: Iterable[str]) -> list[str]:
    result = []
    for home in homes:
        home = os.path.normpath(str(home))
        result.append(home)
        real = os.path.realpath(home)
        if real != home:
            result.append(real)
    return result


def _within(path: str, other: str) -> bool:
    return path == other or path.startswith(other.rstrip("/") + "/")


def _is_git_entry(path: str) -> bool:
    """``path`` (a ``.../.git``) is what git takes for a repository.

    As git's own discovery: a ``.git`` file (a linked worktree or submodule:
    ``gitdir: ...``), a ``.git`` symbolic link (git follows it; refused whatever
    it points to), or a ``.git`` directory with a ``HEAD``. An empty ``.git``
    directory is not a repository to git (``not a git repository``) and does not
    count, so a stray one in ``/tmp`` does not block every path below it.
    """
    return _is_git_entry_at(path, None)


def _is_git_entry_at(path: str, dir_fd: int | None) -> bool:
    """``_is_git_entry`` of ``path`` relative to ``dir_fd`` (``None``: absolute)."""
    try:
        status = os.lstat(path, dir_fd=dir_fd)
    except (FileNotFoundError, NotADirectoryError):
        return False  # nothing there, or the directory itself is a file
    except OSError:
        return True  # cannot tell: refuse
    if not stat.S_ISDIR(status.st_mode):
        return True
    try:
        os.lstat(os.path.join(path, "HEAD"), dir_fd=dir_fd)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True  # cannot tell: refuse
    return True


def _refuse_git(fd: int) -> None:
    """Refuse a directory of the projection that holds a git repository.

    ``check_root_path`` looks at the root and above when the target is opened; a
    ``.git`` made later in the root or in any managed directory would make
    ``sync`` write the projection into that checkout."""
    if _is_git_entry_at(".git", fd):
        raise ProjectionTargetError(TargetProblem.INSIDE_GIT_WORK_TREE)


def check_root_path(root: str | Path, protected: Collection[str]) -> str:
    """The canonical text of ``root``, or ``ProjectionTargetError`` (no side effect)."""
    path = str(root)
    if not os.path.isabs(path):
        raise ProjectionTargetError(TargetProblem.NOT_ABSOLUTE)
    if os.path.normpath(path) != path or path.startswith("//"):
        raise ProjectionTargetError(TargetProblem.NOT_CANONICAL)
    if path == "/":
        raise ProjectionTargetError(TargetProblem.FILESYSTEM_ROOT)
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise ProjectionTargetError(TargetProblem.PARENT_MISSING)
    if os.path.realpath(parent) != parent:
        raise ProjectionTargetError(TargetProblem.NOT_CANONICAL)
    if os.path.lexists(path) and os.path.islink(path):
        raise ProjectionTargetError(TargetProblem.NOT_CANONICAL)
    for home in _canonical_homes(protected):
        if _within(path, home) or _within(home, path):
            raise ProjectionTargetError(TargetProblem.OVERLAPS_HOME)
    directory = path
    while True:
        if _is_git_entry(os.path.join(directory, ".git")):
            raise ProjectionTargetError(TargetProblem.INSIDE_GIT_WORK_TREE)
        if directory == "/":
            break
        directory = os.path.dirname(directory)
    return path


def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
    """Open (and with ``create``, make) one directory below ``parent_fd``, ``0700``."""
    if create:
        try:
            os.mkdir(name, DIRECTORY_MODE, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ProjectionTargetError(TargetProblem.UNSAFE_ENTRY) from None
        raise
    try:
        _own_directory(fd, TargetProblem.UNSAFE_ENTRY)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_existing(parent_fd: int, name: str, *, required: bool) -> int | None:
    """Open an existing directory of the projection without changing it.

    ``None`` when it is missing, or is not a directory and not ``required`` (left
    as unmanaged by ``sync``); ``unsafe_entry`` when a ``required`` one is not a
    directory, or when it is a directory of someone else. A directory this user
    cannot read raises the ``OSError`` that ``sync`` would meet."""
    entry = _lstat(name, parent_fd)
    if entry is None:
        return None
    if not stat.S_ISDIR(entry.st_mode):
        if required:
            raise ProjectionTargetError(TargetProblem.UNSAFE_ENTRY)
        return None
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ProjectionTargetError(TargetProblem.UNSAFE_ENTRY) from None
        raise
    try:
        _check_owned(fd, TargetProblem.UNSAFE_ENTRY)
        os.listdir(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _preflight_leaf(fd: int, files: Iterable[str]) -> None:
    for name in files:
        entry = _lstat(name, fd)
        if entry is not None and stat.S_ISDIR(entry.st_mode):
            raise ProjectionTargetError(TargetProblem.UNSAFE_ENTRY)


def _check_owned(fd: int, problem: TargetProblem) -> None:
    if os.fstat(fd).st_uid != os.geteuid():
        raise ProjectionTargetError(problem)


def _tighten(fd: int) -> None:
    if stat.S_IMODE(os.fstat(fd).st_mode) != DIRECTORY_MODE:
        os.fchmod(fd, DIRECTORY_MODE)


def _own_directory(fd: int, problem: TargetProblem) -> None:
    _check_owned(fd, problem)
    _tighten(fd)


def _lstat(name: str, dir_fd: int) -> os.stat_result | None:
    try:
        return os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return None


def _same_file(dir_fd: int, name: str, data: bytes) -> bool:
    """``name`` is our regular ``0600`` file and holds exactly ``data``."""
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError:
        return False  # missing, a link (ELOOP), or unreadable: replace it
    try:
        status = os.fstat(fd)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != FILE_MODE
            or status.st_nlink != 1
            or status.st_size != len(data)
        ):
            return False
        chunks = []
        remaining = len(data) + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks) == data
    finally:
        os.close(fd)


def _write_file(dir_fd: int, name: str, data: bytes) -> None:
    """Write ``data`` to a temporary name and rename it over ``name`` (atomic)."""
    temporary = f"{TEMPORARY_PREFIX}{secrets.token_hex(_TEMPORARY_HEX_BYTES)}"
    fd = os.open(temporary, _CREATE_FLAGS, FILE_MODE, dir_fd=dir_fd)
    try:
        try:
            os.fchmod(fd, FILE_MODE)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(temporary, dir_fd=dir_fd)
        except OSError:
            pass
        raise


class LockedTarget:
    """An opened, verified projection directory whose lock this process holds."""

    def __init__(self, root_fd: int, marker_fd: int) -> None:
        self._root_fd = root_fd
        self._marker_fd = marker_fd
        self._closed = False

    def close(self) -> None:
        """Release the lock and close the directory (idempotent)."""
        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._marker_fd)  # releases the flock
        finally:
            os.close(self._root_fd)

    def sync(self, plan: ProjectionPlan) -> WriteReport:
        """Make the directory hold exactly ``plan`` (plus unmanaged entries)."""
        if self._closed:
            raise RuntimeError("the projection target is closed")
        counts = {"written": 0, "unchanged": 0, "removed": 0, "unmanaged": 0}
        wanted_tops: dict[str, dict[str, Mapping[str, bytes]]] = {}
        for key, files in plan.directories.items():
            _check_key(key)
            if key == (SHARED_DIRECTORY,):
                wanted_tops.setdefault(SHARED_DIRECTORY, {})[""] = files
            else:
                wanted_tops.setdefault(key[0], {})[key[1]] = files
        self._preflight(wanted_tops)
        _write_file(self._root_fd, INCOMPLETE_NAME, INCOMPLETE_CONTENT)
        # The flag must survive a power loss that keeps any later change.
        os.fsync(self._root_fd)
        for top in _ALL_TOP_DIRECTORIES:
            self._sync_top(top, wanted_tops.get(top), counts)
        for name in os.listdir(self._root_fd):
            if name not in _ROOT_FILES and name not in _ALL_TOP_DIRECTORIES:
                counts["unmanaged"] += 1
        os.fsync(self._root_fd)
        return WriteReport(**counts)

    def mark_complete(self) -> None:
        """Remove the incomplete flag: the write finished and was recorded."""
        if self._closed:
            raise RuntimeError("the projection target is closed")
        try:
            os.unlink(INCOMPLETE_NAME, dir_fd=self._root_fd)
        except FileNotFoundError:
            pass
        os.fsync(self._root_fd)

    def _preflight(self, wanted_tops: Mapping[str, Mapping[str, object]]) -> None:
        """Refuse, before any change, a tree ``sync`` could not finish.

        Visits every directory ``sync`` will open: the wanted ones and the
        existing managed ones it would clean up (a top directory, a ``<uuid>``
        directory), exactly as ``_sync_top`` / ``_sync_keyed`` decide, and
        refuses a git repository (``.git``) in the root or in any of them."""
        _refuse_git(self._root_fd)
        for top in _ALL_TOP_DIRECTORIES:
            wanted = wanted_tops.get(top)
            fd = _open_existing(self._root_fd, top, required=wanted is not None)
            if fd is None:
                continue
            try:
                _refuse_git(fd)
                if top == SHARED_DIRECTORY:
                    _preflight_leaf(fd, (wanted or {}).get("", {}))
                    continue
                wanted = wanted or {}
                for name in sorted(set(os.listdir(fd)) | wanted.keys()):
                    if not is_uuid_name(name):
                        continue
                    child = _open_existing(fd, name, required=name in wanted)
                    if child is None:
                        continue
                    try:
                        _refuse_git(child)
                        _preflight_leaf(child, wanted.get(name, {}))
                    finally:
                        os.close(child)
            finally:
                os.close(fd)

    def _sync_top(
        self,
        top: str,
        wanted: dict[str, Mapping[str, bytes]] | None,
        counts: dict[str, int],
    ) -> None:
        entry = _lstat(top, self._root_fd)
        if entry is None and wanted is None:
            return
        if entry is not None and not stat.S_ISDIR(entry.st_mode):
            if wanted is not None:
                raise ProjectionTargetError(TargetProblem.UNSAFE_ENTRY)
            return  # counted as unmanaged by ``sync``
        fd = _open_directory(self._root_fd, top, create=wanted is not None)
        try:
            if top == SHARED_DIRECTORY:
                _sync_leaf(fd, (wanted or {}).get("", {}), counts)
            else:
                self._sync_keyed(fd, wanted or {}, counts)
            os.fsync(fd)
        finally:
            os.close(fd)
        if wanted is None:
            _remove_if_empty(self._root_fd, top)

    @staticmethod
    def _sync_keyed(
        fd: int, wanted: dict[str, Mapping[str, bytes]], counts: dict[str, int]
    ) -> None:
        for name in sorted(set(os.listdir(fd)) | wanted.keys()):
            if not is_uuid_name(name):
                counts["unmanaged"] += 1
                continue
            entry = _lstat(name, fd)
            if entry is not None and not stat.S_ISDIR(entry.st_mode):
                if name in wanted:
                    raise ProjectionTargetError(TargetProblem.UNSAFE_ENTRY)
                counts["unmanaged"] += 1
                continue
            child = _open_directory(fd, name, create=name in wanted)
            try:
                _sync_leaf(child, wanted.get(name, {}), counts)
                os.fsync(child)
            finally:
                os.close(child)
            if name not in wanted:
                _remove_if_empty(fd, name)


def _check_key(key: DirectoryKey) -> None:
    if key == (SHARED_DIRECTORY,):
        return
    if len(key) != 2 or key[0] not in KEYED_TOP_DIRECTORIES or not is_uuid_name(key[1]):
        raise ValueError("not a directory of the projection")


def _is_temporary(name: str) -> bool:
    """A name ``_write_file`` makes (``.tmp-`` and 16 hex digits), nothing else."""
    return _TEMPORARY_NAME.fullmatch(name) is not None


def _managed_file(name: str) -> bool:
    return name == INDEX_FILE or is_memory_file_name(name) or _is_temporary(name)


def _sync_leaf(fd: int, files: Mapping[str, bytes], counts: dict[str, int]) -> None:
    for name in sorted(files):
        if not _managed_file(name) or _is_temporary(name):
            raise ValueError("not a file name of the projection")
        if _same_file(fd, name, files[name]):
            counts["unchanged"] += 1
            continue
        entry = _lstat(name, fd)
        if entry is not None and stat.S_ISDIR(entry.st_mode):
            raise ProjectionTargetError(TargetProblem.UNSAFE_ENTRY)
        _write_file(fd, name, files[name])
        counts["written"] += 1
    for name in sorted(os.listdir(fd)):
        if name in files:
            continue
        if not _managed_file(name):
            counts["unmanaged"] += 1
            continue
        entry = _lstat(name, fd)
        if entry is None:
            continue
        if stat.S_ISDIR(entry.st_mode):
            counts["unmanaged"] += 1
            continue
        os.unlink(name, dir_fd=fd)
        if not _is_temporary(name):
            counts["removed"] += 1


def _remove_if_empty(parent_fd: int, name: str) -> None:
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except OSError as error:
        if error.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
            raise


def open_target(root: str | Path, protected: Collection[str]) -> LockedTarget:
    """Verify ``root``, make it if missing, claim it with the marker, take the lock.

    ``protected`` are the home directories the root must neither be in nor
    contain (``system_home_directories()`` in production). Raises
    ``ProjectionTargetError`` or ``ProjectionBusyError``; nothing is written
    unless the root is accepted (then at most the directory and its marker).
    """
    path = check_root_path(root, protected)
    try:
        os.mkdir(path, DIRECTORY_MODE)
    except FileExistsError:
        pass
    try:
        root_fd = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ProjectionTargetError(TargetProblem.NOT_A_DIRECTORY) from None
        raise
    try:
        # The permissions change only once the root is accepted as ours: a
        # refused directory (not empty, a foreign marker) is left as it was.
        _check_owned(root_fd, TargetProblem.NOT_OWNED)
        marker_fd = _open_marker(root_fd)
    except BaseException:
        os.close(root_fd)
        raise
    try:
        _tighten(root_fd)
    except BaseException:
        os.close(marker_fd)
        os.close(root_fd)
        raise
    try:
        fcntl.flock(marker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(marker_fd)
        os.close(root_fd)
        raise ProjectionBusyError() from None
    except BaseException:
        os.close(marker_fd)
        os.close(root_fd)
        raise
    return LockedTarget(root_fd, marker_fd)


def _open_marker(root_fd: int) -> int:
    try:
        fd = os.open(MARKER_NAME, _READ_FLAGS, dir_fd=root_fd)
    except FileNotFoundError:
        if os.listdir(root_fd):
            raise ProjectionTargetError(TargetProblem.NOT_EMPTY) from None
        _write_file(root_fd, MARKER_NAME, MARKER_CONTENT)
        return os.open(MARKER_NAME, _READ_FLAGS, dir_fd=root_fd)
    except OSError:
        raise ProjectionTargetError(TargetProblem.MARKER_INVALID) from None
    try:
        status = os.fstat(fd)
        content = os.read(fd, len(MARKER_CONTENT) + 1)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_uid != os.geteuid()
            or content != MARKER_CONTENT
        ):
            raise ProjectionTargetError(TargetProblem.MARKER_INVALID)
    except BaseException:
        os.close(fd)
        raise
    return fd


__all__ = [
    "DIRECTORY_MODE",
    "FILE_MODE",
    "INCOMPLETE_NAME",
    "MARKER_CONTENT",
    "MARKER_NAME",
    "LockedTarget",
    "ProjectionBusyError",
    "ProjectionTargetError",
    "TargetProblem",
    "check_root_path",
    "open_target",
    "system_home_directories",
]
