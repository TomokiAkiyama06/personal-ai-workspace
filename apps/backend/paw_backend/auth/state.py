"""The authentication state a session reports, and the Step-up / Passkey seams.

**Step-up** is "prove again, now, that you are the account's owner" before a
sensitive operation. ``StepUpVerifier`` is the seam: one implementation per
method: the password one (``service.PasswordStepUpVerifier``) and the Passkey one
(``passkeys.service.PasskeyStepUpVerifier``, PAW-023, registered when Passkeys are
configured).

**Passkey status** is asked through ``PasskeyEnrollment`` (default ``NoPasskeys``:
the feature is off, nobody has one; ``passkeys.store.PasskeyRegistry`` is the real
thing). What the *policy* says about a Passkey is ``AuthPolicy.requirement_for``; this
module only combines the two into what the client sees. What is *enforced* is the
session's ``PasskeyGate`` (``models``), decided at sign-in by ``AuthService``: a
"required" requirement with no Passkey enrolled gives an enrolment-only session
(Decision 0005 / 0025: never a dead end; password login and ``owner-recover`` stay).
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.audit import AuthReason
from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.models import AuthMethod, PasskeyGate, PasskeyRequirement
from paw_backend.auth.passkeys.types import AssertionCredential
from paw_backend.auth.sessions import SessionRecord
from paw_backend.authz.roles import SystemRole


class PasskeyEnrollment(Protocol):
    """What ``AuthService`` asks about a user's Passkeys.

    Only ``is_enrolled`` is required of an implementation. The others are used when
    ``available`` is true (Passkeys are configured): ``count_active_in`` at sign-in,
    to decide the session's gate, and ``confirm_in`` when a Passkey step-up is
    recorded, to confirm in that transaction that the credential is still active.
    """

    async def is_enrolled(self, user_id: uuid.UUID) -> bool: ...


class NoPasskeys:
    """The feature is off: nobody has a Passkey, nothing is enforced."""

    available = False

    async def is_enrolled(self, user_id: uuid.UUID) -> bool:
        return False

    async def count_active_in(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        return 0

    async def confirm_in(
        self, session: AsyncSession, user_id: uuid.UUID, credential_id: bytes
    ) -> uuid.UUID | None:
        return None


@dataclass(frozen=True, slots=True)
class StepUpEvidence:
    """What a client presents for a step-up: a password, or a Passkey assertion."""

    method: AuthMethod
    password: str | None = field(default=None, repr=False)
    # The checked answer to ``navigator.credentials.get()`` (Passkey Step-up).
    assertion: AssertionCredential | None = field(default=None, repr=False)
    # The session the assertion answers a challenge of. Set by ``AuthService`` from
    # the authenticated session; a value a caller passes in is replaced.
    session_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "method", AuthMethod(self.method))
        except ValueError:
            raise InvalidAuthInputError("method") from None
        if self.password is not None and not isinstance(self.password, str):
            raise InvalidAuthInputError("password")
        if self.assertion is not None and not isinstance(
            self.assertion, AssertionCredential
        ):
            raise InvalidAuthInputError("assertion")
        if self.session_id is not None and not isinstance(self.session_id, uuid.UUID):
            raise InvalidAuthInputError("session_id")


class StepUpRefused(Exception):
    """A verifier's way of saying "no" with a reason worth an audit row.

    ``StepUpVerifier.verify`` answers ``True`` or ``False``; a verifier that can tell
    WHY (a Passkey challenge that expired, a counter that went backwards) raises this
    instead, and ``AuthService`` records that reason and refuses like for ``False``.
    It carries an enum, never a value of the caller.
    """

    def __init__(self, reason: AuthReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


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
    # Passkeys are configured on this server (otherwise nothing is enforced).
    available: bool = False
    # What THIS session may do (``PasskeyGate``): "open", or restricted until the
    # user registers a Passkey / completes a Passkey authentication.
    gate: PasskeyGate = PasskeyGate.OPEN

    @property
    def next_step(self) -> str | None:
        """What the client should do next for a restricted session, else ``None``."""
        if self.gate is PasskeyGate.ENROLLMENT_REQUIRED:
            return "register"
        if self.gate is PasskeyGate.ASSERTION_REQUIRED:
            return "authenticate"
        return None


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
    available: bool = False,
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
        available=available,
        gate=record.passkey_gate,
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
