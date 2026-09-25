"""Password credentials, server-side sessions, login throttles, auth policy (PAW-022).

Revision ID: 0022
Revises: 0026
Create Date: 2026-09-25

Tables (every column and constraint is spelled out below; the models in
``paw_backend.auth.models`` repeat them and ``tests/test_auth_migration.py``
fails when the two drift apart):

* ``password_credentials``: one row per user, the Argon2id encoding of the
  password. Never the password.
* ``auth_sessions``: server-side sessions. Only the SHA-256 of the session id is
  stored. Expiry (idle and absolute) and revocation are columns, and every
  validity decision is made against the database's clock.
* ``auth_throttles``: the progressive login backoff (per hashed login name and
  per source) and the rate limits of the Owner token endpoint (per source and
  global). One row per (scope, hashed key); the keys are hashes, so the table
  holds neither a login name nor an address.
* ``auth_policy``: the single row of the workspace authentication policy (the
  Passkey requirement per role, whether Users are urged to register one, the
  Step-up window). The requirements' policy is the seeded default; the Owner may
  change it (Decision 0015). ``auth_policy_changes``: every change (who, when,
  each field before and after), append-only.

Privileges of the web application's role (``PAW_APP_DATABASE_ROLE``), the least
each service executes (``paw_backend.db_roles.grant_app_privileges``); the exact
sets are pinned by ``tests/test_auth_grants.py``:

* ``password_credentials``: SELECT, INSERT, UPDATE of ``hash`` and ``changed_at``.
  No DELETE (a password is replaced, never removed).
* ``auth_sessions``: SELECT, INSERT, DELETE (purging old rows), UPDATE of the
  columns a session's life changes. Not of ``user_id``, ``created_at`` or the
  expiry limits.
* ``auth_throttles``: SELECT, INSERT, DELETE (purging), UPDATE of the counters.
* ``auth_policy``: SELECT and UPDATE of the policy's own columns. No INSERT (the
  row is seeded here) and no DELETE. A trigger makes every change move the
  version up by exactly one, so two writers cannot both apply the same version.
* ``auth_policy_changes``: SELECT and INSERT. A trigger rejects UPDATE and DELETE.
* ``users`` (``0021``): unchanged. The one thing the web application must do to
  it, mark an ``invited`` user ``active`` once a password is set, goes through
  ``paw_activate_invited_user`` below (EXECUTE only), not through an UPDATE
  privilege on ``users.status``: a compromised application cannot use it to
  reactivate a deleted user or to delete an active one.

``paw_activate_invited_user`` is ``SECURITY DEFINER`` with a pinned
``search_path`` (``pg_catalog, pg_temp``) and names ``users`` by the schema this
migration ran in, so that a temporary table cannot stand in for it. It changes
one thing: ``status`` from ``invited`` to ``active`` (and ``updated_at``).

What this does NOT guard: the web application can write ``password_credentials``
(it has to, to set a password), so a compromised application can change a
password. Decision 0005 accepted this; PAW-023's Step-up is the answer.

``downgrade()`` drops the five tables and the functions: it DESTROYS EVERY
PASSWORD, SESSION AND POLICY CHANGE. Development and test databases only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import configured_app_role, grant_app_privileges

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values are written out here and not imported: a migration is a snapshot.
AUTH_METHODS = "'password', 'passkey'"
REVOKE_REASONS = (
    "'logout', 'revoked_by_user', 'logout_others', 'password_changed', "
    "'password_reset', 'recovery', 'account_closed', 'admin', 'replaced'"
)
THROTTLE_SCOPES = "'login_account', 'login_source', 'redeem_source', 'redeem_global'"
REQUIREMENTS = "'required', 'optional'"

_GUARD_POLICY_FUNCTION = """\
CREATE FUNCTION paw_guard_auth_policy_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.version IS DISTINCT FROM OLD.version + 1 THEN
        RAISE EXCEPTION 'auth_policy: a change must move the version up by one'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""

_REJECT_HISTORY_CHANGE_FUNCTION = """\
CREATE FUNCTION paw_reject_auth_policy_changes_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'auth_policy_changes is append-only'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

_ACTIVATE_FUNCTION = """\
CREATE FUNCTION paw_activate_invited_user(p_user_id uuid, p_now timestamptz)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    UPDATE {schema}.users
       SET status = 'active', updated_at = p_now
     WHERE id = p_user_id AND status = 'invited';
    RETURN FOUND;
