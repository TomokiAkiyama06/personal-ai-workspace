"""Typed errors of the project module.

Messages are fixed strings built from closed vocabularies written in this code
base (field names, :class:`InputProblem` and status values). They never contain
caller-supplied content (names, descriptions, ids), driver messages or SQL, so
they are safe to log and to map to an API response. ``code`` is the stable
machine-readable identifier. Database errors that the service does not handle
(connection loss and so on) propagate unchanged; their text can contain SQL
parameters, so a caller must never show ``str(error)`` to a user.

Existence is not disclosed: when the acting user is neither an accepted member
of a project nor allowed by a system role to do the action, the project is
reported as :class:`ProjectNotFoundError`, exactly like a project that does not
exist (see ``ProjectService``).
"""

from enum import StrEnum
from typing import ClassVar

from paw_backend.authz.policy import Reason
from paw_backend.projects.records import LifecycleAction, ProjectStatus


class InputProblem(StrEnum):
    """Why an argument was rejected. A closed set; never a caller's own text."""

    NOT_A_UUID = "not_a_uuid"
    NOT_A_STRING = "not_a_string"
    EMPTY = "empty"
    TOO_LONG = "too_long"
    INVALID_CHARACTERS = "invalid_characters"
    NOT_A_ROLE = "not_a_role"
    NOT_A_STATUS = "not_a_status"
    NOT_AN_INTEGER = "not_an_integer"
    OUT_OF_RANGE = "out_of_range"
    NOT_A_DATETIME = "not_a_datetime"
    NAIVE_DATETIME = "naive_datetime"
    INVALID_CURSOR = "invalid_cursor"


class ProjectError(Exception):
    """Base class of every error raised by the project module."""

    code: ClassVar[str] = "project_error"


class InvalidProjectInputError(ProjectError, ValueError):
    """An argument was rejected. ``field`` names it, ``problem`` says why."""

    code = "invalid_project_input"

    def __init__(self, field: str, problem: InputProblem) -> None:
        self.field = field
        self.problem = problem
        super().__init__(f"Invalid {field}: {problem.value}")


class ProjectNotFoundError(ProjectError):
    """No such project *for this user*.

    Raised for a missing project, a Deleted one, and a project the acting user
    may not even know about (not a member, and no system-role grant for the
    action). The cases are deliberately indistinguishable.
    """

    code = "project_not_found"

    def __init__(self) -> None:
        super().__init__("Project not found")


class ProjectPermissionDeniedError(ProjectError):
    """The Authorizer (or the actor check) denied the action.

    ``reason`` is the stable reason code of the decision
    (``paw_backend.authz.Reason``), for the API layer and the logs. The API
    layer maps ``Reason.AUDIT_UNAVAILABLE`` to 503 and every other reason to a
    fixed 403 body that does not say which rule applied.
    """

    code = "project_permission_denied"

    def __init__(self, reason: Reason) -> None:
        self.reason = reason
        super().__init__("Permission denied")


class ProjectStateError(ProjectError):
    """The project's lifecycle status does not allow this action.

    Raised only for a member (or a system-role holder) whose action the policy
    denies with ``Reason.PROJECT_STATE_FORBIDS``: Archived is read-only,
    Pending deletion allows only the lifecycle operations.
    """

    code = "project_state_forbids"

    def __init__(self, status: ProjectStatus) -> None:
        self.status = status
        super().__init__(f"Project is {status.value}")


class IllegalTransitionError(ProjectError):
    """The lifecycle does not allow ``action`` in ``status``."""

    code = "illegal_transition"

    def __init__(self, status: ProjectStatus, action: LifecycleAction) -> None:
        self.status = status
        self.action = action
        super().__init__(f"Cannot {action.value} a {status.value} project")


class DeletionWindowClosedError(ProjectError):
    """The 30 days are over: the project can no longer be restored."""

    code = "deletion_window_closed"

    def __init__(self) -> None:
        super().__init__("The restore window has closed")


class ConfirmationMismatchError(ProjectError):
    """The confirmation text is not exactly the project's name."""

    code = "confirmation_mismatch"

    def __init__(self) -> None:
        super().__init__("The confirmation does not match the project name")


class NoManagerError(ProjectError):
    """Restore refused: the project would come back with no Manager."""

    code = "no_manager"

    def __init__(self) -> None:
        super().__init__("The project has no Manager")


class LastManagerError(ProjectError):
    """The change would leave the project without an accepted Manager."""

    code = "last_manager"

    def __init__(self) -> None:
        super().__init__("The last Manager cannot leave, be removed or be demoted")


class MemberNotFoundError(ProjectError):
    """The user is not an (accepted or invited) member of the project."""

    code = "member_not_found"

    def __init__(self) -> None:
        super().__init__("Member not found")


class InviteNotFoundError(ProjectError):
    """The acting user has no invitation to this project that can be answered."""

    code = "invite_not_found"

    def __init__(self) -> None:
        super().__init__("Invitation not found")


class InviteExpiredError(ProjectError):
    """The invitation exists but its time is over."""

    code = "invite_expired"

    def __init__(self) -> None:
        super().__init__("The invitation has expired")


class AlreadyMemberError(ProjectError):
    """The invited user already is an accepted member."""

    code = "already_member"

    def __init__(self) -> None:
        super().__init__("The user is already a member")


class AlreadyInvitedError(ProjectError):
    """The invited user has an open invitation already."""

    code = "already_invited"

    def __init__(self) -> None:
        super().__init__("The user already has an open invitation")


class InviteeUnavailableError(ProjectError):
    """The invited user does not exist or is not an active user.

    The two cases are deliberately indistinguishable.
    """

    code = "invitee_unavailable"

    def __init__(self) -> None:
        super().__init__("The user cannot be invited")


class MemberLimitError(ProjectError):
    """The project holds the maximum number of members plus open invitations."""

    code = "member_limit"

    def __init__(self) -> None:
        super().__init__("The project has reached its member limit")


class ProjectBusyError(ProjectError):
    """A row lock was not granted within ``lock_timeout_ms``; try again."""

    code = "project_busy"

    def __init__(self) -> None:
        super().__init__("The project is busy")
