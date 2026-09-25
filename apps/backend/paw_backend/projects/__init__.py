"""Projects: create, membership (invite-only) and lifecycle (PAW-026).

See ``apps/backend/README.md`` ("Project CRUD / Membership / Lifecycle") and
Decision 0008 (Approved 2026-09-25). The service performs authorization through
``paw_backend.authz.Authorizer``; there is no HTTP endpoint yet.
``ProjectTaskStopper`` carries out the "stop the project's tasks" request that
``ProjectService.begin_deletion`` records (Decision 0008, section 8); the
orchestrator (PAW-034) calls it. ``ProjectStateGate`` is the Project state gate that
``TaskService`` and ``TaskQueue`` are built with (Issue #83): it locks the project
row ``FOR SHARE`` and refuses new work unless the project is Active.
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
from paw_backend.projects.task_gate import ProjectStateGate
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
    "ProjectStateGate",
    "ProjectStatus",
    "ProjectTaskStopper",
    "PurgeResult",
    "TaskStopResult",
    "TransitionPlan",
]
