"""Where the worktrees of a task live and what their branches are called (PAW-035).

Pure functions, no I/O. Every name is derived from ids the backend owns (the
task, its attempt, the repository, the node key), never from a model or a user,
so the same node of the same attempt always gets the same worktree back (a
retry continues where the last attempt stopped) and two nodes never share one::

    <home>/<workspace_subdir>/.paw-worktrees/<task>/<attempt>/<repository>/<node>
    branch  paw/<task>/<attempt>/<node>

The integration worktree of a repository is the node ``_integration`` (a node
key starts with a letter, so no node can be called that) on the branch
``paw/<task>/<attempt>/_integration``. Every branch is inside the ``paw/``
namespace: a default branch inside it is refused (``WorktreeProblem.
DEFAULT_BRANCH_IN_NAMESPACE``), so no branch the backend writes can ever be the
default branch. Decision 0036 (Approved) lists these choices.
"""

import re
import uuid

from paw_backend.orchestrator.limits import KEY_PATTERN
from paw_backend.orchestrator.workspaces import (
    WorktreeProblem,
    WorktreeUnavailableError,
)
from paw_backend.repositories.errors import InvalidRepositoryInputError
from paw_backend.repositories.limits import MAX_PATH_BYTES, MAX_PATH_CHARS
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.policy import validate_workspace_subdir
from paw_backend.repositories.validation import validate_branch
from paw_backend.tools.scope import TargetError, normalise_path

WORKTREE_DIRECTORY = ".paw-worktrees"
BRANCH_NAMESPACE = "paw"
INTEGRATION_KEY = "_integration"
_KEY = re.compile(KEY_PATTERN)


def in_namespace(branch: str) -> bool:
    """Whether ``branch`` is inside the namespace the workspace writes to."""
    return branch == BRANCH_NAMESPACE or branch.startswith(f"{BRANCH_NAMESPACE}/")


def _key(key: str) -> str:
    if key == INTEGRATION_KEY or (type(key) is str and _KEY.fullmatch(key)):
        return key
    raise ValueError("not a node key")


def branch_name(task_id: uuid.UUID, attempt: int, key: str) -> str:
    """``paw/<task>/<attempt>/<key>`` (``key``: a node key or ``_integration``)."""
    if not isinstance(task_id, uuid.UUID):
        raise TypeError("task_id must be a UUID")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive int")
    name = f"{BRANCH_NAMESPACE}/{task_id}/{attempt}/{_key(key)}"
    try:
        return validate_branch(name)
    except InvalidRepositoryInputError:
        raise WorktreeUnavailableError(WorktreeProblem.TOO_LONG) from None


def worktree_base(account: LinuxAccount, workspace_subdir: str) -> str:
    """``<home>/<workspace_subdir>/.paw-worktrees``: where every worktree of the
    account's tasks lies (next to its checkouts, never inside one)."""
    subdir = validate_workspace_subdir(workspace_subdir)
    return f"{account.home}/{subdir}/{WORKTREE_DIRECTORY}"


def worktree_path(
    base: str, task_id: uuid.UUID, attempt: int, repo_id: uuid.UUID, key: str
) -> str:
    """The worktree of node ``key`` (or ``_integration``) of ``repo_id``."""
    if not isinstance(repo_id, uuid.UUID) or not isinstance(task_id, uuid.UUID):
        raise TypeError("the ids must be UUIDs")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive int")
    path = f"{base}/{task_id}/{attempt}/{repo_id}/{_key(key)}"
    try:
        canonical = normalise_path(path)
    except TargetError:
        raise WorktreeUnavailableError(WorktreeProblem.TOO_LONG) from None
    if canonical != path:
        raise WorktreeUnavailableError(WorktreeProblem.NOT_THE_WORKTREE)
    if len(path) > MAX_PATH_CHARS or len(path.encode()) > MAX_PATH_BYTES:
        raise WorktreeUnavailableError(WorktreeProblem.TOO_LONG)
    return path
