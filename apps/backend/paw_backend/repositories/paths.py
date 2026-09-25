"""Path safety: where a repository may live, and whether a directory can be trusted.

Everything here answers one question about the **acting user's own Linux
account** (:class:`LinuxAccount`): may this directory be used as that user's
checkout? Nothing here reads a path a caller wrote before it is proven to be
lexically safe, and no function follows a symbolic link it was not told about.

Two kinds of path
-----------------
* A **managed checkout** is created by the backend under
  ``<home>/<workspace_subdir>/<project directory>/<repository name>``
  (``REQUIREMENTS.md``: ``/home/<user>/workspaces/<project>/<repo>``). It is built
  from the real path of the home, every directory on the way is opened with
  ``O_NOFOLLOW`` from that home (a symbolic link put there is refused, and there
  is no window between "check" and "use" in which one can be swapped in), and the
  final directory is created with ``mkdir`` (an existing one is refused, never
  reused).
* An **existing repository** the user names. It must be given in the canonical
  spelling, resolved (no symbolic link in it anywhere), strictly below a root of
  the user's own (the home by default), with no hidden component below that root,
  owned by the user's Linux account, not writable by everybody, with a real
  ``.git`` directory that is theirs, and none of the ``.git`` tricks that make git
  read another directory (a ``.git`` file, an ``alternates`` file, a
  ``commondir``, a symbolic link for ``HEAD`` / ``config`` / ``objects``).
  ``GitClient.inspect`` then asks git itself where the work tree and git
  directory are and compares.

What this cannot do
-------------------
A directory the user owns can be changed by the user at any moment, so the answer
is a fact of the instant it was given. The Tool Broker resolves every path again
when a tool is called (``paw_backend.tools.scope``); registration is not an
access control on files. All functions here block on the file system: call them
with ``asyncio.to_thread``.
"""

import os
import re
import shutil
import stat
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from paw_backend.authz.subjects import to_uuid
from paw_backend.repositories.errors import PathProblem, PathRejectedError
from paw_backend.repositories.limits import MAX_PATH_CHARS, MAX_PROJECT_SLUG_CHARS
from paw_backend.tools.scope import TargetError, normalise_path, path_within

_USERNAME = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,63}")
_MAX_UID = 4_294_967_295
_DIRECTORY_MODE = 0o700


@dataclass(frozen=True, slots=True)
class LinuxAccount:
    """The Linux account a workspace user acts as.

    ``home`` is the home directory as the account database gives it (it may
    contain a symbolic link; the module resolves it). ``uid`` is what
    "owned by the right Linux user" means.
    """

    user_id: uuid.UUID
    username: str
    uid: int
    home: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", to_uuid(self.user_id, "user_id"))
        if not isinstance(self.username, str) or not _USERNAME.fullmatch(self.username):
            raise ValueError("username is not a valid Linux user name")
        if (
            isinstance(self.uid, bool)
            or not isinstance(self.uid, int)
            or not 0 <= self.uid <= _MAX_UID
        ):
            raise ValueError("uid is not a valid uid")
        try:
            home = normalise_path(self.home)
        except TargetError:
            raise ValueError("home is not a valid absolute path") from None
        if home == "/" or home != self.home:
            raise ValueError("home must be a canonical absolute path below /")


def expand_root(template: str, account: LinuxAccount) -> str:
    """The root ``template`` means for ``account``: plain replacement, no formatting."""
    root = template.replace("{home}", account.home).replace("{user}", account.username)
    try:
        return normalise_path(root)
    except TargetError:
        raise PathRejectedError(PathProblem.NOT_ABSOLUTE) from None


