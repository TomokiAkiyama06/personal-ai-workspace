"""Running git as another Linux user over SSH (Issue #105, addendum to PAW-027).

Decision 0017 §4 (Approved) runs git as the backend process's own Linux user and
refuses anything else (``GitFailure.IDENTITY_MISMATCH``): a deployment where the
backend is one service account cannot make a per-user checkout that way. The
human's direction on Issue #105 (2026-09-25) is to reach the account's *own*
Linux user through a single, tightly restricted, non-interactive SSH connection
instead — ``ssh <linux user>@127.0.0.1`` with a key that exists only for this
purpose — and let a **forced command** on the other end (``command=`` in that
user's ``authorized_keys``, plus ``from="127.0.0.1"``, ``no-pty``,
``no-agent-forwarding``, ``no-port-forwarding``, ``no-X11-forwarding``) run git,
never a shell. Decision 0029 (``docs/decisions/0029-per-user-git-runner-ssh.md``,
approved 2026-09-27) works out the details: the wrapper's allowed sub-commands, key
rotation and revocation, error handling, and the migration from
``SubprocessGitRunner``.

**What this module is.** :class:`SshGitRunner`, the client half of that seam: it
builds the ``ssh`` invocation and the one string carried as the remote command,
exactly the way :class:`~paw_backend.repositories.git.SubprocessGitRunner` builds
a local ``git`` invocation, and shares the same process-running, timeout and
output-limit machinery (``paw_backend.repositories.git._run_subprocess``).

**What this module is not.** The *server* half — the per-user key, the
``authorized_keys`` line and the wrapper program the forced command names — is
deployment work, described in Decision 0029 but not shipped by it: nothing here
creates a Linux account, writes a key or an ``authorized_keys`` file, or execs
git directly. A wrong or missing key, connection failure, or the account's Linux
user being missing all surface the same way: :class:`GitFailure.SSH_UNAVAILABLE`
(``ssh`` itself exited 255 — its own convention for "the command never ran",
distinct from git's exit code) or :class:`GitFailure.SSH_KEY_UNAVAILABLE` (this
side could not read a usable key file at all).

The wire format the wrapper must parse
--------------------------------------
``ssh`` joins the arguments given after the destination into one string with a
single space each (it does no quoting of its own), delivered to the forced
command as ``$SSH_ORIGINAL_COMMAND``. :func:`build_remote_command` builds that
string as **one** argument (so nothing here depends on how ``ssh`` would have
joined several), from these fields, each individually ``shlex.quote``-d so that a
wrapper that unquotes with POSIX word-splitting rules (never ``eval`` on
unquoted text) recovers exactly this list, never fewer or more words than were
sent:

1. ``PROTOCOL_TAG`` (``"paw-git-run/v1"``) — a wrapper must refuse anything that
   does not begin with the version tag it implements, rather than guess.
2. the working directory git should run in (already validated by
   ``paths.py`` / ``validation.py`` on this side; the wrapper must still confirm
   it is inside *that Linux user's own* checkout root before using it — this
   module has no way to prove that from the client side).
3. ``GIT_CEILING_DIRECTORIES``, or ``-`` for "not set".
4. ``--`` (a fixed separator, present even though word 5 also starts the real
   argument list, so a parser never has to guess where options end).
5. onward: the ``-c key=value`` pairs from :func:`~paw_backend.repositories.git.
   git_config_arguments` (informational only: a wrapper must **not** trust these
   and should apply its own fixed hardening instead, then the git sub-command and
   its arguments, restricted to the allowlist in Decision 0029 (``rev-parse``,
   ``symbolic-ref``, ``config`` (read-only forms, and ``remote add`` — never
   arbitrary keys), ``clone``, ``init``, ``remote``: the only sub-commands
   :class:`~paw_backend.repositories.git.GitClient` ever issues).

Nothing above is a security boundary by itself: the boundary is the restricted
key plus ``command=`` (the client cannot make the server run anything else) and
the wrapper's own re-validation (the client's word is not trusted for the
sub-command allowlist or the working directory). This module only has to be a
*correct*, unambiguous encoding for that wrapper to check.
"""

