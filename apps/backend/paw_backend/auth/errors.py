"""Errors of the login / session / password code.

None of them carries a value from its caller (a login name, a password, a token)
or from the database: the messages are fixed, so that they can be shown or
logged. A caller learns *which kind* of failure it was, never why an
authentication failed (that stays in the audit trail).
"""

from enum import StrEnum


class AuthError(Exception):
    """Base class of the errors below."""


class InvalidAuthInputError(AuthError):
    """An argument has the wrong type or is out of range (nothing was done).

    ``field`` is the name of the argument (a fixed identifier of the code, never
    the value that was passed).
    """

    def __init__(self, field: str) -> None:
        super().__init__(f"{field} is invalid")
        self.field = field


class PasswordProblem(StrEnum):
    """Why a new password was refused (safe to show; it never quotes the input)."""

    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    INVALID_CHARACTER = "invalid_character"
    TOO_COMMON = "too_common"
    CONTAINS_LOGIN_NAME = "contains_login_name"
    SAME_AS_CURRENT = "same_as_current"


class PasswordPolicyError(AuthError):
    """A new password does not meet the policy (``problem`` says which rule)."""

    def __init__(self, problem: PasswordProblem) -> None:
        super().__init__(f"the password is not acceptable ({problem.value})")
        self.problem = problem


class InvalidCredentialsError(AuthError):
    """The login name / password (or the current password) was not accepted.

    Deliberately the same error for a wrong password, an unknown account, an
    account that cannot log in and an account without a password.
    """

    def __init__(self) -> None:
        super().__init__("the credentials were not accepted")


class ThrottledError(AuthError):
    """Too many attempts: try again after ``retry_after_seconds`` (at least 1)."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("too many attempts")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class AuthUnavailableError(AuthError):
    """The database, the audit trail or the hashing workers cannot serve now.

    Fail closed: nothing was authenticated, changed or recorded as done.
    """

    def __init__(self) -> None:
        super().__init__("the authentication service is temporarily unavailable")


class SessionNotFoundError(AuthError):
    """No such session of this user (also: not the user's own)."""

    def __init__(self) -> None:
        super().__init__("no such session")


class StepUpRequiredError(AuthError):
    """A recent step-up authentication is needed for this operation."""

    def __init__(self) -> None:
        super().__init__("a recent step-up authentication is required")


class AuthPermissionError(AuthError):
    """The actor may not do this to this account (beyond what the capability says)."""

    def __init__(self) -> None:
        super().__init__("not permitted")


class TokenRejectedError(AuthError):
    """An Owner setup / recovery token was not accepted.

    One error, one message, for every token that is not acceptable (wrong,
    unknown, expired, used, revoked, locked out, its user no longer the Owner):
    what happened is in the audit trail only.
    """

    def __init__(self) -> None:
        super().__init__("the token was not accepted")


class SessionEndedError(AuthError):
    """The session this request belongs to ended while it was being served."""

    def __init__(self) -> None:
        super().__init__("the session ended")


class AccountNotFoundError(AuthError):
    """No such (live) account."""

    def __init__(self) -> None:
        super().__init__("no such account")


class PolicyVersionConflictError(AuthError):
    """The authentication policy changed since it was read (optimistic lock)."""

    def __init__(self) -> None:
        super().__init__("the policy was changed by someone else")
