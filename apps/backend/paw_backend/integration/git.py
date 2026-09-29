"""The git operations of the worktrees and their integration (PAW-035).

Every command goes through a :class:`~paw_backend.repositories.git.GitRunner`
(``SubprocessGitRunner`` for the backend's own Linux user, ``SshGitRunner`` for
another user's, Decision 0029), so it gets the runner's fixed environment and
hardening (no hooks, no fsmonitor, no global or system configuration, a timeout,
bounded output). This module adds the rules of its own:

* **Fixed argument lists.** Branches and revisions are the backend's own names
  (``layout.py``), checked again here with ``validate_branch`` and passed as
  ``refs/heads/<branch>`` (which cannot start with ``-``); paths are placed after
  ``--``. No argument comes from a model.
* **Never a push, never the default branch.** There is no ``push``, ``fetch``,
  ``checkout`` or ``reset`` here, and the only commands that move a branch are
  ``worktree add -b`` (a new ``paw/`` branch) and ``merge`` run inside a worktree
  of a ``paw/`` branch (the caller checks the worktree's branch first).
* **Answers are git's, checked.** Where a worktree is and what it has checked out
  are asked of git (``rev-parse --show-toplevel``, ``symbolic-ref``) rather than of
  the local file system, so the checks hold when git runs as another Linux user
  over SSH and the backend cannot read that user's home.
* **A worktree's own ``.git`` is never trusted.** A Worker's worktree is a
  directory an agent writes to: a ``.git`` it replaced would bring its own
  configuration (a filter driver is a command, run by ``status``). Every
  command that runs *inside* a worktree gets that worktree's git directory in
  the checkout explicitly (``--git-dir=<common>/worktrees/<name>
  --work-tree=<path>``: :class:`PinnedWorktree`, :meth:`WorktreeGit.pin`), and
  the git directory is accepted only when it is one of the checkout's (the
  checkout's ``.git`` is out of every agent's scope) and has the expected branch
  checked out (git lets one branch be checked out in one worktree only).
* **Merge commits have a fixed identity and no signature.** The workspace, not
  a person, makes an integration merge (``MERGE_IDENTITY``); ``commit.gpgSign``
  is off (a repository's own configuration cannot make the merge need a key).

The sub-commands used here are listed in Decision 0036 (Approved) as additions to
the Wrapper's allowlist of Decision 0029; the second form of ``status``
(``--ignored``, :meth:`WorktreeGit.is_exactly_committed`) is Decision 0051's.
"""

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from paw_backend.repositories.errors import GitCommandError, GitFailure
from paw_backend.repositories.git import GitResult, GitRunner, command_name
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.validation import validate_branch

_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# ``git rev-parse --verify --quiet``: the revision names no object.
_NO_SUCH_REVISION = 1
MERGE_NAME = "Personal AI Workspace"
MERGE_EMAIL = "integration@paw.invalid"
# Put in front of the commands that may create a commit (``merge``): a fixed
# identity (the environment has none, ``GIT_CONFIG_GLOBAL=/dev/null``) and no
# signing, whatever the repository's own configuration says.
_COMMIT_CONFIG = (
    "-c",
    f"user.name={MERGE_NAME}",
    "-c",
    f"user.email={MERGE_EMAIL}",
    "-c",
    "commit.gpgSign=false",
    "-c",
    "merge.verifySignatures=false",
)


@dataclass(frozen=True, slots=True)
class MergeCheck:
    """What ``merge-tree`` says a merge would do (nothing is written)."""

    clean: bool
    conflicted_files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PinnedWorktree:
    """A worktree together with ITS git directory in the checkout
    (``<common>/worktrees/<name>``): git run with both never reads the
    ``.git`` in the worktree. Made by :meth:`WorktreeGit.pin` only."""

    path: str
    git_dir: str


Where = str | PinnedWorktree


def _ref(branch: str) -> str:
    return f"refs/heads/{validate_branch(branch)}"


