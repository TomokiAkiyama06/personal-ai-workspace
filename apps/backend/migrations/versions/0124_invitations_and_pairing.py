"""User invitations, device pairing and the user lifecycle (PAW-024).

Revision ID: 0124
Revises: 0034
Create Date: 2026-09-28

The revision id is the issue's (PAW-024), written 0124 so that it is not read as
Decision 0024; it is not an order in the chain. The design and every value it
chose is Decision 0033 (Approved 2026-09-28).

Tables (every column and constraint is spelled out below; the models in
``paw_backend.auth.onboarding.models`` repeat them and
``tests/test_onboarding_migration.py`` fails when the two drift apart):

* ``user_invitations``: the one-time invitation token of an ``invited`` user
  (a salted HMAC only; at most one outstanding per user; a CHECK caps the lifetime
  at 14 days).
* ``device_pairings``: one "add a new device" (QR code / link): the one-time
  pairing token, and the new device's claim while an Owner's / Admin's pairing
  waits for a trusted device's approval, with the claim's confirmation code (a
  salted HMAC and a count of wrong entries: Decision 0033, point 12) (at most one
  live pairing per user; a CHECK caps each lifetime at an hour).
* ``user_status_changes``: the append-only history of ``users.status`` (a
  trigger rejects UPDATE and DELETE). Only the two functions below write it. Its
  foreign key to ``users`` is RESTRICT: a user who has a history (every invited
  user) is never hard-deleted; the row stays as a tombstone.

Functions (``SECURITY DEFINER``, ``search_path`` pinned to ``pg_catalog, pg_temp``,
``users`` named by the schema this migration ran in, like 0022's
``paw_activate_invited_user``). The web role gets EXECUTE and no new privilege on
``users``:

* ``paw_invite_user(id, login_name, role, now, actor)``: inserts an ``invited``
  user whose role is ``user`` or ``admin`` (never ``owner``: the Owner is the
  CLI's, Decision 0005) and its first history row.
* ``paw_change_user_status(user_id, from, to, now, actor)``: moves a user that is
  not the Owner from ``from`` to ``to`` along one of four edges only (``invited``
  to ``active`` or ``deleted``, ``active`` to ``pending_deletion``,
  ``pending_deletion`` to ``active``) and writes the history row. ``deleted`` is
  never reached from ``pending_deletion`` here: the erasure that must come first is
  a later issue (Decision 0033, section 3).

Changes to 0022's tables: ``auth_sessions.auth_method`` accepts ``pairing`` (the
Step-up methods stay ``password`` and ``passkey``: a pairing steps nothing up);
``auth_throttles.scope`` accepts ``pairing_source`` and ``pairing_global``.

Privileges of the web role (``PAW_APP_DATABASE_ROLE``), the least the services
execute (``tests/test_onboarding_grants.py`` pins them):

* ``user_invitations``: SELECT, INSERT, UPDATE of ``attempts``, ``used_at``,
  ``revoked_at``, ``revoked_reason``, ``locked_at``. Not of the hash, the salt, the
  user or the expiry.
* ``device_pairings``: SELECT, INSERT, UPDATE of the columns a pairing's life
  changes (not ``user_id``, the token's salt / hash, ``created_at``,
  ``issued_by_session``). No DELETE.
* ``user_status_changes``: SELECT only.

What this does NOT guard: the web role inserts the tokens (it issues them) and
writes passwords and sessions (0022), so a compromised application can create an
invited Admin and sign in as anyone; Decisions 0005 / 0022 / 0025 accepted the
same kind of limit for passwords and Passkeys.

``downgrade()`` drops the three tables and the two functions (EVERY INVITATION,
PAIRING AND STATUS HISTORY IS LOST) and deletes the sessions that a pairing
created and the pairing throttle rows, so that 0022's constraints hold again.
Development and test databases only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import configured_app_role, grant_app_privileges

revision: str = "0124"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values are written out here and not imported: a migration is a snapshot.
OLD_AUTH_METHODS = "'password', 'passkey'"
NEW_AUTH_METHODS = OLD_AUTH_METHODS + ", 'pairing'"
OLD_THROTTLE_SCOPES = (
    "'login_account', 'login_source', 'redeem_source', 'redeem_global'"
)
NEW_THROTTLE_SCOPES = OLD_THROTTLE_SCOPES + ", 'pairing_source', 'pairing_global'"
INVITATION_ENDS = "'revoked', 'superseded', 'cancelled'"
PAIRING_STATES = "'issued', 'claimed', 'approved', 'completed', 'rejected', 'revoked'"
PAIRING_ENDS = (
    "'revoked_by_user', 'superseded', 'account_closed', 'confirmation_failed'"
)
USER_STATUSES = "'invited', 'active', 'pending_deletion', 'deleted'"

_INVITE_FUNCTION = """\
CREATE FUNCTION paw_invite_user(p_id uuid, p_login_name text, p_role text,
                                p_now timestamptz, p_actor uuid)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF p_role IS NULL OR p_role NOT IN ('user', 'admin') THEN
        RAISE EXCEPTION 'paw_invite_user: only a user or an admin can be invited'
            USING ERRCODE = 'check_violation';
    END IF;
    INSERT INTO {schema}.users (id, login_name, system_role, status,
                                passkey_required, created_at, updated_at)
    VALUES (p_id, p_login_name, p_role, 'invited', p_role = 'admin', p_now, p_now);
    INSERT INTO {schema}.user_status_changes
        (id, user_id, old_status, new_status, changed_at, changed_by, recorded_at)
    VALUES (gen_random_uuid(), p_id, NULL, 'invited', p_now, p_actor,
            clock_timestamp());
