"""``gh auth`` per Linux user: connection status, and the ``GitHubGateway`` seam
this issue fills (PAW-028; ``REQUIREMENTS.md`` "Git / GitHub").

What it does
------------
V1 does not use a GitHub App; each Linux user runs their own ``gh auth login``
(``~/.config/gh``, an SSH key, a git identity, all separated by the OS). This module
never holds, reads or shows a token or a private key: they live only in the
account's own ``~/.config/gh``, on disk, and this module runs ``gh`` **as that
account** and reads only the parts of its output that name what is needed --
whether an account is logged in to a host, and its GitHub login (a public
username) -- never a token (``--show-token`` is never passed; the JSON ``gh``
gives without it has no field wide enough to hold one) and never a private key
(SSH keys are not a ``gh auth status`` concern at all).

* :class:`GhRunner` / :class:`SubprocessGhRunner` run one ``gh`` subcommand safely:
  same discipline as :mod:`paw_backend.repositories.git` (an allow-listed
  environment, a bounded timeout and output size, no shell). **Only as the
  account's own user**: exactly :class:`~paw_backend.repositories.git.GitRunner`,
  it is refused (:class:`GhCommandError`, ``IDENTITY_MISMATCH``) unless the
  backend's own process is that account's Linux user -- Decision 0017 (Approved),
  section 4, names this as PAW-028's own limitation too ("PAW-028
  (``gh auth`` は Linux User ごと) も同じ仕組みを必要とする"): a backend that runs
  as one Linux user cannot yet act as another's until a deployment supplies a
  :class:`GhRunner` that really switches identity (Issue #105, the same seam
  ``GitRunner`` waits on). This PR does not add that switch.
* :class:`GitHubConnectionService` recognises a Linux user's ``gh auth status``
  (the acceptance criterion "Linux UserごとのGitHub認証状態を認識"): a plain
  connected / not-connected state and, only when connected, the GitHub login --
  never more. Viewing one's own is ``github.use`` (``Scope.SELF``, Decision 0004);
  viewing another user's (an Admin UI) is ``admin.usage.view`` (Decision 0004's
  existing "per-user Usage Dashboard" capability -- ``docs/SECURITY_RBAC_AUDIT.md``
  already lists "Repos/PRs" among what it shows; no capability is added here). The
  Authorizer audits every decision (``REQUIRED``), so who checked whose connection
  is on the audit trail even though the check itself calls no API and stores
  nothing.
* :class:`GhCliGitHubGateway` fulfils the seam
  :class:`~paw_backend.repositories.github.GitHubGateway` that ``create_github``
  calls (the acceptance criterion "issue / PR / API操作を対象User identityで実行"):
  ``gh repo create`` runs as the acting user's own Linux account, so the repository
  is created under **their** ``gh auth login``, never a workspace-wide credential.
  Its caller (``RepositoryService._create_on_github``) already treats a gateway as
  foreign code: every exception from here becomes ``GitHubUnavailableError`` and is
  logged by type only, so this class raises whatever it finds naturally instead of
  swallowing it.

What is not here
-----------------
* Starting ``gh auth login`` itself (the interactive / browser device flow):
  ``REQUIREMENTS.md`` says a GUI may start it, but the flow is inherently
  interactive (a browser, a one-time code) and runs in the account's own shell;
  there is no HTTP layer in this repository yet for any repository-module
  concern (``RepositoryService``'s own docstring says the same). Nothing here
  drives ``gh auth login``; an operator (or the user, at a terminal) runs it, and
  this module only observes the result.
* Revoking / re-authenticating through an API, and multiple workspace users
  sharing one Linux account: out of scope (see the PR description).
* Creating an issue or a pull request: no consumer of that exists yet
  (``pr.create`` / ``project.pr.create`` are declared capabilities with no
  service behind them). :class:`GhRunner` is the reusable route a later issue
  runs those through, as the correct Linux identity, the same way this module's
  own :class:`GhCliGitHubGateway` does for repository creation.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from paw_backend.authz import Authorizer, Capability, Principal, Resource
from paw_backend.authz.policy import Reason
from paw_backend.repositories.accounts import AccountDirectory
from paw_backend.repositories.errors import (
    GhCommandError,
    GhFailure,
    InputProblem,
    InvalidRepositoryInputError,
    RepositoryPermissionDeniedError,
)
from paw_backend.repositories.git import SAFE_PATH
from paw_backend.repositories.github import GitHubRepo, parse_github_source
from paw_backend.repositories.limits import DEFAULT_GH_TIMEOUT_S, MAX_GH_OUTPUT_BYTES
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.validation import validate_uuid

logger = logging.getLogger(__name__)

# The audit resource kind (``Resource(kind=..., ...)``); never a table name.
_RESOURCE_KIND = "github_connection"

# A GitHub login (username): the same shape ``repositories/github.py`` accepts for
# an owner. A ``gh`` that returned anything else is not trusted with it.
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class GitHubConnectionState(StrEnum):
    """What is shown for a Linux user's GitHub connection. Nothing else exists."""

    CONNECTED = "connected"
    NOT_CONNECTED = "not_connected"


