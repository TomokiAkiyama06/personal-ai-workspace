"""Users and the initial Owner setup / recovery (PAW-021).

``users`` here is the minimal identity (id, login name, system role, status,
Passkey requirement). Passwords, sessions and Passkeys belong to PAW-022 and
PAW-023. See ``apps/backend/README.md`` ("Owner の初期設定と復旧").

This package's ``__init__`` exports only what the web application may use:
``TokenRedeemer`` (spends a token) and the models and errors. The operator side,
``paw_backend.identity.operator`` (creates the Owner, issues tokens), is
deliberately not imported here: importing this package must not bring it in, and
only ``paw_backend.cli`` may import it.
"""

from paw_backend.identity.audit import AuditAction, AuditReason
from paw_backend.identity.errors import (
    AuditUnavailableError,
    IdentityError,
    InvalidLoginNameError,
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
    OwnerNotLiveError,
    RecoveryNotPrivilegedError,
    RedeemHookError,
    SetupTokenRejectedError,
)
from paw_backend.identity.login_name import normalize_login_name
from paw_backend.identity.models import (
    PASSKEY_REQUIRED_ROLES,
    SetupTokenRow,
    TokenPurpose,
    UserRow,
    UserStatus,
    passkey_required_for,
)
from paw_backend.identity.redeemer import RedeemHook, Redemption, TokenRedeemer

__all__ = [
    "PASSKEY_REQUIRED_ROLES",
    "AuditAction",
    "AuditReason",
    "AuditUnavailableError",
    "IdentityError",
    "InvalidLoginNameError",
    "LoginNameTakenError",
    "OwnerAlreadyExistsError",
    "OwnerNotFoundError",
    "OwnerNotLiveError",
    "RecoveryNotPrivilegedError",
    "RedeemHook",
    "RedeemHookError",
    "Redemption",
    "SetupTokenRejectedError",
    "SetupTokenRow",
    "TokenPurpose",
    "TokenRedeemer",
    "UserRow",
    "UserStatus",
    "normalize_login_name",
    "passkey_required_for",
]
