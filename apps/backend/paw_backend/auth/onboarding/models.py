"""ORM models of invitations, device pairings and user status changes (``0124``).

* ``user_invitations``: the one-time invitation token of an ``invited`` user
  (PAW-024). Stored like the Owner's token (``setup_tokens``): a salted HMAC of
  the secret, never the token; ``id`` is the lookup key (part of the token) and
  ``audit_ref`` the only name of the token in audit events and logs.
* ``device_pairings``: one "add a new device" of a signed-in user: the one-time
  pairing token (QR code / link), and, for an Owner or an Admin, the new device's
  claim while it waits for the explicit approval of a trusted device.
* ``user_status_changes``: the append-only history of ``users.status``. Only the
  ``SECURITY DEFINER`` functions of the migration write it (the web role reads).

The constraint definitions repeat the migration's on purpose (a migration is a
frozen snapshot); ``tests/test_onboarding_migration.py`` fails when they drift.
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
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.auth.limits import DEVICE_LABEL_MAX_LENGTH
from paw_backend.db import Base

SALT_BYTES = 16
HASH_BYTES = 32  # HMAC-SHA256
# The database refuses a token that would live longer than this, whatever the
# setting says (the settings' upper bounds are below it).
INVITATION_MAX_LIFETIME = "interval '14 days'"
PAIRING_MAX_LIFETIME = "interval '1 hour'"

TABLE_NAMES = ("user_invitations", "device_pairings", "user_status_changes")


class InvitationEnd(StrEnum):
    """Why an unused invitation token stopped counting."""

    REVOKED = "revoked"  # an administrator revoked it
    SUPERSEDED = "superseded"  # a new token was issued
    CANCELLED = "cancelled"  # the invitation (the invited user) was cancelled


class PairingState(StrEnum):
    """The state machine of one pairing (Decision 0033, section 2)."""

    ISSUED = "issued"  # a trusted device shows the token (QR code / link)
    CLAIMED = "claimed"  # an Owner's / Admin's new device waits for the approval
    APPROVED = "approved"  # a trusted device approved it; the new device completes
    COMPLETED = "completed"  # the new device has its session
    REJECTED = "rejected"  # a trusted device refused the new device
    REVOKED = "revoked"  # ended by the user, a new pairing or the account's closing


# The states in which a pairing can still lead to a session.
LIVE_PAIRING_STATES = (
    PairingState.ISSUED,
    PairingState.CLAIMED,
    PairingState.APPROVED,
)


class PairingEnd(StrEnum):
    REVOKED_BY_USER = "revoked_by_user"
    SUPERSEDED = "superseded"
    ACCOUNT_CLOSED = "account_closed"
    # The approver entered a wrong confirmation code too many times.
    CONFIRMATION_FAILED = "confirmation_failed"
    # The Owner / an Admin reset the account's Passkeys and password (#108).
    CREDENTIALS_RESET = "credentials_reset"


def _in(column: str, values: Iterable[str], name: str) -> CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({listed})", name=name)


class UserInvitationRow(Base):
    __tablename__ = "user_invitations"

    # The public part of the token (``pawiv1.<id>.<secret>``): a lookup key that
    # must never reach an audit event or a log line.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    audit_ref: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    # The Owner / Admin who issued it (an id only).
    invited_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("audit_ref"),
        # The referential action of ``users`` and the history of one user.
        Index("ix_user_invitations_user_id", "user_id"),
        # A user has at most one outstanding (unused, unrevoked) invitation token.
        Index(
            "uq_user_invitations_one_outstanding",
            "user_id",
            unique=True,
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
        CheckConstraint(f"octet_length(salt) = {SALT_BYTES}", name="salt_length"),
        CheckConstraint(
            f"octet_length(secret_hash) = {HASH_BYTES}", name="secret_hash_length"
        ),
        CheckConstraint(
            f"expires_at > created_at AND "
            f"expires_at <= created_at + {INVITATION_MAX_LIFETIME}",
            name="lifetime_bounded",
        ),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL", name="used_or_revoked"
        ),
        _in(
            "revoked_reason",
            [reason.value for reason in InvitationEnd],
            "revoked_reason_valid",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name="revocation_complete",
        ),
    )


class DevicePairingRow(Base):
    __tablename__ = "device_pairings"

    # The lookup key of the pairing token (``pawpr1.<id>.<secret>``), shown in the
    # QR code / link. Never audited or logged.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    audit_ref: Mapped[uuid.UUID] = mapped_column(Uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    # The trusted device (session) that issued it.
    issued_by_session: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="SET NULL")
    )
    state: Mapped[str] = mapped_column(Text)
    # Whether a trusted device must approve the new device (an Owner or an Admin
    # at the time the token was claimed).
    approval_required: Mapped[bool] = mapped_column(Boolean)
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary)
    # The new device's claim (only while an approval is involved). ``claim_id``
    # is its own lookup key (``pawpc1.<claim_id>.<secret>``), a fresh random one:
    # whoever saw the QR code knows ``id`` but not this one, so cannot spend the
    # claim's attempts. Never audited or logged.
    claim_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claim_salt: Mapped[bytes | None] = mapped_column(LargeBinary)
    claim_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    # The confirmation code of the claim (Decision 0033, point 12): shown on the
    # new device only, entered on the approving device. Generated with the claim
    # (a CSPRNG, independent of the token and the claim), stored as a salted
    # HMAC-SHA256 and compared in constant time; wrong entries are counted.
    # Never audited or logged.
    confirm_salt: Mapped[bytes | None] = mapped_column(LargeBinary)
    confirm_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    confirm_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    # What the new device asked for.
    device_label: Mapped[str | None] = mapped_column(Text)
    remember_me: Mapped[bool | None] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The token's expiry while ``issued``; the approval's once ``claimed``.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_session: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The session the new device received.
    created_session: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("auth_sessions.id", ondelete="SET NULL")
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_reason: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("audit_ref"),
        UniqueConstraint("claim_id"),
        # What the referential actions of ``users`` and ``auth_sessions`` (the
        # session purge sets these to NULL) need to find the referencing rows.
        Index("ix_device_pairings_user_id", "user_id"),
        *(
            Index(
                f"ix_device_pairings_{column}",
                column,
                postgresql_where=text(f"{column} IS NOT NULL"),
            )
            for column in (
                "issued_by_session",
                "decided_by_session",
                "created_session",
            )
        ),
        # "At most one valid add-a-device token per user at a time."
        Index(
            "uq_device_pairings_one_live",
            "user_id",
            unique=True,
            postgresql_where=text("state IN ('issued', 'claimed', 'approved')"),
        ),
        _in("state", [state.value for state in PairingState], "state_valid"),
        CheckConstraint(f"octet_length(salt) = {SALT_BYTES}", name="salt_length"),
        CheckConstraint(
            f"octet_length(secret_hash) = {HASH_BYTES}", name="secret_hash_length"
        ),
        CheckConstraint(
            f"claim_salt IS NULL OR octet_length(claim_salt) = {SALT_BYTES}",
            name="claim_salt_length",
        ),
        CheckConstraint(
            f"claim_hash IS NULL OR octet_length(claim_hash) = {HASH_BYTES}",
            name="claim_hash_length",
        ),
        CheckConstraint(
            "(claim_salt IS NULL) = (claim_hash IS NULL)", name="claim_complete"
        ),
        CheckConstraint(
            "(claim_id IS NULL) = (claim_hash IS NULL)", name="claim_has_id"
        ),
        CheckConstraint(
            f"confirm_salt IS NULL OR octet_length(confirm_salt) = {SALT_BYTES}",
            name="confirm_salt_length",
        ),
        CheckConstraint(
            f"confirm_hash IS NULL OR octet_length(confirm_hash) = {HASH_BYTES}",
            name="confirm_hash_length",
        ),
        CheckConstraint(
            "(confirm_salt IS NULL) = (confirm_hash IS NULL)", name="confirm_complete"
        ),
        CheckConstraint(
            "(claim_hash IS NULL) = (confirm_hash IS NULL)",
            name="claim_has_confirmation",
        ),
        CheckConstraint("confirm_attempts >= 0", name="confirm_attempts_not_negative"),
        CheckConstraint(
            f"device_label IS NULL OR char_length(device_label) "
            f"BETWEEN 1 AND {DEVICE_LABEL_MAX_LENGTH}",
            name="device_label_length",
        ),
        CheckConstraint(
            f"expires_at > created_at AND expires_at <= "
            f"coalesce(claimed_at, created_at) + {PAIRING_MAX_LIFETIME}",
            name="lifetime_bounded",
        ),
        # A claim exists exactly when the token was handed in and an approval was
        # (or is) involved.
        CheckConstraint(
            "state <> 'issued' OR (claimed_at IS NULL AND claim_hash IS NULL)",
            name="issued_is_unclaimed",
        ),
        CheckConstraint(
            "state NOT IN ('claimed', 'approved', 'rejected') "
            "OR (claim_hash IS NOT NULL AND approval_required)",
            name="claim_needs_approval",
        ),
        CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name="decision_recorded",
        ),
        CheckConstraint(
            "(state = 'completed') = (completed_at IS NOT NULL)",
            name="completion_recorded",
        ),
        CheckConstraint(
            "(state = 'revoked') = (ended_reason IS NOT NULL)", name="end_recorded"
        ),
        CheckConstraint(
            "(ended_at IS NULL) = (ended_reason IS NULL)", name="end_complete"
        ),
        _in(
            "ended_reason",
            [reason.value for reason in PairingEnd],
            "ended_reason_valid",
        ),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
    )


class UserStatusChangeRow(Base):
    """One change of ``users.status`` (written by the migration's functions only)."""

    __tablename__ = "user_status_changes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    # RESTRICT: the history is append-only, so a user who has one is never
    # hard-deleted (the users row stays as a tombstone, Decision 0033).
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    # ``NULL`` for the row that created the user (an invitation).
    old_status: Mapped[str | None] = mapped_column(Text)
    new_status: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The person who made the change (``NULL``: the invited user themself, or the
    # system).
    changed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # The database's clock when the row was written (the order of the history;
    # ``changed_at`` is the service's clock, a test seam).
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_user_status_changes_user_id", "user_id", "changed_at"),
        _in(
            "old_status",
            ["invited", "active", "pending_deletion", "deleted"],
            "old_status_valid",
        ),
        _in(
            "new_status",
            ["invited", "active", "pending_deletion", "deleted"],
            "new_status_valid",
        ),
    )