@dataclass(frozen=True, slots=True)
class GitHubConnectionStatus:
    """One host's answer for one Linux account. Never a token; never a private key.

    ``login`` is set exactly when ``state`` is ``CONNECTED`` (the GitHub username
    ``gh`` reports as the active account of that host) -- the only metadata
    ``REQUIREMENTS.md`` allows an Admin to see beyond the state itself.
    """

    hostname: str
    state: GitHubConnectionState
    login: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.hostname, str) or not self.hostname:
            raise ValueError("hostname must be a non-empty string")
        if not isinstance(self.state, GitHubConnectionState):
            raise ValueError("state must be a GitHubConnectionState")
        connected = self.state is GitHubConnectionState.CONNECTED
        if connected != (self.login is not None):
            raise ValueError("login is set exactly when state is CONNECTED")
        if self.login is not None and _LOGIN.fullmatch(self.login) is None:
            raise ValueError("login is not a GitHub user name")


@dataclass(frozen=True, slots=True)
class GhResult:
    """A finished ``gh`` command. ``stdout`` is text; stderr is dropped on purpose."""

    returncode: int
    stdout: str


class GhRunner(Protocol):
    """Runs one ``gh`` command as the account's user (module docstring)."""

    async def run(
        self,
        args: Sequence[str],
        *,
        account: LinuxAccount,
        hostname: str,
        timeout_s: float,
    ) -> GhResult: ...


def gh_environment(
    account: LinuxAccount, *, hostname: str, path: str = SAFE_PATH
) -> dict[str, str]:
    """The whole environment of a ``gh`` child: a fixed allowlist, nothing inherited.

    ``GH_CONFIG_DIR`` pins ``gh`` to this account's own ``~/.config/gh``
    (``REQUIREMENTS.md``: separated per Linux user) rather than relying on the
    default resolution from ``HOME``. ``GH_HOST`` fixes the one host a command
    targets (every caller in this module also passes ``--hostname`` explicitly:
    belt and suspenders). The notifier and prompt are disabled: this runs
    unattended and must never block on stdin (there is none: see
    :class:`SubprocessGhRunner`).
    """
    return {
        "PATH": path,
        "HOME": account.home,
        "LC_ALL": "C",
        "LANG": "C",
        "GH_CONFIG_DIR": f"{account.home}/.config/gh",
        "GH_HOST": hostname,
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "NO_COLOR": "1",
    }


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


class SubprocessGhRunner:
    """Runs the ``gh`` executable as a child process of the backend.

    ``gh_executable`` fixes the binary (a test plants a script); by default the
    real ``gh`` is looked up on ``path`` (``SAFE_PATH``: the backend's own ``PATH``
    is never used). No caller-supplied configuration is ever added to the
    argument list beyond what each method here builds (unlike git, ``gh`` has no
    ``-c`` seam to misuse, so there is nothing to fix at construction).
    """

    def __init__(
        self,
        *,
        gh_executable: str | None = None,
        path: str = SAFE_PATH,
        max_output_bytes: int = MAX_GH_OUTPUT_BYTES,
    ) -> None:
        if isinstance(max_output_bytes, bool) or not (
            isinstance(max_output_bytes, int) and max_output_bytes >= 1
        ):
            raise ValueError("max_output_bytes must be a positive int")
        self._executable = gh_executable
        self._path = path
        self._max_output = max_output_bytes

    def _gh(self) -> str:
        if self._executable is None:
            found = shutil.which("gh", path=self._path)
            if found is None:
                raise GhCommandError("run", GhFailure.NOT_INSTALLED)
            self._executable = found
        return self._executable

    async def run(
        self,
        args: Sequence[str],
        *,
        account: LinuxAccount,
        hostname: str,
        timeout_s: float,
    ) -> GhResult:
        name = args[0] if args else "gh"
        if account.uid != os.geteuid():
            raise GhCommandError(name, GhFailure.IDENTITY_MISMATCH)
        argv = [self._gh(), *args]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=account.home,
                env=gh_environment(account, hostname=hostname, path=self._path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # its own process group, killed as one
            )
        except OSError:
            raise GhCommandError(name, GhFailure.NOT_INSTALLED) from None
        assert process.stdout is not None and process.stderr is not None
        readers = [
            asyncio.ensure_future(_drain(process.stdout, self._max_output)),
            asyncio.ensure_future(_drain(process.stderr, self._max_output)),
        ]
        failure: GhFailure | None = None
        output = b""
        try:
            async with asyncio.timeout(timeout_s):
                output, _ = await asyncio.gather(*readers)
                await process.wait()
        except TimeoutError:
            failure = GhFailure.TIMEOUT
        except _OutputTooLarge:
            failure = GhFailure.OUTPUT_TOO_LARGE
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
            logger.warning("gh %s stopped (%s)", name, failure.value)
            raise GhCommandError(name, failure)
        try:
            text = output.decode("utf-8")
        except UnicodeDecodeError:
            raise GhCommandError(name, GhFailure.UNSAFE_OUTPUT) from None
        return GhResult(process.returncode or 0, text)


