"""The Tool Broker's strong-approval Step-up, answered by the Passkey Step-up.

``paw_backend.tools.approvals.StepUpVerifier`` is what ``ApprovalService`` asks
before it grants a ``STRONG_APPROVAL`` (Decision 0006: a human confirms, and where the
policy asks for it, proves again with a Passkey): ``verify(user_id, approval_id)``.
``ApprovalService`` keeps its own default, ``FailClosedStepUp`` (nobody has stepped up,
so no strong approval); this class is what a deployment passes instead
(``AuthServices.approval_step_up``).

It answers ``True`` only when the user has, in a **live session**, a Passkey Step-up
inside the policy's window: the same fact the Owner's and Admin's sensitive
operations rely on (``stepup.py``), read in one statement at the database's clock. A
password Step-up does not count, a session whose Passkey gate is not open does not
count, a user who is not ``active`` does not count, and a user without a Passkey can
never be stepped up: their strong approvals stay pending (fail closed).

**What it does not do**: it is bound to the USER, not to the approval or to the
session the approval request comes from (``ApprovalService`` passes neither the
session nor a challenge). A Step-up the user made a minute ago in any of their
sessions covers an approval decided in another. The approval endpoint (not built yet)
should call ``AuthService.step_up`` in the deciding session first, or bind a challenge
to the approval; Decision 0025 lists this as a limit.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.db import run
from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.db import Database


class PasskeyApprovalStepUp:
    """``tools.approvals.StepUpVerifier`` backed by the sessions' Passkey Step-up."""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be in (0, 60]")
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout = float(timeout_seconds)

    async def verify(self, user_id: uuid.UUID, approval_id: uuid.UUID) -> bool:
        """Only an explicit ``True`` counts; a failure raises (a caller reads no)."""
        if not isinstance(user_id, uuid.UUID) or not isinstance(approval_id, uuid.UUID):
            raise InvalidAuthInputError("user_id")
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")

        async def work(session: AsyncSession) -> bool:
            return bool(
                (
                    await session.execute(
                        text(
                            """
                            WITH clock AS (SELECT greatest(CAST(:now AS timestamptz),
                                                           clock_timestamp()) AS ts)
                            SELECT EXISTS (
                                SELECT 1
                                  FROM clock, auth_sessions s
                                  JOIN users u ON u.id = s.user_id
                                  CROSS JOIN auth_policy p
                                 WHERE s.user_id = :user_id AND u.status = 'active'
                                   AND s.revoked_at IS NULL
                                   AND s.idle_expires_at > clock.ts
                                   AND s.absolute_expires_at > clock.ts
                                   AND s.passkey_gate = 'open'
                                   AND s.stepup_method = 'passkey'
                                   AND s.stepup_at + p.stepup_window_minutes
                                       * interval '1 minute' > clock.ts
                            )
                            """
                        ),
                        {"now": now, "user_id": user_id},
                    )
                ).scalar_one()
            )

        return await run(self._database, work, self._timeout)
