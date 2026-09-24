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
unrevoked) token. ``id`` is the token's public lookup key; ``audit_ref`` is the
unrelated identifier that audit events and logs use. ``locked_at`` records a
lockout, so that changing the attempt limit later cannot reopen a token.
``issued_by_uid`` / ``issued_by_sudo_uid`` are the numeric ids of whoever ran the
issuing command.

Two database roles, split like the audit table's (see ``PAW_APP_DATABASE_ROLE``
and ``PAW_MIGRATION_DATABASE_URL`` in the README). Whoever can INSERT a token row
can mint a token for the Owner, so the web application's role may not:

* ``PAW_APP_DATABASE_ROLE`` (the web application; granted through
  ``paw_backend.db_roles.grant_app_privileges`` like every table): SELECT on both
  tables, UPDATE
  of ``users.updated_at`` (PostgreSQL wants an UPDATE privilege for the row lock
  ``SELECT ... FOR UPDATE`` that redeeming takes) and of ``attempts``,
  ``used_at``, ``locked_at`` of ``setup_tokens``. No INSERT, no DELETE, and no
  UPDATE of a role, a salt, a hash, an expiry or ``revoked_at``.
* ``PAW_OPERATOR_DATABASE_ROLE`` (the server-local commands; a different role,
  so granted here, validated with the same ``validate_role_name``): SELECT and INSERT
  on both tables, UPDATE of ``users.system_role`` / ``updated_at`` and of
  ``setup_tokens.revoked_at``, and INSERT on ``audit_events`` (their audit
  events), which is granted here only if that table exists.

A trigger also makes ``setup_tokens`` rows immutable except for what a token's
life needs: ``used_at``, ``revoked_at`` and ``locked_at`` can only be set once
and ``attempts`` can only grow, for every role.

What this does NOT guard: a role that owns the tables (the migration role) or is
a superuser can do anything. The web role can still burn the outstanding token
(set ``used_at``) or its attempts, which locks the Owner out until the operator
runs recovery: an availability problem, not a takeover. When PAW-022 / PAW-023
add password and Passkey tables the application must be able to write them, so a
compromised application can still change the Owner's credentials there.

The lists of allowed values are written out here; change them with a new
revision. The ``users`` table is not referenced by the earlier tables (``tasks``,
``memory_versions`` ... keep plain UUID columns); adding those foreign keys is a
later migration.