def _parse_auth_status(stdout: str, hostname: str) -> GitHubConnectionStatus:
    """``gh auth status --json hosts`` (only the current active login, if any).

    Fails closed (:class:`GhCommandError`, ``INVALID_RESPONSE``) on anything that
    is not the documented shape, rather than guessing a state from partial data;
    a host with no active, successful login (including one this backend cannot
    parse enough of to trust) is reported as :data:`GitHubConnectionState.
    NOT_CONNECTED`, never as an error, since "nobody is logged in" is the normal
    case for a user who has not run ``gh auth login`` yet.
    """
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        raise GhCommandError("auth status", GhFailure.INVALID_RESPONSE) from None
    hosts = payload.get("hosts") if isinstance(payload, dict) else None
    if not isinstance(hosts, dict):
        raise GhCommandError("auth status", GhFailure.INVALID_RESPONSE)
    entries = hosts.get(hostname, [])
    if not isinstance(entries, list):
        raise GhCommandError("auth status", GhFailure.INVALID_RESPONSE)
    active = next(
        (e for e in entries if isinstance(e, dict) and e.get("active") is True), None
    )
    if active is None:
        return GitHubConnectionStatus(hostname, GitHubConnectionState.NOT_CONNECTED)
    login = active.get("login")
    if (
        active.get("state") != "success"
        or not isinstance(login, str)
        or _LOGIN.fullmatch(login) is None
    ):
        return GitHubConnectionStatus(hostname, GitHubConnectionState.NOT_CONNECTED)
    return GitHubConnectionStatus(hostname, GitHubConnectionState.CONNECTED, login)


