"""Parallel worktrees and the integration node (PAW-035).

Every Worker node that may write gets its own worktree and branch of each
repository it works on; after the DAG, their branches are merged into a
task-owned integration branch / worktree per repository (never into the default
branch; conflicts are detected and reported), and the integrated result is what
the tests, the Evaluator and the review check (``gate.py``). Once every check
passed, the checked commit of each ``target`` repository is pushed and its pull
request opened (``publish.py``, issue #132, Decision 0052 Proposed). git runs
through the deployment's ``GitRunner`` as the task creator's own Linux account. See
``apps/backend/README.md`` ("Parallel Worktree / Integration") and
``docs/decisions/0036-parallel-worktree-integration.md`` (Approved).
"""

from paw_backend.integration.coordinator import (
    GitWorktreeCoordinator,
    IntegrationTarget,
)
from paw_backend.integration.gate import (
    CHECK_ORDER,
    CheckKind,
    CheckRequest,
    CheckVerdict,
    GateOutcome,
    GateReport,
    IntegrationGate,
    Publication,
)
from paw_backend.integration.git import MERGE_EMAIL, MERGE_NAME, MergeCheck, WorktreeGit
from paw_backend.integration.layout import (
    BRANCH_NAMESPACE,
    INTEGRATION_KEY,
    WORKTREE_DIRECTORY,
    branch_name,
    in_namespace,
    worktree_base,
    worktree_path,
)
from paw_backend.integration.publish import (
    GitHubPullRequestPublisher,
    PublishProblem,
    PublishRequest,
    PullRequestNotPublishedError,
)

__all__ = [
    "BRANCH_NAMESPACE",
    "CHECK_ORDER",
    "INTEGRATION_KEY",
    "MERGE_EMAIL",
    "MERGE_NAME",
    "WORKTREE_DIRECTORY",
    "CheckKind",
    "CheckRequest",
    "CheckVerdict",
    "GateOutcome",
    "GateReport",
    "GitHubPullRequestPublisher",
    "GitWorktreeCoordinator",
    "IntegrationGate",
    "IntegrationTarget",
    "MergeCheck",
    "Publication",
    "PublishProblem",
    "PublishRequest",
    "PullRequestNotPublishedError",
    "WorktreeGit",
    "branch_name",
    "in_namespace",
    "worktree_base",
    "worktree_path",
]
