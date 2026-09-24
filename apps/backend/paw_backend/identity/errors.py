"""Errors of the identity service.

None of them carries a value from its caller (a login name, a token) or from
the database: the messages are fixed, so that they can be shown or logged.
"""


class IdentityError(Exception):
    """Base class of the identity service errors."""


class InvalidLoginNameError(IdentityError):
    def __init__(self) -> None:
        super().__init__(
            "the login name must be 3 to 64 characters: lower-case letters, digits "
            "and . _ - inside, starting and ending with a letter or digit"
        )


class OwnerAlreadyExistsError(IdentityError):
    """An Owner exists: the initial setup path is closed (use recovery)."""

    def __init__(self) -> None:
        super().__init__("an Owner already exists")


class LoginNameTakenError(IdentityError):
    def __init__(self) -> None:
        super().__init__("the login name is already in use")


class OwnerNotFoundError(IdentityError):
    """There is no Owner (in a state that can be recovered)."""

    def __init__(self) -> None:
        super().__init__("there is no Owner to recover")


class SetupTokenRejectedError(IdentityError):
    """A setup / recovery token was not accepted.

    Deliberately the same error, with the same message, whether the token was
    wrong, unknown, expired, used, revoked or locked out: a caller (and anyone
    watching the response) learns nothing about which. The reason is written to
    the audit trail only.
    """

    def __init__(self) -> None:
        super().__init__("the token was not accepted")


class AuditUnavailableError(IdentityError):
    """The audit event could not be stored, so the action was not performed."""

    def __init__(self) -> None:
        super().__init__("the audit trail could not be written; nothing was changed")
