"""ORM models of the login / session / password tables (Alembic revision ``0022``).

* ``password_credentials``: one row per user, the Argon2id encoding of the
  password (never the password). A row per *kind* of credential would be the
  shape of PAW-023's Passkeys too, but a Passkey has several rows per user and
  other columns, so it gets a table of its own; nothing here needs to change.
* ``auth_sessions``: server-side sessions. Only the SHA-256 of the session id
  is stored; the id itself exists in the user's cookie and nowhere else.
* ``auth_throttles``: the progressive backoff of failed logins and the rate
  limits of the token endpoint, one row per (scope, hashed key).
* ``auth_policy`` / ``auth_policy_changes``: the Owner-controlled workspace
  policy (Passkey requirement per role, Step-up window) and its append-only
  history.

The constraint definitions repeat the migration's on purpose (a migration is a
frozen snapshot); ``tests/test_auth_migration.py`` fails when the two drift.
"""

import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.auth.limits import DEVICE_LABEL_MAX_LENGTH
from paw_backend.db import Base

TOKEN_HASH_BYTES = 32  # SHA-256


class AuthMethod(StrEnum):
    """How a session was authenticated. ``PASSKEY`` is PAW-023's (unused here)."""

    PASSWORD = "password"
    PASSKEY = "passkey"


class RevokeReason(StrEnum):
    """Why a session ended before it expired (an enum value, safe to store)."""

    LOGOUT = "logout"
    REVOKED_BY_USER = "revoked_by_user"  # another device, chosen in the device list
    LOGOUT_OTHERS = "logout_others"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET = "password_reset"
    RECOVERY = "recovery"
    ACCOUNT_CLOSED = "account_closed"  # pending deletion / deleted
    ADMIN = "admin"
    REPLACED = "replaced"  # a new login on a browser that still had a session


class ThrottleScope(StrEnum):
    LOGIN_ACCOUNT = "login_account"
    LOGIN_SOURCE = "login_source"
    REDEEM_SOURCE = "redeem_source"
    REDEEM_GLOBAL = "redeem_global"


class PasskeyRequirement(StrEnum):
    """The Owner-controlled requirement of a Passkey for a role."""

    REQUIRED = "required"
    OPTIONAL = "optional"


def _in(column: str, values: Iterable[str], name: str) -> CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class PasswordCredentialRow(Base):
    __tablename__ = "password_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # ``$argon2id$v=19$m=...,t=...,p=...$<salt>$<tag>``.
    hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("hash LIKE '$argon2id$%'", name="hash_is_argon2id"),
        CheckConstraint("changed_at >= created_at", name="changed_after_creation"),
    )