def project_directory_name(project_name: str, project_id: uuid.UUID) -> str:
    """``<slug>-<8 hex of the id>``: readable, unique, safe as a directory name.

    The slug is the ASCII letters and digits of the name (lower case, runs of
    anything else become one ``-``), at most 40 characters, ``project`` when
    nothing is left (a name in another script, say). The id part makes two
    projects with the same name different directories and is what keeps the name
    unique without a lookup. Renaming a project does not move a directory: the
    path is stored per checkout.
    """
    folded = unicodedata.normalize("NFKD", project_name).encode("ascii", "ignore")
    slug = re.sub(r"[^a-z0-9]+", "-", folded.decode("ascii").lower()).strip("-")
    slug = slug[:MAX_PROJECT_SLUG_CHARS].strip("-") or "project"
    return f"{slug}-{project_id.hex[:8]}"


def _resolve(path: str, problem: PathProblem) -> str:
    try:
        return os.path.realpath(path, strict=True)
    except (OSError, ValueError):
        raise PathRejectedError(problem) from None


def _lstat(path: str, problem: PathProblem = PathProblem.NOT_FOUND) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        raise PathRejectedError(problem) from None
    except OSError:
        raise PathRejectedError(PathProblem.NOT_FOUND) from None


def _require_owned_directory(info: os.stat_result, uid: int) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise PathRejectedError(PathProblem.SYMLINK)
    if not stat.S_ISDIR(info.st_mode):
        raise PathRejectedError(PathProblem.NOT_A_DIRECTORY)
    if info.st_uid != uid:
        raise PathRejectedError(PathProblem.NOT_OWNER)
    if info.st_mode & stat.S_IWOTH:
        raise PathRejectedError(PathProblem.WORLD_WRITABLE)


def real_home(account: LinuxAccount) -> str:
    """The resolved home directory: a directory that is the account's, or refused."""
    home = _resolve(account.home, PathProblem.HOME_UNSAFE)
    if home == "/":
        raise PathRejectedError(PathProblem.HOME_UNSAFE)
    try:
        info = os.lstat(home)
    except OSError:
        raise PathRejectedError(PathProblem.HOME_UNSAFE) from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.uid:
        raise PathRejectedError(PathProblem.HOME_UNSAFE)
    return home


def checkout_root(account: LinuxAccount, workspace_subdir: str) -> str:
    """``<real home>/<workspace_subdir>``: where the backend makes checkouts."""
    return f"{real_home(account)}/{workspace_subdir}"


def plan_checkout_path(
    account: LinuxAccount,
    workspace_subdir: str,
    project_directory: str,
    repository_name: str,
) -> str:
    """The path a new managed checkout will have. Nothing is created.

    Directories that exist already on the way must be real directories of the
    account (a symbolic link or a foreign directory is refused now, before a
    reservation is made; the creation checks again with ``O_NOFOLLOW``).
    """
    root = checkout_root(account, workspace_subdir)
    walked = real_home(account)
    for part in (workspace_subdir, project_directory):
        walked = f"{walked}/{part}"
        try:
            info = os.lstat(walked)
        except FileNotFoundError:
            break
        except OSError:
            raise PathRejectedError(PathProblem.NOT_FOUND) from None
        _require_owned_directory(info, account.uid)
    path = f"{root}/{project_directory}/{repository_name}"
    if len(path) > MAX_PATH_CHARS:
        # The database stores at most this many characters (and 1024 characters
        # never exceed the 4096 bytes of PATH_MAX). A long home can make a valid
        # account generate such a path: refused here, before anything is inserted.
        raise PathRejectedError(PathProblem.TOO_LONG)
    return path


@dataclass(frozen=True, slots=True)
class RootState:
    """What a registered checkout root is *now* (``inspect_checkout_root``).

    ``problem`` is ``None`` when the root is exactly what was registered.
    ``resolved`` is the real path the stored path leads to (``None`` when it leads
    nowhere); ``identity`` is ``(st_dev, st_ino)`` of the entry at the stored path.
    """

    problem: PathProblem | None
    resolved: str | None
    identity: tuple[int, int] | None


