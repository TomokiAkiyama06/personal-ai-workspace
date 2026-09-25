"""Server-side sessions: create, look up, rotate, revoke, list.

A session is a row of ``auth_sessions``. The browser holds only the session id
(256 random bits, in an HttpOnly cookie); the row holds its SHA-256, so a
database read does not give anyone a usable session.

**Time.** Every decision is made with the database's clock: the "now" of a
statement is ``greatest(<caller's clock>, clock_timestamp())`` (the caller's
clock is the seam of the tests and can only make a session end EARLIER, never
later, than the database says), read in the statement that judges the row. A
session is valid when it is not revoked, ``idle_expires_at`` and
``absolute_expires_at`` are both in the future, and its user is ``active``.
``idle_expires_at`` moves forward on use (at most once per touch interval) and
never past ``absolute_expires_at``.

**Lifetimes** (settings; Decision 0015): a normal session ends after
``session_idle_days`` without use and after ``session_absolute_days`` at the
latest; "keep me signed in" (Remember Me) ends after ``session_remember_days``,
used or not. The values are copied into the row, so changing a setting affects
new sessions only.

**Rotation.** ``rotate`` gives a session a new id and invalidates the old one
in one compare-and-swap ``UPDATE``: of two requests that rotate the same session
at once exactly one wins.

Every function that changes state takes the caller's ``AsyncSession``, so a
login, a password change or a recovery commits its sessions together with the
rest of the change (and with its audit event).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.limits import (
    DEVICE_LABEL_MAX_LENGTH,
    PURGE_BATCH,
    SESSION_LIST_LIMIT,
    SESSION_RETENTION_SECONDS,
)
from paw_backend.auth.models import TOKEN_HASH_BYTES, AuthMethod, RevokeReason
from paw_backend.auth.tokens import (
    hash_session_token,
    new_session_token,
    parse_session_token,
)
from paw_backend.authz.roles import SystemRole
from paw_backend.config import Settings

_CLOCK = (
    "WITH clock AS (SELECT greatest(CAST(:now AS timestamptz), "
    "clock_timestamp()) AS ts)"
)
_COLUMNS = (
    "s.id, s.user_id, s.remember_me, s.auth_method, s.device_label, s.created_at, "
    "s.last_used_at, s.idle_expires_at, s.absolute_expires_at, s.stepup_at, "
    "s.stepup_method"
)
_VALID = (
    "s.revoked_at IS NULL AND s.idle_expires_at > clock.ts "
    "AND s.absolute_expires_at > clock.ts"
)


@dataclass(frozen=True, slots=True)
class SessionLifetimes:
    """The lifetimes of new sessions, in seconds."""

    idle_seconds: int
    remember_seconds: int
    absolute_seconds: int
    touch_interval_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> "SessionLifetimes":
        day = 86_400
        return cls(
            settings.session_idle_days * day,
            settings.session_remember_days * day,
            settings.session_absolute_days * day,
            settings.session_touch_interval_seconds,
        )

    def __post_init__(self) -> None:
        for name in (
            "idle_seconds",
            "remember_seconds",
            "absolute_seconds",
            "touch_interval_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive int")


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """A session as the user sees it in the list of their devices."""

    id: uuid.UUID
    user_id: uuid.UUID
    remember_me: bool
    auth_method: AuthMethod
    device_label: str | None
    created_at: datetime
    last_used_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    stepup_at: datetime | None
    stepup_method: AuthMethod | None

    @property
    def expires_at(self) -> datetime:
        """When it ends if it is not used again (idle) or at the latest (absolute)."""
        return min(self.idle_expires_at, self.absolute_expires_at)


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """A valid session, with the stored facts about its user."""

    record: SessionRecord
    login_name: str
    system_role: SystemRole
    # The time the database judged it at (the caller's clock or the database's).
    checked_at: datetime
    token_hash: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A session that was just created. ``token`` is shown once, to the client."""

    token: str = field(repr=False)
    record: SessionRecord


def _record(row) -> SessionRecord:
    return SessionRecord(
        id=row.id,
        user_id=row.user_id,
        remember_me=row.remember_me,
        auth_method=AuthMethod(row.auth_method),
        device_label=row.device_label,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        idle_expires_at=row.idle_expires_at,
        absolute_expires_at=row.absolute_expires_at,
        stepup_at=row.stepup_at,
        stepup_method=AuthMethod(row.stepup_method) if row.stepup_method else None,
    )


