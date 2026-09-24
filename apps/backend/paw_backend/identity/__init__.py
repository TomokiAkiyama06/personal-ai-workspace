"""Users and the initial Owner setup / recovery (PAW-021).

``users`` here is the minimal identity (id, login name, system role, status,
Passkey requirement). Passwords, sessions and Passkeys belong to PAW-022 and
PAW-023. See ``apps/backend/README.md`` ("Owner の初期設定と復旧").
"""

from paw_backend.identity.errors import (
    AuditUnavailableError,
    IdentityError,
    InvalidLoginNameError,
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
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
from paw_backend.identity.service import (
    AuditAction,
    AuditReason,
    IssuedToken,
    OwnerSetupService,
    RedeemHook,
    Redemption,
)

__all__ = [
    "PASSKEY_REQUIRED_ROLES",
    "AuditAction",
    "AuditReason",
    "AuditUnavailableError",
    "IdentityError",
    "InvalidLoginNameError",
    "IssuedToken",
    "LoginNameTakenError",
    "OwnerAlreadyExistsError",
    "OwnerNotFoundError",
    "OwnerSetupService",
    "RedeemHook",
    "Redemption",
    "SetupTokenRejectedError",
    "SetupTokenRow",
    "TokenPurpose",
    "UserRow",
    "UserStatus",
    "normalize_login_name",
    "passkey_required_for",
]
