"""Typed errors of the repository module.

Messages are fixed strings built from closed vocabularies written in this code
base (field names, :class:`InputProblem`, :class:`PathProblem`,
:class:`GitFailure` and status values). They never contain caller-supplied
content (names, paths, URLs, ids), what git printed, driver messages or SQL, so
they are safe to log and to map to an API response. ``code`` is the stable
machine-readable identifier. Database errors that the service does not handle
(connection loss and so on) propagate unchanged; their text can contain SQL
parameters, so a caller must never show ``str(error)`` to a user.

Existence is not disclosed: when the acting user is not an accepted member of a
project (and no rule of the policy lets them act), the project is reported as
:class:`ProjectUnavailableError`, exactly like a project that does not exist.
"""

import uuid
from enum import StrEnum
from typing import ClassVar

from paw_backend.authz.policy import Reason
from paw_backend.projects.records import ProjectStatus


class InputProblem(StrEnum):
    """Why an argument was rejected. A closed set; never a caller's own text."""

    NOT_A_UUID = "not_a_uuid"
    NOT_A_STRING = "not_a_string"
    NOT_A_BOOL = "not_a_bool"
    NOT_AN_INTEGER = "not_an_integer"
    NOT_A_COLLECTION = "not_a_collection"
    NOT_A_PERMISSION = "not_a_permission"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    TOO_MANY = "too_many"
    INVALID_CHARACTERS = "invalid_characters"
    INVALID_FORMAT = "invalid_format"
    HOST_NOT_ALLOWED = "host_not_allowed"
    OUT_OF_RANGE = "out_of_range"


class PathProblem(StrEnum):
    """Why a path (or a repository behind it) was refused. A closed set."""

    NOT_ABSOLUTE = "not_absolute"
    NOT_CANONICAL = "not_canonical"  # not spelled the way the backend spells it
    OUTSIDE_ROOTS = "outside_roots"  # not below an allowed root of this user
    IS_A_ROOT = "is_a_root"  # an allowed root itself, or above the checkout root
    HIDDEN = "hidden"  # a component below the root starts with a dot
    NOT_FOUND = "not_found"
    SYMLINK = "symlink"  # a component is a symbolic link (or not fully resolved)
    NOT_A_DIRECTORY = "not_a_directory"
    NOT_OWNER = "not_owner"  # not owned by the acting user's Linux account
    WORLD_WRITABLE = "world_writable"
    NOT_A_REPOSITORY = "not_a_repository"
    GIT_TRICK = "git_trick"  # .git is a file / link, alternates, other work tree
    BARE = "bare"  # a bare repository has no working tree to check out
    DEFAULT_BRANCH_UNKNOWN = "default_branch_unknown"
    UNSUPPORTED_BRANCH = "unsupported_branch"  # a branch name this module refuses
    EXISTS = "exists"  # something is at the path already
    HOME_UNSAFE = "home_unsafe"  # the home directory cannot be used
    TOO_LONG = "too_long"  # the generated path is longer than a stored path may be
    CHANGED = "changed"  # not the directory that was registered (or overlapping one)


class GitFailure(StrEnum):
    """Why a git command did not give a usable result. A closed set."""

    NOT_INSTALLED = "not_installed"
    IDENTITY_MISMATCH = "identity_mismatch"  # the process is not the account's user
    TIMEOUT = "timeout"
    OUTPUT_TOO_LARGE = "output_too_large"
    NONZERO_EXIT = "nonzero_exit"
    UNSAFE_OUTPUT = "unsafe_output"


class RemoteProblem(StrEnum):
    """Why a repository's own remote URL is refused. A closed set."""

    HAS_CREDENTIALS = "has_credentials"  # user information in the URL
    TOO_MANY = "too_many"


class RepositoryError(Exception):
    """Base class of every error raised by the repository module."""

    code: ClassVar[str] = "repository_error"


