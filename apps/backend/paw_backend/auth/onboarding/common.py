"""What the invitation, pairing and lifecycle services share.

* argument checks that raise the typed ``InvalidAuthInputError`` before any work;
* the administrator rules of Decision 0033 (the same as ``decide_role_change``: an
  Admin manages Users, the Owner also Admins, nobody the Owner or themself);
* the Passkey Step-up of an administrator's session, judged once under the
  session row's lock (``paw_backend.auth.stepup``), with the refusal remembered so
  that the service can audit it after its transaction rolled back;
* the result of checking a one-time token in a transaction that must COMMIT the
  attempt it counted even when the token is refused (``TokenCheck``).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.audit import AuthReason
from paw_backend.auth.auth_policy import AuthPolicyService
from paw_backend.auth.context import RequestContext
from paw_backend.auth.errors import (
    InvalidAuthInputError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.auth.stepup import require_passkey_step_up_in
from paw_backend.authz.roles import SystemRole
from paw_backend.authz.subjects import Principal

ADMINISTRATORS = (SystemRole.OWNER, SystemRole.ADMIN)
# The roles whose new device needs a trusted device's explicit approval
# (REQUIREMENTS.md "User Invitation / Multi-device Login").
APPROVAL_ROLES = (SystemRole.OWNER, SystemRole.ADMIN)
# What an invitation can make (the Owner is the server-local CLI's, Decision 0005).
INVITABLE_ROLES = (SystemRole.USER, SystemRole.ADMIN)
# The most a token argument may be (the real ones are 81 characters).
TOKEN_INPUT_MAX = 512


def require_uuid(name: str, value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidAuthInputError(name)
    return value


def require_context(value: object) -> RequestContext:
    if not isinstance(value, RequestContext):
        raise InvalidAuthInputError("context")
    return value


def require_actor(value: object) -> Principal:
    if not isinstance(value, Principal):
        raise InvalidAuthInputError("actor")
    return value


def require_token_text(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) > TOKEN_INPUT_MAX:
        raise InvalidAuthInputError(name)
    return value


def may_administer(actor: Principal, target_role: SystemRole | str) -> bool:
    """Whether ``actor`` may invite / delete a user of ``target_role``.

    An Admin manages Users; the Owner manages Users and Admins; the Owner's own
    account is never the target (Decision 0005: the CLI owns it).
    """
    try:
        role = SystemRole(target_role)
    except ValueError:
        return False
    if actor.system_role is SystemRole.OWNER:
        return role in (SystemRole.USER, SystemRole.ADMIN)
    if actor.system_role is SystemRole.ADMIN:
        return role is SystemRole.USER
    return False


class StepUpGuard:
    """The Passkey Step-up of an administrator's own session (Decision 0033)."""

    def __init__(self, policy: AuthPolicyService) -> None:
        self._policy = policy
        # The refusal of the last ``require_in``, for the caller's audit.
        self.refused: AuthReason | None = None

    async def require_in(
        self,
        session: AsyncSession,
        *,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        now: datetime,
    ) -> None:
        policy = await self._policy.get_in(session)
        try:
            await require_passkey_step_up_in(
                session,
                session_id=session_id,
                user_id=user_id,
                window_minutes=policy.stepup_window_minutes,
                now=now,
            )
        except StepUpMethodInsufficientError:
            self.refused = AuthReason.STEP_UP_METHOD_INSUFFICIENT
            raise
        except StepUpRequiredError:
            self.refused = AuthReason.STEP_UP_REQUIRED
            raise


@dataclass(frozen=True, slots=True)
class TokenRefusal:
    """A token that was refused inside a transaction that still commits.

    ``reason`` is ``None`` when nothing may be audited (the token names no row, or
    it was locked out already: a row anyone could make would otherwise be written).
    """

    reason: AuthReason | None
    user_id: uuid.UUID | None = None
    role: str | None = None
    audit_ref: uuid.UUID | None = None
