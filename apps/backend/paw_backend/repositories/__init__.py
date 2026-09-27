"""Repository registration and per-user checkouts (PAW-027), and GitHub user
connections (PAW-028).

See ``apps/backend/README.md`` ("Repository Registration / Per-user Checkout") and
Decision 0017 (Approved 2026-09-26). A project's repositories are logical records;
what a user or an agent edits is that user's own checkout in their own Linux account.
The service performs authorization through ``paw_backend.authz.Authorizer``;
there is no HTTP endpoint yet. GitHub credentials (``gh auth``, PAW-028,
``github_connection.py``) are per Linux user, never held by the backend:
``GitHubGateway`` is the seam PAW-028 fills with ``GhCliGitHubGateway``.
"""

from paw_backend.repositories.accounts import (
    AccountDirectory,
    LoginNameAccountDirectory,
)
from paw_backend.repositories.errors import (
    CheckoutChangedError,
    CheckoutExistsError,
    CheckoutGoneError,
    CheckoutInProgressError,
    CheckoutNotFoundError,
    GhCommandError,
    GhFailure,
    GitCommandError,
    GitFailure,
    GitHubUnavailableError,
    InputProblem,
    InvalidRepositoryInputError,
    LinuxAccountUnavailableError,
    NoCloneSourceError,
    PathProblem,
    PathRejectedError,
    ProjectNotActiveError,
    ProjectUnavailableError,
    RemoteAlreadyRegisteredError,
    RemoteError,
    RemoteNotFoundError,
    RemoteProblem,
    RepositoryBusyError,
    RepositoryError,
    RepositoryLimitError,
    RepositoryNameTakenError,
    RepositoryNotFoundError,
    RepositoryPermissionDeniedError,
    TooManyCheckoutsError,
)
from paw_backend.repositories.git import (
    GitClient,
    GitResult,
    GitRunner,
    RepositoryFacts,
    SubprocessGitRunner,
)
from paw_backend.repositories.github import (
    GitHubGateway,
    GitHubRepo,
    UnavailableGitHubGateway,
    parse_github_source,
)
from paw_backend.repositories.github_connection import (
    GhCliGitHubGateway,
    GhResult,
    GhRunner,
    GitHubConnectionService,
    GitHubConnectionState,
    GitHubConnectionStatus,
    SubprocessGhRunner,
)
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.policy import RepositoryPolicy
from paw_backend.repositories.records import (
    Checkout,
    CheckoutState,
    Registered,
    Remote,
    Repository,
    RepositoryDetail,
    RepositorySource,
)
from paw_backend.repositories.service import RepositoryService
from paw_backend.repositories.ssh import (
    SshGitRunner,
    SshGitRunnerPolicy,
    SshKeyDirectory,
    TemplateSshKeyDirectory,
)

__all__ = [
    "AccountDirectory",
    "Checkout",
    "CheckoutChangedError",
    "CheckoutExistsError",
    "CheckoutGoneError",
    "CheckoutInProgressError",
    "CheckoutNotFoundError",
    "CheckoutState",
    "GhCliGitHubGateway",
    "GhCommandError",
    "GhFailure",
    "GhResult",
    "GhRunner",
    "GitClient",
    "GitCommandError",
    "GitFailure",
    "GitHubConnectionService",
    "GitHubConnectionState",
    "GitHubConnectionStatus",
    "GitHubGateway",
    "GitHubRepo",
    "GitHubUnavailableError",
    "GitResult",
    "GitRunner",
    "InputProblem",
    "InvalidRepositoryInputError",
    "LinuxAccount",
    "LinuxAccountUnavailableError",
    "LoginNameAccountDirectory",
    "NoCloneSourceError",
    "PathProblem",
    "PathRejectedError",
    "ProjectNotActiveError",
    "ProjectUnavailableError",
    "Registered",
    "Remote",
    "RemoteAlreadyRegisteredError",
    "RemoteError",
    "RemoteNotFoundError",
    "RemoteProblem",
    "Repository",
    "RepositoryBusyError",
    "RepositoryDetail",
    "RepositoryError",
    "RepositoryFacts",
    "RepositoryLimitError",
    "RepositoryNameTakenError",
    "RepositoryNotFoundError",
    "RepositoryPermissionDeniedError",
    "RepositoryPolicy",
    "RepositoryService",
    "RepositorySource",
    "SshGitRunner",
    "SshGitRunnerPolicy",
    "SshKeyDirectory",
    "SubprocessGhRunner",
    "SubprocessGitRunner",
    "TemplateSshKeyDirectory",
    "TooManyCheckoutsError",
    "UnavailableGitHubGateway",
    "parse_github_source",
]
