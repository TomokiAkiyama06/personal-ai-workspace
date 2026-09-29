"""Running git safely for the repository module (PAW-027).

The rules, all enforced here and nowhere else:

* **Never a shell.** ``asyncio.create_subprocess_exec`` with an argument list;
  no argument is ever interpreted by a shell, and every value that came from a
  caller is validated (``validation.py``) and placed after ``--`` or after an
  option that takes it, never in a position where it could be read as an option.
* **An allowlisted environment.** The child gets ``PATH``, ``HOME`` (the acting
  user's home), a fixed locale and the ``GIT_*`` variables below, nothing else:
  nothing of the backend's own environment (database URL, tokens, ``SSH_*``,
  ``GIT_DIR`` and the like) reaches git. ``GIT_CONFIG_GLOBAL=/dev/null`` and
  ``GIT_CONFIG_NOSYSTEM=1`` remove every user and system configuration, so a
  hostile ``~/.gitconfig`` is not read either (and neither is a credential
  helper: PAW-028 supplies credentials through ``extra_config``).
* **Configuration that beats the repository's own.** Every command carries
  ``-c core.hooksPath=/dev/null``, ``-c core.fsmonitor=false``,
  ``-c protocol.allow=never`` (plus ``protocol.<name>.allow=always`` for the
  transports the runner was built with, ``https`` by default), and
  ``-c submodule.recurse=false``. Command-line configuration takes precedence over
  ``.git/config``, so an untrusted repository cannot turn on a hook, a file-system
  monitor, another transport or a submodule fetch.
* **No credentials on a command line or in a log.** No secret is ever passed here;
  the runner logs the sub-command name and the exit code only, never an argument
  or any output. Errors carry a closed :class:`GitFailure`, not git's text.
* **Bounded.** A timeout (the whole process group is killed), and a limit on how
  much a command may write (a command that writes more is killed). Output is
  strictly UTF-8; anything else is refused.
* **A push goes where it says.** Before a ``push``, the configuration it would
  read is listed (``git config --includes --show-scope --list``, with the same
  ``-c`` options) and the push is refused (``GitFailure.UNSAFE_CONFIGURATION``)
  when the repository's own configuration has a ``url.<base>.insteadOf`` or
  ``pushInsteadOf``: it would send the push to another URL than the one named
  (Decision 0052; the SSH wrapper's ``redirects_push``). The runner's own
  ``extra_config`` (scope ``command``) is the deployment's, not the repository's.
* **Only as the account's own user.** git runs as the backend's own Linux user. It
  is refused (``GitFailure.IDENTITY_MISMATCH``) unless that user is the account
  the checkout belongs to: a backend that runs as another user must supply a
  :class:`GitRunner` that really switches identity. ``SubprocessGitRunner`` (this
  module) is that identity check done the simple way, for a backend that already
  runs as the single Linux user every checkout belongs to.
  ``paw_backend.repositories.ssh.SshGitRunner`` is the other implementation of the
  same :class:`GitRunner` protocol: a backend that runs as one Linux user reaches
  *another* Linux user's checkout over a restricted SSH connection instead
  (Issue #105, Decision 0029). Both share the process-running machinery below
  (:func:`run_subprocess`); they differ only in which executable, argument list
  and environment they build.
"""

import asyncio
import logging
import os
import re
import shutil
import signal
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from paw_backend.repositories.errors import (
    GitCommandError,
    GitFailure,
    PathProblem,
    PathRejectedError,
)
from paw_backend.repositories.limits import MAX_GIT_OUTPUT_BYTES
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.policy import RepositoryPolicy
from paw_backend.repositories.validation import validate_branch

logger = logging.getLogger(__name__)

# Where git and its helpers are looked for. Fixed: the backend's PATH is not used.
SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_PROTOCOLS = re.compile(r"[a-z][a-z0-9+.-]{0,15}")


@dataclass(frozen=True, slots=True)
class GitResult:
    """A finished git command. ``stdout`` is text; stderr is dropped on purpose."""

    returncode: int
    stdout: str


