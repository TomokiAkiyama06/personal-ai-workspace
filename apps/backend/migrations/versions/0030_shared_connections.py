"""Shared Codex / Claude connections, per-user quotas and usage (PAW-030).

Three tables:

* ``shared_connections``: the workspace-level Codex and Claude connections, at most
  one per kind (``uq_shared_connections_kind``). ``secret_handle`` holds the OPAQUE
  HANDLE of the credential in the secret store and never the credential: a CHECK
  constraint accepts only the ``cred_`` + 32 lower-case hex characters shape.
  ``status`` is the last health check's verdict, ``enabled`` the admin's switch.
* ``connection_quotas``: one limit per ``(user_id, kind, metric, period)``;
  ``limit_value`` NULL is the explicit Unlimited. ``user_id`` references ``users``
  (revision 0021) and cascades.
* ``connection_usage``: one row per call, inserted by the admission in the same
  transaction as the quota check and settled when the call ends. ``user_id`` and
  ``project_id`` are plain UUIDs (history that outlives the user), ``task_id``
  references ``tasks`` (revision 0032) with ``ON DELETE RESTRICT``. No column can
  hold a prompt, an answer or a credential.

**Depends on revisions 0021 (``users``) and 0032 (``tasks``)**: both are in this
revision's ancestry (0030 follows 0022, whose chain contains them).

The definitions repeat the ones in ``paw_backend.connections.models`` on purpose (a
migration is a frozen snapshot); ``tests/test_connections_schema.py`` fails when the
two drift apart. Constraint names come from the naming convention of
``paw_backend.db.Base.metadata``.

Revision ID: 0030
Revises: 0022
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = "'codex', 'claude'"
FAILURE_CODES = (
    "'rate_limited', 'unavailable', 'expired', 'timeout',"
    " 'invalid_response', 'internal_error'"
)


def upgrade() -> None:
    op.create_table(
        "shared_connections",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("secret_handle", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind"),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="kind_valid"),
        sa.CheckConstraint(
            "status IN ('connected', 'unavailable', 'expired')", name="status_valid"
        ),
        sa.CheckConstraint(
            "secret_handle ~ '^cred_[0-9a-f]{32}$'", name="handle_shape"
        ),
    )
    op.create_table(
        "connection_quotas",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("limit_value", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "kind", "metric", "period"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="kind_valid"),
        sa.CheckConstraint(
            "metric IN ('requests', 'tasks', 'tokens', 'runtime_seconds')",
            name="metric_valid",
        ),
        sa.CheckConstraint(
            "period IN ('rolling_5h', 'day', 'week', 'month')", name="period_valid"
        ),
        sa.CheckConstraint(
            "limit_value IS NULL OR limit_value BETWEEN 0 AND 1000000000000",
            name="limit_range",
        ),
    )
    op.create_table(
        "connection_usage",
        sa.Column(
            "id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="kind_valid"),
        sa.CheckConstraint(
            "purpose IN ('chat', 'coding', 'review', 'research', 'evaluation',"
            " 'other')",
            name="purpose_valid",
        ),
        sa.CheckConstraint(
            "status IN ('in_flight', 'succeeded', 'failed', 'cancelled')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            f"failure_code IS NULL OR failure_code IN ({FAILURE_CODES})",
            name="failure_code_valid",
        ),
        sa.CheckConstraint(
            "model ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$'", name="model_shape"
        ),
        sa.CheckConstraint(
            "(status = 'failed') = (failure_code IS NOT NULL)",
            name="failure_matches_status",
        ),
        sa.CheckConstraint(
            "(status = 'in_flight') = (finished_at IS NULL)",
            name="finished_matches_status",
        ),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (duration_ms IS NULL)",
            name="duration_matches_finish",
        ),
        sa.CheckConstraint(
            "status <> 'in_flight' OR (input_tokens IS NULL AND output_tokens IS NULL)",
            name="no_tokens_in_flight",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="finish_after_start",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms BETWEEN 0 AND 1000000000000",
            name="duration_range",
        ),
        sa.CheckConstraint(
            "input_tokens IS NULL OR input_tokens BETWEEN 0 AND 1000000000",
            name="input_tokens_range",
        ),
        sa.CheckConstraint(
            "output_tokens IS NULL OR output_tokens BETWEEN 0 AND 1000000000",
            name="output_tokens_range",
        ),
    )
    op.create_index(
        "ix_connection_usage_user_id_kind_started_at",
        "connection_usage",
        ["user_id", "kind", "started_at"],
    )
    op.create_index(
        "ix_connection_usage_task_id_kind", "connection_usage", ["task_id", "kind"]
    )

    # Least privilege for the application role (PAW_APP_DATABASE_ROLE), exactly what
    # ``ConnectionService`` executes.
    # A connection is created, disconnected (deleted), and changed in five columns:
    # the credential handle (replace), the status and the time of the last check,
    # the admin's switch and ``updated_at``. Its id, kind and creation time never
    # change. The row locks the admission takes (SELECT ... FOR SHARE) need the
    # UPDATE privilege on at least one column, which these grants give.
    grant_app_privileges(
        op,
        "shared_connections",
        insert=True,
        delete=True,
        update_columns=(
            "secret_handle",
            "status",
            "enabled",
            "checked_at",
            "updated_at",
        ),
    )
    # A quota is created, set again (limit and ``updated_at``) and removed; the
    # user, kind, metric, period and the creation time of a row never change.
    # ``SELECT ... FOR UPDATE`` on it (the admission's lock) needs the UPDATE
    # privilege on a column, which ``limit_value`` gives.
    grant_app_privileges(
        op,
        "connection_quotas",
        insert=True,
        delete=True,
        update_columns=("limit_value", "updated_at"),
    )
    # A usage row is inserted by the admission and settled once (status, failure
    # code, the tokens and the end). It is history: never deleted, and who, which
    # task, which model and purpose, and when it started never change.
    grant_app_privileges(
        op,
        "connection_usage",
        insert=True,
        update_columns=(
            "status",
            "failure_code",
            "input_tokens",
            "output_tokens",
            "finished_at",
            "duration_ms",
        ),
    )


def downgrade() -> None:
    # Reverse order of creation; dropping a table drops its indexes.
    op.drop_table("connection_usage")
    op.drop_table("connection_quotas")
    op.drop_table("shared_connections")