def inspect_checkout_root(
    path: str, uid: int, expected: tuple[int, int] | None
) -> RootState:
    """Whether ``path`` is still the directory that was registered as a checkout.

    Checked, in this order: the path leads somewhere (``NOT_FOUND``); it is its own
    real path, no symbolic link in it anywhere (``SYMLINK``); the entry is a
    directory (``NOT_A_DIRECTORY``) of the account (``NOT_OWNER``); and, when
    ``expected`` is given, it is the same directory (``st_dev``, ``st_ino``) that was
    recorded when the checkout became ready (``CHANGED``). This is what the Tool
    Broker will resolve, so a scope must be derived from this, not from the stored
    text. A fact of the instant it is read (the Broker still resolves again when a
    tool runs).
    """
    try:
        resolved = os.path.realpath(path, strict=True)
        info = os.lstat(path)
    except (OSError, ValueError):
        return RootState(PathProblem.NOT_FOUND, None, None)
    identity = (info.st_dev, info.st_ino)
    problem: PathProblem | None = None
    if resolved != path:
        problem = PathProblem.SYMLINK
    elif not stat.S_ISDIR(info.st_mode):
        problem = PathProblem.NOT_A_DIRECTORY
    elif info.st_uid != uid:
        problem = PathProblem.NOT_OWNER
    elif expected is not None and identity != expected:
        problem = PathProblem.CHANGED
    return RootState(problem, resolved, identity)


def read_checkout_identity(path: str, account: LinuxAccount) -> tuple[int, int]:
    """``(st_dev, st_ino)`` of a checkout root that is being registered.

    The root must already be what a checkout root has to be (resolved, a directory
    of the account); otherwise ``PathRejectedError``. The pair is recorded with the
    checkout and compared by every later ``inspect_checkout_root``.
    """
    state = inspect_checkout_root(path, account.uid, None)
    if state.problem is not None or state.identity is None:
        raise PathRejectedError(state.problem or PathProblem.NOT_FOUND)
    return state.identity


def _open_directory(name: str, dir_fd: int, uid: int) -> int:
    """Open ``name`` below ``dir_fd`` without following links; it must be the user's."""
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=dir_fd,
        )
    except OSError:
        # ELOOP (a link), ENOTDIR (a file) and the rest: never follow, never guess.
        raise PathRejectedError(PathProblem.SYMLINK) from None
    try:
        info = os.fstat(fd)
        if info.st_uid != uid:
            raise PathRejectedError(PathProblem.NOT_OWNER)
        if info.st_mode & stat.S_IWOTH:
            raise PathRejectedError(PathProblem.WORLD_WRITABLE)
    except BaseException:
        os.close(fd)
        raise
    return fd


def create_checkout_directory(path: str, account: LinuxAccount) -> None:
    """Create the empty directory ``path`` (from :func:`plan_checkout_path`).

    Every directory from the home down is created if it is missing (mode 0700)
    and opened with ``O_NOFOLLOW``; each must belong to the account. The last one
    is made with ``mkdir``: an existing entry (of any kind) is
    ``PathProblem.EXISTS``. The path must lie below the real home.
    """
    home = real_home(account)
    if not path_within(path, home) or path == home:
        raise PathRejectedError(PathProblem.OUTSIDE_ROOTS)
    parts = path[len(home) + 1 :].split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise PathRejectedError(PathProblem.NOT_CANONICAL)
    try:
        fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        raise PathRejectedError(PathProblem.HOME_UNSAFE) from None
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, _DIRECTORY_MODE, dir_fd=fd)
            except FileExistsError:
                pass
            except OSError:
                raise PathRejectedError(PathProblem.NOT_FOUND) from None
            child = _open_directory(part, fd, account.uid)
            os.close(fd)
            fd = child
        try:
            os.mkdir(parts[-1], _DIRECTORY_MODE, dir_fd=fd)
        except FileExistsError:
            raise PathRejectedError(PathProblem.EXISTS) from None
        except OSError:
            raise PathRejectedError(PathProblem.NOT_FOUND) from None
    finally:
        os.close(fd)


