"""The host side of the sandbox's ``git``: a read-only git, pinned (Decision 0083, 2).

``repo.git.read`` (``tools.py``) runs here what the shim was asked
(:func:`~.gitargs.parse_git_read` already turned it into canonical words). git
runs on the host as the account's user through the backend's hardened
``GitRunner`` (``SshGitRunner`` and the forced-command wrapper, or
``SubprocessGitRunner``), never inside the sandbox:

* **where**: :meth:`GitReader.locate` finds the repository of a root. A writing
  node's worktree is used only pinned to ITS git directory in the user's
  checkout (``--git-dir`` / ``--work-tree``, as ``GitWorktreeCoordinator`` does:
  the worktree's ``.git`` is a hint, accepted only when it names a
  ``worktrees/<name>`` directory of a checkout the node's scope protects, and
  only on a ``paw/`` branch). Any other root must be a plain checkout whose git
  directory is its own ``.git``. Anything else is refused.
* **which history**: a worktree with a base (``refs/paw/base/<branch>``, made by
  the worktree's preparation) may read the commits from its base to ``HEAD``:
  every revision must resolve to a commit (an object id of a tree, a blob or a
  tag is refused), and that commit must be the base or a descendant of the base
  and an ancestor of ``HEAD`` (``merge-base --is-ancestor``, twice). ``log``
  always stops at the base (``^<base>``). Without a base (a node without a
  worktree, or a worktree whose base is not recorded yet) only the current state
  can be read: ``status``, ``diff`` without revisions, ``log`` by name only from
  ``HEAD``; ``show`` and contents of the history are refused (an excluded file's
  old content could be in it).
* **which files**: the pathspec is always built here: each path the model gave
  becomes ``:(literal)<path>`` once it passed the broker's scope check (inside
  the repository, not excluded, symbolic links resolved), else ``.``; and every
  excluded or protected path inside the repository is always added as
  ``:(exclude,literal)<path>`` (so ``show <commit>`` or ``log -p`` never show an
  excluded file's change).
* **how**: ``--no-color --no-ext-diff --no-textconv`` (and ``--no-decorate`` for
  ``log`` / ``show``); the runner's fixed environment (no ``GIT_*`` of the
  backend) and hardening ``-c``. The output goes back through the tool runner,
  which redacts it before anyone sees it.
"""

import os
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field

from paw_backend.integration.git import PinnedWorktree, WorktreeGit
from paw_backend.repositories.errors import GitCommandError, GitFailure
from paw_backend.repositories.git import GitResult, GitRunner, command_name
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.sandbox.gitargs import (
    BASE_REF_PREFIX,
    NAMES_ONLY_LOG,
    GitReadArgs,
    GitReadRefused,
    GitRefusal,
    Revision,
)
from paw_backend.tools.capabilities import ScopeStatus
from paw_backend.tools.scope import (
    PathResolutionError,
    PathResolver,
    Target,
    TargetError,
    TargetKind,
    TaskScope,
    classify_targets,
    normalise_path,
    path_within,
)

#: How long one git command of a read may take.
DEFAULT_TIMEOUT_SECONDS = 60.0
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
#: ``merge-base --is-ancestor`` answers with 0 (yes) and 1 (no).
_YES, _NO = 0, 1
#: What the wrapper exits with when it refuses (never git's own status).
_WRAPPER_REFUSALS = frozenset({125, 126})
_FIXED = {
    "status": (),
    "diff": ("--no-color", "--no-ext-diff", "--no-textconv"),
    "log": ("--no-color", "--no-ext-diff", "--no-textconv", "--no-decorate"),
    "show": ("--no-color", "--no-ext-diff", "--no-textconv", "--no-decorate"),
}


@dataclass(frozen=True, slots=True)
class RepositoryGit:
    """The git facts of one repository root, found by :meth:`GitReader.locate`.

    ``pinned``: the worktree with its git directory (a writing node's own
    worktree); ``None`` for a checkout. ``base``: the commit of
    ``refs/paw/base/<branch>`` (``None``: no base, only the current state may be
    read). ``git_dir`` / ``common_dir``: absolute, as git reports them (the
    sandbox masks the checkout's ones when they lie in the mounted root)."""

    root: str
    git_dir: str
    common_dir: str
    pinned: PinnedWorktree | None = None
    branch: str | None = None
    base: str | None = None

    @property
    def has_history(self) -> bool:
        return self.pinned is not None and self.base is not None


@dataclass(frozen=True, slots=True)
class GitReadAnswer:
    """What the shim prints: git's exit status and standard output."""

    exit_code: int
    output: str = field(repr=False)