class GitRunner(Protocol):
    """Runs one git command as the account's user (see the module docstring)."""

    async def run(
        self,
        args: Sequence[str],
        *,
        account: LinuxAccount,
        cwd: str | None,
        timeout_s: float,
        ceiling: str | None = None,
    ) -> GitResult:
        """``git <args>`` in ``cwd``; ``ceiling`` stops repository discovery."""
        ...


def command_name(args: Sequence[str]) -> str:
    """The git sub-command of ``args``: the first word after any leading global
    options, ``-c key=value`` (a command that must override the repository's own
    configuration, PAW-035's ``merge``, puts them first) and ``--git-dir=<path>``
    / ``--work-tree=<path>`` (PAW-035 runs git in a Worker's worktree with the
    worktree's git directory in the checkout, never the one the worktree names),
    else ``"git"``. What a failure is logged and reported as, never an
    argument."""
    index = 0
    while index < len(args):
        if args[index] == "-c" and index + 1 < len(args):
            index += 2
        elif args[index].startswith(("--git-dir=", "--work-tree=")):
            index += 1
        else:
            break
    return args[index] if index < len(args) else "git"


class _OutputTooLarge(Exception):
    pass


async def _drain(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await stream.read(8192):
        total += len(chunk)
        if total > limit:
            raise _OutputTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def run_subprocess(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    timeout_s: float,
    max_output_bytes: int,
    log_name: str,
) -> GitResult:
    """Run ``argv`` to completion and return its result, or raise a closed
    :class:`GitFailure`.

    Shared by every :class:`GitRunner`: the executable is missing
    (``NOT_INSTALLED``), the whole process group is killed if ``timeout_s``
    passes (``TIMEOUT``) or if a stream writes more than ``max_output_bytes``
    (``OUTPUT_TOO_LARGE``), the same on ``asyncio.CancelledError`` (the ``finally``
    below runs regardless of why the ``try`` exited), and output must decode as
    UTF-8 (``UNSAFE_OUTPUT``). ``log_name`` is the only thing a failure logs: never
    an argument, a path or output (the module docstring). The caller has already
    decided the executable, every argument, the environment and the identity this
    runs as; this function knows none of that.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, killed as one
        )
    except OSError:
        raise GitCommandError(log_name, GitFailure.NOT_INSTALLED) from None
    assert process.stdout is not None and process.stderr is not None
    readers = [
        asyncio.ensure_future(_drain(process.stdout, max_output_bytes)),
        asyncio.ensure_future(_drain(process.stderr, max_output_bytes)),
    ]
    failure: GitFailure | None = None
    output = b""
    try:
        async with asyncio.timeout(timeout_s):
            output, _ = await asyncio.gather(*readers)
            await process.wait()
    except TimeoutError:
        failure = GitFailure.TIMEOUT
    except _OutputTooLarge:
        failure = GitFailure.OUTPUT_TOO_LARGE
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await process.wait()
    if failure is not None:
        logger.warning("git %s stopped (%s)", log_name, failure.value)
        raise GitCommandError(log_name, failure)
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise GitCommandError(log_name, GitFailure.UNSAFE_OUTPUT) from None
    return GitResult(process.returncode or 0, text)


def git_environment(
    account: LinuxAccount, *, path: str = SAFE_PATH, ceiling: str | None = None
) -> dict[str, str]:
    """The whole environment of a git child: a fixed allowlist, nothing inherited."""
    environment = {
        "PATH": path,
        "HOME": account.home,
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
    }
    if ceiling is not None:
        environment["GIT_CEILING_DIRECTORIES"] = ceiling
    return environment


def redirects_push(key: str) -> bool:
    """Whether the configuration ``key`` sends a push elsewhere than the URL it
    names: ``url.<base>.insteadOf`` / ``pushInsteadOf`` rewrite it (Decision
    0052). A named remote's ``url`` / ``pushurl`` do not apply: the push names
    a URL, not a remote."""
    key = key.lower()
    section, _, rest = key.partition(".")
    variable = key.rpartition(".")[2]
    return (
        section == "url" and "." in rest and variable in ("insteadof", "pushinsteadof")
    )


def push_redirected(listed: str) -> bool:
    """Whether the output of ``git config --show-scope --list -z`` has a
    :func:`redirects_push` key outside the scope ``command``; output that is
    not that shape counts as one (refused, never guessed)."""
    words = listed.split("\0")
    if words[-1:] == [""]:
        words.pop()
    if len(words) % 2:
        return True
    for scope, entry in zip(words[::2], words[1::2], strict=True):
        if scope != "command" and redirects_push(entry.partition("\n")[0]):
            return True
    return False


def validate_allowed_protocols(allowed_protocols: Collection[str]) -> tuple[str, ...]:
    """A non-empty tuple of protocol names, or ``ValueError``.

    Shared by every :class:`GitRunner` construction (local or SSH): a caller-built
    collection is never trusted to already hold safe values.
    """
    protocols = tuple(allowed_protocols)
    if not protocols or not all(
        isinstance(p, str) and _PROTOCOLS.fullmatch(p) for p in protocols
    ):
        raise ValueError("allowed_protocols must be protocol names")
    return protocols


def git_config_arguments(
    allowed_protocols: Collection[str], extra_config: Sequence[tuple[str, str]] = ()
) -> list[str]:
    """The ``-c`` options in front of every command (the module docstring)."""
    pairs = [
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
        ("submodule.recurse", "false"),
        ("protocol.allow", "never"),
    ]
    pairs.extend((f"protocol.{name}.allow", "always") for name in allowed_protocols)
    pairs.extend(extra_config)
    arguments: list[str] = []
    for key, value in pairs:
        arguments.extend(("-c", f"{key}={value}"))
    return arguments


class SubprocessGitRunner:
    """Runs the ``git`` executable as a child process of the backend.

    ``allowed_protocols`` are the transports a command may use (``https``; a test
    adds ``file`` to clone from a local bare repository). ``extra_config`` are
    ``(key, value)`` pairs added after the built-in ones, **fixed at construction,
    never from a caller** (the seam PAW-028 uses for a credential helper; a test
    uses ``url.<base>.insteadOf`` to send ``https://github.com/`` to a local
    repository).
    """

    def __init__(
        self,
        *,
        git_executable: str | None = None,
        allowed_protocols: Collection[str] = ("https",),
        extra_config: Sequence[tuple[str, str]] = (),
        path: str = SAFE_PATH,
        max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
    ) -> None:
        protocols = validate_allowed_protocols(allowed_protocols)
        if isinstance(max_output_bytes, bool) or not (
            isinstance(max_output_bytes, int) and max_output_bytes >= 1
        ):
            raise ValueError("max_output_bytes must be a positive int")
        self._executable = git_executable
        self._protocols = protocols
        self._extra = tuple((str(k), str(v)) for k, v in extra_config)
        self._path = path
        self._max_output = max_output_bytes

    def _git(self) -> str:
        if self._executable is None:
            found = shutil.which("git", path=self._path)
            if found is None:
                raise GitCommandError("run", GitFailure.NOT_INSTALLED)
            self._executable = found
        return self._executable

    async def run(
        self,
        args: Sequence[str],
        *,
        account: LinuxAccount,
        cwd: str | None,
        timeout_s: float,
        ceiling: str | None = None,
    ) -> GitResult:
        name = command_name(args)
        if account.uid != os.geteuid():
            raise GitCommandError(name, GitFailure.IDENTITY_MISMATCH)
        prefix = [self._git(), *git_config_arguments(self._protocols, self._extra)]
        environment = git_environment(account, path=self._path, ceiling=ceiling)
        if name == "push":
            listed = await run_subprocess(
                [*prefix, "config", "--includes", "--show-scope", "--list", "-z"],
                env=environment,
                cwd=cwd,
                timeout_s=timeout_s,
                max_output_bytes=self._max_output,
                log_name=name,
            )
            if listed.returncode != 0:
                raise GitCommandError(name, GitFailure.NONZERO_EXIT)
            if push_redirected(listed.stdout):
                logger.warning("git push refused (%s)", "unsafe_configuration")
                raise GitCommandError(name, GitFailure.UNSAFE_CONFIGURATION)
        return await run_subprocess(
            [*prefix, *args],
            env=environment,
            cwd=cwd,
            timeout_s=timeout_s,
            max_output_bytes=self._max_output,
            log_name=name,
        )


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    """What git says about an existing repository (all of it untrusted, validated).

    ``default_branch`` is the branch ``origin/HEAD`` points at, else the current
    branch. ``head`` is the commit id ``HEAD`` points at (``None``: no commit yet).
    ``origin_url`` is the raw ``remote.origin.url`` (or ``None``), **not** a
    canonical or a safe URL: ``remotes.py``-style checks belong to the caller.
    """

    default_branch: str
    current_branch: str | None
    head: str | None
    origin_url: str | None


def _gh_credential_helper_args(gh_executable: str) -> tuple[str, str]:
    """``git clone``'s own ``-c`` (documented, and applied before the fetch
    itself, unlike a plain ``-c`` before the subcommand which only ever reaches
    ``.git/config``): the same string ``gh auth setup-git`` writes for
    ``gh_executable``. Never built from caller input beyond that one fixed
    path -- ``gh`` resolves the account's own ``gh auth login`` from ``HOME``
    (already the only identity git's own environment allowlist carries; see
    :func:`git_environment`).
    """
    return ("-c", f"credential.helper=!{gh_executable} auth git-credential")


class GitClient:
    """The few git operations the service needs, each with fixed arguments.

    ``credential_helper`` (PAW-028) makes :meth:`clone` add gh's own credential
    helper to the clone, so a private GitHub repository can be cloned under the
    acting Linux user's own ``gh auth login`` -- this backend never sees or
    stores the token. Fixed at construction like ``extra_config``
    (:class:`SubprocessGitRunner`), never a per-call choice. ``gh_executable``
    (default ``"gh"``, resolved from git's own fixed ``PATH``, ``SAFE_PATH`` --
    never the caller's) must name the same binary a ``gh_runner`` given to
    ``RepositoryService.from_policy`` was configured with
    (``SubprocessGhRunner(gh_executable=...)``), so the credential helper `gh`
    invokes during a clone is the one that is actually installed; git's own
    ``PATH`` does not necessarily agree with the runner's (Codex review: a
    ``gh`` outside ``SAFE_PATH`` made every private clone fail with the helper
    not found, even though status checks and repository creation, which go
    through the configured ``gh_runner`` directly, worked).
    """

    def __init__(
        self,
        runner: GitRunner,
        policy: RepositoryPolicy,
        *,
        credential_helper: bool = False,
        gh_executable: str = "gh",
    ) -> None:
        if type(credential_helper) is not bool:
            raise TypeError("credential_helper must be a bool")
        if type(gh_executable) is not str or not gh_executable:
            raise TypeError("gh_executable must be a non-empty str")
        self._runner = runner
        self._policy = policy
        self._credential_helper = credential_helper
        self._gh_executable = gh_executable

    async def _run(
        self,
        args: Sequence[str],
        account: LinuxAccount,
        *,
        cwd: str | None,
        timeout_s: float | None = None,
        ceiling: str | None = None,
    ) -> GitResult:
        return await self._runner.run(
            args,
            account=account,
            cwd=cwd,
            timeout_s=self._policy.git_timeout_s if timeout_s is None else timeout_s,
            ceiling=ceiling,
        )

    async def _checked(
        self, args: Sequence[str], account: LinuxAccount, **options
    ) -> str:
        result = await self._run(args, account, **options)
        if result.returncode != 0:
            raise GitCommandError(args[0], GitFailure.NONZERO_EXIT)
        return result.stdout

    async def inspect(self, path: str, account: LinuxAccount) -> RepositoryFacts:
        """Ask git about the (already path-checked) repository at ``path``.

        git is asked where the work tree and the git directory are, and the answer
        must be ``path`` and ``path/.git``: a ``core.worktree`` in the repository's
        own configuration, or any other trick that points git elsewhere, is
        ``GIT_TRICK``; ``core.bare`` makes it a bare repository (``BARE``).
        Repository discovery is stopped at the parent of ``path`` so that git can
        never adopt a repository above it.
        """
        parent = os.path.dirname(path) or "/"
        options = {"cwd": path, "ceiling": parent}
        state = await self._run(
            ["rev-parse", "--is-bare-repository"], account, **options
        )
        if state.returncode != 0 or state.stdout not in ("false\n", "true\n"):
            raise PathRejectedError(PathProblem.NOT_A_REPOSITORY)
        if state.stdout != "false\n":
            raise PathRejectedError(PathProblem.BARE)
        places = await self._run(
            ["rev-parse", "--show-toplevel", "--absolute-git-dir"], account, **options
        )
        lines = places.stdout.split("\n")
        if places.returncode != 0 or len(lines) != 3 or lines[2] != "":
            raise PathRejectedError(PathProblem.NOT_A_REPOSITORY)
        resolved = await asyncio.to_thread(
            lambda: (os.path.realpath(lines[0]), os.path.realpath(lines[1]))
        )
        if resolved != (path, f"{path}/.git"):
            raise PathRejectedError(PathProblem.GIT_TRICK)

        branch = await self._branch("HEAD", account, options)
        origin_head = await self._branch("refs/remotes/origin/HEAD", account, options)
        if origin_head is not None:
            origin_head = origin_head.removeprefix("origin/")
        default = origin_head or branch
        if default is None:
            raise PathRejectedError(PathProblem.DEFAULT_BRANCH_UNKNOWN)
        try:
            default = validate_branch(default)
            if branch is not None:
                branch = validate_branch(branch)
        except ValueError:
            raise PathRejectedError(PathProblem.UNSUPPORTED_BRANCH) from None

        head = await self._run(
            ["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], account, **options
        )
        commit = head.stdout.strip() if head.returncode == 0 else None
        if commit is not None and _OBJECT_ID.fullmatch(commit) is None:
            raise GitCommandError("rev-parse", GitFailure.UNSAFE_OUTPUT)
        # ``--local``: only this repository's own file, and includes stay off.
        origin = await self._run(
            ["config", "--local", "--get", "remote.origin.url"], account, **options
        )
        url = origin.stdout.strip() if origin.returncode == 0 else None
        if url is not None and (
            not url or len(url) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in url)
        ):
            raise GitCommandError("config", GitFailure.UNSAFE_OUTPUT)
        return RepositoryFacts(default, branch, commit, url)

    async def _branch(
        self, ref: str, account: LinuxAccount, options: Mapping[str, object]
    ) -> str | None:
        result = await self._run(
            ["symbolic-ref", "--quiet", "--short", ref], account, **options
        )
        if result.returncode != 0:
            return None  # detached, or the reference does not exist
        name = result.stdout.strip()
        return name or None

    async def clone(
        self,
        url: str,
        destination: str,
        account: LinuxAccount,
        *,
        branch: str | None = None,
    ) -> None:
        """``git clone`` ``url`` into the existing empty directory ``destination``.

        ``url`` and ``branch`` are validated by the caller; both are placed where
        git cannot read them as options (``--branch <b>`` and after ``--``).
        ``credential_helper`` (PAW-028), when this client was built with one,
        is added as ``clone``'s own ``-c`` so a private ``https`` remote can
        authenticate; without it a private remote fails exactly as before.
        """
        args = ["clone", "--quiet"]
        if self._credential_helper:
            args.extend(_gh_credential_helper_args(self._gh_executable))
        if branch is not None:
            args.extend(("--branch", branch))
        args.extend(("--", url, destination))
        await self._checked(
            args,
            account,
            cwd=os.path.dirname(destination) or "/",
            timeout_s=self._policy.clone_timeout_s,
        )

    async def init(
        self, destination: str, account: LinuxAccount, *, initial_branch: str
    ) -> None:
        """``git init`` in the existing empty directory ``destination``.

        No template directory is copied (``--template=``), and no file is created
        or committed: the repository is empty, so nothing of the workspace is
        injected into it (``REQUIREMENTS.md``).
        """
        await self._checked(
            [
                "init",
                "--quiet",
                "--template=",
                f"--initial-branch={initial_branch}",
                "--",
                destination,
            ],
            account,
            cwd=destination,
        )

    async def add_origin(self, path: str, url: str, account: LinuxAccount) -> None:
        """``git remote add origin <url>`` (``url`` is a validated ``https`` URL)."""
        await self._checked(
            ["remote", "add", "--", "origin", url],
            account,
            cwd=path,
            ceiling=os.path.dirname(path) or "/",
        )
