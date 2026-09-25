"""ORM models of the Passkey tables (Alembic revision ``0023``).

* ``user_passkeys``: the registered WebAuthn credentials, several per user. Only
  what verifying an assertion needs is stored: the credential id, the public key
  (a COSE key, never a private key), the signature counter and a few facts about
  the authenticator. A revoked credential keeps its row (``revoked_at``,
  ``revoked_reason``) so that the audit trail's ids still resolve and a credential
  id can never be registered again by someone else.
* ``passkey_challenges``: the server-side challenge of a ceremony in progress, one
  per (session, purpose), single use, short lived. A challenge is bound to the
  session that asked for it, so another session (a thief's, the same user's) can
  neither answer it nor overwrite it.

The constraint definitions repeat the migration's on purpose (a migration is a
frozen snapshot); ``tests/test_passkey_migration.py`` fails when the two drift.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base

CHALLENGE_BYTES = 32
CREDENTIAL_ID_MIN_BYTES = 16
CREDENTIAL_ID_MAX_BYTES = 1023  # WebAuthn: at most 1023 bytes
PUBLIC_KEY_MIN_BYTES = 16
PUBLIC_KEY_MAX_BYTES = 2048
SIGN_COUNT_MAX = 4_294_967_295  # a 32-bit unsigned counter
PASSKEY_NAME_MAX_LENGTH = 64
# The most a user may have registered (active) at once.
MAX_PASSKEYS_PER_USER = 10


class PasskeyRevokeReason(StrEnum):
    """Why a Passkey ended (an enum value, safe to store)."""

    REVOKED_BY_USER = "revoked_by_user"
    RECOVERY = "recovery"  # Owner Recovery invalidates every Passkey (Decision 0005)


class PasskeyPurpose(StrEnum):
    """What a stored challenge was issued for."""

    REGISTER = "register"
    AUTHENTICATE = "authenticate"


def _in(column: str, values: Iterable[str], name: str) -> CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class UserPasskeyRow(Base):
    __tablename__ = "user_passkeys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    # Raw credential id. Unique across every user: one credential, one account.
    credential_id: Mapped[bytes] = mapped_column(LargeBinary)
    # The COSE_Key of the credential's public key.
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(Text)
    aaguid: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    backup_eligible: Mapped[bool] = mapped_column(Boolean)
    backed_up: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("credential_id"),
        Index(
            "ix_user_passkeys_user_id_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        CheckConstraint(
            f"octet_length(credential_id) BETWEEN {CREDENTIAL_ID_MIN_BYTES} "
            f"AND {CREDENTIAL_ID_MAX_BYTES}",
            name="credential_id_length",
        ),
        CheckConstraint(
            f"octet_length(public_key) BETWEEN {PUBLIC_KEY_MIN_BYTES} "
            f"AND {PUBLIC_KEY_MAX_BYTES}",
            name="public_key_length",
        ),
        CheckConstraint(
            f"sign_count BETWEEN 0 AND {SIGN_COUNT_MAX}", name="sign_count_range"
        ),
        CheckConstraint(
            f"char_length(name) BETWEEN 1 AND {PASSKEY_NAME_MAX_LENGTH}",
            name="name_length",
        ),
        CheckConstraint("NOT backed_up OR backup_eligible", name="backup_state_valid"),
        _in(
            "revoked_reason",
            [reason.value for reason in PasskeyRevokeReason],
            "revoked_reason_valid",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="revocation_complete",
        ),
    )


class PasskeyChallengeRow(Base):
    __tablename__ = "passkey_challenges"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(Text)
    challenge: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("session_id", "purpose"),
        Index("ix_passkey_challenges_expires_at", "expires_at"),
        _in(
            "purpose",
            [purpose.value for purpose in PasskeyPurpose],
            "purpose_valid",
        ),
        CheckConstraint(
            f"octet_length(challenge) = {CHALLENGE_BYTES}", name="challenge_length"
        ),
        CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '1 hour'",
            name="short_lived",
        ),
    )