class GitReader:
    """Read-only git for the sandbox's shim, on the host (see the module)."""

    def __init__(
        self, runner: GitRunner, *, timeout_s: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        if not hasattr(runner, "run"):
            raise TypeError("runner must be a GitRunner")
        self._runner = runner
        self._timeout = float(timeout_s)
        self._worktrees = WorktreeGit(runner, timeout_s=timeout_s)

    async def _run(
        self,
        args: Sequence[str],
        account: LinuxAccount,
        repository: RepositoryGit | str,
    ) -> GitResult:
        if isinstance(repository, RepositoryGit) and repository.pinned is not None:
            pinned = repository.pinned
            cwd = pinned.path
            args = [f"--git-dir={pinned.git_dir}", f"--work-tree={pinned.path}", *args]
        else:
            cwd = (
                repository.root if isinstance(repository, RepositoryGit) else repository
            )
        return await self._runner.run(
            args,
            account=account,
            cwd=cwd,
            timeout_s=self._timeout,
            ceiling=os.path.dirname(cwd) or "/",
        )

    async def _path_of(
        self, args: Sequence[str], account: LinuxAccount, root: str
    ) -> str:
        result = await self._run(args, account, root)
        lines = result.stdout.split("\n")
        if (
            result.returncode != 0
            or len(lines) != 2
            or lines[1] != ""
            or not lines[0].startswith("/")
        ):
            raise GitReadRefused(GitRefusal.UNAVAILABLE)
        return lines[0]

    async def locate(
        self, root: str, *, account: LinuxAccount, checkouts: Collection[str]
    ) -> RepositoryGit:
        """The git of ``root``, or ``GitReadRefused`` (``UNAVAILABLE``).

        ``checkouts`` are the paths a worktree's git directory may lie in: the
        node scope's excluded paths (the user's checkout of each repository the
        node has a worktree of is one of them, ``NodeWorktree.protected``)."""
        git_dir = await self._path_of(
            ["rev-parse", "--path-format=absolute", "--git-dir"], account, root
        )
        common = await self._path_of(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], account, root
        )
        if git_dir == f"{root}/.git" and common == git_dir:
            return RepositoryGit(root, git_dir, common)
        checkout = common.removesuffix("/.git")
        prefix = f"{common}/worktrees/"
        if (
            not common.endswith("/.git")
            or checkout not in checkouts
            or not git_dir.startswith(prefix)
        ):
            raise GitReadRefused(GitRefusal.UNAVAILABLE)
        pinned = await self._worktrees.pin(checkout, root, account)
        if pinned is None or pinned.git_dir != git_dir:
            raise GitReadRefused(GitRefusal.UNAVAILABLE)
        branch = await self._worktrees.current_branch(pinned, account)
        if branch is None or not branch.startswith("paw/"):
            raise GitReadRefused(GitRefusal.UNAVAILABLE)
        base = await self._worktrees.commit_of(
            pinned, f"{BASE_REF_PREFIX}{branch}", account
        )
        return RepositoryGit(root, git_dir, common, pinned, branch, base)

    # -- one read --------------------------------------------------------------------

    async def read(
        self,
        repository: RepositoryGit,
        args: GitReadArgs,
        *,
        account: LinuxAccount,
        pathspecs: Sequence[str],
    ) -> GitReadAnswer:
        """Run ``args`` on ``repository`` with ``pathspecs`` (:func:`pathspecs`),
        or ``GitReadRefused``. A git failure (timeout, output too large or not
        UTF-8, the server refused) is ``GitCommandError``."""
        if not pathspecs:
            raise GitReadRefused(GitRefusal.PATH)
        if not repository.has_history:
            _current_state_only(args)
            revisions: list[str] = []
        else:
            revisions = await self._revisions(repository, args, account)
        command = [
            args.subcommand,
            *_FIXED[args.subcommand],
            *args.options,
            *revisions,
            "--",
            *pathspecs,
        ]
        result = await self._run(command, account, repository)
        if result.returncode in _WRAPPER_REFUSALS:
            raise GitCommandError(command_name(command), GitFailure.NONZERO_EXIT)
        return GitReadAnswer(result.returncode, result.stdout)

    async def _revisions(
        self, repository: RepositoryGit, args: GitReadArgs, account: LinuxAccount
    ) -> list[str]:
        """The revision words for git: each resolved to a commit in the range."""
        assert repository.base is not None
        head = await self._commit(repository, "HEAD", account)
        words: list[str] = []
        for revision in args.revisions:
            commit = await self._resolve(repository, revision, account)
            if commit != repository.base and not (
                await self._is_ancestor(repository, repository.base, commit, account)
                and await self._is_ancestor(repository, commit, head, account)
            ):
                raise GitReadRefused(GitRefusal.OUT_OF_RANGE)
            # ``A..B`` reads ``^A B`` for ``log``; ``diff`` takes ``A B``.
            negated = revision.negated and args.subcommand == "log"
            words.append(f"^{commit}" if negated else commit)
        if args.subcommand == "log":
            # The history stops at the base, whatever was asked: below it lies the
            # shared history, where an excluded file may once have been.
            if not args.positive:
                words.append(head)
            words.append(f"^{repository.base}")
        return words

    async def _resolve(
        self, repository: RepositoryGit, revision: Revision, account: LinuxAccount
    ) -> str:
        text = revision.text
        if _OBJECT_ID.fullmatch(text):
            result = await self._run(["cat-file", "-t", text], account, repository)
            if result.returncode != 0:
                raise GitReadRefused(GitRefusal.OUT_OF_RANGE)
            if result.stdout != "commit\n":
                # A tree, a blob or a tag: ``^{commit}`` would peel a tag, and a
                # tree or blob id reads content by id. Refused, never peeled.
                raise GitReadRefused(GitRefusal.REVISION)
            return text
        if text.startswith(BASE_REF_PREFIX):
            if text != f"{BASE_REF_PREFIX}{repository.branch}":
                raise GitReadRefused(GitRefusal.OUT_OF_RANGE)
            assert repository.base is not None
            return repository.base
        return await self._commit(repository, text, account)

    async def _commit(
        self, repository: RepositoryGit, revision: str, account: LinuxAccount
    ) -> str:
        assert repository.pinned is not None
        commit = await self._worktrees.commit_of(repository.pinned, revision, account)
        if commit is None:
            raise GitReadRefused(GitRefusal.OUT_OF_RANGE)
        return commit

    async def _is_ancestor(
        self,
        repository: RepositoryGit,
        ancestor: str,
        descendant: str,
        account: LinuxAccount,
    ) -> bool:
        result = await self._run(
            ["merge-base", "--is-ancestor", ancestor, descendant], account, repository
        )
        if result.returncode == _YES:
            return True
        if result.returncode == _NO:
            return False
        raise GitCommandError("merge-base", GitFailure.NONZERO_EXIT)


