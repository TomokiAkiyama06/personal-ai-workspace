"""Initial Owner setup, Owner recovery and redemption of their one-time tokens.

There is exactly one way to create the Owner: ``setup_owner`` (from the
server-local CLI). It creates the Owner user (status ``invited``, no credential)
and a one-time setup token; nothing on the web can create an Owner, so the first
visitor of the site is nobody. ``recover_owner`` issues a recovery token for the
existing Owner. The web flow (PAW-022) later calls ``redeem`` with the token the
operator hands over.

Every step is audited through the ``AuditSink`` (ids and enum values only) and
**fails closed**: the audit event is written before the database transaction
commits, so if it cannot be stored nothing changes and no token is ever shown.
(If the commit itself fails after the event was stored, the trail holds an
event for something that did not happen; the token was never shown.)

Token handling: see ``paw_backend.identity.tokens``. In short, the plaintext
exists only in the returned ``IssuedToken``; the database keeps a salted HMAC.
"""

import asyncio
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz.audit import AuditEvent, AuditSink
from paw_backend.authz.roles import SystemRole
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.identity import tokens
from paw_backend.identity.errors import (
    AuditUnavailableError,
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
    SetupTokenRejectedError,
)
from paw_backend.identity.login_name import normalize_login_name
from paw_backend.identity.models import (
    SetupTokenRow,
    TokenPurpose,
    UserRow,
    UserStatus,
    passkey_required_for,
)

logger = logging.getLogger(__name__)

# Bounds of the two settings (``Settings.setup_token_*`` uses the same numbers).
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 86_400
DEFAULT_TTL_SECONDS = 1_800
MIN_MAX_ATTEMPTS = 1
MAX_MAX_ATTEMPTS = 20
DEFAULT_MAX_ATTEMPTS = 5

# Constraint names the service tells apart when an INSERT is refused.
_SINGLE_OWNER = "uq_users_single_owner"
_LOGIN_NAME_UNIQUE = "uq_users_login_name"

# A user whose Owner token can still be honoured.
_LIVE_STATUSES = (UserStatus.INVITED.value, UserStatus.ACTIVE.value)


class AuditAction(StrEnum):
    """``AuditEvent.action`` values written by this service."""

    OWNER_CREATE = "owner.create"
    SETUP_TOKEN_ISSUE = "owner.setup_token.issue"
    RECOVERY_TOKEN_ISSUE = "owner.recovery_token.issue"
    TOKEN_REVOKE = "owner.token.revoke"
    TOKEN_REDEEM = "owner.token.redeem"


class AuditReason(StrEnum):
    """``AuditEvent.reason`` values. Only the audit trail sees the fine reasons."""

    # allow
    CREATED = "created"
    ISSUED = "issued"
    SUPERSEDED = "superseded"
    REDEEMED = "redeemed"
    # deny
    OWNER_EXISTS = "owner_exists"
    LOGIN_NAME_TAKEN = "login_name_taken"
    OWNER_MISSING = "owner_missing"
    TOKEN_MISMATCH = "token_mismatch"
    TOKEN_EXPIRED = "token_expired"
    TOKEN_USED = "token_used"
    TOKEN_REVOKED = "token_revoked"
    TOKEN_UNAVAILABLE = "token_unavailable"  # consumed / revoked while redeeming
    USER_NOT_ELIGIBLE = "user_not_eligible"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A token to show to the operator once. ``token`` is not part of ``repr``."""

    token: str = field(repr=False)
    token_id: uuid.UUID
    user_id: uuid.UUID
    login_name: str
    purpose: TokenPurpose
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class Redemption:
    """What a successful ``redeem`` tells the web flow about the token's user."""

    user_id: uuid.UUID
    token_id: uuid.UUID
    purpose: TokenPurpose
    user_status: UserStatus
    # True for the Owner: registering a Passkey is mandatory (PAW-023).
    passkey_required: bool