``downgrade()`` drops ``setup_tokens`` and ``users``: it DESTROYS EVERY USER,
INCLUDING THE OWNER, and every token. Development and test databases only.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.config import Settings
from paw_backend.db_roles import (
    configured_app_role,
    grant_app_privileges,
    validate_role_name,
)

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The same expression as ``paw_backend.identity.login_name`` (a migration must
# not follow later changes of the Python code).
LOGIN_NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$"


logger = logging.getLogger("paw_backend.migrations.0021")

_GUARD_FUNCTION = """
CREATE FUNCTION paw_guard_setup_tokens_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.audit_ref IS DISTINCT FROM OLD.audit_ref
       OR NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.purpose IS DISTINCT FROM OLD.purpose
       OR NEW.salt IS DISTINCT FROM OLD.salt
       OR NEW.secret_hash IS DISTINCT FROM OLD.secret_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.issued_by_uid IS DISTINCT FROM OLD.issued_by_uid
       OR NEW.issued_by_sudo_uid IS DISTINCT FROM OLD.issued_by_sudo_uid
       OR (OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at)
       OR (OLD.revoked_at IS NOT NULL
           AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
       OR (OLD.locked_at IS NOT NULL
           AND NEW.locked_at IS DISTINCT FROM OLD.locked_at)
       OR NEW.attempts < OLD.attempts THEN
        RAISE EXCEPTION 'setup_tokens: this change is not allowed'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _quoted_operator_role(role: str | None) -> str | None:
    """The operator role as a quoted identifier, or ``None`` if none is set.

    Validated with the same rules as the application role
    (``paw_backend.db_roles.validate_role_name``) and quoted by the dialect like
    ``grant_app_privileges`` does; never interpolated as text. Online, the role
    must exist (a missing role fails the migration before anything is granted).
    """
    if role is None:
        return None
    role = validate_role_name(role)
    context = op.get_context()
    if not context.as_sql:
        found = op.get_bind().execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        if found.first() is None:
            raise RuntimeError(
                "PAW_OPERATOR_DATABASE_ROLE names a PostgreSQL role that does not "
                "exist; create it before running the migration."
            )
    return context.dialect.identifier_preparer.quote_identifier(role)


def _grant_operator_privileges() -> None:
    """The privileges of the server-local commands (a different role)."""
    settings = Settings()
    operator_role = _quoted_operator_role(settings.operator_database_role)
    if operator_role is not None:
        op.execute(f"GRANT SELECT, INSERT ON users, setup_tokens TO {operator_role}")
        op.execute(
            f"GRANT UPDATE (system_role, updated_at) ON users TO {operator_role}"
        )
        op.execute(f"GRANT UPDATE (revoked_at) ON setup_tokens TO {operator_role}")
        # The audit table belongs to another revision, whose ``REVOKE ALL ... FROM
        # PUBLIC`` does not touch a grant to a named role: grant only if it exists.
        op.execute(
            "DO $$ BEGIN IF to_regclass('audit_events') IS NOT NULL THEN "
            f"GRANT INSERT ON audit_events TO {operator_role}; END IF; END $$"
        )
    elif settings.migration_database_url is not None:
        logger.warning(
            "PAW_MIGRATION_DATABASE_URL is set but PAW_OPERATOR_DATABASE_ROLE is "
            "not: no role may create Owner tokens except the schema owner, so the "
            "server-local Owner commands must run as that role "
            "(PAW_OPERATOR_DATABASE_URL)."
        )
    if operator_role is not None and settings.operator_database_role == (
        configured_app_role()
    ):
        logger.warning(
            "PAW_APP_DATABASE_ROLE and PAW_OPERATOR_DATABASE_ROLE are the same "
            "role: the web application can create Owner tokens. Use two roles."
        )


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
    # The web application reads users. The only UPDATE it gets is ``updated_at``:
    # PostgreSQL wants an UPDATE privilege for the row lock (SELECT ... FOR UPDATE)
    # that redeeming takes. No INSERT, and no UPDATE of a role or a status.
    grant_app_privileges(op, "users", select=True, update_columns=("updated_at",))

    op.create_table(
        "setup_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("audit_ref", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("issued_by_uid", sa.BigInteger(), nullable=True),
        sa.Column("issued_by_sudo_uid", sa.BigInteger(), nullable=True),
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
        sa.CheckConstraint(
            "issued_by_uid BETWEEN 0 AND 4294967295",
            name=op.f("ck_setup_tokens_issued_by_uid_range"),
        ),
        sa.CheckConstraint(
            "issued_by_sudo_uid BETWEEN 0 AND 4294967295",
            name=op.f("ck_setup_tokens_issued_by_sudo_uid_range"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_setup_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_tokens")),
        sa.UniqueConstraint("audit_ref", name=op.f("uq_setup_tokens_audit_ref")),
    )
    op.create_index(
        "uq_setup_tokens_one_outstanding",
        "setup_tokens",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )

    # The web application reads tokens and writes only what redeeming writes.
    # No INSERT: whoever can insert a token row can mint a token for the Owner.
    grant_app_privileges(
        op,
        "setup_tokens",
        select=True,
        update_columns=("attempts", "used_at", "locked_at"),
    )

    op.execute(_GUARD_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_setup_tokens_guard_update BEFORE UPDATE ON setup_tokens "
        "FOR EACH ROW EXECUTE FUNCTION paw_guard_setup_tokens_update()"
    )
    op.execute(
        "ALTER TABLE setup_tokens ENABLE ALWAYS TRIGGER tr_setup_tokens_guard_update"
    )

    _grant_operator_privileges()


def downgrade() -> None:
    # DESTROYS EVERY USER, INCLUDING THE OWNER, and every token (see the module
    # docstring). Dropping a table removes its indexes, trigger and grants; the
    # tokens go first (foreign key). Development and test databases only.
    op.drop_table("setup_tokens")
    op.drop_table("users")
    op.execute("DROP FUNCTION paw_guard_setup_tokens_update()")