def _current_state_only(args: GitReadArgs) -> None:
    """Refuse what reads the history, for a repository without a base."""
    refused = GitReadRefused(GitRefusal.HISTORY)
    if args.subcommand == "show":
        raise refused
    if args.subcommand == "diff" and args.revisions:
        raise refused
    if args.subcommand == "log":
        if any(r.negated or r.text != "HEAD" for r in args.revisions):
            raise refused
        if any(
            o not in NAMES_ONLY_LOG and not o.startswith("--max-count=")
            for o in args.options
        ):
            raise refused


async def pathspecs(
    paths: Sequence[str],
    *,
    workdir: str,
    root: str,
    scope: TaskScope,
    resolver: PathResolver,
    hidden: Collection[str],
) -> list[str]:
    """The pathspec of a read: each of ``paths`` (relative to ``workdir``, or
    absolute) as ``:(literal)<path relative to root>`` once the broker's scope
    check says it is in scope and inside ``root``, else ``.``; then every path
    of ``hidden`` inside ``root`` as ``:(exclude,literal)<path>``.

    ``hidden`` is what the sandbox masks (the scope's excluded and protected
    paths, the other repositories nested in this one). A path that is not in
    scope, lies outside ``root`` or inside a hidden path is ``GitReadRefused``
    (``PATH``)."""
    positive: list[str] = []
    for raw in paths:
        try:
            path = normalise_path(raw, base=workdir)
        except TargetError:
            raise GitReadRefused(GitRefusal.PATH) from None
        if not (path == root or path_within(path, root)) or any(
            path == h or path_within(path, h) for h in hidden
        ):
            raise GitReadRefused(GitRefusal.PATH)
        try:
            classification = await classify_targets(
                [Target(TargetKind.PATH, path)], scope, resolver
            )
        except PathResolutionError:
            raise GitReadRefused(GitRefusal.PATH) from None
        if classification.status is not ScopeStatus.IN_SCOPE:
            raise GitReadRefused(GitRefusal.PATH)
        relative = path[len(root) + 1 :] if path != root else ""
        positive.append(f":(literal){relative}" if relative else ".")
    excluded = [
        f":(exclude,literal){h[len(root) + 1 :]}"
        for h in sorted(set(hidden))
        if path_within(h, root) and h != root
    ]
    if not positive:
        positive.append(".")
    return [*dict.fromkeys(positive), *excluded]