# Runs inside the redemption's transaction (see ``OwnerSetupService.redeem``).
RedeemHook = Callable[[AsyncSession, Redemption], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Attempt:
    """The token row as it was when an attempt was reserved."""

    token_id: uuid.UUID
    user_id: uuid.UUID
    purpose: TokenPurpose
    salt: bytes
    secret_hash: bytes
    expires_at: datetime
    used_at: datetime | None
    revoked_at: datetime | None
    attempts: int


class _Refused(Exception):
    """Internal: redemption stopped for a reason that only the audit trail sees."""

    def __init__(self, reason: AuditReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class OwnerSetupService:
    """Creates the Owner, issues setup / recovery tokens and redeems them.

    It performs no authorisation of its own: the callers are the server-local
    CLI (whoever can run it has the database credentials) and, for ``redeem``,
    the web flow, where the token is the credential.
    """

    def __init__(
        self,
        database: Database,
        audit: AuditSink,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        audit_timeout_seconds: float = 3.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        _require_int("ttl_seconds", ttl_seconds, MIN_TTL_SECONDS, MAX_TTL_SECONDS)
        _require_int("max_attempts", max_attempts, MIN_MAX_ATTEMPTS, MAX_MAX_ATTEMPTS)
        _require_sink(audit)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if not isinstance(audit_timeout_seconds, int | float) or not (
            0 < audit_timeout_seconds <= 60
        ):
            raise ValueError("audit_timeout_seconds must be in (0, 60]")
        self._database = database
        self._audit = audit
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_attempts = max_attempts
        self._audit_timeout = float(audit_timeout_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_settings(
        cls, settings: Settings, database: Database, audit: AuditSink
    ) -> "OwnerSetupService":
        return cls(
            database,
            audit,
            ttl_seconds=settings.setup_token_ttl_seconds,
            max_attempts=settings.setup_token_max_attempts,
            audit_timeout_seconds=settings.database_timeout_seconds,
        )

    # -- initial setup ------------------------------------------------------

    async def setup_owner(self, login_name: str) -> IssuedToken:
        """Create the Owner and a one-time setup token for them.

        Refuses (``OwnerAlreadyExistsError``) when an Owner exists. Two
        concurrent calls cannot both succeed: the database allows one Owner row
        (a unique index), so the second INSERT fails and is refused.
        """
        name = normalize_login_name(login_name)
        correlation_id = uuid.uuid4()
        try:
            return await self._create_owner(name, correlation_id)
        except OwnerAlreadyExistsError:
            await self._record_best_effort(
                self._event(
                    AuditAction.OWNER_CREATE,
                    AuditReason.OWNER_EXISTS,
                    correlation_id=correlation_id,
                    allowed=False,
                )
            )
            raise
        except LoginNameTakenError:
            await self._record_best_effort(
                self._event(
                    AuditAction.OWNER_CREATE,
                    AuditReason.LOGIN_NAME_TAKEN,
                    correlation_id=correlation_id,
                    allowed=False,
                )
            )
            raise

    async def _create_owner(self, name: str, correlation_id: uuid.UUID) -> IssuedToken:
        now = self._now()
        user_id = uuid.uuid4()
        new = tokens.generate()
        expires_at = now + self._ttl
        try:
            async with self._database.session() as session:
                existing = await session.scalar(
                    select(UserRow.id).where(
                        UserRow.system_role == SystemRole.OWNER.value
                    )
                )
                if existing is not None:
                    raise OwnerAlreadyExistsError
                session.add(
                    UserRow(
                        id=user_id,
                        login_name=name,
                        system_role=SystemRole.OWNER.value,
                        status=UserStatus.INVITED.value,
                        passkey_required=passkey_required_for(SystemRole.OWNER),
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.flush()
                session.add(self._token_row(new, user_id, TokenPurpose.SETUP, now))
                await session.flush()
                await self._record(
                    self._event(
                        AuditAction.OWNER_CREATE,
                        AuditReason.CREATED,
                        correlation_id=correlation_id,
                        resource_id=user_id,
                        new_role=SystemRole.OWNER,
                    )
                )
                await self._record(
                    self._token_event(
                        AuditAction.SETUP_TOKEN_ISSUE,
                        AuditReason.ISSUED,
                        correlation_id,
                        new.token_id,
                    )
                )
                await session.commit()
        except IntegrityError as error:
            constraint = _violated_constraint(error)
            if constraint == _SINGLE_OWNER:
                raise OwnerAlreadyExistsError from None
            if constraint == _LOGIN_NAME_UNIQUE:
                raise LoginNameTakenError from None
            raise
        return IssuedToken(
            token=new.token,
            token_id=new.token_id,
            user_id=user_id,
            login_name=name,
            purpose=TokenPurpose.SETUP,
            expires_at=expires_at,
        )

    # -- recovery -----------------------------------------------------------

    async def recover_owner(self) -> IssuedToken:
        """Issue a recovery token for the existing Owner.

        Every outstanding token of the Owner is revoked first (each is audited),
        so only the new one works. Refuses (``OwnerNotFoundError``) when there
        is no Owner.
        """
        correlation_id = uuid.uuid4()
        try:
            return await self._issue_recovery(correlation_id)
        except OwnerNotFoundError:
            await self._record_best_effort(
                self._event(
                    AuditAction.RECOVERY_TOKEN_ISSUE,
                    AuditReason.OWNER_MISSING,
                    correlation_id=correlation_id,
                    allowed=False,
                )
            )
            raise

    async def _issue_recovery(self, correlation_id: uuid.UUID) -> IssuedToken:
        now = self._now()
        new = tokens.generate()
        async with self._database.session() as session:
            # The row lock serialises concurrent recoveries (and a redemption,
            # which takes the same lock first): revoke-then-insert cannot
            # interleave with another one.
            owner = (
                await session.execute(
                    select(UserRow)
                    .where(
                        UserRow.system_role == SystemRole.OWNER.value,
                        UserRow.status.in_(_LIVE_STATUSES),
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if owner is None:
                raise OwnerNotFoundError
            revoked = list(
                (
                    await session.execute(
                        update(SetupTokenRow)
                        .where(
                            SetupTokenRow.user_id == owner.id,
                            SetupTokenRow.used_at.is_(None),
                            SetupTokenRow.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                        .returning(SetupTokenRow.id)
                        .execution_options(synchronize_session=False)
                    )
                ).scalars()
            )
            session.add(self._token_row(new, owner.id, TokenPurpose.RECOVERY, now))
            await session.flush()
            for token_id in revoked:
                await self._record(
                    self._token_event(
                        AuditAction.TOKEN_REVOKE,
                        AuditReason.SUPERSEDED,
                        correlation_id,
                        token_id,
                    )
                )
            await self._record(
                self._token_event(
                    AuditAction.RECOVERY_TOKEN_ISSUE,
                    AuditReason.ISSUED,
                    correlation_id,
                    new.token_id,
                )
            )
            await session.commit()
            login_name = owner.login_name
            user_id = owner.id
        return IssuedToken(
            token=new.token,
            token_id=new.token_id,
            user_id=user_id,
            login_name=login_name,
            purpose=TokenPurpose.RECOVERY,
            expires_at=now + self._ttl,
        )

    # -- redemption ---------------------------------------------------------

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
        The flow sets the password (and, for a recovery, revokes the sessions)
        there. If it raises, the transaction is rolled back, the token is not
        consumed, and the exception propagates unchanged. The attempt counter
        still counts the try, so validate input before calling ``redeem``.

        Attempts are bounded per token: an attempt is reserved (and committed)
        *before* the secret is compared, so at most ``max_attempts`` comparisons
        are ever made for one token, however many requests arrive at once. A
        locked-out token can never be redeemed again; issue a new one.
        """
        if apply is not None and not callable(apply):
            raise TypeError("apply must be callable")
        parsed = tokens.parse(token)
        now = self._now()
        correlation_id = uuid.uuid4()
        # A malformed token still makes the same database round trip (with an
        # id that names nothing), so that its cost does not tell it apart.
        attempt = await self._reserve_attempt(
            parsed.token_id if parsed is not None else uuid.uuid4()
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
                return await self._consume(attempt, now, correlation_id, apply)
            except _Refused as refusal:
                reason = refusal.reason
        await self._record_failure(attempt, reason, correlation_id)
        raise SetupTokenRejectedError

    async def _reserve_attempt(self, token_id: uuid.UUID) -> _Attempt | None:
        """Count one attempt against the token; ``None`` if it may not be tried."""
        async with self._database.session() as session:
            row = (
                await session.execute(
                    update(SetupTokenRow)
                    .where(
                        SetupTokenRow.id == token_id,
                        SetupTokenRow.attempts < self._max_attempts,
                    )
                    .values(attempts=SetupTokenRow.attempts + 1)
                    .returning(
                        SetupTokenRow.user_id,
                        SetupTokenRow.purpose,
                        SetupTokenRow.salt,
                        SetupTokenRow.secret_hash,
                        SetupTokenRow.expires_at,
                        SetupTokenRow.used_at,
                        SetupTokenRow.revoked_at,
                        SetupTokenRow.attempts,
                    )
                    .execution_options(synchronize_session=False)
                )
            ).first()
            await session.commit()
        if row is None:
            return None
        return _Attempt(
            token_id=token_id,
            user_id=row.user_id,
            purpose=TokenPurpose(row.purpose),
            salt=bytes(row.salt),
            secret_hash=bytes(row.secret_hash),
            expires_at=row.expires_at,
            used_at=row.used_at,
            revoked_at=row.revoked_at,
            attempts=row.attempts,
        )

    async def _consume(
        self,
        attempt: _Attempt,
        now: datetime,
        correlation_id: uuid.UUID,
        apply: RedeemHook | None,
    ) -> Redemption:
        async with self._database.session() as session:
            # User first, token second: the same order as ``_issue_recovery``.
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
                or user.status not in _LIVE_STATUSES
            ):
                raise _Refused(AuditReason.USER_NOT_ELIGIBLE)
            consumed = (
                await session.execute(
                    update(SetupTokenRow)
                    .where(
                        SetupTokenRow.id == attempt.token_id,
                        SetupTokenRow.used_at.is_(None),
                        SetupTokenRow.revoked_at.is_(None),
                        SetupTokenRow.expires_at > now,
                    )
                    .values(used_at=now)
                    .returning(SetupTokenRow.id)
                    .execution_options(synchronize_session=False)
                )
            ).first()
            if consumed is None:  # lost a race with another redemption or a revoke
                raise _Refused(AuditReason.TOKEN_UNAVAILABLE)
            redemption = Redemption(
                user_id=user.id,
                token_id=attempt.token_id,
                purpose=attempt.purpose,
                user_status=UserStatus(user.status),
                passkey_required=user.passkey_required,
            )
            if apply is not None:
                await apply(session, redemption)
            await self._record(
                self._event(
                    AuditAction.TOKEN_REDEEM,
                    AuditReason.REDEEMED,
                    correlation_id=correlation_id,
                    actor_id=user.id,
                    actor_role=SystemRole.OWNER,
                    resource_kind="setup_token",
                    resource_id=attempt.token_id,
                )
            )
            await session.commit()
        return redemption

    async def _record_failure(
        self, attempt: _Attempt, reason: AuditReason, correlation_id: uuid.UUID
    ) -> None:
        events = [
            self._token_event(
                AuditAction.TOKEN_REDEEM,
                reason,
                correlation_id,
                attempt.token_id,
                allowed=False,
                actor_role=None,  # nobody is authenticated
            )
        ]
        if attempt.attempts >= self._max_attempts:
            # Written once per token: the reservation never goes past the maximum.
            events.append(
                self._token_event(
                    AuditAction.TOKEN_REDEEM,
                    AuditReason.ATTEMPTS_EXHAUSTED,
                    correlation_id,
                    attempt.token_id,
                    allowed=False,
                    actor_role=None,
                )
            )
        for event in events:
            await self._record_best_effort(event)

    # -- helpers ------------------------------------------------------------

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    def _token_row(
        self,
        new: tokens.NewToken,
        user_id: uuid.UUID,
        purpose: TokenPurpose,
        now: datetime,
    ) -> SetupTokenRow:
        return SetupTokenRow(
            id=new.token_id,
            user_id=user_id,
            purpose=purpose.value,
            salt=new.salt,
            secret_hash=new.secret_hash,
            created_at=now,
            expires_at=now + self._ttl,
            attempts=0,
        )

    def _event(
        self,
        action: AuditAction,
        reason: AuditReason,
        *,
        correlation_id: uuid.UUID,
        allowed: bool = True,
        actor_id: uuid.UUID | None = None,
        actor_role: SystemRole | None = SystemRole.SYSTEM,
        resource_kind: str = "user",
        resource_id: uuid.UUID | None = None,
        new_role: SystemRole | None = None,
    ) -> AuditEvent:
        """An event. By default the actor is the backend itself (the local CLI)."""
        return AuditEvent(
            event_id=uuid.uuid4(),
            correlation_id=correlation_id,
            occurred_at=self._now(),
            actor_id=actor_id,
            actor_role=actor_role.value if actor_role is not None else None,
            action=action.value,
            resource_kind=resource_kind,
            resource_id=resource_id,
            decision="allow" if allowed else "deny",
            reason=reason.value,
            new_role=new_role.value if new_role is not None else None,
        )

    def _token_event(
        self,
        action: AuditAction,
        reason: AuditReason,
        correlation_id: uuid.UUID,
        token_id: uuid.UUID,
        *,
        allowed: bool = True,
        actor_role: SystemRole | None = SystemRole.SYSTEM,
    ) -> AuditEvent:
        return self._event(
            action,
            reason,
            correlation_id=correlation_id,
            allowed=allowed,
            actor_role=actor_role,
            resource_kind="setup_token",
            resource_id=token_id,
        )

    async def _record(self, event: AuditEvent) -> None:
        """Store ``event``; ``AuditUnavailableError`` if that fails."""
        try:
            async with asyncio.timeout(self._audit_timeout):
                await self._audit.record(event)
        except Exception as error:
            # Type name only: a driver message can name the host or the user.
            logger.error(
                "Audit write failed (%s) for action %s",
                type(error).__name__,
                event.action,
            )
            raise AuditUnavailableError from None

    async def _record_best_effort(self, event: AuditEvent) -> None:
        """For events about something that was refused anyway."""
        try:
            await self._record(event)
        except AuditUnavailableError:
            pass  # already logged; the refusal stands


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


def _violated_constraint(error: IntegrityError) -> str | None:
    diag = getattr(error.orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def _require_int(name: str, value: object, low: int, high: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an integer from {low} to {high}")


def _require_sink(audit: object) -> None:
    record = getattr(audit, "record", None)
    if not callable(record) or not inspect.iscoroutinefunction(record):
        raise TypeError("audit must have an async record(event) method")
    try:
        inspect.signature(record).bind(object())
    except TypeError:
        raise TypeError("audit.record must accept one event argument") from None