class InvalidRepositoryInputError(RepositoryError, ValueError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_repository_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class ProjectUnavailableError(RepositoryError):
    """No such project *for this user* (missing, Deleted, or not theirs to see)."""

    code = "project_not_found"

    def __init__(self) -> None:
        super().__init__("Project not found")


class RepositoryPermissionDeniedError(RepositoryError):
    """The Authorizer (or the actor check) denied the action.

    ``reason`` is the stable reason code of the decision
    (``paw_backend.authz.Reason``). The API layer maps ``Reason.AUDIT_UNAVAILABLE``
    to 503 and every other reason to a fixed 403 body.
    """

    code = "repository_permission_denied"

    def __init__(self, reason: Reason) -> None:
        self.reason = reason
        super().__init__("Permission denied")


class ProjectNotActiveError(RepositoryError):
    """The project is not Active: repositories change only in an Active project."""

    code = "project_not_active"

    def __init__(self, status: ProjectStatus) -> None:
        self.status = status
        super().__init__(f"Project is {status.value}")


class RepositoryNotFoundError(RepositoryError):
    """No such repository in this project (or not one the actor may read)."""

    code = "repository_not_found"

    def __init__(self) -> None:
        super().__init__("Repository not found")


class CheckoutNotFoundError(RepositoryError):
    """The user has no (ready) checkout of this repository."""

    code = "checkout_not_found"

    def __init__(self) -> None:
        super().__init__("Checkout not found")


class RepositoryNameTakenError(RepositoryError):
    """The project has a repository with this name (compared without case)."""

    code = "repository_name_taken"

    def __init__(self) -> None:
        super().__init__("A repository with this name exists in the project")


class RepositoryLimitError(RepositoryError):
    """The project holds the maximum number of repositories."""

    code = "repository_limit"

    def __init__(self) -> None:
        super().__init__("The project has reached its repository limit")


class RemoteError(RepositoryError):
    """A remote URL cannot be registered (``problem`` says why)."""

    code = "remote_rejected"

    def __init__(self, problem: RemoteProblem) -> None:
        self.problem = problem
        super().__init__(f"Remote rejected: {problem.value}")


class RemoteAlreadyRegisteredError(RepositoryError):
    """The URL belongs to a repository of this project already."""

    code = "remote_already_registered"

    def __init__(self) -> None:
        super().__init__("The remote is registered already")


class RemoteNotFoundError(RepositoryError):
    """The repository has no such remote."""

    code = "remote_not_found"

    def __init__(self) -> None:
        super().__init__("Remote not found")


class CheckoutExistsError(RepositoryError):
    """The user has a checkout of this repository (or a directory is registered)."""

    code = "checkout_exists"

    def __init__(self) -> None:
        super().__init__("The checkout exists already")


class CheckoutInProgressError(RepositoryError):
    """The user's checkout is being created right now (try again later)."""

    code = "checkout_in_progress"

    def __init__(self) -> None:
        super().__init__("The checkout is being created")


class CheckoutGoneError(RepositoryError):
    """The registration disappeared while the checkout was being created."""

    code = "checkout_gone"

    def __init__(self) -> None:
        super().__init__("The repository was removed during the operation")


class NoCloneSourceError(RepositoryError):
    """The repository has no remote a checkout can be cloned from."""

    code = "no_clone_source"

    def __init__(self) -> None:
        super().__init__("The repository has no remote to clone from")


class CheckoutChangedError(RepositoryError):
    """A checkout root is not what was registered, so no scope is derived from it.

    ``problem`` says why (``SYMLINK`` / ``NOT_FOUND`` / ``NOT_A_DIRECTORY`` /
    ``NOT_OWNER`` for the checkout itself, ``CHANGED`` for another identity or for a
    changed checkout that overlaps it); ``checkout_id`` is the opaque id of the
    checkout that changed. No path is ever part of the error.
    """

    code = "checkout_changed"

    def __init__(self, problem: PathProblem, checkout_id: uuid.UUID | None = None):
        self.problem = problem
        self.checkout_id = checkout_id
        super().__init__(f"Checkout changed: {problem.value}")


class TooManyCheckoutsError(RepositoryError):
    """The user has more checkouts than one scope can be verified against."""

    code = "too_many_checkouts"

    def __init__(self) -> None:
        super().__init__("Too many checkouts to derive a scope")


class PathRejectedError(RepositoryError):
    """A path (or the repository behind it) is refused. ``problem`` says why."""

    code = "path_rejected"

    def __init__(self, problem: PathProblem) -> None:
        self.problem = problem
        super().__init__(f"Path rejected: {problem.value}")


class LinuxAccountUnavailableError(RepositoryError):
    """The user has no usable Linux account (missing, inactive or a system account)."""

    code = "linux_account_unavailable"

    def __init__(self) -> None:
        super().__init__("The user has no usable Linux account")


class GitCommandError(RepositoryError):
    """A git command failed. Only ``operation`` and ``failure`` are known here."""

    code = "git_failed"

    def __init__(self, operation: str, failure: GitFailure) -> None:
        self.operation = operation
        self.failure = failure
        super().__init__(f"git {operation} failed: {failure.value}")


class GitHubUnavailableError(RepositoryError):
    """No GitHub gateway is configured, or it could not create the repository."""

    code = "github_unavailable"

    def __init__(self) -> None:
        super().__init__("GitHub is not available")


class RepositoryBusyError(RepositoryError):
    """A row lock was not granted within ``lock_timeout_ms``; try again."""

    code = "repository_busy"

    def __init__(self) -> None:
        super().__init__("The repository is busy")
