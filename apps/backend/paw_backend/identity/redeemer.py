"""Redemption of the one-time Owner setup / recovery tokens (the web-facing part).

``TokenRedeemer`` is all the web flow (PAW-022) gets: it can spend a token it is
handed, and nothing else. Creating the Owner and issuing tokens is the
operator's (``paw_backend.identity.operator``, only the server-local commands
import it), and a test keeps it that way. The database role the web application
runs as cannot INSERT tokens either (migration ``0021``).

Every redemption is audited through the ``AuditSink`` (ids and enum values only,
the token named by its ``audit_ref``) and **fails closed**: the audit event is
written before the transaction commits, so if it cannot be stored the token is
not consumed.

Token handling: see ``paw_backend.identity.tokens``.
"""

import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, case, event, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.audit import AuditSink
from paw_backend.authz.roles import SystemRole
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.identity import tokens
from paw_backend.identity.audit import (
    AuditAction,
    AuditReason,
    IdentityAudit,
    require_int,
)
from paw_backend.identity.errors import RedeemHookError, SetupTokenRejectedError
from paw_backend.identity.limits import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_MAX_ATTEMPTS,
    MIN_MAX_ATTEMPTS,
)
from paw_backend.identity.models import (
    SetupTokenRow,
    TokenPurpose,
    UserRow,
    UserStatus,
)

logger = logging.getLogger(__name__)

# A user whose Owner token can still be honoured.
LIVE_STATUSES = (UserStatus.INVITED.value, UserStatus.ACTIVE.value)


@dataclass(frozen=True, slots=True)
class Redemption:
    """What a successful ``redeem`` tells the web flow about the token's user."""

    user_id: uuid.UUID
    # The token as audit events name it. Never the lookup id.
    audit_ref: uuid.UUID
    purpose: TokenPurpose
    user_status: UserStatus
    # True for the Owner: registering a Passkey is mandatory (PAW-023).
    passkey_required: bool


