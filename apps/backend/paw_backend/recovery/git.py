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

import contextlib
import hashlib
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

COMMITTER_NAME = "Personal AI Workspace"
COMMITTER_EMAIL = "recovery@personal-ai-workspace.invalid"
DEFAULT_TIMEOUT_SECONDS = 300.0
# The index a commit is built in (inside ``.git``, never the checkout's own).
INDEX_NAME = "paw-recovery-index"


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
    UNSAFE_OBJECT = "unsafe_object"


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
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        data: bytes | None = None,
        index: str | None = None,
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
        environment = _environment()
        if index is not None:
            environment["GIT_INDEX_FILE"] = index
        try:
            result = subprocess.run(
                command,
                input=data if data is not None else b"",
                capture_output=True,
                env=environment,
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

    def has_no_history(self) -> bool:
        """No commit at ``HEAD`` and no ref at all: an empty clone (or a new
        ``git init``), which the backup may claim. A clone of a project's
        repository has refs even when its work tree is empty (``--no-checkout``,
        a new orphan branch), so it is never claimed (Decision 0054 1)."""
        if self.head() is not None:
            return False
        refs = self._run(["for-each-ref", "--count=1", "--format=%(refname)"])
        return not refs.stdout.strip()

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

    def commit_files(
        self, files: Mapping[str, bytes], message: str, names: Sequence[str]
    ) -> bool:
        """Commit ``files`` as the whole content of the managed ``names``.

        The commit is built from the bytes given (the rendered backup), in an
        index of its own, never from the work tree or the checkout's index: a
        file changed after it was written, or a path someone staged (even while
        this runs), cannot enter the commit. Paths outside ``names`` keep what
        ``HEAD`` has. Returns ``False`` (and commits nothing) when the tree does
        not change. The branch is moved only from the ``HEAD`` it was built on
        (a compare-and-swap), and the checkout's index is then reset to the new
        commit for the managed names only (other staged changes stay staged).
        """
        object_format = self._value(["rev-parse", "--show-object-format"])
        if object_format not in ("sha1", "sha256"):
            raise RecoveryGitError(GitProblem.COMMAND_FAILED)

        def object_id(payload: bytes) -> str:
            # The id git itself gives a blob, in the repository's hash.
            return hashlib.new(
                object_format, payload, usedforsecurity=False
            ).hexdigest()

        allowed = set(names)
        for path in files:
            if path.split("/", 1)[0] not in allowed:
                raise ValueError("a file outside the managed names")
        branch = self._value(["symbolic-ref", "--quiet", "HEAD"])
        if branch is None:
            raise RecoveryGitError(GitProblem.DETACHED_HEAD)
        head = self.head()
        git_dir = self._value(["rev-parse", "--absolute-git-dir"])
        if git_dir is None:
            raise RecoveryGitError(GitProblem.NOT_A_GIT_CHECKOUT)
        index = os.path.join(git_dir, INDEX_NAME)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(index)
        try:
            self._run(["read-tree", head or "--empty"], index=index)
            self._run(
                ["rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *names],
                index=index,
            )
            known = set()
            if head is not None:
                listing = self._run(["ls-tree", "-r", "-z", head]).stdout
                for entry in listing.split(b"\0"):
                    if entry:
                        known.add(entry.split(b"\t", 1)[0].split()[2].decode())
            entries = []
            for path in sorted(files):
                data = files[path]
                digest = object_id(b"blob %d\0" % len(data) + data)
                if digest not in known:
                    written = self._value_of(
                        self._run(["hash-object", "-w", "--stdin"], data=data)
                    )
                    if written != digest:
                        raise RecoveryGitError(GitProblem.COMMAND_FAILED)
                    known.add(digest)
                entries.append(f"100644 {digest}\t{path}".encode() + b"\0")
            if entries:
                self._run(
                    ["update-index", "-z", "--index-info"],
                    data=b"".join(entries),
                    index=index,
                )
            tree = self._value_of(self._run(["write-tree"], index=index))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(index)
        if head is not None and tree == self._value(["rev-parse", f"{head}^{{tree}}"]):
            return False
        parents = ["-p", head] if head is not None else []
        commit = self._value_of(
            self._run(["commit-tree", tree, *parents], data=message.encode())
        )
        zero = "0" * (64 if object_format == "sha256" else 40)
        self._run(["update-ref", branch, commit, head or zero])
        self._run(["reset", "-q", "--", *self._known_names(names)])
        return True

    def read_commit_files(
        self, commit: str, names: Sequence[str], *, max_bytes: int
    ) -> dict[str, bytes]:
        """Every file below ``names`` in ``commit`` (path -> bytes), from git's
        object store: never the work tree, which another process could change
        while it is read. A link, a submodule or a file over ``max_bytes`` is
        refused (``unsafe_entry`` is the caller's; here ``command_failed``)."""
        listing = self._run(
            ["ls-tree", "-r", "-z", "--full-tree", "-l", commit, "--", *names]
        ).stdout
        wanted: list[tuple[str, str]] = []
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            mode, kind, oid, size = meta.split()
            if kind != b"blob" or mode not in (b"100644", b"100755"):
                raise RecoveryGitError(GitProblem.UNSAFE_OBJECT)
            if int(size) > max_bytes:
                raise RecoveryGitError(GitProblem.UNSAFE_OBJECT)
            wanted.append((raw_path.decode("utf-8", "surrogateescape"), oid.decode()))
        if not wanted:
            return {}
        output = self._run(
            ["cat-file", "--batch"],
            data="".join(f"{oid}\n" for _, oid in wanted).encode(),
        ).stdout
        files: dict[str, bytes] = {}
        position = 0
        for path, oid in wanted:
            end = output.index(b"\n", position)
            header = output[position:end].split()
            if len(header) != 3 or header[0].decode() != oid or header[1] != b"blob":
                raise RecoveryGitError(GitProblem.COMMAND_FAILED)
            size = int(header[2])
            start = end + 1
            files[path] = output[start : start + size]
            position = start + size + 1
        return files

    def _known_names(self, names: Sequence[str]) -> list[str]:
        """The managed names in the index or in ``HEAD`` (a pathspec git accepts)."""
        known = self._run(["ls-files", "-z", "--", *names]).stdout.split(b"\0")
        known += self._run(["ls-tree", "-z", "--name-only", "HEAD"]).stdout.split(b"\0")
        present = {
            path.decode("utf-8", "surrogateescape").split("/", 1)[0]
            for path in known
            if path
        }
        return [name for name in names if name in present]

    @staticmethod
    def _value_of(result: subprocess.CompletedProcess[bytes]) -> str:
        value = result.stdout.decode("utf-8", "replace").strip()
        if not value:
            raise RecoveryGitError(GitProblem.COMMAND_FAILED)
        return value

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