END;
$$
"""


def _schema_of_this_migration() -> str:
    """The quoted schema the tables are created in (``public`` when rendering SQL)."""
    context = op.get_context()
    if context.as_sql:
        return "public"
    name = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
    return context.dialect.identifier_preparer.quote_identifier(name)


def _grant_execute_to_the_app_role(signature: str) -> None:
    op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    role = configured_app_role()
    if role is not None:
        quoted = op.get_context().dialect.identifier_preparer.quote_identifier(role)
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {quoted}")


def upgrade() -> None:
    op.create_table(
        "password_credentials",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "hash LIKE '$argon2id$%'",
            name=op.f("ck_password_credentials_hash_is_argon2id"),
        ),
        sa.CheckConstraint(
            "changed_at >= created_at",
            name=op.f("ck_password_credentials_changed_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_credentials_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_password_credentials")),
    )
    grant_app_privileges(
        op,
        "password_credentials",
        select=True,
        insert=True,
        update_columns=("hash", "changed_at"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("remember_me", sa.Boolean(), nullable=False),
        sa.Column("auth_method", sa.Text(), nullable=False),
        sa.Column("device_label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stepup_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stepup_method", sa.Text(), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"auth_method IN ({AUTH_METHODS})",
            name=op.f("ck_auth_sessions_auth_method_valid"),
        ),
        sa.CheckConstraint(
            f"stepup_method IN ({AUTH_METHODS})",
            name=op.f("ck_auth_sessions_stepup_method_valid"),
        ),
        sa.CheckConstraint(
            f"revoked_reason IN ({REVOKE_REASONS})",
            name=op.f("ck_auth_sessions_revoked_reason_valid"),
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name=op.f("ck_auth_sessions_token_hash_length"),
        ),
        sa.CheckConstraint(
            "device_label IS NULL OR char_length(device_label) BETWEEN 1 AND 64",
            name=op.f("ck_auth_sessions_device_label_length"),
        ),
        sa.CheckConstraint(
            "idle_timeout_seconds > 0",
            name=op.f("ck_auth_sessions_idle_timeout_positive"),
        ),
        sa.CheckConstraint(
            "absolute_expires_at > created_at",
            name=op.f("ck_auth_sessions_expires_after_creation"),
        ),
        sa.CheckConstraint(
            "idle_expires_at <= absolute_expires_at",
            name=op.f("ck_auth_sessions_idle_within_absolute"),
        ),
        sa.CheckConstraint(
            "(stepup_at IS NULL) = (stepup_method IS NULL)",
            name=op.f("ck_auth_sessions_stepup_complete"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name=op.f("ck_auth_sessions_revocation_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_sessions_token_hash")),
    )
    op.create_index(
        "ix_auth_sessions_user_id_active",
        "auth_sessions",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "ix_auth_sessions_idle_expires_at",
        "auth_sessions",
        ["idle_expires_at"],
    )
    op.create_index(
        "ix_auth_sessions_revoked_at",
        "auth_sessions",
        ["revoked_at"],
        postgresql_where=sa.text("revoked_at IS NOT NULL"),
    )
    grant_app_privileges(
        op,
        "auth_sessions",
        select=True,
        insert=True,
        delete=True,
        update_columns=(
            "token_hash",
            "last_used_at",
            "idle_expires_at",
            "stepup_at",
            "stepup_method",
            "rotated_at",
            "revoked_at",
            "revoked_reason",
        ),
    )

    op.create_table(
        "auth_throttles",
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.LargeBinary(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"scope IN ({THROTTLE_SCOPES})",
            name=op.f("ck_auth_throttles_scope_valid"),
        ),
        sa.CheckConstraint(
            "octet_length(key_hash) = 32",
            name=op.f("ck_auth_throttles_key_hash_length"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_auth_throttles_attempts_not_negative")
        ),
        sa.PrimaryKeyConstraint("scope", "key_hash", name=op.f("pk_auth_throttles")),
    )
    op.create_index(
        "ix_auth_throttles_last_attempt_at", "auth_throttles", ["last_attempt_at"]
    )
    grant_app_privileges(
        op,
        "auth_throttles",
        select=True,
        insert=True,
        delete=True,
        update_columns=("attempts", "last_attempt_at", "locked_until"),
    )

    op.create_table(
        "auth_policy",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("passkey_owner", sa.Text(), nullable=False),
        sa.Column("passkey_admin", sa.Text(), nullable=False),
        sa.Column("passkey_user", sa.Text(), nullable=False),
        sa.Column("recommend_passkey_to_users", sa.Boolean(), nullable=False),
        sa.Column("stepup_window_minutes", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_auth_policy_single_row")),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_auth_policy_version_positive")
        ),
        sa.CheckConstraint(
            f"passkey_owner IN ({REQUIREMENTS})",
            name=op.f("ck_auth_policy_passkey_owner_valid"),
        ),
        sa.CheckConstraint(
            f"passkey_admin IN ({REQUIREMENTS})",
            name=op.f("ck_auth_policy_passkey_admin_valid"),
        ),
        sa.CheckConstraint(
            f"passkey_user IN ({REQUIREMENTS})",
            name=op.f("ck_auth_policy_passkey_user_valid"),
        ),
        sa.CheckConstraint(
            "stepup_window_minutes BETWEEN 5 AND 240",
            name=op.f("ck_auth_policy_stepup_window_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_policy")),
    )
    # The requirements' policy (REQUIREMENTS.md "Passkey Policy") is the default:
    # Owner and Admin required, User optional but recommended, 30-minute Step-up.
    op.execute(
        "INSERT INTO auth_policy (id, version, passkey_owner, passkey_admin, "
        "passkey_user, recommend_passkey_to_users, stepup_window_minutes, "
        "updated_at, updated_by) VALUES (1, 1, 'required', 'required', "
        "'optional', true, 30, now(), NULL)"
    )
    grant_app_privileges(
        op,
        "auth_policy",
        select=True,
        update_columns=(
            "version",
            "passkey_owner",
            "passkey_admin",
            "passkey_user",
            "recommend_passkey_to_users",
            "stepup_window_minutes",
            "updated_at",
            "updated_by",
        ),
    )
    op.execute(_GUARD_POLICY_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_auth_policy_guard_update BEFORE UPDATE ON auth_policy "
        "FOR EACH ROW EXECUTE FUNCTION paw_guard_auth_policy_update()"
    )
    op.execute(
        "ALTER TABLE auth_policy ENABLE ALWAYS TRIGGER tr_auth_policy_guard_update"
    )

    op.create_table(
        "auth_policy_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_passkey_owner", sa.Text(), nullable=False),
        sa.Column("new_passkey_owner", sa.Text(), nullable=False),
        sa.Column("old_passkey_admin", sa.Text(), nullable=False),
        sa.Column("new_passkey_admin", sa.Text(), nullable=False),
        sa.Column("old_passkey_user", sa.Text(), nullable=False),
        sa.Column("new_passkey_user", sa.Text(), nullable=False),
        sa.Column("old_recommend_passkey_to_users", sa.Boolean(), nullable=False),
        sa.Column("new_recommend_passkey_to_users", sa.Boolean(), nullable=False),
        sa.Column("old_stepup_window_minutes", sa.Integer(), nullable=False),
        sa.Column("new_stepup_window_minutes", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "version >= 2", name=op.f("ck_auth_policy_changes_version_after_seed")
        ),
        *(
            sa.CheckConstraint(
                f"{prefix}_passkey_{role} IN ({REQUIREMENTS})",
                name=op.f(f"ck_auth_policy_changes_{prefix}_passkey_{role}_valid"),
            )
            for prefix in ("old", "new")
            for role in ("owner", "admin", "user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_policy_changes")),
        sa.UniqueConstraint("version", name=op.f("uq_auth_policy_changes_version")),
    )
    grant_app_privileges(op, "auth_policy_changes", select=True, insert=True)
    op.execute(_REJECT_HISTORY_CHANGE_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_auth_policy_changes_reject_update_delete "
        "BEFORE UPDATE OR DELETE ON auth_policy_changes "
        "FOR EACH ROW EXECUTE FUNCTION paw_reject_auth_policy_changes_change()"
    )
    op.execute(
        "ALTER TABLE auth_policy_changes ENABLE ALWAYS TRIGGER "
        "tr_auth_policy_changes_reject_update_delete"
    )

    op.execute(_ACTIVATE_FUNCTION.format(schema=_schema_of_this_migration()))
    _grant_execute_to_the_app_role("paw_activate_invited_user(uuid, timestamptz)")


def downgrade() -> None:
    # DESTROYS EVERY PASSWORD, SESSION AND POLICY CHANGE (see the module
    # docstring). Dropping a table removes its indexes, triggers and grants.
    op.execute("DROP FUNCTION paw_activate_invited_user(uuid, timestamptz)")
    op.drop_table("auth_policy_changes")
    op.drop_table("auth_policy")
    op.drop_table("auth_throttles")
    op.drop_table("auth_sessions")
    op.drop_table("password_credentials")
    op.execute("DROP FUNCTION paw_reject_auth_policy_changes_change()")
    op.execute("DROP FUNCTION paw_guard_auth_policy_update()")