# Runs inside the redemption's transaction (see ``TokenRedeemer.redeem``).
RedeemHook = Callable[[AsyncSession, Redemption], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Attempt:
    """The token row as it was when an attempt was reserved."""

    token_id: uuid.UUID
    audit_ref: uuid.UUID
    user_id: uuid.UUID
    purpose: TokenPurpose
    salt: bytes
    secret_hash: bytes
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    # True when this very attempt used up the last one and locked the token.
    locked_now: bool


class _Refused(Exception):
    """Internal: redemption stopped for a reason that only the audit trail sees."""

    def __init__(self, reason: AuditReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class TokenRedeemer:
    """Spends setup / recovery tokens. It cannot create or revoke any."""

    def __init__(
        self,
        database: Database,
        audit: AuditSink,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        audit_timeout_seconds: float = 3.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        require_int("max_attempts", max_attempts, MIN_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS)
        self._database = database
        self._audit = IdentityAudit(
            audit, timeout_seconds=audit_timeout_seconds, clock=clock
        )
        self._max_attempts = max_attempts

    @classmethod
    def from_settings(
        cls, settings: Settings, database: Database, audit: AuditSink
    ) -> "TokenRedeemer":
        return cls(
            database,
            audit,
            max_attempts=settings.setup_token_max_attempts,
            audit_timeout_seconds=settings.database_timeout_seconds,
        )

    async def redeem(
        self, token: object, *, apply: RedeemHook | None = None
    ) -> Redemption:
        """Consume a setup / recovery token, once.

        Raises ``SetupTokenRejectedError`` (always the same error, whatever
        was wrong) unless the token is well-formed, known, correct, unused,
        unrevoked, unexpired, not locked out, and its user is (still) the
        Owner.

        ``apply`` is how the web flow does its part atomically: it runs inside
        the redemption's transaction, after the token was consumed and before
        the commit, with that transaction's session and the ``Redemption``.
        The flow sets the password (and, for a recovery, revokes the sessions and
        credentials) there. If it raises, the transaction is rolled back, the
        token is not consumed, and the exception propagates unchanged. The hook
        must not commit, roll back or close the session: that is refused
        (``RedeemHookError``) and rolls everything back. The Owner's ``users``
        row is locked (``SELECT ... FOR UPDATE``) until the redemption ends, so
        keep the hook short. The attempt counter still counts the try, so
        validate input before calling ``redeem``.

        The expiry is judged twice: cheaply at the start (an expired token
        never waits for the Owner lock), and again, for good, by the statement
        that consumes the token, after the Owner lock was obtained (a wait for
        that lock cannot let a token be used after its ``expires_at``). The
        consuming statement counts a token as expired as soon as this process's
        clock (read again after the lock) or the database's ``clock_timestamp()``
        says so, so a clock that is ahead can only shorten the lifetime.

        Attempts are bounded per token: an attempt is reserved (and committed)
        *before* the secret is compared, so at most ``max_attempts`` comparisons
        are ever made for one token, however many requests arrive at once. The
        attempt that uses up the last one locks the token for good
        (``locked_at``): it can never be redeemed again, whatever the setting
        says later. Issue a new one.
        """
        if apply is not None and not callable(apply):
            raise TypeError("apply must be callable")
        parsed = tokens.parse(token)
        now = self._audit.now()
        correlation_id = uuid.uuid4()
        # A malformed token still makes the same database round trip (with an
        # id that names nothing), so that its cost does not tell it apart.
        attempt = await self._reserve_attempt(
            parsed.token_id if parsed is not None else uuid.uuid4(), now
        )
        # The same comparison work whatever the token was (see tokens.verify).
        matches = tokens.verify(
            parsed,
            attempt.salt if attempt is not None else None,
            attempt.secret_hash if attempt is not None else None,
        )
        if attempt is None:
            # Malformed, unknown or locked out. Nothing to count against, and
            # nothing that anyone can write to the audit table at will: a log
            # line only (no token, no id).
            logger.info("Setup token rejected (no usable token row)")
            raise SetupTokenRejectedError
        reason = _rejection_reason(attempt, matches, now)
        if reason is None:
            try:
                return await self._consume(attempt, correlation_id, apply)
            except _Refused as refusal:
                reason = refusal.reason
        await self._record_failure(attempt, reason, correlation_id)
        raise SetupTokenRejectedError

    async def _reserve_attempt(
        self, token_id: uuid.UUID, now: datetime
    ) -> _Attempt | None:
        """Count one attempt against the token; ``None`` if it may not be tried."""
        counted = SetupTokenRow.attempts + 1
        async with self._database.session() as session:
            row = (
                await session.execute(
                    update(SetupTokenRow)
                    .where(
                        SetupTokenRow.id == token_id,
                        SetupTokenRow.locked_at.is_(None),
                        SetupTokenRow.attempts < self._max_attempts,
                    )
                    .values(
                        attempts=counted,
                        # Locked by the attempt that uses up the last one.
                        locked_at=case((counted >= self._max_attempts, now)),
                    )
                    .returning(
                        SetupTokenRow.audit_ref,
                        SetupTokenRow.user_id,
                        SetupTokenRow.purpose,
                        SetupTokenRow.salt,
                        SetupTokenRow.secret_hash,
                        SetupTokenRow.expires_at,
                        SetupTokenRow.used_at,
                        SetupTokenRow.revoked_at,
                        SetupTokenRow.locked_at,
                    )
                    .execution_options(synchronize_session=False)
                )
            ).first()
            await session.commit()
        if row is None:
            return None
        return _Attempt(
            token_id=token_id,
            audit_ref=row.audit_ref,
            user_id=row.user_id,
            purpose=TokenPurpose(row.purpose),
            salt=bytes(row.salt),
            secret_hash=bytes(row.secret_hash),
            expires_at=row.expires_at,
            used_at=row.used_at,
            revoked_at=row.revoked_at,
            locked_now=row.locked_at is not None,
        )

    async def _consume(
        self,
        attempt: _Attempt,
        correlation_id: uuid.UUID,
        apply: RedeemHook | None,
    ) -> Redemption:
        async with self._database.session() as session:
            # User first, token second: the same order as recovery in the
            # operator (so that the two cannot deadlock).
            user = (
                await session.execute(
                    select(UserRow)
                    .where(UserRow.id == attempt.user_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                user is None
                or user.system_role != SystemRole.OWNER.value
                or user.status not in LIVE_STATUSES
            ):
                raise _Refused(AuditReason.USER_NOT_ELIGIBLE)
            # The lock above may have been waited for, so the instant the attempt
            # began with says nothing about whether the token is still live: the
            # expiry is judged now, by the consuming statement itself (Decision
            # 0005, point 10). The token is expired as soon as EITHER this
            # process's clock (read again, after the lock: the seam of the
            # tests) OR the database's says so. ``clock_timestamp()`` is the
            # wall clock at the moment the statement judges the row, not
            # ``now()`` (the start of the transaction).
            now = self._audit.now()
            consumed = (
                await session.execute(
                    update(SetupTokenRow)
                    .where(
                        SetupTokenRow.id == attempt.token_id,
                        SetupTokenRow.used_at.is_(None),
                        SetupTokenRow.revoked_at.is_(None),
                        SetupTokenRow.expires_at
                        > func.greatest(
                            literal(now, DateTime(timezone=True)),
                            func.clock_timestamp(),
                        ),
                    )
                    .values(used_at=now)
                    .returning(SetupTokenRow.id)
                    .execution_options(synchronize_session=False)
                )
            ).first()
            if consumed is None:
                raise _Refused(await self._why_not_consumed(session, attempt))
            redemption = Redemption(
                user_id=user.id,
                audit_ref=attempt.audit_ref,
                purpose=attempt.purpose,
                user_status=UserStatus(user.status),
                passkey_required=user.passkey_required,
            )
            if apply is not None:
                with _transaction_must_stay_open(session):
                    await apply(session, redemption)
                # A hook that rolled back or closed the session undid the
                # consumption: refuse rather than audit something that is gone.
                used_at = await session.scalar(
                    select(SetupTokenRow.used_at).where(
                        SetupTokenRow.id == attempt.token_id
                    )
                )
                if used_at is None:
                    raise RedeemHookError
            await self._audit.record(
                self._audit.event(
                    AuditAction.TOKEN_REDEEM,
                    AuditReason.REDEEMED,
                    correlation_id=correlation_id,
                    actor_id=user.id,
                    actor_role=SystemRole.OWNER,
                    resource_kind="setup_token",
                    resource_id=attempt.audit_ref,
                )
            )
            await session.commit()
        return redemption

    async def _why_not_consumed(
        self, session: AsyncSession, attempt: _Attempt
    ) -> AuditReason:
        """Why the consuming statement matched nothing (the Owner row is locked).

        Nobody can use or revoke the token meanwhile (both take the user's lock
        first), so if it is still unused and unrevoked, the expiry is all that
        is left of the statement's conditions.
        """
        still_open = await session.scalar(
            select(
                SetupTokenRow.used_at.is_(None) & SetupTokenRow.revoked_at.is_(None)
            ).where(SetupTokenRow.id == attempt.token_id)
        )
        if still_open:
            return AuditReason.TOKEN_EXPIRED
        return AuditReason.TOKEN_UNAVAILABLE  # used / revoked while redeeming

    async def _record_failure(
        self, attempt: _Attempt, reason: AuditReason, correlation_id: uuid.UUID
    ) -> None:
        events = [
            self._audit.token_event(
                AuditAction.TOKEN_REDEEM,
                reason,
                correlation_id,
                attempt.audit_ref,
                allowed=False,
                actor_role=None,  # nobody is authenticated
            )
        ]
        if attempt.locked_now:
            # Written once per token: only one attempt can lock it.
            events.append(
                self._audit.token_event(
                    AuditAction.TOKEN_REDEEM,
                    AuditReason.ATTEMPTS_EXHAUSTED,
                    correlation_id,
                    attempt.audit_ref,
                    allowed=False,
                    actor_role=None,
                )
            )
        for audit_event in events:
            await self._audit.record_best_effort(audit_event)


@contextlib.contextmanager
def _transaction_must_stay_open(session: AsyncSession) -> Iterator[None]:
    """Make ``session.commit()`` fail while the ``apply`` hook runs.

    A hook that commits would make the token's consumption permanent before its
    audit event is stored. Rolling back or closing is caught afterwards (the
    token would no longer be consumed). Raw ``COMMIT`` statements, or a commit
    on a connection taken from the session, cannot be stopped from here: the
    hook is trusted code, this only catches the mistake.
    """
    sync_session = session.sync_session

    def refuse(_: object) -> None:
        raise RedeemHookError

    event.listen(sync_session, "before_commit", refuse)
    try:
        yield
    finally:
        event.remove(sync_session, "before_commit", refuse)


def _rejection_reason(
    attempt: _Attempt, matches: bool, now: datetime
) -> AuditReason | None:
    """Why the token cannot be used, or ``None``. Every check is evaluated."""
    mismatch = not matches
    revoked = attempt.revoked_at is not None
    used = attempt.used_at is not None
    expired = attempt.expires_at <= now
    if mismatch:
        return AuditReason.TOKEN_MISMATCH
    if revoked:
        return AuditReason.TOKEN_REVOKED
    if used:
        return AuditReason.TOKEN_USED
    if expired:
        return AuditReason.TOKEN_EXPIRED
    return None
