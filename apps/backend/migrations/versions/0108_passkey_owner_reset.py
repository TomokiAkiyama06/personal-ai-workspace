"""An administrator's reset of another account's Passkeys and password (Issue #108).

Revision ID: 0108
Revises: 0041
Create Date: 2026-09-28

The way back for an Admin or a User who lost every Passkey (Decision 0025 section 7,
the follow-up issue; the choices are proposed in Decision 0032):

* ``user_passkeys.revoked_reason`` accepts ``admin_reset`` (every Passkey of the
  account ended by the Owner's, or an Admin's, reset).
* ``setup_tokens.purpose`` accepts ``password_reset``: the one-time password reset
  token of REQUIREMENTS.md ("1回限りのPassword再設定フロー", Decision 0015 section
  14). It shares the table, the attempt limit, the immutability trigger, the "one
  outstanding token per user" index and the redemption (``POST
  /auth/token/redeem``) with the Owner's setup / recovery tokens.
* ``paw_issue_password_reset_token`` (``SECURITY DEFINER``, ``search_path`` pinned
  to ``pg_catalog, pg_temp``, the tables named by the schema this migration ran in):
  the ONLY way the web application's role creates a token row. It refuses (returns
  ``false``, changes nothing) unless the user is an ``admin`` or a ``user`` whose
  status is ``invited`` or ``active``: the web role still cannot mint a token for
  the Owner (Decision 0005: whoever can INSERT a token row can take the Owner's
  account; that role never gets INSERT on ``setup_tokens``). In one statement
  sequence it locks the user row, revokes the user's outstanding token (only a
  reset token can be outstanding for a non-Owner), deletes the password (the old
  password stops working at once: a reset that left it would let whoever stole it
  enrol the first new Passkey) and stores the new token. The expiry must lie in
  ``(created_at, created_at + 72 hours]`` and ``created_at`` within 5 minutes of
  the database's ``clock_timestamp()`` (so the lifetime is bounded in real time,
  not only relative to a ``created_at`` the caller chose).

Privileges: EXECUTE on the function for ``PAW_APP_DATABASE_ROLE`` only (revoked from
PUBLIC). No table privilege changes: the web role still has no INSERT on
``setup_tokens`` and no DELETE on ``password_credentials``.

``downgrade()`` drops the function, deletes every ``password_reset`` token (the
older constraint does not allow them; a user whose reset is not redeemed yet then
has no password and no token: an Owner / operator sets them up again) and relabels
the Passkeys ended by a reset as ``recovery``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import configured_app_role

revision: str = "0108"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values are written out here and not imported: a migration is a snapshot.
OLD_PASSKEY_REVOKE_REASONS = "'revoked_by_user', 'recovery'"
NEW_PASSKEY_REVOKE_REASONS = OLD_PASSKEY_REVOKE_REASONS + ", 'admin_reset'"
OLD_TOKEN_PURPOSES = "'setup', 'recovery'"
NEW_TOKEN_PURPOSES = OLD_TOKEN_PURPOSES + ", 'password_reset'"
MAX_RESET_TOKEN_LIFETIME = "interval '72 hours'"
# How far ``p_created_at`` may be from the database clock (the application's clock
# and the database's may drift apart a little). Without it the 72-hour cap, which
# is relative to ``p_created_at``, would not bound the lifetime at all.
MAX_CLOCK_SKEW = "interval '5 minutes'"

SIGNATURE = (
    "paw_issue_password_reset_token(uuid, uuid, uuid, bytea, bytea, "
    "timestamptz, timestamptz)"
)

_ISSUE_FUNCTION = """\
CREATE FUNCTION paw_issue_password_reset_token(
    p_user_id uuid, p_token_id uuid, p_audit_ref uuid, p_salt bytea,
    p_secret_hash bytea, p_created_at timestamptz, p_expires_at timestamptz)
RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_role text;
    v_status text;
BEGIN
    IF p_created_at IS NULL OR p_expires_at IS NULL
       OR p_expires_at <= p_created_at
       OR p_expires_at > p_created_at + {max_lifetime}
       OR p_created_at < clock_timestamp() - {max_skew}
       OR p_created_at > clock_timestamp() + {max_skew} THEN
        RETURN false;
    END IF;
    SELECT system_role, status INTO v_role, v_status
      FROM {schema}.users WHERE id = p_user_id FOR UPDATE;
    IF NOT FOUND OR v_role NOT IN ('admin', 'user')
       OR v_status NOT IN ('invited', 'active') THEN
        RETURN false;
    END IF;
    UPDATE {schema}.setup_tokens SET revoked_at = p_created_at
     WHERE user_id = p_user_id AND used_at IS NULL AND revoked_at IS NULL;
    DELETE FROM {schema}.password_credentials WHERE user_id = p_user_id;
    INSERT INTO {schema}.setup_tokens
        (id, audit_ref, user_id, purpose, salt, secret_hash, created_at,
         expires_at, attempts)
    VALUES (p_token_id, p_audit_ref, p_user_id, 'password_reset', p_salt,
            p_secret_hash, p_created_at, p_expires_at, 0);
    RETURN true;
END;
$$
"""


def _schema_of_this_migration() -> str:
    """The quoted schema the tables live in (``public`` when rendering SQL)."""
    context = op.get_context()
    if context.as_sql:
        return "public"
    name = op.get_bind().execute(sa.text("SELECT current_schema()")).scalar_one()
    return context.dialect.identifier_preparer.quote_identifier(name)


def _replace_check(table: str, name: str, values: str) -> None:
    op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, f"{values}")


def upgrade() -> None:
    _replace_check(
        "user_passkeys",
        "revoked_reason_valid",
        f"revoked_reason IN ({NEW_PASSKEY_REVOKE_REASONS})",
    )
    _replace_check(
        "setup_tokens", "purpose_valid", f"purpose IN ({NEW_TOKEN_PURPOSES})"
    )
    op.execute(
        _ISSUE_FUNCTION.format(
            schema=_schema_of_this_migration(),
            max_lifetime=MAX_RESET_TOKEN_LIFETIME,
            max_skew=MAX_CLOCK_SKEW,
        )
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    role = configured_app_role()
    if role is not None:
        quoted = op.get_context().dialect.identifier_preparer.quote_identifier(role)
        op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {quoted}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.drop_constraint(
        op.f("ck_setup_tokens_purpose_valid"), "setup_tokens", type_="check"
    )
    # The older constraint cannot hold them (see the module docstring).
    op.execute("DELETE FROM setup_tokens WHERE purpose = 'password_reset'")
    op.create_check_constraint(
        op.f("ck_setup_tokens_purpose_valid"),
        "setup_tokens",
        f"purpose IN ({OLD_TOKEN_PURPOSES})",
    )
    op.drop_constraint(
        op.f("ck_user_passkeys_revoked_reason_valid"), "user_passkeys", type_="check"
    )
    op.execute(
        "UPDATE user_passkeys SET revoked_reason = 'recovery' "
        "WHERE revoked_reason = 'admin_reset'"
    )
    op.create_check_constraint(
        op.f("ck_user_passkeys_revoked_reason_valid"),
        "user_passkeys",
        f"revoked_reason IN ({OLD_PASSKEY_REVOKE_REASONS})",
    )
