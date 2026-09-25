"""Passkeys (WebAuthn), their challenges and the session's Passkey gate (PAW-023).

Revision ID: 0023
Revises: 0030
Create Date: 2026-09-26

Tables (every column and constraint is spelled out below; the models in
``paw_backend.auth.passkeys.models`` and ``paw_backend.auth.models`` repeat them and
``tests/test_passkey_migration.py`` fails when the two drift apart):

* ``user_passkeys``: the registered WebAuthn credentials, several per user (the
  credential id, the public key, the signature counter, a few facts about the
  authenticator). Never a private key. A revoked credential keeps its row.
* ``passkey_challenges``: the server-side challenge of a ceremony in progress, one
  per (session, purpose), single use, short lived (a CHECK caps the lifetime at an
  hour; the application uses minutes and the database's clock).

Changes to ``auth_sessions`` (revision ``0022``):

* ``passkey_gate`` (``open`` | ``enrollment_required`` | ``assertion_required``):
  what the session may do until the Passkey the policy asks for is dealt with
  (Decision 0025). Existing rows become ``open``: a policy applies to new sessions
  only (Decision 0015 section 12), so deploying this never signs anyone out or
  restricts a running session.
* ``passkey_id``: the Passkey that opened the gate. Revoking it ends the session.
* ``revoked_reason`` accepts ``passkey_revoked``.

Privileges of the web application's role (``PAW_APP_DATABASE_ROLE``), the least
each service executes (``paw_backend.db_roles.grant_app_privileges``); the exact
sets are pinned by ``tests/test_passkey_grants.py``:

* ``user_passkeys``: SELECT, INSERT, UPDATE of the counter, ``last_used_at``,
  ``backed_up`` and the revocation columns. No DELETE (a credential is revoked,
  never removed), and not of ``credential_id``, ``public_key`` or ``user_id``: a
  registered credential cannot be re-keyed or handed to another user.
* ``passkey_challenges``: SELECT, INSERT, DELETE (a challenge is consumed by
  deleting it), UPDATE of the three columns a replacement of the challenge of the
  same (session, purpose) changes.
* ``auth_sessions``: UPDATE of ``passkey_gate`` and ``passkey_id`` is added.

What this does NOT guard: the web application writes ``user_passkeys`` and the
session's gate (it has to), so a compromised application can register a Passkey of
its own or open a session's gate, just as it can change a password (Decision 0005
accepted this for the password; Decision 0025 says the same for the Passkey).

``downgrade()`` drops both tables and the two columns: it DESTROYS EVERY REGISTERED
PASSKEY. Development and test databases only. Sessions that ended with
``passkey_revoked`` are relabelled ``admin`` so that the older constraint holds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import configured_app_role, grant_app_privileges

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values are written out here and not imported: a migration is a snapshot.
OLD_REVOKE_REASONS = (
    "'logout', 'revoked_by_user', 'logout_others', 'password_changed', "
    "'password_reset', 'recovery', 'account_closed', 'admin', 'replaced'"
)
NEW_REVOKE_REASONS = OLD_REVOKE_REASONS + ", 'passkey_revoked'"
PASSKEY_REVOKE_REASONS = "'revoked_by_user', 'recovery'"
PURPOSES = "'register', 'authenticate'"
GATES = "'open', 'enrollment_required', 'assertion_required'"


def upgrade() -> None:
    op.create_table(
        "user_passkeys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.LargeBinary(), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("aaguid", sa.Uuid(), nullable=True),
        sa.Column("backup_eligible", sa.Boolean(), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "octet_length(credential_id) BETWEEN 16 AND 1023",
            name=op.f("ck_user_passkeys_credential_id_length"),
        ),
        sa.CheckConstraint(
            "octet_length(public_key) BETWEEN 16 AND 2048",
            name=op.f("ck_user_passkeys_public_key_length"),
        ),
        sa.CheckConstraint(
            "sign_count BETWEEN 0 AND 4294967295",
            name=op.f("ck_user_passkeys_sign_count_range"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 64",
            name=op.f("ck_user_passkeys_name_length"),
        ),
        sa.CheckConstraint(
            "NOT backed_up OR backup_eligible",
            name=op.f("ck_user_passkeys_backup_state_valid"),
        ),
        sa.CheckConstraint(
            f"revoked_reason IN ({PASSKEY_REVOKE_REASONS})",
            name=op.f("ck_user_passkeys_revoked_reason_valid"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name=op.f("ck_user_passkeys_revocation_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_passkeys_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_passkeys")),
        sa.UniqueConstraint(
            "credential_id", name=op.f("uq_user_passkeys_credential_id")
        ),
    )
    op.create_index(
        "ix_user_passkeys_user_id_active",
        "user_passkeys",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    grant_app_privileges(
        op,
        "user_passkeys",
        select=True,
        insert=True,
        update_columns=(
            "sign_count",
            "last_used_at",
            "backed_up",
            "revoked_at",
            "revoked_reason",
        ),
    )

    op.create_table(
        "passkey_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("challenge", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"purpose IN ({PURPOSES})",
            name=op.f("ck_passkey_challenges_purpose_valid"),
        ),
        sa.CheckConstraint(
            "octet_length(challenge) = 32",
            name=op.f("ck_passkey_challenges_challenge_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '1 hour'",
            name=op.f("ck_passkey_challenges_short_lived"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_passkey_challenges_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_passkey_challenges_session_id_auth_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_passkey_challenges")),
        sa.UniqueConstraint(
            "session_id",
            "purpose",
            name=op.f("uq_passkey_challenges_session_id"),
        ),
    )
    op.create_index(
        "ix_passkey_challenges_expires_at", "passkey_challenges", ["expires_at"]
    )
    grant_app_privileges(
        op,
        "passkey_challenges",
        select=True,
        insert=True,
        delete=True,
        update_columns=("challenge", "created_at", "expires_at"),
    )

    # The session's gate. NOT NULL with a default: the sessions that exist now
    # predate the feature and stay unrestricted (Decision 0015: a policy applies to
    # new sessions). The application always states the gate when it creates one.
    op.add_column(
        "auth_sessions",
        sa.Column(
            "passkey_gate",
            sa.Text(),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
    )
    op.add_column("auth_sessions", sa.Column("passkey_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_auth_sessions_passkey_id_user_passkeys"),
        "auth_sessions",
        "user_passkeys",
        ["passkey_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_passkey_gate_valid"),
        "auth_sessions",
        f"passkey_gate IN ({GATES})",
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_passkey_binds_open_session"),
        "auth_sessions",
        "passkey_id IS NULL OR passkey_gate = 'open'",
    )
    op.create_index(
        "ix_auth_sessions_passkey_id_active",
        "auth_sessions",
        ["passkey_id"],
        postgresql_where=sa.text("passkey_id IS NOT NULL AND revoked_at IS NULL"),
    )
    op.drop_constraint(
        op.f("ck_auth_sessions_revoked_reason_valid"), "auth_sessions", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_revoked_reason_valid"),
        "auth_sessions",
        f"revoked_reason IN ({NEW_REVOKE_REASONS})",
    )
    _grant_session_columns()


def _grant_session_columns() -> None:
    """UPDATE of the two new session columns, for the web role only."""
    role = configured_app_role()
    if role is None:
        return
    quoted = op.get_context().dialect.identifier_preparer.quote_identifier(role)
    op.execute(f"GRANT UPDATE (passkey_gate, passkey_id) ON auth_sessions TO {quoted}")


def downgrade() -> None:
    # DESTROYS EVERY REGISTERED PASSKEY (see the module docstring). Dropping a
    # table removes its indexes, constraints and grants; dropping a column removes
    # the constraints and the index that name it.
    op.drop_table("passkey_challenges")
    op.drop_index("ix_auth_sessions_passkey_id_active", table_name="auth_sessions")
    op.drop_constraint(
        op.f("ck_auth_sessions_revoked_reason_valid"), "auth_sessions", type_="check"
    )
    op.execute(
        "UPDATE auth_sessions SET revoked_reason = 'admin' "
        "WHERE revoked_reason = 'passkey_revoked'"
    )
    op.create_check_constraint(
        op.f("ck_auth_sessions_revoked_reason_valid"),
        "auth_sessions",
        f"revoked_reason IN ({OLD_REVOKE_REASONS})",
    )
    op.drop_column("auth_sessions", "passkey_id")
    op.drop_column("auth_sessions", "passkey_gate")
    op.drop_table("user_passkeys")