def _session(value: object) -> AsyncSession:
    if not isinstance(value, AsyncSession):
        raise InvalidAuthInputError("session")
    return value


def _uuid(name: str, value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidAuthInputError(name)
    return value


def _hash32(name: str, value: object) -> bytes:
    if not isinstance(value, bytes) or len(value) != TOKEN_HASH_BYTES:
        raise InvalidAuthInputError(name)
    return value


def _method(value: object) -> AuthMethod:
    if isinstance(value, AuthMethod):
        return value
    if isinstance(value, str):
        try:
            return AuthMethod(value)
        except ValueError:
            pass
    raise InvalidAuthInputError("method")


def validate_reason(value: object) -> RevokeReason:
    """A ``RevokeReason`` member or its exact serialised value; else a typed error."""
    if isinstance(value, RevokeReason):
        return value
    if isinstance(value, str):
        try:
            return RevokeReason(value)
        except ValueError:
            pass
    raise InvalidAuthInputError("reason")


def validate_device_label(value: object) -> str | None:
    """A device label, or ``None``: 1..64 characters without control characters."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidAuthInputError("device_label")
    label = value.strip()
    if not label:
        return None
    if len(label) > DEVICE_LABEL_MAX_LENGTH or any(
        not character.isprintable() for character in label
    ):
        raise InvalidAuthInputError("device_label")
    return label


class SessionStore:
    """The session statements. It holds no connection and no state but the lifetimes."""

    def __init__(self, lifetimes: SessionLifetimes, *, clock) -> None:
        if not isinstance(lifetimes, SessionLifetimes):
            raise TypeError("lifetimes must be SessionLifetimes")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._lifetimes = lifetimes
        self._clock = clock

    @property
    def lifetimes(self) -> SessionLifetimes:
        return self._lifetimes

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return now

    async def create(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        remember_me: bool,
        device_label: str | None = None,
        auth_method: AuthMethod = AuthMethod.PASSWORD,
    ) -> IssuedSession:
        """Insert a new session with a fresh random id (in ``session``'s transaction).

        Deletes a few sessions whose retention has passed, so that the table does
        not grow without bound (no separate job is needed).
        """
        _session(session)
        _uuid("user_id", user_id)
        if not isinstance(remember_me, bool):
            raise InvalidAuthInputError("remember_me")
        auth_method = _method(auth_method)
        device_label = validate_device_label(device_label)
        lifetimes = self._lifetimes
        idle = lifetimes.remember_seconds if remember_me else lifetimes.idle_seconds
        absolute = (
            lifetimes.remember_seconds if remember_me else lifetimes.absolute_seconds
        )
        token = new_session_token()
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    INSERT INTO auth_sessions (id, user_id, token_hash, remember_me,
                        auth_method, device_label, created_at, last_used_at,
                        idle_timeout_seconds, idle_expires_at, absolute_expires_at)
                    SELECT :id, :user_id, :token_hash, :remember_me, :auth_method,
                           :device_label, ts, ts, :idle,
                           ts + :idle * interval '1 second',
                           ts + :absolute * interval '1 second'
                    FROM clock
                    RETURNING id AS id, user_id AS user_id, remember_me AS remember_me,
                        auth_method AS auth_method, device_label AS device_label,
                        created_at AS created_at, last_used_at AS last_used_at,
                        idle_expires_at AS idle_expires_at,
                        absolute_expires_at AS absolute_expires_at,
                        stepup_at AS stepup_at, stepup_method AS stepup_method"""
                ),
                {
                    "now": self._now(),
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "token_hash": hash_session_token(token),
                    "remember_me": remember_me,
                    "auth_method": auth_method.value,
                    "device_label": device_label,
                    "idle": idle,
                    "absolute": absolute,
                },
            )
        ).one()
        await self.purge(session)
        return IssuedSession(token, _record(row))

    async def authenticate(
        self, session: AsyncSession, token: object
    ) -> AuthenticatedSession | None:
        """The valid session of ``token`` (and its active user), or ``None``.

        Also moves the session's idle expiry forward, at most once per touch
        interval. A token that has not the exact shape of a session id is
        ``None`` without a query.
        """
        _session(session)
        parsed = parse_session_token(token)
        if parsed is None:
            return None
        token_hash = hash_session_token(parsed)
        now = self._now()
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    SELECT {_COLUMNS}, u.login_name, u.system_role,
                           clock.ts AS checked_at
                    FROM clock, auth_sessions s JOIN users u ON u.id = s.user_id
                    WHERE s.token_hash = :token_hash AND {_VALID}
                      AND u.status = 'active'"""
                ),
                {"now": now, "token_hash": token_hash},
            )
        ).first()
        if row is None:
            return None
        record = _record(row)
        touched = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    UPDATE auth_sessions s
                       SET last_used_at = clock.ts,
                           idle_expires_at = least(
                               clock.ts + s.idle_timeout_seconds * interval '1 second',
                               s.absolute_expires_at)
                      FROM clock
                     WHERE s.id = :id AND {_VALID}
                       AND s.last_used_at <= clock.ts - :touch * interval '1 second'
                    RETURNING s.last_used_at AS last_used_at,
                              s.idle_expires_at AS idle_expires_at"""
                ),
                {
                    "now": now,
                    "id": record.id,
                    "touch": self._lifetimes.touch_interval_seconds,
                },
            )
        ).first()
        if touched is not None:
            record = replace(
                record,
                last_used_at=touched.last_used_at,
                idle_expires_at=touched.idle_expires_at,
            )
        return AuthenticatedSession(
            record=record,
            login_name=row.login_name,
            system_role=SystemRole(row.system_role),
            checked_at=row.checked_at,
            token_hash=token_hash,
        )

    async def rotate(
        self, session: AsyncSession, session_id: uuid.UUID, old_token_hash: bytes
    ) -> str | None:
        """Give the session a new id; ``None`` if it changed or ended meanwhile.

        One compare-and-swap: the row is updated only while it still holds
        ``old_token_hash`` and is valid, so of two concurrent rotations exactly
        one succeeds and the other's old id is already dead.
        """
        _session(session)
        _uuid("session_id", session_id)
        _hash32("old_token_hash", old_token_hash)
        token = new_session_token()
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    UPDATE auth_sessions s
                       SET token_hash = :new_hash, rotated_at = clock.ts
                      FROM clock
                     WHERE s.id = :id AND s.token_hash = :old_hash AND {_VALID}
                    RETURNING s.id"""
                ),
                {
                    "now": self._now(),
                    "id": session_id,
                    "old_hash": old_token_hash,
                    "new_hash": hash_session_token(token),
                },
            )
        ).first()
        return None if row is None else token

    async def record_step_up(
        self,
        session: AsyncSession,
        session_id: uuid.UUID,
        old_token_hash: bytes,
        method: AuthMethod,
    ) -> IssuedSession | None:
        """Mark the session as freshly step-up authenticated AND rotate its id.

        A new authentication level is a privilege change: the id changes with it.
        Returns the session as it is now, with its new id; ``None`` if the
        session changed or ended meanwhile.
        """
        _session(session)
        _uuid("session_id", session_id)
        _hash32("old_token_hash", old_token_hash)
        method = _method(method)
        token = new_session_token()
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    UPDATE auth_sessions s
                       SET token_hash = :new_hash, rotated_at = clock.ts,
                           stepup_at = clock.ts, stepup_method = :method
                      FROM clock
                     WHERE s.id = :id AND s.token_hash = :old_hash AND {_VALID}
                    RETURNING {_COLUMNS}"""
                ),
                {
                    "now": self._now(),
                    "id": session_id,
                    "old_hash": old_token_hash,
                    "new_hash": hash_session_token(token),
                    "method": method.value,
                },
            )
        ).first()
        return None if row is None else IssuedSession(token, _record(row))

    async def revoke(
        self,
        session: AsyncSession,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: RevokeReason,
    ) -> bool:
        """End one of the user's own sessions. ``False`` if there is none (or ended)."""
        _session(session)
        _uuid("session_id", session_id)
        _uuid("user_id", user_id)
        reason = validate_reason(reason)
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    UPDATE auth_sessions s
                       SET revoked_at = clock.ts, revoked_reason = :reason
                      FROM clock
                     WHERE s.id = :id AND s.user_id = :user_id
                       AND s.revoked_at IS NULL
                    RETURNING s.id"""
                ),
                {
                    "now": self._now(),
                    "id": session_id,
                    "user_id": user_id,
                    "reason": reason.value,
                },
            )
        ).first()
        return row is not None

    async def revoke_by_token(
        self, session: AsyncSession, token: object, reason: RevokeReason
    ) -> uuid.UUID | None:
        """End the session a token belongs to, whoever's it is; its id, or ``None``.

        For the browser that logs in again: whatever session it still held is
        replaced, so a session id fixed before the login is never the one that
        counts afterwards.
        """
        _session(session)
        reason = validate_reason(reason)
        parsed = parse_session_token(token)
        if parsed is None:
            return None
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    UPDATE auth_sessions s
                       SET revoked_at = clock.ts, revoked_reason = :reason
                      FROM clock
                     WHERE s.token_hash = :token_hash AND s.revoked_at IS NULL
                    RETURNING s.id"""
                ),
                {
                    "now": self._now(),
                    "token_hash": hash_session_token(parsed),
                    "reason": reason.value,
                },
            )
        ).first()
        return None if row is None else row.id

    async def revoke_all(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        reason: RevokeReason,
        *,
        except_id: uuid.UUID | None = None,
    ) -> int:
        """End every session of the user (except ``except_id``); how many ended.

        The building block of every "all sessions" rule (password reset,
        recovery, "log out everywhere", an account being closed).
        """
        _session(session)
        _uuid("user_id", user_id)
        reason = validate_reason(reason)
        if except_id is not None:
            _uuid("except_id", except_id)
        result = await session.execute(
            text(
                f"""{_CLOCK}
                UPDATE auth_sessions s
                   SET revoked_at = clock.ts, revoked_reason = :reason
                  FROM clock
                 WHERE s.user_id = :user_id AND s.revoked_at IS NULL
                   AND (CAST(:except_id AS uuid) IS NULL OR s.id <> :except_id)
                RETURNING s.id"""
            ),
            {
                "now": self._now(),
                "user_id": user_id,
                "reason": reason.value,
                "except_id": except_id,
            },
        )
        return len(result.all())

    async def list_active(
        self, session: AsyncSession, user_id: uuid.UUID, limit: int = SESSION_LIST_LIMIT
    ) -> Sequence[SessionRecord]:
        """The user's valid sessions, most recently used first."""
        _session(session)
        _uuid("user_id", user_id)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not (1 <= limit <= SESSION_LIST_LIMIT)
        ):
            raise InvalidAuthInputError("limit")
        rows = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    SELECT {_COLUMNS} FROM clock, auth_sessions s
                     WHERE s.user_id = :user_id AND {_VALID}
                     ORDER BY s.last_used_at DESC, s.id LIMIT :limit"""
                ),
                {"now": self._now(), "user_id": user_id, "limit": limit},
            )
        ).all()
        return tuple(_record(row) for row in rows)

    async def purge(self, session: AsyncSession) -> int:
        """Delete a few sessions that ended more than the retention period ago.

        At most ``PURGE_BATCH`` rows. Two statements, each served by one index
        (``ix_auth_sessions_absolute_expires_at`` for what reached its absolute
        limit, the partial ``ix_auth_sessions_revoked_at`` for what was revoked),
        never an ``OR`` of both that only a sequential scan could answer.
        """
        _session(session)
        deleted = 0
        for condition in (
            "s.absolute_expires_at < clock.ts - :keep * interval '1 second'",
            "s.revoked_at IS NOT NULL "
            "AND s.revoked_at < clock.ts - :keep * interval '1 second'",
        ):
            result = await session.execute(
                text(
                    f"""{_CLOCK}
                    DELETE FROM auth_sessions WHERE id IN (
                        SELECT s.id FROM clock, auth_sessions s
                         WHERE {condition}
                         LIMIT :batch)
                    RETURNING id"""
                ),
                {
                    "now": self._now(),
                    "keep": SESSION_RETENTION_SECONDS,
                    "batch": PURGE_BATCH - deleted,
                },
            )
            deleted += len(result.all())
            if deleted >= PURGE_BATCH:
                break
        return deleted