import asyncio
import os
import shlex
import shutil
import stat
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

from paw_backend.config import Settings
from paw_backend.repositories.errors import GitCommandError, GitFailure
from paw_backend.repositories.git import (
    SAFE_PATH,
    GitResult,
    command_name,
    git_config_arguments,
    run_subprocess,
    validate_allowed_protocols,
)
from paw_backend.repositories.limits import (
    DEFAULT_SSH_CONNECT_TIMEOUT_S,
    DEFAULT_SSH_PORT,
    MAX_GIT_OUTPUT_BYTES,
    MAX_SSH_CONNECT_TIMEOUT_S,
)
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.tools.scope import TargetError, normalise_host, normalise_path

#: The first word of every remote command: a wrapper that does not recognise it
#: (an older or newer protocol version) must refuse rather than guess.
PROTOCOL_TAG = "paw-git-run/v1"

#: ``ssh``'s own convention: 255 means ssh itself never delivered a remote
#: command result (unreachable host, rejected key, host key mismatch, the
#: forced command's shell failing to start because the account does not exist
#: or is locked); every other code (0-254) is the remote command's own exit
#: status and must never be treated as a transport failure.
SSH_TRANSPORT_FAILURE_CODE = 255

_SSH_EXECUTABLE_NAME = "ssh"


class SshKeyDirectory(Protocol):
    """Finds the private key file that authenticates as one account's Linux user.

    ``key_path_of`` returns an absolute path this process may read, or raises
    ``OSError`` (``FileNotFoundError`` / ``PermissionError``, its usual
    subclasses) when there is none: :class:`SshGitRunner` turns that into
    :class:`GitFailure.SSH_KEY_UNAVAILABLE`, never a bare traceback. It never
    returns a key for a user other than ``account`` (mirrors
    ``AccountDirectory.account_of``'s contract for the same reason: a wrong
    answer here would let one user's checkout be reached with another's key).
    """

    async def key_path_of(self, account: LinuxAccount) -> str: ...


