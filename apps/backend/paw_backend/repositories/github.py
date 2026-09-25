"""GitHub sources and the seam to the GitHub API (PAW-027, PAW-028).

Cloning needs no API: ``git clone`` of a ``https`` URL on an allowed host. What
this module holds is (1) the strict parsing of "which repository on GitHub" and
(2) :class:`GitHubGateway`, the **seam PAW-028 fills** (GitHub user connection,
``gh auth``): creating a repository on GitHub needs the acting user's own
credentials, which this issue neither holds nor reads. The default gateway
refuses.

Credentials never appear here: a URL has no user information, a command line
carries no token, and the gateway is the only thing that will ever hold one.
"""

import re
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from paw_backend.repositories.errors import (
    GitHubUnavailableError,
    InputProblem,
    InvalidRepositoryInputError,
    RemoteError,
    RemoteProblem,
)
from paw_backend.repositories.limits import MAX_NAME_CHARS
from paw_backend.repositories.validation import (
    is_storable_remote,
    validate_remote_url,
)
from paw_backend.tools.scope import TargetError, normalise_remote

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_URL = re.compile(
    r"https://(?P<host>[A-Za-z0-9.-]{1,253})/(?P<owner>[^/?#@\\\s:]+)/"
    r"(?P<repo>[^/?#@\\\s:]+)/?"
)
_SHORT = re.compile(r"(?P<owner>[^/?#@\\\s:]+)/(?P<repo>[^/?#@\\\s:]+)")


@dataclass(frozen=True, slots=True)
class GitHubRepo:
    """A repository on a GitHub host: ``https://<host>/<owner>/<repo>``.

    ``repo`` has no ``.git`` suffix. ``host`` is lower case.
    """

    host: str
    owner: str
    repo: str

    @property
    def clone_url(self) -> str:
        """What ``git clone`` is given (the ``.git`` spelling)."""
        return f"https://{self.host}/{self.owner}/{self.repo}.git"

    @property
    def remote_urls(self) -> tuple[str, str]:
        """Both spellings an executor may be given (the Tool Broker needs each)."""
        base = f"https://{self.host}/{self.owner}/{self.repo}"
        return base, base + ".git"


def parse_github_source(
    value: object, allowed_hosts: Collection[str], field: str = "source"
) -> GitHubRepo:
    """The repository ``value`` names: ``owner/repo`` or an ``https`` URL of one.

    Only ``https://<host>/<owner>/<repo>[.git][/]`` with ``<host>`` one of
    ``allowed_hosts`` exactly (lower-cased): no user information, port, query,
    fragment or further path, no other scheme. ``owner/repo`` means the first
    allowed host. The owner and repository names follow GitHub's own rules;
    ``.``, ``..`` and names ending in ``.git`` after the suffix is removed are
    refused. The value is never echoed by an error.
    """

    def bad(problem: InputProblem = InputProblem.INVALID_FORMAT):
        return InvalidRepositoryInputError(field, problem)

    if not isinstance(value, str):
        raise bad(InputProblem.NOT_A_STRING)
    if not value:
        raise bad(InputProblem.EMPTY)
    if len(value) > 512 or not value.isascii():
        raise bad()
    hosts = [host.lower() for host in allowed_hosts]
    if not hosts:
        raise bad(InputProblem.HOST_NOT_ALLOWED)
    if value.lower().startswith("https://"):
        match = _URL.fullmatch("https://" + value[8:])
        if match is None:
            raise bad()
        host = match["host"].lower()
        if host not in hosts:
            raise bad(InputProblem.HOST_NOT_ALLOWED)
    else:
        match = _SHORT.fullmatch(value)
        if match is None:
            raise bad()
        host = hosts[0]
    owner, repo = match["owner"], match["repo"]
    repo = repo.removesuffix(".git")
    if (
        _OWNER.fullmatch(owner) is None
        or owner.endswith("-")
        or _REPO.fullmatch(repo) is None
        or repo in (".", "..")
        or len(repo) > MAX_NAME_CHARS
        or repo.lower().endswith(".git")
    ):
        raise bad()
    return GitHubRepo(host, owner, repo)


