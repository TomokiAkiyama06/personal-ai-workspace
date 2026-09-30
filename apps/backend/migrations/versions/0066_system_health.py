"""System Health: the metric time series, the health events and the index of the
connection reaper (PAW-066, issue #52, Decision 0059 Proposed).

Revision ID: 0066
Revises: 0154
Create Date: 2026-09-30

* ``health_metric_samples``: one row per metric, resolution (0 = a raw sample,
  60, 300 or 3600 seconds) and bucket: count, sum, minimum and maximum. The
  sampling loop inserts raw rows, the roll-up moves old rows to the next
  resolution (``paw_backend/health/store.py``).
* ``health_events``: a component's severity changed (never aggregated).
* ``ix_connection_usage_in_flight_started_at``: a partial index on
  ``connection_usage (started_at) WHERE status = 'in_flight'``. The reaper of
  abandoned calls (``orchestrator/connection_reaper.py``) and System Health look
  for the rows that are still ``in_flight`` (issue #52's note from PR #106): the
  index holds only those, so it stays small as the settled rows grow.
* ``ix_task_events_retry_fail_created_at`` (``task_events (created_at) WHERE
  command IN ('retry', 'fail')``) and ``ix_loop_failure_signatures_created_at``:
  System Health counts the failures, the retries and the detected loops of the
  last hour / day (Codex P1 on PR #170).
* ``ix_tasks_active_state`` (``tasks (state)`` of the active tasks) and
  ``ix_tasks_ended_updated_at`` (``tasks (updated_at)`` of the completed and
  cancelled ones): System Health counts the tasks by state every sample without
  reading the tasks that ended long ago (Codex P2 on PR #170).

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT, INSERT,
UPDATE and DELETE on ``health_metric_samples`` (the roll-up merges rows with
``ON CONFLICT DO UPDATE`` and deletes the rows it moved; the purge deletes old
aggregates), SELECT, INSERT and DELETE on ``health_events`` (the purge after the
retention period; an event is never updated). No privilege on ``connection_usage``
changes.

``CREATE INDEX`` blocks the writes of ``connection_usage``, ``task_events``,
``loop_failure_signatures`` and ``tasks`` while it runs (not ``CONCURRENTLY``:
Alembic runs the revision in a transaction); each build reads its table once.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0066"
down_revision: str | Sequence[str] | None = "0154"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_health_migration.py`` compares them with the model.
METRIC_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,40}(\.[a-z][a-z0-9_]{0,40}){1,3}$"
RESOLUTIONS = (0, 60, 300, 3600)
COMPONENTS = (
    "database",
    "compute",
    "task_queue",
    "memory_worker",
    "connections",
    "connection_reaper",
    "recovery_backup",
    "memory_projection",
    "audit_retention",
)
SEVERITIES = ("info", "warning", "error", "critical")
STATUSES = (
    "ok",
    "degraded",
    "failing",
    "stale",
    "unavailable",
    "not_configured",
    "never_ran",
    "check_failed",
)
MAX_REASONS_CHARS = 500
IN_FLIGHT_INDEX = "ix_connection_usage_in_flight_started_at"
TASK_EVENTS_INDEX = "ix_task_events_retry_fail_created_at"
ACTIVE_TASKS_INDEX = "ix_tasks_active_state"
ENDED_TASKS_INDEX = "ix_tasks_ended_updated_at"
ACTIVE_STATES = ("queued", "running", "waiting", "paused", "evaluating")
ENDED_STATES = ("completed", "cancelled")
LOOP_INDEX = "ix_loop_failure_signatures_created_at"


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "health_metric_samples",
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("resolution_seconds", sa.Integer(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sample_count", sa.BigInteger(), nullable=False),
        sa.Column("value_sum", sa.Double(), nullable=False),
        sa.Column("value_min", sa.Double(), nullable=False),
        sa.Column("value_max", sa.Double(), nullable=False),
        sa.CheckConstraint(
            f"metric ~ '{METRIC_NAME_PATTERN}'",
            name=op.f("ck_health_metric_samples_metric_shape"),
        ),
        sa.CheckConstraint(
            "resolution_seconds IN ("
            + ", ".join(str(value) for value in RESOLUTIONS)
            + ")",
            name=op.f("ck_health_metric_samples_resolution_valid"),
        ),
        sa.CheckConstraint(
            "sample_count >= 1",
            name=op.f("ck_health_metric_samples_sample_count_positive"),
        ),
        sa.CheckConstraint(
            "value_min <= value_max",
            name=op.f("ck_health_metric_samples_min_not_above_max"),
        ),
        sa.PrimaryKeyConstraint(
            "metric",
            "resolution_seconds",
            "bucket_start",
            name=op.f("pk_health_metric_samples"),
        ),
    )
    op.create_index(
        "ix_health_metric_samples_resolution_seconds_bucket_start",
        "health_metric_samples",
        ["resolution_seconds", "bucket_start"],
    )
    grant_app_privileges(
        op,
        "health_metric_samples",
        select=True,
        insert=True,
        update=True,
        delete=True,
    )

    op.create_table(
        "health_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("component", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("previous_severity", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reasons", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.CheckConstraint(
            f"component IN ({_listed(COMPONENTS)})",
            name=op.f("ck_health_events_component_valid"),
        ),
        sa.CheckConstraint(
            f"severity IN ({_listed(SEVERITIES)})",
            name=op.f("ck_health_events_severity_valid"),
        ),
        sa.CheckConstraint(
            "previous_severity IS NULL OR previous_severity IN "
            f"({_listed(SEVERITIES)})",
            name=op.f("ck_health_events_previous_severity_valid"),
        ),
        sa.CheckConstraint(
            f"status IN ({_listed(STATUSES)})",
            name=op.f("ck_health_events_status_valid"),
        ),
        sa.CheckConstraint(
            f"char_length(reasons) <= {MAX_REASONS_CHARS}",
            name=op.f("ck_health_events_reasons_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_health_events")),
    )
    op.create_index(
        "ix_health_events_component_id", "health_events", ["component", "id"]
    )
    op.create_index("ix_health_events_occurred_at", "health_events", ["occurred_at"])
    grant_app_privileges(op, "health_events", select=True, insert=True, delete=True)

    op.create_index(
        IN_FLIGHT_INDEX,
        "connection_usage",
        ["started_at"],
        postgresql_where=sa.text("status = 'in_flight'"),
    )
    op.create_index(
        TASK_EVENTS_INDEX,
        "task_events",
        ["created_at"],
        postgresql_where=sa.text("command IN ('retry', 'fail')"),
    )
    op.create_index(LOOP_INDEX, "loop_failure_signatures", ["created_at"])
    op.create_index(
        ACTIVE_TASKS_INDEX,
        "tasks",
        ["state"],
        postgresql_where=sa.text(f"state IN ({_listed(ACTIVE_STATES)})"),
    )
    op.create_index(
        ENDED_TASKS_INDEX,
        "tasks",
        ["updated_at"],
        postgresql_where=sa.text(f"state IN ({_listed(ENDED_STATES)})"),
    )


def downgrade() -> None:
    op.drop_index(ENDED_TASKS_INDEX, table_name="tasks")
    op.drop_index(ACTIVE_TASKS_INDEX, table_name="tasks")
    op.drop_index(LOOP_INDEX, table_name="loop_failure_signatures")
    op.drop_index(TASK_EVENTS_INDEX, table_name="task_events")
    op.drop_index(IN_FLIGHT_INDEX, table_name="connection_usage")
    op.drop_table("health_events")
    op.drop_table("health_metric_samples")