END;
$$
"""

_CHANGE_STATUS_FUNCTION = """\
CREATE FUNCTION paw_change_user_status(p_user_id uuid, p_from text, p_to text,
                                       p_now timestamptz, p_actor uuid)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF NOT ((p_from = 'invited' AND p_to IN ('active', 'deleted'))
            OR (p_from = 'active' AND p_to = 'pending_deletion')
            OR (p_from = 'pending_deletion' AND p_to = 'active')) THEN
        RAISE EXCEPTION 'paw_change_user_status: not an allowed transition'
            USING ERRCODE = 'check_violation';
    END IF;
    UPDATE {schema}.users
       SET status = p_to, updated_at = p_now
     WHERE id = p_user_id AND status = p_from AND system_role <> 'owner';
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO {schema}.user_status_changes
        (id, user_id, old_status, new_status, changed_at, changed_by, recorded_at)
    VALUES (gen_random_uuid(), p_user_id, p_from, p_to, p_now, p_actor,
            clock_timestamp());
    RETURN true;
END;
$$
"""

_REJECT_HISTORY_CHANGE_FUNCTION = """\
CREATE FUNCTION paw_reject_user_status_changes_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'user_status_changes is append-only'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""

INVITE_SIGNATURE = "paw_invite_user(uuid, text, text, timestamptz, uuid)"
CHANGE_SIGNATURE = "paw_change_user_status(uuid, text, text, timestamptz, uuid)"


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
        "user_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("audit_ref", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "octet_length(salt) = 16", name=op.f("ck_user_invitations_salt_length")
        ),
        sa.CheckConstraint(
            "octet_length(secret_hash) = 32",
            name=op.f("ck_user_invitations_secret_hash_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= created_at + interval '14 days'",
            name=op.f("ck_user_invitations_lifetime_bounded"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_user_invitations_attempts_not_negative")
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR revoked_at IS NULL",
            name=op.f("ck_user_invitations_used_or_revoked"),
        ),
        sa.CheckConstraint(
            f"revoked_reason IN ({INVITATION_ENDS})",
            name=op.f("ck_user_invitations_revoked_reason_valid"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_reason IS NULL)",
            name=op.f("ck_user_invitations_revocation_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_invitations_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_invitations")),
        sa.UniqueConstraint("audit_ref", name=op.f("uq_user_invitations_audit_ref")),
    )
    op.create_index("ix_user_invitations_user_id", "user_invitations", ["user_id"])
    op.create_index(
        "uq_user_invitations_one_outstanding",
        "user_invitations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )
    grant_app_privileges(
        op,
        "user_invitations",
        select=True,
        insert=True,
        update_columns=(
            "attempts",
            "used_at",
            "revoked_at",
            "revoked_reason",
            "locked_at",
        ),
    )

    op.create_table(
        "device_pairings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("audit_ref", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("issued_by_session", sa.Uuid(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("salt", sa.LargeBinary(), nullable=False),
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        sa.Column("claim_id", sa.Uuid(), nullable=True),
        sa.Column("claim_salt", sa.LargeBinary(), nullable=True),
        sa.Column("claim_hash", sa.LargeBinary(), nullable=True),
        sa.Column("confirm_salt", sa.LargeBinary(), nullable=True),
        sa.Column("confirm_hash", sa.LargeBinary(), nullable=True),
        sa.Column("confirm_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("device_label", sa.Text(), nullable=True),
        sa.Column("remember_me", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_session", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_session", sa.Uuid(), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_reason", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"state IN ({PAIRING_STATES})", name=op.f("ck_device_pairings_state_valid")
        ),
        sa.CheckConstraint(
            "octet_length(salt) = 16", name=op.f("ck_device_pairings_salt_length")
        ),
        sa.CheckConstraint(
            "octet_length(secret_hash) = 32",
            name=op.f("ck_device_pairings_secret_hash_length"),
        ),
        sa.CheckConstraint(
            "claim_salt IS NULL OR octet_length(claim_salt) = 16",
            name=op.f("ck_device_pairings_claim_salt_length"),
        ),
        sa.CheckConstraint(
            "claim_hash IS NULL OR octet_length(claim_hash) = 32",
            name=op.f("ck_device_pairings_claim_hash_length"),
        ),
        sa.CheckConstraint(
            "(claim_salt IS NULL) = (claim_hash IS NULL)",
            name=op.f("ck_device_pairings_claim_complete"),
        ),
        sa.CheckConstraint(
            "(claim_id IS NULL) = (claim_hash IS NULL)",
            name=op.f("ck_device_pairings_claim_has_id"),
        ),
        sa.CheckConstraint(
            "confirm_salt IS NULL OR octet_length(confirm_salt) = 16",
            name=op.f("ck_device_pairings_confirm_salt_length"),
        ),
        sa.CheckConstraint(
            "confirm_hash IS NULL OR octet_length(confirm_hash) = 32",
            name=op.f("ck_device_pairings_confirm_hash_length"),
        ),
        sa.CheckConstraint(
            "(confirm_salt IS NULL) = (confirm_hash IS NULL)",
            name=op.f("ck_device_pairings_confirm_complete"),
        ),
        sa.CheckConstraint(
            "(claim_hash IS NULL) = (confirm_hash IS NULL)",
            name=op.f("ck_device_pairings_claim_has_confirmation"),
        ),
        sa.CheckConstraint(
            "confirm_attempts >= 0",
            name=op.f("ck_device_pairings_confirm_attempts_not_negative"),
        ),
        sa.CheckConstraint(
            "device_label IS NULL OR char_length(device_label) BETWEEN 1 AND 64",
            name=op.f("ck_device_pairings_device_label_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at AND expires_at <= "
            "coalesce(claimed_at, created_at) + interval '1 hour'",
            name=op.f("ck_device_pairings_lifetime_bounded"),
        ),
        sa.CheckConstraint(
            "state <> 'issued' OR (claimed_at IS NULL AND claim_hash IS NULL)",
            name=op.f("ck_device_pairings_issued_is_unclaimed"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('claimed', 'approved', 'rejected') "
            "OR (claim_hash IS NOT NULL AND approval_required)",
            name=op.f("ck_device_pairings_claim_needs_approval"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('approved', 'rejected') OR decided_at IS NOT NULL",
            name=op.f("ck_device_pairings_decision_recorded"),
        ),
        sa.CheckConstraint(
            "(state = 'completed') = (completed_at IS NOT NULL)",
            name=op.f("ck_device_pairings_completion_recorded"),
        ),
        sa.CheckConstraint(
            "(state = 'revoked') = (ended_reason IS NOT NULL)",
            name=op.f("ck_device_pairings_end_recorded"),
        ),
        sa.CheckConstraint(
            "(ended_at IS NULL) = (ended_reason IS NULL)",
            name=op.f("ck_device_pairings_end_complete"),
        ),
        sa.CheckConstraint(
            f"ended_reason IN ({PAIRING_ENDS})",
            name=op.f("ck_device_pairings_ended_reason_valid"),
        ),
        sa.CheckConstraint(
            "attempts >= 0", name=op.f("ck_device_pairings_attempts_not_negative")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_device_pairings_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["issued_by_session"],
            ["auth_sessions.id"],
            name=op.f("fk_device_pairings_issued_by_session_auth_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_session"],
            ["auth_sessions.id"],
            name=op.f("fk_device_pairings_decided_by_session_auth_sessions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_session"],
            ["auth_sessions.id"],
            name=op.f("fk_device_pairings_created_session_auth_sessions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_pairings")),
        sa.UniqueConstraint("audit_ref", name=op.f("uq_device_pairings_audit_ref")),
        sa.UniqueConstraint("claim_id", name=op.f("uq_device_pairings_claim_id")),
    )
    # The referential actions of ``users`` and ``auth_sessions`` (the session purge
    # sets the three session columns to NULL) find the referencing rows by these.
    op.create_index("ix_device_pairings_user_id", "device_pairings", ["user_id"])
    for column in ("issued_by_session", "decided_by_session", "created_session"):
        op.create_index(
            f"ix_device_pairings_{column}",
            "device_pairings",
            [column],
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )
    op.create_index(
        "uq_device_pairings_one_live",
        "device_pairings",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('issued', 'claimed', 'approved')"),
    )
    grant_app_privileges(
        op,
        "device_pairings",
        select=True,
        insert=True,
        update_columns=(
            "state",
            "approval_required",
            "claim_id",
            "claim_salt",
            "claim_hash",
            "confirm_salt",
            "confirm_hash",
            "confirm_attempts",
            "device_label",
            "remember_me",
            "expires_at",
            "claimed_at",
            "decided_at",
            "decided_by_session",
            "completed_at",
            "created_session",
            "ended_at",
            "ended_reason",
            "attempts",
            "locked_at",
        ),
    )

    op.create_table(
        "user_status_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("old_status", sa.Text(), nullable=True),
        sa.Column("new_status", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"old_status IN ({USER_STATUSES})",
            name=op.f("ck_user_status_changes_old_status_valid"),
        ),
        sa.CheckConstraint(
            f"new_status IN ({USER_STATUSES})",
            name=op.f("ck_user_status_changes_new_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_status_changes_user_id_users"),
            # RESTRICT: the history is append-only (the trigger below), so a
            # users row that has one is never hard-deleted; it is a tombstone
            # (Decision 0033, section 3). A CASCADE would only be refused.
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_status_changes")),
    )
    op.create_index(
        "ix_user_status_changes_user_id",
        "user_status_changes",
        ["user_id", "changed_at"],
    )
    grant_app_privileges(op, "user_status_changes", select=True)
    op.execute(_REJECT_HISTORY_CHANGE_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_user_status_changes_reject_update_delete "
        "BEFORE UPDATE OR DELETE ON user_status_changes "
        "FOR EACH ROW EXECUTE FUNCTION paw_reject_user_status_changes_change()"
    )
    op.execute(
        "ALTER TABLE user_status_changes ENABLE ALWAYS TRIGGER "
        "tr_user_status_changes_reject_update_delete"
    )

    schema = _schema_of_this_migration()
    op.execute(_INVITE_FUNCTION.format(schema=schema))
    _grant_execute_to_the_app_role(INVITE_SIGNATURE)
    op.execute(_CHANGE_STATUS_FUNCTION.format(schema=schema))
    _grant_execute_to_the_app_role(CHANGE_SIGNATURE)

    _replace_check(
        "ck_auth_sessions_auth_method_valid",
        "auth_sessions",
        f"auth_method IN ({NEW_AUTH_METHODS})",
    )
    _replace_check(
        "ck_auth_throttles_scope_valid",
        "auth_throttles",
        f"scope IN ({NEW_THROTTLE_SCOPES})",
    )


def _replace_check(name: str, table: str, condition: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, condition)


def downgrade() -> None:
    # DESTROYS EVERY INVITATION, PAIRING AND STATUS HISTORY (see the docstring).
    op.execute("DELETE FROM auth_sessions WHERE auth_method = 'pairing'")
    op.execute(
        "DELETE FROM auth_throttles WHERE scope IN ('pairing_source', 'pairing_global')"
    )
    _replace_check(
        "ck_auth_throttles_scope_valid",
        "auth_throttles",
        f"scope IN ({OLD_THROTTLE_SCOPES})",
    )
    _replace_check(
        "ck_auth_sessions_auth_method_valid",
        "auth_sessions",
        f"auth_method IN ({OLD_AUTH_METHODS})",
    )
    op.execute(f"DROP FUNCTION {CHANGE_SIGNATURE}")
    op.execute(f"DROP FUNCTION {INVITE_SIGNATURE}")
    op.drop_table("user_status_changes")
    op.execute("DROP FUNCTION paw_reject_user_status_changes_change()")
    op.drop_table("device_pairings")
    op.drop_table("user_invitations")