def check_created_repository(
    created: object, requested_name: str, allowed_hosts: Collection[str]
) -> GitHubRepo:
    """The repository a gateway returned, validated as strictly as a caller's input.

    A gateway is foreign code and ``GitHubRepo`` is a plain value, so nothing about
    it is trusted. All three fields must be text and must be exactly what
    :func:`parse_github_source` accepts for ``https://<host>/<owner>/<repo>`` on an
    allowed host (dot segments, slashes, control characters, look-alike Unicode,
    an over-long name, a ``.git`` suffix, an upper-case host: refused); the result
    must be the value itself (nothing is normalised silently); the repository must
    be the one that was asked for (compared without case); and every URL derived from
    it (the two remotes and the clone URL) must pass ``validate_remote_url``, the
    normaliser the Tool Broker applies. ``InvalidRepositoryInputError`` (field
    ``repository``) otherwise; the value is never echoed.
    """

    def bad() -> InvalidRepositoryInputError:
        return InvalidRepositoryInputError("repository", InputProblem.INVALID_FORMAT)

    if not isinstance(created, GitHubRepo):
        raise bad()
    # What is registered is ``checked`` (built by the parser from plain text), never
    # ``created`` itself: a value that is not text, or a ``str`` subclass that lies
    # about its content, cannot get past the comparison with ``created`` below.
    checked = parse_github_source(
        f"https://{created.host}/{created.owner}/{created.repo}",
        allowed_hosts,
        "repository",
    )
    if checked != created or checked.repo.lower() != requested_name.lower():
        raise bad()
    for url in (*checked.remote_urls, checked.clone_url):
        validate_remote_url(url, "repository")
    return checked


class GitHubGateway(Protocol):
    """Creates a repository on GitHub as the acting user (PAW-028 implements it).

    The implementation runs with the **user's own** GitHub identity (their Linux
    account's ``gh auth``); the backend never sees the token. It returns the
    repository that now exists, which the service checks against the allowed
    hosts before it registers the URL. Any failure is a
    :class:`GitHubUnavailableError`; nothing about the failure is passed on.
    """

    async def create_repository(
        self, *, user_id: uuid.UUID, name: str, private: bool
    ) -> GitHubRepo: ...


class UnavailableGitHubGateway:
    """The default: no GitHub connection exists yet, so creating one is refused."""

    async def create_repository(
        self, *, user_id: uuid.UUID, name: str, private: bool
    ) -> GitHubRepo:
        raise GitHubUnavailableError()


# --- what an existing repository says its origin is ------------------------------

_SCP = re.compile(
    r"[A-Za-z0-9._-]{1,32}@(?P<host>[A-Za-z0-9.-]{1,253}):(?P<path>[^\s:]+)"
)
_SSH_URL = re.compile(
    r"ssh://[A-Za-z0-9._-]{1,32}@(?P<host>[A-Za-z0-9.-]{1,253})/(?P<path>[^\s:]+)"
)
_HTTP_USERINFO = re.compile(r"https?://[^/?#\s]*@", re.IGNORECASE)


def remote_urls_from_origin(
    origin: str | None, allowed_hosts: Collection[str]
) -> tuple[str, ...]:
    """The ``https`` URLs to register for the ``remote.origin.url`` of a repository.

    The value is read from an untrusted repository, so it is never taken as it is:

    * ``None`` or a local path, ``file://``, ``http://`` or any other transport:
      nothing is registered (the Tool Broker maps ``https`` URLs only). The
      repository has no remote then, and nobody else can clone it.
    * ``https`` with **user information** (``https://user:secret@host/...``) is
      refused with :class:`RemoteError` (``HAS_CREDENTIALS``): registering it
      would store a secret in the database, and the caller must clean the
      repository's configuration first. The value is not echoed.
    * a GitHub-style ``git@host:owner/repo(.git)`` or ``ssh://git@host/owner/repo``
      on an allowed host becomes the two ``https`` spellings of that repository;
      any other host is not registered.
    * ``https://host/owner/repo(.git)`` on an allowed host: both spellings;
      any other ``https`` URL that is canonical: that URL.
    """
    if origin is None:
        return ()
    hosts = {host.lower() for host in allowed_hosts}
    if _HTTP_USERINFO.match(origin):
        raise RemoteError(RemoteProblem.HAS_CREDENTIALS)
    for pattern in (_SCP, _SSH_URL):
        match = pattern.fullmatch(origin)
        if match is not None:
            host = match["host"].lower()
            path = match["path"].strip("/")
            if host not in hosts:
                return ()
            try:
                ref = parse_github_source(f"https://{host}/{path}", hosts)
            except InvalidRepositoryInputError:
                return ()
            return ref.remote_urls
    if not origin.lower().startswith("https://"):
        return ()
    try:
        ref = parse_github_source(origin, hosts)
    except InvalidRepositoryInputError:
        ref = None
    if ref is not None:
        return ref.remote_urls
    try:
        canonical = normalise_remote(origin)
    except TargetError:
        return ()
    return (canonical,) if is_storable_remote(canonical) else ()
