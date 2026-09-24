"""Projects: create, membership (invite-only) and lifecycle (PAW-026).

See ``apps/backend/README.md`` ("Project CRUD / Membership / Lifecycle") and
Decision 0008 (Proposed). The service performs authorization through
``paw_backend.authz.Authorizer``; there is no HTTP endpoint yet.
"""

from paw_backend.projects.errors import (
    AlreadyInvitedError,
    AlreadyMemberError,
    ConfirmationMismatchError,
    DeletionWindowClosedError,
    IllegalTransitionError,
    InputProblem,
    InvalidProjectInputError,
    InviteeUnavailableError,
    InviteExpiredError,
    InviteNotFoundError,
    LastManagerError,
    MemberLimitError,
    MemberNotFoundError,
    NoManagerError,
    ProjectBusyError,
    ProjectError,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectStateError,
)
from paw_backend.projects.records import (
    InviteState,
    LifecycleAction,
    Member,
    MemberStatus,
    PendingInvite,
    Project,
    ProjectStatus,
    PurgeResult,
    TransitionPlan,
)
from paw_backend.projects.service import ProjectService

__all__ = [
    "AlreadyInvitedError",
    "AlreadyMemberError",
    "ConfirmationMismatchError",
    "DeletionWindowClosedError",
    "IllegalTransitionError",
    "InputProblem",
    "InvalidProjectInputError",
    "InviteExpiredError",
    "InviteNotFoundError",
    "InviteState",
    "InviteeUnavailableError",
    "LastManagerError",
    "LifecycleAction",
    "Member",
    "MemberLimitError",
    "MemberNotFoundError",
    "MemberStatus",
    "NoManagerError",
    "PendingInvite",
    "Project",
    "ProjectBusyError",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectPermissionDeniedError",
    "ProjectService",
    "ProjectStateError",
    "ProjectStatus",
    "PurgeResult",
    "TransitionPlan",
]
