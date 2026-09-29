"""git for the Recovery Repository's checkout (PAW-047, Decision 0054 1, 5).

Every command runs as ``git -C <checkout>`` with:

* no hook (``core.hooksPath=/dev/null``), no signing, no fsmonitor, a fixed
  committer identity (``Personal AI Workspace <recovery@personal-ai-workspace
  .invalid>``): nothing in the checkout's configuration runs a program of its
  choosing, and the commits do not carry a person's identity;
* an environment without ``GIT_*`` variables of the caller (``GIT_DIR`` and the
  like cannot redirect it) and with ``GIT_TERMINAL_PROMPT=0`` (a push that needs
  a password fails instead of waiting);
* a timeout.

git's output is never shown or recorded (a remote's error text can quote its URL);
a failure is ``RecoveryGitError`` with a closed code. A push is always a plain
fast-forward push to the branch's configured upstream (``branch.<name>.remote``
and ``.merge``): never ``--force``, never another branch, never a new remote.
Everything here blocks: call it with ``asyncio.to_thread``.
"""

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

COMMITTER_NAME = "Personal AI Workspace"
COMMITTER_EMAIL = "recovery@personal-ai-workspace.invalid"
DEFAULT_TIMEOUT_SECONDS = 300.0


class GitProblem(StrEnum):
    """Why a git step failed (a closed vocabulary)."""

    GIT_UNAVAILABLE = "git_unavailable"
    TIMEOUT = "timeout"
    NOT_A_GIT_CHECKOUT = "not_a_git_checkout"
    NOT_TOP_LEVEL = "not_top_level"
    DETACHED_HEAD = "detached_head"
    NO_UPSTREAM = "no_upstream"
    COMMAND_FAILED = "command_failed"
    PUSH_REJECTED = "push_rejected"
    NOT_CLEAN = "not_clean"
    NOT_LATEST = "not_latest"
    NO_COMMIT = "no_commit"
    UNRELATED_STAGED_CHANGES = "unrelated_staged_changes"


class RecoveryGitError(Exception):
    """A git step failed (see ``problem``). The text names no path and no URL."""

    def __init__(self, problem: GitProblem) -> None:
        self.problem = problem
        super().__init__(f"recovery git failed: {problem.value}")


@dataclass(frozen=True, slots=True)
class Upstream:
    branch: str
    remote: str
    merge: str  # refs/heads/<name> on the remote

    @property
    def tracking_ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.merge.removeprefix('refs/heads/')}"


def _environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["LC_ALL"] = "C"
    return environment


