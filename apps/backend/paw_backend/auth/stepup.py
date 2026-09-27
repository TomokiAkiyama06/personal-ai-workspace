"""Judging whether a session has stepped up recently enough, and how.

Every sensitive operation of an Owner or an Admin needs a Passkey Step-up inside
the policy's window (REQUIREMENTS.md "Passkey Policy"): changing the policy,
lifting another account's login lock, registering or revoking a Passkey. They all
ask the same question of the same row, so it is asked here, once.

* **The clock is the database's** (``greatest(<the service clock>, clock_timestamp())``:
  the service clock, a test seam, can only make a Step-up end EARLIER) and it is read
  AFTER the session row is locked (``FOR SHARE``): a wait for a lock must not let an
  expired Step-up pass, and the lock keeps a concurrent revocation or rotation of the
  session from slipping in between this check and the caller's commit.
* **The method counts.** A password Step-up is not a Passkey Step-up: whoever holds
  the password can obtain one (``POST /auth/step-up``), so accepting it for these
  operations would turn a stolen password into the authority the Passkey exists to
  protect (Decision 0015 section 12). ``StepUpMethodInsufficientError`` says "there is
  a Step-up, of the wrong kind".
* **Nothing is decided by the client.** The method and the time are columns only the
  Step-up code writes.

The functions take the caller's ``AsyncSession`` and must be called inside its
transaction (they lock).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.errors import (
    InvalidAuthInputError,
    StepUpMethodInsufficientError,
    StepUpRequiredError,
)
from paw_backend.auth.models import AuthMethod, PasskeyGate


@dataclass(frozen=True, slots=True)
class Freshness:
    """How recently the session proved who is behind it (judged at one instant)."""

    # The method of the session's Step-up if it is inside the window, else ``None``.
    step_up: AuthMethod | None
    # The session itself (its password sign-in) is younger than the window.
    signed_in_recently: bool
    gate: PasskeyGate

    @property
    def passkey_step_up(self) -> bool:
        return self.step_up is AuthMethod.PASSKEY

    @property
    def recently_authenticated(self) -> bool:
        return self.step_up is not None or self.signed_in_recently


async def read_freshness_in(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    window_minutes: int,
    now: datetime,
) -> Freshness | None:
    """The session's freshness, or ``None`` if it is not a live session of the user.

    Locks the session row ``FOR SHARE`` first, then reads the clock in a statement of
    its own (see the module docstring).
    """
    if not isinstance(session, AsyncSession):
        raise InvalidAuthInputError("session")
    if not isinstance(session_id, uuid.UUID) or not isinstance(user_id, uuid.UUID):
        raise InvalidAuthInputError("session_id")
    if (
        isinstance(window_minutes, bool)
        or not isinstance(window_minutes, int)
        or not 1 <= window_minutes <= 1_440
    ):
        raise InvalidAuthInputError("window_minutes")
    locked = (
        await session.execute(
            text(
                "SELECT id FROM auth_sessions WHERE id = :id AND user_id = :user_id "
                "FOR SHARE"
            ),
            {"id": session_id, "user_id": user_id},
        )
    ).first()
    if locked is None:
        return None
    row = (
        await session.execute(
            text(
                """
                WITH clock AS (SELECT greatest(CAST(:now AS timestamptz),
                                               clock_timestamp()) AS ts)
                SELECT CASE WHEN s.stepup_at IS NOT NULL
                             AND s.stepup_at + :window * interval '1 minute' > clock.ts
                            THEN s.stepup_method END AS stepup_method,
                       s.created_at + :window * interval '1 minute' > clock.ts
                           AS signed_in_recently,
                       s.passkey_gate AS passkey_gate
                  FROM clock, auth_sessions s
                 WHERE s.id = :id AND s.user_id = :user_id
                   AND s.revoked_at IS NULL
                   AND s.idle_expires_at > clock.ts
                   AND s.absolute_expires_at > clock.ts
                """
            ),
            {
                "now": now,
                "id": session_id,
                "user_id": user_id,
                "window": window_minutes,
            },
        )
    ).first()
    if row is None:
        return None
    return Freshness(
        step_up=AuthMethod(row.stepup_method) if row.stepup_method else None,
        signed_in_recently=row.signed_in_recently,
        gate=PasskeyGate(row.passkey_gate),
    )


async def require_passkey_step_up_in(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    window_minutes: int,
    now: datetime,
) -> None:
    """Refuse unless the session has a Passkey Step-up inside the window.

    ``StepUpRequiredError`` when there is none (or the session is not live);
    ``StepUpMethodInsufficientError`` (a subclass) when there is one but by a weaker
    method. A session whose Passkey gate is not open never passes: it has not
    finished signing in.
    """
    freshness = await read_freshness_in(
        session,
        session_id=session_id,
        user_id=user_id,
        window_minutes=window_minutes,
        now=now,
    )
    if freshness is None or freshness.step_up is None:
        raise StepUpRequiredError
    if freshness.step_up is not AuthMethod.PASSKEY:
        raise StepUpMethodInsufficientError
    if freshness.gate is not PasskeyGate.OPEN:
        raise StepUpRequiredError