class AuthSessionRow(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    # SHA-256 of the session id. Unique: the cookie is looked up by it.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    remember_me: Mapped[bool] = mapped_column(Boolean)
    auth_method: Mapped[str] = mapped_column(Text)
    device_label: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_timeout_seconds: Mapped[int] = mapped_column(Integer)
    # ``last_used_at + idle_timeout_seconds``, capped at ``absolute_expires_at``.
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The last step-up authentication of this session (PAW-023 adds Passkeys).
    stepup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stepup_method: Mapped[str | None] = mapped_column(Text)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("token_hash"),
        Index(
            "ix_auth_sessions_user_id_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # The purge finds what went idle long ago (a CHECK constraint keeps the
        # idle expiry at or before the absolute one, so this also covers a session
        # that reached its absolute limit).
        Index("ix_auth_sessions_idle_expires_at", "idle_expires_at"),
        Index(
            "ix_auth_sessions_revoked_at",
            "revoked_at",
            postgresql_where=text("revoked_at IS NOT NULL"),
        ),
        _in("auth_method", [m.value for m in AuthMethod], "auth_method_valid"),
        _in(
            "stepup_method",
            [m.value for m in AuthMethod],
            "stepup_method_valid",
        ),
        _in(
            "revoked_reason",
            [reason.value for reason in RevokeReason],
            "revoked_reason_valid",
        ),
        CheckConstraint(
            f"octet_length(token_hash) = {TOKEN_HASH_BYTES}", name="token_hash_length"
        ),
        CheckConstraint(
            f"device_label IS NULL OR char_length(device_label) "
            f"BETWEEN 1 AND {DEVICE_LABEL_MAX_LENGTH}",
            name="device_label_length",
        ),
        CheckConstraint("idle_timeout_seconds > 0", name="idle_timeout_positive"),
        CheckConstraint(
            "absolute_expires_at > created_at", name="expires_after_creation"
        ),
        CheckConstraint(
            "idle_expires_at <= absolute_expires_at", name="idle_within_absolute"
        ),
        CheckConstraint(
            "(stepup_at IS NULL) = (stepup_method IS NULL)",
            name="stepup_complete",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="revocation_complete",
        ),
    )


class AuthThrottleRow(Base):
    __tablename__ = "auth_throttles"

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    # SHA-256 of a domain-separated key (a login name, a client address bucket,
    # or a constant for the global scope): never the key itself.
    key_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    # Attempts counted at reservation, before the outcome is known (see
    # ``paw_backend.auth.throttle``): a crash between the two can only leave an
    # attempt counted, never forgotten.
    attempts: Mapped[int] = mapped_column(Integer)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        _in("scope", [scope.value for scope in ThrottleScope], "scope_valid"),
        CheckConstraint(
            f"octet_length(key_hash) = {TOKEN_HASH_BYTES}", name="key_hash_length"
        ),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        Index("ix_auth_throttles_last_attempt_at", "last_attempt_at"),
    )


class AuthPolicyRow(Base):
    """The single row of the workspace authentication policy."""

    __tablename__ = "auth_policy"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    passkey_owner: Mapped[str] = mapped_column(Text)
    passkey_admin: Mapped[str] = mapped_column(Text)
    passkey_user: Mapped[str] = mapped_column(Text)
    recommend_passkey_to_users: Mapped[bool] = mapped_column(Boolean)
    stepup_window_minutes: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # NULL for the row the migration seeds (the requirements' default).
    updated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("version >= 1", name="version_positive"),
        _in(
            "passkey_owner",
            [r.value for r in PasskeyRequirement],
            "passkey_owner_valid",
        ),
        _in(
            "passkey_admin",
            [r.value for r in PasskeyRequirement],
            "passkey_admin_valid",
        ),
        _in(
            "passkey_user",
            [r.value for r in PasskeyRequirement],
            "passkey_user_valid",
        ),
        CheckConstraint(
            "stepup_window_minutes BETWEEN 5 AND 240", name="stepup_window_range"
        ),
    )


class AuthPolicyChangeRow(Base):
    """One change of the policy: who, when, and every field before and after."""

    __tablename__ = "auth_policy_changes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    # The version the policy has AFTER this change.
    version: Mapped[int] = mapped_column(Integer)
    changed_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    old_passkey_owner: Mapped[str] = mapped_column(Text)
    new_passkey_owner: Mapped[str] = mapped_column(Text)
    old_passkey_admin: Mapped[str] = mapped_column(Text)
    new_passkey_admin: Mapped[str] = mapped_column(Text)
    old_passkey_user: Mapped[str] = mapped_column(Text)
    new_passkey_user: Mapped[str] = mapped_column(Text)
    old_recommend_passkey_to_users: Mapped[bool] = mapped_column(Boolean)
    new_recommend_passkey_to_users: Mapped[bool] = mapped_column(Boolean)
    old_stepup_window_minutes: Mapped[int] = mapped_column(Integer)
    new_stepup_window_minutes: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint("version"),
        CheckConstraint("version >= 2", name="version_after_seed"),
        *(
            _in(
                f"{prefix}_passkey_{role}",
                [r.value for r in PasskeyRequirement],
                f"{prefix}_passkey_{role}_valid",
            )
            for prefix in ("old", "new")
            for role in ("owner", "admin", "user")
        ),
    )
