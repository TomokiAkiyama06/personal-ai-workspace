"""Parallel worktrees and the integration node (PAW-035).

Every Worker node that may write gets its own worktree and branch of each
repository it works on; after the DAG, their branches are merged into a
task-owned integration branch / worktree per repository (never into the default
branch; conflicts are detected and reported), and the integrated result is what
the tests, the Evaluator and the review check (``gate.py``). git runs through the
deployment's ``GitRunner`` as the task creator's own Linux account. See
``apps/backend/README.md`` ("Parallel Worktree / Integration") and
``docs/decisions/0036-parallel-worktree-integration.md`` (Proposed).
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
    "GitWorktreeCoordinator",
    "IntegrationGate",
    "IntegrationTarget",
    "MergeCheck",
    "WorktreeGit",
    "branch_name",
    "in_namespace",
    "worktree_base",
    "worktree_path",
]