def validate_identity_template(value: object) -> str:
    """A path template for a per-user private key: absolute, canonical, and
    naming exactly one placeholder, ``{user}`` (the Linux user name).

    Unlike ``paths.validate_root_template`` this never accepts ``{home}``: a key
    the backend alone may read must not live inside a directory the workspace
    user (who that key impersonates) can write to or replace.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("an identity template must be a non-empty string")
    if "{home}" in value:
        raise ValueError("an identity template cannot use {home}")
    if value.count("{user}") != 1:
        raise ValueError("an identity template must contain {user} exactly once")
    probe = value.replace("{user}", "probe")
    if "{" in probe or "}" in probe:
        raise ValueError("an identity template has a stray brace")
    try:
        canonical = normalise_path(probe)
    except TargetError:
        raise ValueError("an identity template is not a valid absolute path") from None
    if canonical != probe or canonical == "/":
        raise ValueError("an identity template must be a canonical absolute path")
    return value


class TemplateSshKeyDirectory:
    """``identity_template`` with ``{user}`` filled in: one key file per account.

    The file must exist and be exactly what only this process may use as a
    credential: a regular file (no symbolic link anywhere in its path), owned by
    this process's own effective user, unreadable and unwritable by anyone else
    (mode bits for group and other are both zero). Any other state is refused as
    :class:`FileNotFoundError` / :class:`PermissionError`; the caller never sees
    *why* beyond that (this module's own errors carry no path — see
    ``errors.py``). Deploying such a directory and its keys (an Admin operation)
    is Decision 0029's concern, not this class's.
    """

    def __init__(self, template: str = "/etc/paw/ssh-keys/{user}.key") -> None:
        self._template = validate_identity_template(template)

    async def key_path_of(self, account: LinuxAccount) -> str:
        path = self._template.replace("{user}", account.username)
        return await asyncio.to_thread(self._check, path)

    @staticmethod
    def _check(path: str) -> str:
        try:
            resolved = os.path.realpath(path, strict=True)
        except OSError:
            raise FileNotFoundError(path) from None
        if resolved != path:
            raise PermissionError("identity file path is not fully resolved")
        info = os.lstat(resolved)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError("identity file is not a regular file")
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError("identity file is readable or writable by others")
        if info.st_uid != os.geteuid():
            raise PermissionError("identity file is not owned by this process")
        return resolved


@dataclass(frozen=True, slots=True)
class SshGitRunnerPolicy:
    """Validated settings for :class:`SshGitRunner` (``PAW_REPOSITORY_SSH_*``).

    ``host`` is the loopback address the backend connects to (``127.0.0.1`` by
    default, matching the ``from="127.0.0.1"`` restriction Decision 0029 puts on
    the key: connecting to ``localhost`` can resolve to ``::1`` first depending
    on host configuration, which that restriction would then refuse). An IP
    literal is accepted here (unlike ``RepositoryPolicy.clone_hosts``, which
    forbids one against SSRF to an attacker-supplied host): this destination is
    fixed deployment configuration, never derived from a request.
    ``known_hosts_path`` is the pinned host key file (Decision 0029: the host key
    is fixed, never learned on first use); there is no default that is not a
    guess about the deployment, so a missing or wrong file simply makes every
    call fail closed with :class:`GitFailure.SSH_UNAVAILABLE` (``ssh`` refuses an
    unrecognised host key with strict checking on).
    """

    host: str = "127.0.0.1"
    port: int = DEFAULT_SSH_PORT
    connect_timeout_s: int = DEFAULT_SSH_CONNECT_TIMEOUT_S
    known_hosts_path: str = "/etc/paw/ssh_known_hosts"

    def __post_init__(self) -> None:
        try:
            host = normalise_host(self.host)
        except TargetError:
            raise ValueError("host is not a valid host name or address") from None
        if host != self.host:
            raise ValueError("host must be the canonical spelling")
        object.__setattr__(self, "host", host)
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65_535
        ):
            raise ValueError("port must be an int from 1 to 65535")
        if (
            isinstance(self.connect_timeout_s, bool)
            or not isinstance(self.connect_timeout_s, int)
            or not 1 <= self.connect_timeout_s <= MAX_SSH_CONNECT_TIMEOUT_S
        ):
            raise ValueError(
                "connect_timeout_s must be an int from 1 to "
                f"{MAX_SSH_CONNECT_TIMEOUT_S}"
            )
        try:
            known_hosts = normalise_path(self.known_hosts_path)
        except TargetError:
            raise ValueError("known_hosts_path is not a valid absolute path") from None
        if known_hosts != self.known_hosts_path:
            raise ValueError("known_hosts_path must be a canonical absolute path")

    @classmethod
    def from_settings(cls, settings: Settings) -> "SshGitRunnerPolicy":
        """The policy of the ``PAW_REPOSITORY_SSH_*`` settings."""
        return cls(
            host=settings.repository_ssh_host,
            port=settings.repository_ssh_port,
            connect_timeout_s=settings.repository_ssh_connect_timeout_seconds,
            known_hosts_path=settings.repository_ssh_known_hosts_path,
        )


def build_remote_command(
    args: Sequence[str],
    *,
    cwd: str | None,
    ceiling: str | None,
    allowed_protocols: Collection[str],
    extra_config: Sequence[tuple[str, str]] = (),
) -> str:
    """The one string sent as the SSH remote command (the module docstring).

    Every word is quoted on its own (``shlex.quote``), then joined with plain
    spaces: a wrapper that unquotes with POSIX word-splitting rules recovers
    exactly this list of words, whatever ``cwd`` or ``args`` contain (a space, a
    quote, a newline). Pure and synchronous: no process, no file, no network.
    """
    words = [
        PROTOCOL_TAG,
        cwd if cwd is not None else ".",
        ceiling if ceiling is not None else "-",
        "--",
        *git_config_arguments(allowed_protocols, extra_config),
        *args,
    ]
    return " ".join(shlex.quote(word) for word in words)


class SshGitRunner:
    """Runs git as the account's own Linux user over a restricted SSH connection.

    ``keys`` resolves the per-user private key (:class:`SshKeyDirectory`;
    :class:`TemplateSshKeyDirectory` is the usual one). ``allowed_protocols`` and
    ``extra_config`` have the same meaning and the same trust boundary as
    :class:`~paw_backend.repositories.git.SubprocessGitRunner`'s: **fixed at
    construction, never from a caller**, sent to the wrapper as part of the
    encoded command for it to apply (the wrapper is not obliged to trust them —
    see the module docstring — but this runner offers the same seam PAW-028
    needs for a credential helper).

    Every call is a fresh, non-interactive ``ssh`` process: no ``ControlMaster``,
    no agent, no local ``ssh`` configuration (``-F`` points at
    ``/dev/null`` unless overridden), a fixed, pinned host key file, and a local
    environment of just ``PATH`` (none of the backend's own environment, and no
    inherited ``SSH_*`` variable, reaches the child — the same allowlist
    philosophy as ``git_environment``). None of that is optional per call: a
    caller cannot loosen it through ``args``.
    """

    def __init__(
        self,
        keys: SshKeyDirectory,
        *,
        policy: SshGitRunnerPolicy | None = None,
        allowed_protocols: Collection[str] = ("https",),
        extra_config: Sequence[tuple[str, str]] = (),
        ssh_executable: str | None = None,
        ssh_config_path: str = "/dev/null",
        path: str = SAFE_PATH,
        max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
    ) -> None:
        if not hasattr(keys, "key_path_of"):
            raise TypeError("keys must be an SshKeyDirectory")
        if policy is not None and not isinstance(policy, SshGitRunnerPolicy):
            raise TypeError("policy must be an SshGitRunnerPolicy")
        protocols = validate_allowed_protocols(allowed_protocols)
        if isinstance(max_output_bytes, bool) or not (
            isinstance(max_output_bytes, int) and max_output_bytes >= 1
        ):
            raise ValueError("max_output_bytes must be a positive int")
        try:
            ssh_config = normalise_path(ssh_config_path)
        except TargetError:
            raise ValueError("ssh_config_path is not a valid absolute path") from None
        if ssh_config != ssh_config_path:
            raise ValueError("ssh_config_path must be a canonical absolute path")
        self._keys = keys
        self._policy = policy or SshGitRunnerPolicy()
        self._protocols = protocols
        self._extra = tuple((str(k), str(v)) for k, v in extra_config)
        self._executable = ssh_executable
        self._ssh_config = ssh_config_path
        self._path = path
        self._max_output = max_output_bytes

    def _ssh(self) -> str:
        if self._executable is None:
            found = shutil.which(_SSH_EXECUTABLE_NAME, path=self._path)
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
        try:
            identity = await self._keys.key_path_of(account)
        except OSError:
            raise GitCommandError(name, GitFailure.SSH_KEY_UNAVAILABLE) from None
        policy = self._policy
        command = build_remote_command(
            args,
            cwd=cwd,
            ceiling=ceiling,
            allowed_protocols=self._protocols,
            extra_config=self._extra,
        )
        argv = [
            self._ssh(),
            "-i",
            identity,
            "-F",
            self._ssh_config,
            "-p",
            str(policy.port),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={policy.known_hosts_path}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"ConnectTimeout={policy.connect_timeout_s}",
            "-o",
            "RequestTTY=no",
            "-o",
            "ForwardAgent=no",
            "-o",
            "ForwardX11=no",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "PermitLocalCommand=no",
            "-o",
            "LogLevel=ERROR",
            "--",
            f"{account.username}@{policy.host}",
            command,
        ]
        result = await run_subprocess(
            argv,
            env={"PATH": self._path},
            cwd=None,
            timeout_s=timeout_s,
            max_output_bytes=self._max_output,
            log_name=name,
        )
        if result.returncode == SSH_TRANSPORT_FAILURE_CODE:
            raise GitCommandError(name, GitFailure.SSH_UNAVAILABLE)
        return result