class GitHubConnectionService:
    """Recognises a Linux user's ``gh auth login`` state (module docstring, PAW-028).

    Authorization: viewing one's own status is ``github.use`` (``Scope.SELF``);
    viewing another user's is ``admin.usage.view`` (Decision 0004's existing
    per-user Usage Dashboard capability -- no capability is added for this).
    The Authorizer audits every decision (``REQUIRED``): a failed audit write
    turns an allowed view into a denial, exactly like every other capability in
    this codebase. Only a human principal may call this (``system`` and an
    agent's own principal are refused, ``UNAUTHENTICATED`` /
    ``CAPABILITY_NOT_GRANTED``: there is no delegation story for this read yet).
    """

    def __init__(
        self,
        authorizer: Authorizer,
        accounts: AccountDirectory,
        runner: GhRunner,
        *,
        hosts: Collection[str] = ("github.com",),
        timeout_s: float = DEFAULT_GH_TIMEOUT_S,
    ) -> None:
        if not isinstance(authorizer, Authorizer):
            raise TypeError("authorizer must be an Authorizer")
        if not hasattr(accounts, "account_of"):
            raise TypeError("accounts must be an AccountDirectory")
        if not hasattr(runner, "run"):
            raise TypeError("runner must be a GhRunner")
        hosts = tuple(hosts)
        if not hosts or any(not isinstance(h, str) or not h for h in hosts):
            raise ValueError("hosts must be a non-empty collection of strings")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise TypeError("timeout_s must be a number")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._authorizer = authorizer
        self._accounts = accounts
        self._runner = runner
        self._hosts = hosts
        self._timeout_s = float(timeout_s)

    async def status(
        self, actor: object, user_id: object, *, hostname: str | None = None
    ) -> GitHubConnectionStatus:
        """The connection status of ``user_id``'s Linux account on ``hostname``.

        ``hostname`` defaults to the first configured host
        (``RepositoryPolicy.clone_hosts``, the same list ``create_github`` clones
        and creates on); any other value must be one of the configured hosts.
        Raises :class:`~paw_backend.repositories.errors.LinuxAccountUnavailableError`
        when the user has none, and :class:`GhCommandError` when ``gh`` itself
        could not be asked (missing, an identity mismatch, a timeout, ...): those
        are environment failures, never reported as "not connected".
        """
        principal = self._human(actor)
        target = validate_uuid("user_id", user_id)
        host = self._validate_hostname(hostname)
        await self._authorize_view(principal, target)
        account = await self._accounts.account_of(target)
        result = await self._runner.run(
            ["auth", "status", "--hostname", host, "--json", "hosts"],
            account=account,
            hostname=host,
            timeout_s=self._timeout_s,
        )
        if result.returncode != 0:
            # ``--json`` makes ``gh`` exit 0 for an ordinary "not logged in";
            # a non-zero exit here is ``gh``'s own report of a fatal problem.
            raise GhCommandError("auth status", GhFailure.NONZERO_EXIT)
        return _parse_auth_status(result.stdout, host)

    def _validate_hostname(self, hostname: str | None) -> str:
        if hostname is None:
            return self._hosts[0]
        if not isinstance(hostname, str) or hostname not in self._hosts:
            raise InvalidRepositoryInputError("hostname", InputProblem.HOST_NOT_ALLOWED)
        return hostname

    @staticmethod
    def _human(actor: object) -> Principal:
        if not isinstance(actor, Principal):
            raise RepositoryPermissionDeniedError(Reason.UNAUTHENTICATED)
        return actor

    async def _authorize_view(self, principal: Principal, user_id: uuid.UUID) -> None:
        if user_id == principal.user_id:
            capability = Capability.GITHUB_USE
            resource = Resource.owned_by(user_id, _RESOURCE_KIND, user_id)
        else:
            capability = Capability.ADMIN_USAGE_VIEW
            resource = Resource(kind=_RESOURCE_KIND, id=user_id)
        decision = await self._authorizer.authorize(
            principal, capability, resource, correlation_id=uuid.uuid4()
        )
        if not decision.allowed:
            raise RepositoryPermissionDeniedError(decision.reason)


class GhCliGitHubGateway:
    """Fulfils :class:`~paw_backend.repositories.github.GitHubGateway` with ``gh``.

    ``create_repository`` runs ``gh repo create`` **as the acting user's own
    Linux account** (never a workspace-wide credential): the repository is
    created under whichever GitHub identity that account's own ``gh auth login``
    holds. The result is whatever the last non-blank line of ``gh``'s stdout
    names, parsed exactly as strictly as a caller's own input
    (``parse_github_source``); the caller
    (``RepositoryService._create_on_github``) re-validates it again
    (``check_created_repository``) before it is ever stored, since a gateway is
    foreign-shaped code even though this implementation lives in this package.

    Every failure (``gh`` missing, an identity mismatch, a timeout, a non-zero
    exit -- not logged in, the name is taken, a quota -- or output this module
    cannot parse as a repository) is left to propagate: the caller turns any
    exception from this seam into ``GitHubUnavailableError`` and logs only its
    type, so nothing here needs to pre-digest a failure into that shape itself.
    """

    def __init__(
        self,
        accounts: AccountDirectory,
        runner: GhRunner,
        hosts: Collection[str],
        *,
        timeout_s: float = DEFAULT_GH_TIMEOUT_S,
    ) -> None:
        if not hasattr(accounts, "account_of"):
            raise TypeError("accounts must be an AccountDirectory")
        if not hasattr(runner, "run"):
            raise TypeError("runner must be a GhRunner")
        hosts = tuple(hosts)
        if not hosts or any(not isinstance(h, str) or not h for h in hosts):
            raise ValueError("hosts must be a non-empty collection of strings")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
            raise TypeError("timeout_s must be a number")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._accounts = accounts
        self._runner = runner
        self._hosts = hosts
        self._timeout_s = float(timeout_s)

    async def create_repository(
        self, *, user_id: uuid.UUID, name: str, private: bool
    ) -> GitHubRepo:
        account = await self._accounts.account_of(user_id)
        host = self._hosts[0]
        args = ["repo", "create", name, "--private" if private else "--public"]
        result = await self._runner.run(
            args, account=account, hostname=host, timeout_s=self._timeout_s
        )
        if result.returncode != 0:
            raise GhCommandError("repo create", GhFailure.NONZERO_EXIT)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        candidate = lines[-1] if lines else ""
        return parse_github_source(candidate, self._hosts, "repository")
