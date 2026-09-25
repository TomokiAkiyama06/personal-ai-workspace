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


class StepUpMethodInsufficientError(StepUpRequiredError):
    """The session has a recent step-up, but by a method too weak for this operation.

    REQUIREMENTS.md: the Owner's and Admin's sensitive operations need a *Passkey*
    step-up. A password step-up does not count, so a stolen password cannot be
    turned into the authority to relax the policy. Until PAW-023 provides Passkeys
    such an operation is refused (fail closed).
    """

    def __init__(self) -> None:
        AuthError.__init__(self, "the step-up method is not sufficient")


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


# -- Passkeys (PAW-023) ---------------------------------------------------------
# Like everything above: fixed messages, never a value of the caller, a credential
# or the database, never why a ceremony failed beyond the kind (the audit trail has
# the reason).


class PasskeyError(AuthError):
    """Base class of the Passkey errors below."""


class PasskeyUnavailableError(PasskeyError):
    """Passkeys are not configured on this server (``PAW_PASSKEY_RP_ID``)."""

    def __init__(self) -> None:
        super().__init__("passkeys are not configured")


class PasskeyRequiredError(PasskeyError):
    """The session is restricted until a Passkey is registered / used (the gate).

    Also raised for a Passkey operation the session's gate does not allow (for
    example registering another Passkey before the sign-in assertion).
    """

    def __init__(self) -> None:
        super().__init__("a passkey is required for this session")


class PasskeyChallengeError(PasskeyError):
    """The challenge is unknown, expired or already used (begin the ceremony again)."""

    def __init__(self) -> None:
        super().__init__("the challenge is not valid")


class PasskeyVerificationError(PasskeyError):
    """The browser's answer did not verify (a registration; an assertion is a
    ``InvalidCredentialsError`` like every other proof)."""

    def __init__(self) -> None:
        super().__init__("the passkey response was not accepted")


class PasskeyNotFoundError(PasskeyError):
    """No such (active) Passkey of this user (also: not the user's own)."""

    def __init__(self) -> None:
        super().__init__("no such passkey")


class PasskeyExistsError(PasskeyError):
    """That authenticator's credential is already registered."""

    def __init__(self) -> None:
        super().__init__("the credential is already registered")


class PasskeyLimitError(PasskeyError):
    """The user already has the most Passkeys a user may have."""

    def __init__(self) -> None:
        super().__init__("too many passkeys")


class LastPasskeyError(PasskeyError):
    """The last Passkey of an account whose role requires one cannot be revoked."""

    def __init__(self) -> None:
        super().__init__("the last required passkey cannot be revoked")


class NoPasskeyError(PasskeyError):
    """The account has no Passkey to authenticate with (register one first)."""

    def __init__(self) -> None:
        super().__init__("the account has no passkey")
