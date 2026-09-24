"""Users and one-time setup tokens for the initial Owner setup (PAW-021).

Revision ID: 0021
Revises: 0040
Create Date: 2026-09-24

``users`` is the minimal identity: UUID id, a normalised unique login name, the
system role, the lifecycle status, whether a Passkey is mandatory and the
timestamps. There is NO password hash, session or Passkey here: Login / Session
(PAW-022) and Passkey (PAW-023) add those.

* At most one Owner row can exist (a partial unique index): two concurrent
  initial setups cannot both succeed.
* ``passkey_required`` must be true for an Owner or Admin (CHECK).
* ``login_name`` must already be in normalised form (CHECK, the same pattern as
  ``paw_backend.identity.login_name``).

``setup_tokens`` keeps the one-time setup / recovery tokens as a salted HMAC
only (never the token). A user can have at most one outstanding (unused,
unrevoked) token.

When ``PAW_APP_DATABASE_ROLE`` is set, that role is granted SELECT, INSERT and
UPDATE on both tables (and no DELETE), so that the application and the
server-local CLI can run as the restricted role. The lists of allowed values
are written out here; change them with a new revision.

The ``users`` table is not referenced by the earlier tables (``tasks``,
``memory_versions`` ... keep plain UUID columns); adding those foreign keys is a
later migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.config import Settings

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same expression as ``paw_backend.identity.login_name`` (a migration must
# not follow later changes of the Python code).
LOGIN_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$"


def _quoted_app_role() -> str | None:
    """The application role as a quoted identifier, or ``None`` if none is set.

    Validated by ``Settings`` and quoted by the dialect; never interpolated as
    text. Online, the role must exist (a missing role fails the migration).
    """
    role = Settings().app_database_role
    if role is None:
        return None
    context = op.get_context()
    if not context.as_sql:
        found = op.get_bind().execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        if found.first() is None:
            raise RuntimeError(
                "PAW_APP_DATABASE_ROLE names a PostgreSQL role that does not exist; "
                "create it before running the migration."
            )
    return context.dialect.identifier_preparer.quote_identifier(role)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("login_name", sa.Text(), nullable=False),
        sa.Column("system_role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("passkey_required", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "system_role IN ('owner', 'admin', 'user')",
            name=op.f("ck_users_system_role_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('invited', 'active', 'pending_deletion', 'deleted')",
            name=op.f("ck_users_status_valid"),
        ),
        sa.CheckConstraint(
            f"login_name ~ '{LOGIN_NAME_PATTERN}'",
            name=op.f("ck_users_login_name_normalised"),
        ),
        sa.CheckConstraint(
            "system_role NOT IN ('owner', 'admin') OR passkey_required",
            name=op.f("ck_users_passkey_required_for_privileged"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("login_name", name=op.f("uq_users_login_name")),
    )
    op.create_index(
        "uq_users_single_owner",
        "users",
        ["system_role"],
        unique=True,
        postgresql_where=sa.text("system_role = 'owner'"),
    )

    op.create_table(
        "setup_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('setup', 'recovery')",
            name=op.f("ck_setup_tokens_purpose_valid"),
        ),
        sa.CheckConstraint(
            "octet_length(salt) = 16", name=op.f("ck_setup_tokens_salt_length")
        ),
        sa.CheckConstraint(
            "octet_length(secret_hash) = 32",
            name=op.f("ck_setup_tokens_secret_hash_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_setup_tokens_expires_after_creation"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_setup_tokens_attempts_not_negative")
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL",
            name=op.f("ck_setup_tokens_used_or_revoked"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_setup_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_tokens")),
    )
    op.create_index(
        "uq_setup_tokens_one_outstanding",
        "setup_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )

    app_role = _quoted_app_role()
    if app_role is not None:
        # No DELETE: a user leaves through its status, a token through used_at /
        # revoked_at.
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON users TO {app_role}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON setup_tokens TO {app_role}")


def downgrade() -> None:
    # Dropping a table removes its indexes and grants. The tokens go first
    # (foreign key). Development and test databases only: this destroys users.
    op.drop_table("setup_tokens")
    op.drop_table("users")
