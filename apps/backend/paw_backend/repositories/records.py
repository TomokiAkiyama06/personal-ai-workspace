"""Value objects of the repository module: enums, records and small results.

All records are immutable. The ones with a rule that ties fields together
refuse an inconsistent combination when they are built, the same combinations
that the CHECK constraints of the database refuse.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from paw_backend.authz import RepoAcl


class RepositorySource(StrEnum):
    """How a repository came into the project (``REQUIREMENTS.md`` V1 paths)."""

    GITHUB_CLONE = "github_clone"  # cloned from GitHub into the user's workspace
    EXISTING_PATH = "existing_path"  # an existing Git repository on this Ubuntu
    NEW_LOCAL = "new_local"  # created here, no remote
    NEW_GITHUB = "new_github"  # created here and on GitHub


class CheckoutState(StrEnum):
    """``PENDING`` reserves a path while its clone or init runs; it is never used."""

    PENDING = "pending"
    READY = "ready"


def _aware(name: str, value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be an aware datetime")


@dataclass(frozen=True, slots=True)
class Repository:
    """One repository of a project as stored.

    ``acl`` is the resolved ACL (``RepoAcl.inherit`` unless an override is
    stored) bound to this repository and project, ready for
    ``Resource.repository``. ``created_by`` is an opaque user id (``None`` once
    that user row is gone).
    """

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    default_branch: str
    source: RepositorySource
    acl: RepoAcl
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.source, RepositorySource):
            raise ValueError("source must be a RepositorySource")
        if not isinstance(self.acl, RepoAcl):
            raise ValueError("acl must be a RepoAcl")
        if self.acl.repo_id != self.id or self.acl.project_id != self.project_id:
            raise ValueError("the ACL belongs to another repository")
        _aware("created_at", self.created_at)
        _aware("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class Remote:
    """One URL that addresses a repository (``repository_remotes``).

    ``url`` is in the canonical form of ``paw_backend.tools.scope.normalise_remote``
    (an ``https`` URL), i.e. exactly what a ``ScopedRepository`` accepts.
    """

    repository_id: uuid.UUID
    project_id: uuid.UUID
    url: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Checkout:
    """One user's own working copy of a repository.

    ``path`` is absolute and fully resolved (no symbolic link in it). A user has
    at most one checkout per repository, and a directory belongs to at most one
    checkout. ``root_device`` / ``root_inode`` identify the directory that was
    registered (set exactly while the checkout is ``ready``).
    """

    id: uuid.UUID
    repository_id: uuid.UUID
    project_id: uuid.UUID
    user_id: uuid.UUID
    path: str
    state: CheckoutState
    created_at: datetime
    updated_at: datetime
    # ``st_dev`` / ``st_ino`` of the directory when the checkout became ready.
    root_device: int | None = None
    root_inode: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, CheckoutState):
            raise ValueError("state must be a CheckoutState")
        identity = (self.root_device, self.root_inode)
        if any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            for value in identity
        ):
            raise ValueError("the directory identity must be non-negative integers")
        if (self.state is CheckoutState.READY) != all(v is not None for v in identity):
            raise ValueError("a ready checkout, and only it, has a directory identity")
        _aware("created_at", self.created_at)
        _aware("updated_at", self.updated_at)


@dataclass(frozen=True, slots=True)
class Registered:
    """The outcome of a registration: the repository, the actor's checkout and more.

    ``remotes`` are the URLs stored for the repository. ``head`` is the commit
    ``HEAD`` pointed at when the checkout was registered (``None`` for a
    repository without a commit); it is a fact of that instant, not stored.
    """

    repository: Repository
    checkout: Checkout
    remotes: tuple[str, ...]
    head: str | None


@dataclass(frozen=True, slots=True)
class RepositoryDetail:
    """A repository with its remote URLs (``get_repository``)."""

    repository: Repository
    remotes: tuple[str, ...]
