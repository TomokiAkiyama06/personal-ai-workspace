"""Every statement on ``user_passkeys`` and ``passkey_challenges``.

``PasskeyRegistry`` is what ``AuthService`` sees (``PasskeyEnrollment``) and what the
Passkey service and verifier use. Methods named ``*_in`` take the caller's
``AsyncSession`` and must run inside its transaction (they lock); the others open a
transaction of their own (``paw_backend.auth.db.run``: an abortable connection with
one deadline, fail closed).

**Time.** The "now" of a statement is ``greatest(<the service clock>,
clock_timestamp())``: the service clock is the seam of the tests and can only make
something end EARLIER than the database says, never later. A challenge's expiry is
judged in the RETURNING list of the statement that consumes it, i.e. after its row
lock is held: a wait for another transaction that holds the row (a second ``begin``
replacing the challenge) must not let an expired challenge pass.

**Single use.** A challenge is consumed by DELETE ... RETURNING: of two answers to
one challenge exactly one gets it. Consuming is unconditional (an expired or wrong
answer burns the challenge too): a challenge gets one attempt.

**The signature counter** is compared and stored in ONE conditional UPDATE
(``record_use_in``): the new count must be greater than the stored one, or both
must be 0 (an authenticator without a counter). Two concurrent uses of the same
assertion (or a replay) therefore cannot both succeed, and a count that does not go
up is reported as a regression (the credential may have been cloned), never
silently accepted.

**Locks.** A user's Passkeys are changed under ``users ... FOR UPDATE`` (register,
revoke) and a sign-in counts them under ``FOR SHARE``: the count a session's gate is
decided on cannot change under it, and two registrations cannot both pass the limit.
A Passkey is confirmed for a Step-up under ``FOR SHARE`` on its own row: a
revocation (an UPDATE of that row) waits for the recording transaction, and after
it, sees the session that was just stepped up and ends it.
"""

import enum
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.auth.db import run
from paw_backend.auth.errors import InvalidAuthInputError
from paw_backend.auth.passkeys.ceremony import VerifiedAssertion, VerifiedRegistration
from paw_backend.auth.passkeys.config import PasskeyConfig
from paw_backend.auth.passkeys.models import (
    CHALLENGE_BYTES,
    PASSKEY_NAME_MAX_LENGTH,
    PasskeyPurpose,
    PasskeyRevokeReason,
)
from paw_backend.db import Database

logger = logging.getLogger(__name__)

# Expired challenges are removed a few at a time by whoever asks for a new one.
PURGE_BATCH = 50
# An expired challenge is only kept this long (it is unusable at once; the row
# lingers so that a purge statement is not on every request's path).
PURGE_AFTER_SECONDS = 3_600
LIST_LIMIT = 50


@dataclass(frozen=True, slots=True)
class PasskeyRecord:
    """A registered Passkey as its owner sees it in the list of their devices."""

    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    backup_eligible: bool
    backed_up: bool


@dataclass(frozen=True, slots=True)
class StoredPasskey:
    """What verifying an assertion needs of a registered credential."""

    id: uuid.UUID
    public_key: bytes
    sign_count: int


class UseOutcome(enum.Enum):
    USED = "used"
    REGRESSION = "regression"  # the count did not go up: replay, or a clone
    GONE = "gone"  # unknown, not the user's, or revoked meanwhile
    CHANGED = "changed"  # a fact that never changes (backup eligibility) did


def _uuid(name: str, value: object) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise InvalidAuthInputError(name)
    return value


def _session(value: object) -> AsyncSession:
    if not isinstance(value, AsyncSession):
        raise InvalidAuthInputError("session")
    return value


def _bytes(name: str, value: object, low: int, high: int) -> bytes:
    if not isinstance(value, bytes) or not low <= len(value) <= high:
        raise InvalidAuthInputError(name)
    return value


_CLOCK = (
    "WITH clock AS (SELECT greatest(CAST(:now AS timestamptz), "
    "clock_timestamp()) AS ts)"
)
_NOW = "greatest(CAST(:now AS timestamptz), clock_timestamp())"