class WorktreeGit:
    """The worktree and merge commands, each with fixed arguments."""

    def __init__(self, runner: GitRunner, *, timeout_s: float) -> None:
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise TypeError("timeout_s must be a number")
        if not 0 < timeout_s <= 3600:
            raise ValueError("timeout_s is out of range")
        self._runner = runner
        self._timeout = float(timeout_s)

    async def _run(
        self, args: Sequence[str], account: LinuxAccount, where: Where
    ) -> GitResult:
        if isinstance(where, PinnedWorktree):
            cwd = where.path
            args = [
                f"--git-dir={where.git_dir}",
                f"--work-tree={where.path}",
                *args,
            ]
        else:
            cwd = where
        return await self._runner.run(
            args,
            account=account,
            cwd=cwd,
            timeout_s=self._timeout,
            ceiling=os.path.dirname(cwd) or "/",
        )

    async def _checked(
        self, args: Sequence[str], account: LinuxAccount, where: Where
    ) -> str:
        result = await self._run(args, account, where)
        if result.returncode != 0:
            raise GitCommandError(command_name(args), GitFailure.NONZERO_EXIT)
        return result.stdout

    # -- reading ------------------------------------------------------------------

    async def commit_of(
        self, path: Where, revision: str, account: LinuxAccount
    ) -> str | None:
        """The commit ``revision`` (a full ref name) names, or ``None`` when it
        names none.

        ``rev-parse --verify --quiet`` exits 1 for a revision that does not name a
        commit; any other failure (128: the checkout is not a repository any
        more, it is corrupted or cannot be read) is ``GitCommandError``, never an
        absent ref (Codex review of PAW-035: a branch that could not be read is
        not a branch to skip)."""
        result = await self._run(
            ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
            account,
            path,
        )
        if result.returncode == _NO_SUCH_REVISION:
            return None
        if result.returncode != 0:
            raise GitCommandError("rev-parse", GitFailure.NONZERO_EXIT)
        commit = result.stdout.strip()
        if _OBJECT_ID.fullmatch(commit) is None:
            raise GitCommandError("rev-parse", GitFailure.UNSAFE_OUTPUT)
        return commit

    async def branch_commit(
        self, path: str, branch: str, account: LinuxAccount
    ) -> str | None:
        """The commit of the local branch ``branch``, or ``None`` (no such branch)."""
        return await self.commit_of(path, _ref(branch), account)

    async def _absolute_path(
        self, args: Sequence[str], account: LinuxAccount, where: Where
    ) -> str | None:
        """The one absolute path a ``rev-parse`` prints (``None``: it failed)."""
        result = await self._run(args, account, where)
        if result.returncode != 0:
            return None
        lines = result.stdout.split("\n")
        if len(lines) != 2 or lines[1] != "" or not lines[0].startswith("/"):
            raise GitCommandError("rev-parse", GitFailure.UNSAFE_OUTPUT)
        return lines[0]

    async def common_dir(self, checkout: str, account: LinuxAccount) -> str:
        """The git directory the checkout's worktrees share (its ``.git``)."""
        common = await self._absolute_path(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"],
            account,
            checkout,
        )
        if common is None:
            raise GitCommandError("rev-parse", GitFailure.NONZERO_EXIT)
        return common

    async def pin(
        self, checkout: str, path: str, account: LinuxAccount
    ) -> PinnedWorktree | None:
        """The worktree at ``path`` with its git directory in ``checkout``, or
        ``None`` when the git directory ``path`` names is not a worktree
        directory of the checkout (``<common>/worktrees/<name>``).

        What ``path`` names is only a hint (``rev-parse`` reads the ``.git``
        there, and runs nothing: no filter, no hook, no monitor); the answer is
        accepted only inside the checkout's own git directory, and the caller
        still checks the branch through the pinned git directory (``HEAD`` of a
        worktree directory is the checkout's, not the agent's)."""
        common = await self.common_dir(checkout, account)
        hint = await self._absolute_path(
            ["rev-parse", "--path-format=absolute", "--git-dir"], account, path
        )
        prefix = f"{common}/worktrees/"
        if hint is None or not hint.startswith(prefix):
            return None
        name = hint.removeprefix(prefix)
        if not name or "/" in name or name in (".", ".."):
            return None
        return PinnedWorktree(path, hint)

    async def toplevel(self, path: str, account: LinuxAccount) -> str | None:
        """The work tree git finds at ``path`` (``None``: not a work tree)."""
        return await self._absolute_path(
            ["rev-parse", "--show-toplevel"], account, path
        )

    async def current_branch(self, path: Where, account: LinuxAccount) -> str | None:
        """The branch checked out at ``path`` (``None``: detached or none)."""
        result = await self._run(["symbolic-ref", "--quiet", "HEAD"], account, path)
        if result.returncode != 0:
            return None
        ref = result.stdout.strip()
        if not ref.startswith("refs/heads/"):
            return None
        return ref.removeprefix("refs/heads/")

    async def origin_head(self, path: str, account: LinuxAccount) -> str | None:
        """The branch ``origin/HEAD`` points at (without ``origin/``), or ``None``."""
        result = await self._run(
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            account,
            path,
        )
        name = result.stdout.strip() if result.returncode == 0 else ""
        if not name.startswith("origin/"):
            return None
        return name.removeprefix("origin/") or None

    async def worktree_branch(
        self, checkout: str, path: str, account: LinuxAccount
    ) -> str | None:
        """What the worktree registered at ``path`` has checked out, as the
        repository of ``checkout`` lists it: its branch, ``""`` when it is
        detached or has something else checked out, ``None`` when no usable
        worktree is registered there (not listed, or its directory is gone:
        ``prunable``)."""
        output = await self._checked(
            ["worktree", "list", "--porcelain", "-z"], account, checkout
        )
        for record in output.split("\0\0"):
            fields = [field for field in record.split("\0") if field]
            if not fields or fields[0] != f"worktree {path}":
                continue
            if any(field.startswith("prunable") for field in fields):
                return None
            for field in fields:
                if field.startswith("branch refs/heads/"):
                    return field.removeprefix("branch refs/heads/")
            return ""
        return None

    async def prune_worktrees(self, checkout: str, account: LinuxAccount) -> None:
        """Forget the worktrees whose directory is gone (``worktree prune``)."""
        await self._checked(["worktree", "prune"], account, checkout)

    async def is_clean(self, path: Where, account: LinuxAccount) -> bool:
        """No staged, unstaged or untracked change (ignored files do not count:
        a Worker's worktree, whose commits alone are integrated)."""
        output = await self._checked(
            ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            account,
            path,
        )
        return output == ""

    async def is_exactly_committed(self, path: Where, account: LinuxAccount) -> bool:
        """No staged, unstaged, untracked **or ignored** file: the directory is
        exactly its commit (an integration worktree, which the checks read; an
        uncommitted ``.env`` or a generated file is in no commit either).

        ``--untracked-files=normal`` lists an untracked or ignored directory as
        one entry (``node_modules/``), so the answer stays within the runner's
        output limit; only whether there is any entry matters."""
        output = await self._checked(
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=normal",
                "--ignored=traditional",
            ],
            account,
            path,
        )
        return output == ""

    async def merging(self, path: Where, account: LinuxAccount) -> bool:
        """Whether a merge was left unfinished at ``path`` (``MERGE_HEAD``)."""
        return await self.commit_of(path, "MERGE_HEAD", account) is not None

    async def is_ancestor(
        self, path: str, ancestor: str, descendant: str, account: LinuxAccount
    ) -> bool:
        """Whether branch ``ancestor`` is contained in branch ``descendant``."""
        result = await self._run(
            ["merge-base", "--is-ancestor", _ref(ancestor), _ref(descendant)],
            account,
            path,
        )
        if result.returncode not in (0, 1):
            raise GitCommandError("merge-base", GitFailure.NONZERO_EXIT)
        return result.returncode == 0

    async def check_merge(
        self, path: Where, into: str, branch: str, account: LinuxAccount
    ) -> MergeCheck:
        """Would merging ``branch`` into ``into`` conflict? ``merge-tree`` answers
        without touching a work tree, an index or a ref (git 2.38 or later)."""
        result = await self._run(
            [
                "merge-tree",
                "--write-tree",
                "--name-only",
                "-z",
                "--no-messages",
                _ref(into),
                _ref(branch),
            ],
            account,
            path,
        )
        if result.returncode not in (0, 1):
            raise GitCommandError("merge-tree", GitFailure.NONZERO_EXIT)
        parts = result.stdout.split("\0")
        if not parts or _OBJECT_ID.fullmatch(parts[0]) is None:
            raise GitCommandError("merge-tree", GitFailure.UNSAFE_OUTPUT)
        if result.returncode == 0:
            return MergeCheck(True)
        files = tuple(dict.fromkeys(p for p in parts[1:] if p))
        return MergeCheck(False, files)

    # -- writing ------------------------------------------------------------------

    async def add_worktree(
        self,
        checkout: str,
        path: str,
        branch: str,
        start: str,
        account: LinuxAccount,
    ) -> None:
        """A new worktree at ``path`` on the NEW branch ``branch`` from the
        commit ``start``. git refuses an existing branch or a non-empty path."""
        if _OBJECT_ID.fullmatch(start) is None:
            raise ValueError("start must be a commit id")
        await self._checked(
            [
                "worktree",
                "add",
                "--quiet",
                "-b",
                validate_branch(branch),
                "--",
                path,
                start,
            ],
            account,
            checkout,
        )

    async def attach_worktree(
        self, checkout: str, path: str, branch: str, account: LinuxAccount
    ) -> None:
        """A new worktree at ``path`` on the EXISTING branch ``branch`` (the
        worktree was removed by hand, the branch was kept)."""
        await self._checked(
            ["worktree", "add", "--quiet", "--", path, validate_branch(branch)],
            account,
            checkout,
        )

    async def merge(self, path: Where, branch: str, account: LinuxAccount) -> bool:
        """``git merge --no-ff`` of ``branch`` into the branch checked out at
        ``path``. ``False`` when git did not merge (the merge is then aborted, so
        the worktree is left as it was)."""
        result = await self._run(
            [
                *_COMMIT_CONFIG,
                "merge",
                "--no-ff",
                "--no-edit",
                "--quiet",
                "-m",
                f"Integrate {validate_branch(branch)}",
                _ref(branch),
            ],
            account,
            path,
        )
        if result.returncode == 0:
            return True
        await self.abort_merge(path, account)
        return False

    async def abort_merge(self, path: Where, account: LinuxAccount) -> None:
        """Abort an unfinished merge at ``path`` (nothing to abort is fine)."""
        if await self.merging(path, account):
            await self._checked(["merge", "--abort"], account, path)
