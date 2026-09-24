"""ORM models of ``users`` and ``setup_tokens`` (Alembic revision ``0021``).

``users`` is deliberately minimal: an identity, its system role and status, and
whether a Passkey is mandatory. There is no password hash, no session and no
credential here; Login / Session / Password (PAW-022) and Passkey (PAW-023) add
their own tables and columns.

``setup_tokens`` holds the one-time tokens of the initial Owner setup and of
Owner recovery. A token is stored only as a salted HMAC (``salt`` and
``secret_hash``); the token itself is shown once to the operator and never
stored.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.authz.roles import SystemRole
from paw_backend.db import Base
from paw_backend.identity.login_name import LOGIN_NAME_SQL_PATTERN

SALT_BYTES = 16
HASH_BYTES = 32  # HMAC-SHA256


class UserStatus(StrEnum):
    """The user lifecycle of ``REQUIREMENTS.md`` ("User Lifecycle")."""

    INVITED = "invited"  # created, first registration (credentials) not done
    ACTIVE = "active"
    PENDING_DELETION = "pending_deletion"
    DELETED = "deleted"


class TokenPurpose(StrEnum):
    SETUP = "setup"  # the initial Owner setup
    RECOVERY = "recovery"  # Owner recovery (all Passkeys / devices lost)


# A human user has one of these roles. ``SystemRole.SYSTEM`` is the backend's own
# identity: it has no row and no login.
USER_ROLES = (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER)
# Roles for which a Passkey is mandatory (REQUIREMENTS.md "Passkey Policy").
PASSKEY_REQUIRED_ROLES = (SystemRole.OWNER, SystemRole.ADMIN)


def passkey_required_for(role: SystemRole) -> bool:
    """Whether a Passkey is mandatory for ``role`` (Owner and Admin: yes)."""
    return role in PASSKEY_REQUIRED_ROLES


def utcnow() -> datetime:
    return datetime.now(UTC)


def _in(column: str, values: Iterable[str], name: str) -> CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Normalised (``paw_backend.identity.login_name``) and unique.
    login_name: Mapped[str] = mapped_column(Text)
    system_role: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    # Mandatory for Owner and Admin (a CHECK constraint); optional for a User.
    # PAW-023 enforces it: such a user cannot finish registration or log in
    # without a registered Passkey.
    passkey_required: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("login_name"),
        # At most one Owner, whatever the code does: two concurrent setups
        # cannot both insert one. An ownership transfer must demote the old
        # Owner before it promotes the new one, in one transaction.
        Index(
            "uq_users_single_owner",
            "system_role",
            unique=True,
            postgresql_where=text("system_role = 'owner'"),
        ),
        _in("system_role", [role.value for role in USER_ROLES], "system_role_valid"),
        _in("status", [status.value for status in UserStatus], "status_valid"),
        CheckConstraint(
            f"login_name ~ '{LOGIN_NAME_SQL_PATTERN}'", name="login_name_normalised"
        ),
        CheckConstraint(
            "system_role NOT IN ('owner', 'admin') OR passkey_required",
            name="passkey_required_for_privileged",
        ),
    )


class SetupTokenRow(Base):
    __tablename__ = "setup_tokens"

    # The public part of the token (the token is ``<prefix>.<id>.<secret>``).
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    purpose: Mapped[str] = mapped_column(Text)
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Redemption attempts made with this token id, successful or not. At the
    # configured maximum the token is locked out for good.
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        # A user has at most one outstanding (unused, unrevoked) token.
        Index(
            "uq_setup_tokens_one_outstanding",
            "user_id",
            unique=True,
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
        _in("purpose", [purpose.value for purpose in TokenPurpose], "purpose_valid"),
        CheckConstraint(f"octet_length(salt) = {SALT_BYTES}", name="salt_length"),
        CheckConstraint(
            f"octet_length(secret_hash) = {HASH_BYTES}", name="secret_hash_length"
        ),
        CheckConstraint("expires_at > created_at", name="expires_after_creation"),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        # A token is consumed or revoked, not both.
        CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL", name="used_or_revoked"
        ),
    )