class PasskeyRegistry:
    """The Passkey tables; ``PasskeyEnrollment`` for ``AuthService``."""

    def __init__(
        self,
        database: Database,
        config: PasskeyConfig | None,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if config is not None and not isinstance(config, PasskeyConfig):
            raise TypeError("config must be a PasskeyConfig or None")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("timeout_seconds must be in (0, 60]")
        self._database = database
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout = float(timeout_seconds)

    # -- what AuthService asks -----------------------------------------------------

    @property
    def available(self) -> bool:
        """Passkeys are configured (else nothing is enforced and nothing registered)."""
        return self._config is not None

    @property
    def config(self) -> PasskeyConfig | None:
        return self._config

    def now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return timezone-aware datetimes")
        return value

    async def is_enrolled(self, user_id: uuid.UUID) -> bool:
        """Whether the user has an active Passkey."""
        _uuid("user_id", user_id)

        async def work(session: AsyncSession) -> bool:
            return await self.count_active_in(session, user_id) > 0

        return await run(self._database, work, self._timeout)

    async def count_active_in(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        _session(session)
        _uuid("user_id", user_id)
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM user_passkeys "
                    "WHERE user_id = :user_id AND revoked_at IS NULL"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()

    async def confirm_in(
        self, session: AsyncSession, user_id: uuid.UUID, credential_id: bytes
    ) -> uuid.UUID | None:
        """The id of the user's ACTIVE Passkey with this credential id, locked.

        ``FOR SHARE``: a revocation of the row waits for this transaction (and then
        ends the session that this transaction stepped up); a revocation that has
        already committed is not found.
        """
        _session(session)
        _uuid("user_id", user_id)
        _bytes("credential_id", credential_id, 1, 1023)
        return (
            await session.execute(
                text(
                    "SELECT id FROM user_passkeys WHERE user_id = :user_id "
                    "AND credential_id = :credential_id AND revoked_at IS NULL "
                    "FOR SHARE"
                ),
                {"user_id": user_id, "credential_id": credential_id},
            )
        ).scalar_one_or_none()

    async def revoke_all_in(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        """Owner Recovery: every Passkey of the user ends (Decision 0005, point 7).

        The ``credential_invalidators`` of ``AuthService`` run this in the
        redemption's transaction: the token is spent, the password replaced, the
        sessions ended and the Passkeys revoked together or not at all. The user then
        signs in with the new password into an enrolment-only session (a required
        Passkey and none registered), which is the way back, never a dead end.
        """
        _session(session)
        _uuid("user_id", user_id)
        result = await session.execute(
            text(
                f"""UPDATE user_passkeys
                       SET revoked_at = {_NOW}, revoked_reason = :reason
                     WHERE user_id = :user_id AND revoked_at IS NULL
                 RETURNING id"""
            ),
            {
                "now": self.now(),
                "user_id": user_id,
                "reason": PasskeyRevokeReason.RECOVERY.value,
            },
        )
        revoked = len(result.all())
        await session.execute(
            text("DELETE FROM passkey_challenges WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
        if revoked:
            # Only the count: the credential ids stay out of the logs.
            logger.info("Owner Recovery revoked %d passkey(s)", revoked)

    # -- challenges -------------------------------------------------------------------

    async def issue_challenge_in(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        purpose: PasskeyPurpose,
        challenge: bytes,
    ) -> None:
        """Store the challenge of a ceremony (replacing the session's previous one).

        One row per (session, purpose): asking again replaces the challenge, so the
        answer to the older one no longer works, and a session cannot pile rows up.
        A few long-expired rows of any session are removed as well.
        """
        _session(session)
        _uuid("user_id", user_id)
        _uuid("session_id", session_id)
        if not isinstance(purpose, PasskeyPurpose):
            raise InvalidAuthInputError("purpose")
        _bytes("challenge", challenge, CHALLENGE_BYTES, CHALLENGE_BYTES)
        if self._config is None:
            raise InvalidAuthInputError("config")
        ttl = self._config.challenge_ttl_seconds
        await session.execute(
            text(
                f"""{_CLOCK}
                INSERT INTO passkey_challenges
                    (id, user_id, session_id, purpose, challenge, created_at,
                     expires_at)
                SELECT :id, :user_id, :session_id, :purpose, :challenge, ts,
                       ts + :ttl * interval '1 second'
                  FROM clock
                ON CONFLICT (session_id, purpose) DO UPDATE
                   SET challenge = EXCLUDED.challenge,
                       created_at = EXCLUDED.created_at,
                       expires_at = EXCLUDED.expires_at"""
            ),
            {
                "now": self.now(),
                "id": uuid.uuid4(),
                "user_id": user_id,
                "session_id": session_id,
                "purpose": purpose.value,
                "challenge": challenge,
                "ttl": ttl,
            },
        )
        await session.execute(
            text(
                f"""{_CLOCK}
                DELETE FROM passkey_challenges WHERE id IN (
                    SELECT c.id FROM clock, passkey_challenges c
                     WHERE c.expires_at < clock.ts - :keep * interval '1 second'
                     LIMIT :batch)"""
            ),
            {
                "now": self.now(),
                "keep": PURGE_AFTER_SECONDS,
                "batch": PURGE_BATCH,
            },
        )

    async def consume_challenge_in(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        purpose: PasskeyPurpose,
    ) -> bytes | None:
        """Take (delete) the session's challenge; its bytes if it is still live.

        ``None`` when there is none, it is another user's, or it has expired (the
        database's clock, read after the row lock is held). A challenge is consumed
        whether or not it was live: one attempt each.
        """
        _session(session)
        _uuid("user_id", user_id)
        _uuid("session_id", session_id)
        if not isinstance(purpose, PasskeyPurpose):
            raise InvalidAuthInputError("purpose")
        row = (
            await session.execute(
                text(
                    f"""DELETE FROM passkey_challenges
                         WHERE session_id = :session_id AND user_id = :user_id
                           AND purpose = :purpose
                     RETURNING challenge, expires_at > {_NOW} AS live"""
                ),
                {
                    "now": self.now(),
                    "session_id": session_id,
                    "user_id": user_id,
                    "purpose": purpose.value,
                },
            )
        ).first()
        if row is None or not row.live:
            return None
        return bytes(row.challenge)

    # -- credentials ------------------------------------------------------------------

    async def list_active_in(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> list[PasskeyRecord]:
        _session(session)
        _uuid("user_id", user_id)
        rows = (
            await session.execute(
                text(
                    "SELECT id, name, created_at, last_used_at, backup_eligible, "
                    "backed_up FROM user_passkeys "
                    "WHERE user_id = :user_id AND revoked_at IS NULL "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"user_id": user_id, "limit": LIST_LIMIT},
            )
        ).all()
        return [
            PasskeyRecord(
                id=row.id,
                name=row.name,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
                backup_eligible=row.backup_eligible,
                backed_up=row.backed_up,
            )
            for row in rows
        ]

    async def active_credential_ids_in(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> list[bytes]:
        """The credential ids of the user's active Passkeys (for the options)."""
        _session(session)
        _uuid("user_id", user_id)
        rows = (
            await session.execute(
                text(
                    "SELECT credential_id FROM user_passkeys "
                    "WHERE user_id = :user_id AND revoked_at IS NULL "
                    "ORDER BY created_at, id LIMIT :limit"
                ),
                {"user_id": user_id, "limit": LIST_LIMIT},
            )
        ).all()
        return [bytes(row.credential_id) for row in rows]

    async def stored_in(
        self, session: AsyncSession, user_id: uuid.UUID, credential_id: bytes
    ) -> StoredPasskey | None:
        """The user's active credential with this id (key and counter), or ``None``."""
        _session(session)
        _uuid("user_id", user_id)
        _bytes("credential_id", credential_id, 1, 1023)
        row = (
            await session.execute(
                text(
                    "SELECT id, public_key, sign_count FROM user_passkeys "
                    "WHERE user_id = :user_id AND credential_id = :credential_id "
                    "AND revoked_at IS NULL"
                ),
                {"user_id": user_id, "credential_id": credential_id},
            )
        ).first()
        if row is None:
            return None
        return StoredPasskey(row.id, bytes(row.public_key), row.sign_count)

    async def insert_in(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        name: str,
        registration: VerifiedRegistration,
    ) -> PasskeyRecord | None:
        """Register a verified credential; ``None`` if its credential id exists."""
        _session(session)
        _uuid("user_id", user_id)
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= PASSKEY_NAME_MAX_LENGTH
            or not isinstance(registration, VerifiedRegistration)
        ):
            raise InvalidAuthInputError("passkey")
        row = (
            await session.execute(
                text(
                    f"""{_CLOCK}
                    INSERT INTO user_passkeys (id, user_id, credential_id, public_key,
                        sign_count, name, aaguid, backup_eligible, backed_up,
                        created_at)
                    SELECT :id, :user_id, :credential_id, :public_key, :sign_count,
                           :name, :aaguid, :backup_eligible, :backed_up, ts
                      FROM clock
                    ON CONFLICT (credential_id) DO NOTHING
                    RETURNING id AS id, name AS name, created_at AS created_at,
                              last_used_at AS last_used_at,
                              backup_eligible AS backup_eligible,
                              backed_up AS backed_up"""
                ),
                {
                    "now": self.now(),
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "credential_id": registration.credential_id,
                    "public_key": registration.public_key,
                    "sign_count": registration.sign_count,
                    "name": name,
                    "aaguid": registration.aaguid,
                    "backup_eligible": registration.backup_eligible,
                    "backed_up": registration.backed_up,
                },
            )
        ).first()
        if row is None:
            return None
        return PasskeyRecord(
            id=row.id,
            name=row.name,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            backup_eligible=row.backup_eligible,
            backed_up=row.backed_up,
        )

    async def record_use_in(
        self,
        session: AsyncSession,
        *,
        passkey_id: uuid.UUID,
        user_id: uuid.UUID,
        assertion: VerifiedAssertion,
    ) -> UseOutcome:
        """Store the counter of a verified assertion, if it may be used (see module)."""
        _session(session)
        _uuid("passkey_id", passkey_id)
        _uuid("user_id", user_id)
        if not isinstance(assertion, VerifiedAssertion):
            raise InvalidAuthInputError("assertion")
        params = {
            "now": self.now(),
            "id": passkey_id,
            "user_id": user_id,
            "count": assertion.sign_count,
            "backup_eligible": assertion.backup_eligible,
            "backed_up": assertion.backed_up,
        }
        updated = (
            await session.execute(
                text(
                    f"""UPDATE user_passkeys
                           SET sign_count = :count, last_used_at = {_NOW},
                               backed_up = :backed_up
                         WHERE id = :id AND user_id = :user_id
                           AND revoked_at IS NULL
                           AND backup_eligible = :backup_eligible
                           AND (:count > sign_count
                                OR (:count = 0 AND sign_count = 0))
                     RETURNING id"""
                ),
                params,
            )
        ).first()
        if updated is not None:
            return UseOutcome.USED
        row = (
            await session.execute(
                text(
                    "SELECT backup_eligible, revoked_at FROM user_passkeys "
                    "WHERE id = :id AND user_id = :user_id"
                ),
                {"id": passkey_id, "user_id": user_id},
            )
        ).first()
        if row is None or row.revoked_at is not None:
            return UseOutcome.GONE
        if row.backup_eligible != assertion.backup_eligible:
            return UseOutcome.CHANGED
        return UseOutcome.REGRESSION

    async def lock_active_in(
        self, session: AsyncSession, *, passkey_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """Lock the user's ACTIVE Passkey row (``FOR UPDATE``); ``False`` if none.

        Lock order (it keeps two transactions from waiting for each other): a
        Passkey's row FIRST, then the session rows. A Step-up holds the credential
        ``FOR SHARE`` and then updates its session; a revocation takes the credential
        first too, before it reads or ends any session, so neither can hold what
        the other needs while waiting for the credential.
        """
        _session(session)
        _uuid("passkey_id", passkey_id)
        _uuid("user_id", user_id)
        row = (
            await session.execute(
                text(
                    "SELECT id FROM user_passkeys WHERE id = :id "
                    "AND user_id = :user_id AND revoked_at IS NULL FOR UPDATE"
                ),
                {"id": passkey_id, "user_id": user_id},
            )
        ).first()
        return row is not None

    async def revoke_in(
        self,
        session: AsyncSession,
        *,
        passkey_id: uuid.UUID,
        user_id: uuid.UUID,
        reason: PasskeyRevokeReason,
    ) -> bool:
        """End one of the user's Passkeys; ``False`` if it is not an active one."""
        _session(session)
        _uuid("passkey_id", passkey_id)
        _uuid("user_id", user_id)
        if not isinstance(reason, PasskeyRevokeReason):
            raise InvalidAuthInputError("reason")
        row = (
            await session.execute(
                text(
                    f"""UPDATE user_passkeys
                           SET revoked_at = {_NOW}, revoked_reason = :reason
                         WHERE id = :id AND user_id = :user_id
                           AND revoked_at IS NULL
                     RETURNING id"""
                ),
                {
                    "now": self.now(),
                    "id": passkey_id,
                    "user_id": user_id,
                    "reason": reason.value,
                },
            )
        ).first()
        return row is not None

    async def delete_challenges_of_in(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        """Forget every open challenge of the user (their credentials just changed)."""
        _session(session)
        _uuid("user_id", user_id)
        await session.execute(
            text("DELETE FROM passkey_challenges WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