class RecoveryGit:
    """git in one checkout (``path`` is the checked, canonical directory)."""

    def __init__(self, path: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._path = path
        self._timeout = timeout

    def _run(
        self, arguments: Sequence[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[bytes]:
        command = [
            "git",
            "-C",
            self._path,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"user.name={COMMITTER_NAME}",
            "-c",
            f"user.email={COMMITTER_EMAIL}",
            *arguments,
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                env=_environment(),
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError:
            raise RecoveryGitError(GitProblem.GIT_UNAVAILABLE) from None
        except subprocess.TimeoutExpired:
            raise RecoveryGitError(GitProblem.TIMEOUT) from None
        if check and result.returncode != 0:
            raise RecoveryGitError(GitProblem.COMMAND_FAILED)
        return result

    def _value(self, arguments: Sequence[str]) -> str | None:
        result = self._run(arguments, check=False)
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", "replace").strip() or None

    def check_top_level(self) -> None:
        """The checkout is a work tree and its top (not a directory inside one)."""
        result = self._run(["rev-parse", "--show-toplevel"], check=False)
        if result.returncode != 0:
            raise RecoveryGitError(GitProblem.NOT_A_GIT_CHECKOUT)
        top = result.stdout.decode("utf-8", "replace").strip()
        if os.path.realpath(top) != self._path:
            raise RecoveryGitError(GitProblem.NOT_TOP_LEVEL)

    def head(self) -> str | None:
        """The commit of ``HEAD``, ``None`` before the first commit."""
        return self._value(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])

    def upstream(self) -> Upstream:
        """The current branch and where it is pushed (``no_upstream`` if not set)."""
        branch = self._value(["symbolic-ref", "--quiet", "--short", "HEAD"])
        if branch is None:
            raise RecoveryGitError(GitProblem.DETACHED_HEAD)
        remote = self._value(["config", "--get", f"branch.{branch}.remote"])
        merge = self._value(["config", "--get", f"branch.{branch}.merge"])
        if (
            remote is None
            or merge is None
            or remote == "."
            or not merge.startswith("refs/heads/")
        ):
            raise RecoveryGitError(GitProblem.NO_UPSTREAM)
        return Upstream(branch=branch, remote=remote, merge=merge)

    def stage(self, names: Sequence[str]) -> None:
        """Stage the managed names exactly as they are on disk (also removals)."""
        present = [
            name for name in names if os.path.lexists(os.path.join(self._path, name))
        ]
        absent = [name for name in names if name not in present]
        if present:
            self._run(["add", "--all", "--force", "--", *present])
        if absent:
            self._run(["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *absent])

    def check_only_managed_staged(self, names: Sequence[str]) -> None:
        """Refuse when the index holds a change outside the managed names.

        Something an operator (or an interrupted manual command) staged, such as
        a ``README.md`` or a credential file, would otherwise ride along in the
        automated commit and push. The job neither commits nor unstages it:
        ``unrelated_staged_changes``, and a person cleans the index."""
        result = self._run(
            ["diff", "--cached", "--name-only", "-z", "--no-renames", "--no-ext-diff"]
        )
        allowed = set(names)
        for path in result.stdout.split(b"\0"):
            if not path:
                continue
            top = path.decode("utf-8", "surrogateescape").split("/", 1)[0]
            if top not in allowed:
                raise RecoveryGitError(GitProblem.UNRELATED_STAGED_CHANGES)

    def has_staged_changes(self) -> bool:
        result = self._run(
            ["diff", "--cached", "--quiet", "--no-ext-diff"], check=False
        )
        if result.returncode not in (0, 1):
            raise RecoveryGitError(GitProblem.COMMAND_FAILED)
        return result.returncode == 1

    def commit(self, message: str) -> None:
        self._run(["commit", "--quiet", "--no-verify", "--message", message])

    def needs_push(self, upstream: Upstream) -> bool:
        """``HEAD`` is not what the remote-tracking branch last saw."""
        head = self.head()
        if head is None:
            return False
        return (
            self._value(["rev-parse", "--verify", "--quiet", upstream.tracking_ref])
            != head
        )

    def push(self, upstream: Upstream) -> None:
        """A fast-forward push of ``HEAD`` to the upstream branch (never forced)."""
        result = self._run(
            [
                "push",
                "--porcelain",
                "--quiet",
                upstream.remote,
                f"HEAD:{upstream.merge}",
            ],
            check=False,
        )
        if result.returncode == 0:
            return
        text = result.stdout.decode("utf-8", "replace")
        if "[rejected]" in text or "[remote rejected]" in text:
            raise RecoveryGitError(GitProblem.PUSH_REJECTED)
        raise RecoveryGitError(GitProblem.COMMAND_FAILED)

    def check_restorable(self) -> str:
        """For a restore: a commit, a clean work tree, ``HEAD`` = its upstream.

        Returns the commit. ``not_latest`` when the checkout is not exactly the
        last state the remote-tracking branch knows (an older or unpushed state:
        Decision 0054 8)."""
        head = self.head()
        if head is None:
            raise RecoveryGitError(GitProblem.NO_COMMIT)
        status = self._run(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
        if status.stdout:
            raise RecoveryGitError(GitProblem.NOT_CLEAN)
        upstream = self.upstream()
        tracking = self._value(
            ["rev-parse", "--verify", "--quiet", upstream.tracking_ref]
        )
        if tracking != head:
            raise RecoveryGitError(GitProblem.NOT_LATEST)
        return head


__all__ = [
    "COMMITTER_EMAIL",
    "COMMITTER_NAME",
    "DEFAULT_TIMEOUT_SECONDS",
    "GitProblem",
    "RecoveryGit",
    "RecoveryGitError",
    "Upstream",
]
