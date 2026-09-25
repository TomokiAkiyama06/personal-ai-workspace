"""The authentication state a session reports, and the Step-up / Passkey seams.

**Step-up** is "prove again, now, that you are the account's owner" before a
sensitive operation. ``StepUpVerifier`` is the seam: one implementation per
method. This issue ships the password one (the fallback while a Passkey is not
required or not enrolled); PAW-023 adds a Passkey verifier and registers it in
``AuthService(step_up_verifiers=...)`` without changing anything else.

**Passkey status** is asked through ``PasskeyEnrollment`` (default: nobody has
one, until PAW-023 provides the real thing). What the *policy* says about a
Passkey is ``AuthPolicy.requirement_for``; this module only combines the two into
what the client sees. **Nothing here enforces a Passkey**: PAW-023 does. A
"required" requirement with no Passkey enrolled reports
``enrollment_required=True``, which is the hook for the enrollment-only state
Decision 0005 asks for (a user in it must never reach a dead end: password login
and ``owner-recover`` stay available).
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.models import AuthMethod, PasskeyRequirement
from paw_backend.auth.sessions import SessionRecord
from paw_backend.authz.roles import SystemRole


class PasskeyEnrollment(Protocol):
    """Whether a user has a registered Passkey (PAW-023 implements it)."""

    async def is_enrolled(self, user_id: uuid.UUID) -> bool: ...


class NoPasskeys:
    """The default until PAW-023: nobody has a Passkey."""

    async def is_enrolled(self, user_id: uuid.UUID) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class StepUpEvidence:
    """What a client presents for a step-up. Only the password kind exists here."""

    method: AuthMethod
    password: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "method", AuthMethod(self.method))
        except ValueError:
            raise InvalidAuthInputError("method") from None
        if self.password is not None and not isinstance(self.password, str):
            raise InvalidAuthInputError("password")


class StepUpVerifier(Protocol):
    """Checks step-up evidence for one method."""

    method: AuthMethod

    async def verify(
        self, user_id: uuid.UUID, login_name: str, evidence: StepUpEvidence
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AuthPolicy:
    """The workspace authentication policy (one row, versioned)."""

    version: int
    passkey_owner: PasskeyRequirement
    passkey_admin: PasskeyRequirement
    passkey_user: PasskeyRequirement
    recommend_passkey_to_users: bool
    stepup_window_minutes: int
    updated_at: datetime
    updated_by: uuid.UUID | None

    def requirement_for(self, role: SystemRole) -> PasskeyRequirement:
        """The Passkey requirement of a role. ``SYSTEM`` has no login: optional."""
        match role:
            case SystemRole.OWNER:
                return self.passkey_owner
            case SystemRole.ADMIN:
                return self.passkey_admin
            case SystemRole.USER:
                return self.passkey_user
        return PasskeyRequirement.OPTIONAL


@dataclass(frozen=True, slots=True)
class PasskeyState:
    requirement: PasskeyRequirement
    enrolled: bool
    # The requirement is "required" and no Passkey is registered yet.
    enrollment_required: bool
    # The client should urge the user to register one (a User, policy says so).
    recommended: bool


@dataclass(frozen=True, slots=True)
class StepUpState:
    method: AuthMethod | None
    verified_at: datetime | None
    valid_until: datetime | None
    window_minutes: int
    # A step-up was done and its window has not passed (at ``checked_at``).
    satisfied: bool


@dataclass(frozen=True, slots=True)
class AuthState:
    """``auth`` of the session response."""

    method: AuthMethod
    passkey: PasskeyState
    step_up: StepUpState


def build_auth_state(
    policy: AuthPolicy,
    role: SystemRole,
    record: SessionRecord,
    *,
    enrolled: bool,
    checked_at: datetime,
) -> AuthState:
    requirement = policy.requirement_for(role)
    passkey = PasskeyState(
        requirement=requirement,
        enrolled=enrolled,
        enrollment_required=requirement is PasskeyRequirement.REQUIRED and not enrolled,
        recommended=(
            role is SystemRole.USER
            and policy.recommend_passkey_to_users
            and not enrolled
        ),
    )
    window = policy.stepup_window_minutes
    if record.stepup_at is None or record.stepup_method is None:
        step_up = StepUpState(None, None, None, window, False)
    else:
        valid_until = record.stepup_at + timedelta(minutes=window)
        step_up = StepUpState(
            method=record.stepup_method,
            verified_at=record.stepup_at,
            valid_until=valid_until,
            window_minutes=window,
            satisfied=checked_at < valid_until,
        )
    return AuthState(method=record.auth_method, passkey=passkey, step_up=step_up)