def remove_directory(path: str, account: LinuxAccount) -> bool:
    """Delete a directory this module made (a failed or stale checkout). Best effort.

    Refused (``False``) unless it is a real directory of the account below the real
    home: never a link, never a foreign directory, never the home. Returns whether
    the directory is gone afterwards.
    """
    try:
        home = real_home(account)
        if not path_within(path, home) or path == home:
            return False
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    except (OSError, PathRejectedError):
        return False
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.uid:
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def check_existing_repository(
    path: str,
    account: LinuxAccount,
    roots: Sequence[str],
    workspace_subdir: str,
) -> str:
    """Refuse ``path`` unless it can be registered as an existing repository.

    ``path`` is already canonical (``validation.validate_path_text``). Returns
    the path. The checks, in order (the first failure is the answer): the spelling
    is the resolved path (no symbolic link anywhere); it is strictly below one of
    the user's roots; no component below that root is hidden; it is neither the
    checkout root nor above it; the directory belongs to the account and is not
    world-writable; ``.git`` is a real directory of the account with real
    ``HEAD`` / ``config`` files and no ``alternates`` / ``commondir``.
    """
    resolved = _resolve(path, PathProblem.NOT_FOUND)
    if resolved != path:
        raise PathRejectedError(PathProblem.SYMLINK)
    home = real_home(account)  # also proves the account's home is usable

    root = _root_of(path, account, roots)
    if path == root:
        raise PathRejectedError(PathProblem.IS_A_ROOT)
    if any(part.startswith(".") for part in path[len(root) + 1 :].split("/")):
        raise PathRejectedError(PathProblem.HIDDEN)
    managed = f"{home}/{workspace_subdir}"
    if path_within(managed, path):
        # The checkout root itself (or a directory that contains it) is not a
        # repository: it would contain every checkout of the user.
        raise PathRejectedError(PathProblem.IS_A_ROOT)

    _require_owned_directory(_lstat(path), account.uid)
    git_dir = f"{path}/.git"
    info = _lstat(git_dir, PathProblem.NOT_A_REPOSITORY)
    if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
        # A ``.git`` file (``gitdir: ...``) or link makes git read another place.
        raise PathRejectedError(PathProblem.GIT_TRICK)
    _require_owned_directory(info, account.uid)
    for name in ("HEAD", "config"):
        entry = f"{git_dir}/{name}"
        try:
            kind = os.lstat(entry)
        except FileNotFoundError:
            if name == "HEAD":
                raise PathRejectedError(PathProblem.NOT_A_REPOSITORY) from None
            continue
        except OSError:
            raise PathRejectedError(PathProblem.NOT_A_REPOSITORY) from None
        if stat.S_ISLNK(kind.st_mode):
            raise PathRejectedError(PathProblem.GIT_TRICK)
        if not stat.S_ISREG(kind.st_mode):
            raise PathRejectedError(PathProblem.NOT_A_REPOSITORY)
    for name in ("objects", "refs"):
        kind = _lstat(f"{git_dir}/{name}", PathProblem.NOT_A_REPOSITORY)
        if stat.S_ISLNK(kind.st_mode):
            raise PathRejectedError(PathProblem.GIT_TRICK)
        if not stat.S_ISDIR(kind.st_mode):
            raise PathRejectedError(PathProblem.NOT_A_REPOSITORY)
    for name in ("objects/info/alternates", "commondir"):
        if os.path.lexists(f"{git_dir}/{name}"):
            # Another repository's objects (or another git directory) are read.
            raise PathRejectedError(PathProblem.GIT_TRICK)
    return path


def _root_of(path: str, account: LinuxAccount, roots: Sequence[str]) -> str:
    """The resolved root (of the user's) that ``path`` lies in, else refuse."""
    for template in roots:
        try:
            expanded = expand_root(template, account)
            root = os.path.realpath(expanded, strict=True)
        except (PathRejectedError, OSError, ValueError):
            continue  # a root that does not exist holds nothing
        if root != "/" and path_within(path, root):
            return root
    raise PathRejectedError(PathProblem.OUTSIDE_ROOTS)
