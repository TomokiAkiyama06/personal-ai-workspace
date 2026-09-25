"""Projects: create, membership (invite-only) and lifecycle (PAW-026).

See ``apps/backend/README.md`` ("Project CRUD / Membership / Lifecycle") and
Decision 0008 (Approved 2026-09-25). The service performs authorization through
``paw_backend.authz.Authorizer``, and creating a project, answering an
invitation and leaving one are audited capabilities too (Decision 0022,
Proposed); there is no HTTP endpoint yet.
``ProjectTaskStopper`` carries out the "stop the project's tasks" request that
``ProjectService.begin_deletion`` records (Decision 0008, section 8); the
orchestrator (PAW-034) calls it.
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
from paw_backend.projects.task_stop import ProjectTaskStopper, TaskStopResult

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
    "ProjectTaskStopper",
    "PurgeResult",
    "TaskStopResult",
    "TransitionPlan",
]
