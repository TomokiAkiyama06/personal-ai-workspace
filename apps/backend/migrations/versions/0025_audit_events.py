"""Append-only audit_events table (PAW-025).

Revision ID: 0025
Revises: 0001
Create Date: 2026-09-24

The table is protected in two layers:

* triggers reject UPDATE, DELETE and TRUNCATE (``ENABLE ALWAYS``: they also
  fire under ``session_replication_role = replica``);
* privileges: ``PUBLIC`` gets nothing, and the role named by
  ``PAW_APP_DATABASE_ROLE`` (when set) gets INSERT and SELECT only;
* a BEFORE INSERT trigger sets ``recorded_at`` to the database clock, so even
  an INSERT-capable role cannot choose it.

What is NOT guaranteed: a role that may INSERT can still insert rows with any
other content (actor, decision, occurred_at). The application is the only
writer that is trusted to write true rows.

The second layer is what makes "append-only" hold against the application
itself: the role that owns the table can drop the triggers, alter the columns
or create rules, so the application must connect as a *different* role than
the one that runs migrations (``PAW_MIGRATION_DATABASE_URL``). Without the
split only the triggers guard against buggy DML.

``downgrade()`` drops the table and therefore DESTROYS THE AUDIT HISTORY. It
exists for development and test databases; never run it in production.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.config import Settings

logger = logging.getLogger("paw_backend.migrations.0025")

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = """
CREATE FUNCTION paw_reject_audit_events_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_events is append-only'
        USING ERRCODE = 'restrict_violation';
END;
$$
"""


_RECORDED_AT_FUNCTION = """
CREATE FUNCTION paw_force_audit_events_recorded_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.recorded_at := now();
    RETURN NEW;
END;
$$
"""


def _quoted_app_role() -> str | None:
    """The application role as a quoted identifier, or ``None`` if none is set.

    The name is validated by ``Settings`` (letters, digits, underscore; not
    ``public``, ``pg_*`` or another reserved name), must exist, and is then
    quoted by the dialect's identifier preparer; it is never interpolated into
    SQL as text.
    """
    settings = Settings()
    role = settings.app_database_role
    if role is None:
        if settings.migration_database_url is not None:
            logger.warning(
                "PAW_MIGRATION_DATABASE_URL is set but PAW_APP_DATABASE_ROLE is not: "
                "no role is granted access to audit_events, so the application "
                "cannot write its audit trail and every audited action will be "
                "refused (503) until the role is granted INSERT and SELECT."
            )
        return None
    context = op.get_context()
    if not context.as_sql:  # online: the role must exist, fail loudly if not
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
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("actor_role", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_kind", sa.Text(), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("repo_id", sa.Uuid(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("old_role", sa.Text(), nullable=True),
        sa.Column("new_role", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "decision IN ('allow', 'deny')",
            name=op.f("ck_audit_events_decision_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index(
        op.f("ix_audit_events_occurred_at"), "audit_events", ["occurred_at"]
    )

    op.execute(_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_audit_events_reject_update_delete "
        "BEFORE UPDATE OR DELETE ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION paw_reject_audit_events_change()"
    )
    op.execute(
        "CREATE TRIGGER tr_audit_events_reject_truncate "
        "BEFORE TRUNCATE ON audit_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION paw_reject_audit_events_change()"
    )
    op.execute(
        "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER "
        "tr_audit_events_reject_update_delete"
    )
    op.execute(
        "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER tr_audit_events_reject_truncate"
    )
    op.execute(_RECORDED_AT_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_audit_events_force_recorded_at "
        "BEFORE INSERT ON audit_events "
        "FOR EACH ROW EXECUTE FUNCTION paw_force_audit_events_recorded_at()"
    )
    op.execute(
        "ALTER TABLE audit_events ENABLE ALWAYS TRIGGER "
        "tr_audit_events_force_recorded_at"
    )

    op.execute("REVOKE ALL ON audit_events FROM PUBLIC")
    app_role = _quoted_app_role()
    if app_role is not None:
        op.execute(f"GRANT INSERT, SELECT ON audit_events TO {app_role}")


def downgrade() -> None:
    # DESTROYS THE AUDIT HISTORY (development and test only, see the docstring).
    # Dropping the table removes its triggers and grants.
    op.drop_table("audit_events")
    op.execute("DROP FUNCTION paw_reject_audit_events_change()")
    op.execute("DROP FUNCTION paw_force_audit_events_recorded_at()")
