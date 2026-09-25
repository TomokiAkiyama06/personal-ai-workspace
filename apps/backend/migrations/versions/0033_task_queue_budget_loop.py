"""Task queue, budget usage and failure signatures (PAW-033).

Revision ID: 0033
Revises: 0021
Create Date: 2026-09-24

``queue_entries``: waiting / running work with a lease, at most one active entry
per task. ``budget_usages``: consumption and limit per task and budget item.
``loop_failure_signatures``: a bounded window of failure hashes per task, each
with the task attempt it belongs to (never the failure message). All reference
``tasks.id`` (revision ``0032``) with real foreign keys.

Privileges of the application role (``PAW_APP_DATABASE_ROLE``, see
``paw_backend.db_roles``): each table gets the least the services need, chosen
from what ``TaskQueue`` / ``BudgetTracker`` / ``LoopDetector`` execute. No table
gets a table-level UPDATE, and TRUNCATE is never granted.

The tables deliberately do not start with ``task``: the PAW-032 tests inspect
every table with that prefix.

Enum-like columns are text with CHECK constraints whose value lists are written
out here (a migration must not follow later changes of the Python enums; change
a list with a new revision).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRIORITIES = ("high", "normal", "low")
QUEUE_STATUSES = ("queued", "claimed", "completed", "cancelled")
BUDGET_KINDS = (
    "runtime_seconds",
    "steps",
    "retries",
    "tool_calls",
    "tokens",
    "gpu_seconds",
)
BUDGET_PRESETS = ("standard", "long", "unlimited")
MAX_CONSUMED = 10**15
MAX_APPROACH = 100


def _in(column: str, values: Sequence[str], name: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})", name=op.f(name))


def upgrade() -> None:
    op.create_table(
        "queue_entries",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("priority", sa.String(length=24), nullable=False),
        sa.Column("priority_rank", sa.SmallInteger(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=24),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.String(length=100), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "claim_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_entries")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_queue_entries_task_id_tasks")
        ),
        _in("status", QUEUE_STATUSES, "ck_queue_entries_status_valid"),
        _in("priority", PRIORITIES, "ck_queue_entries_priority_valid"),
        sa.CheckConstraint(
            "(priority = 'high' AND priority_rank = 0)"
            " OR (priority = 'normal' AND priority_rank = 1)"
            " OR (priority = 'low' AND priority_rank = 2)",
            name=op.f("ck_queue_entries_priority_rank_matches_priority"),
        ),
        sa.CheckConstraint(
            "claim_count >= 0", name=op.f("ck_queue_entries_claim_count_not_negative")
        ),
        sa.CheckConstraint(
            "(status = 'claimed') = (lease_expires_at IS NOT NULL)",
            name=op.f("ck_queue_entries_lease_matches_status"),
        ),
        sa.CheckConstraint(
            "status <> 'claimed'"
            " OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL)",
            name=op.f("ck_queue_entries_claimed_has_worker"),
        ),
        sa.CheckConstraint(
            "status <> 'queued' OR (claimed_by IS NULL AND claimed_at IS NULL)",
            name=op.f("ck_queue_entries_queued_has_no_worker"),
        ),
        sa.CheckConstraint(
            "lease_expires_at IS NULL OR lease_expires_at > claimed_at",
            name=op.f("ck_queue_entries_lease_after_claim"),
        ),
        sa.CheckConstraint(
            "(status IN ('completed', 'cancelled')) = (finished_at IS NOT NULL)",
            name=op.f("ck_queue_entries_finished_matches_status"),
        ),
    )
    op.create_index(
        "uq_queue_entries_one_active_per_task",
        "queue_entries",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
    )
    op.create_index(
        "ix_queue_entries_claim_order",
        "queue_entries",
        ["priority_rank", "enqueued_at", "id"],
        postgresql_where=sa.text("status IN ('queued', 'claimed')"),
    )
    # enqueue INSERTs (and reads the generated id back, hence SELECT). claim,
    # heartbeat, release, complete and cancel UPDATE only the lease / status
    # columns (``SELECT ... FOR UPDATE SKIP LOCKED`` needs UPDATE on at least one
    # column, which this grant provides). ``task_id``, ``priority``,
    # ``priority_rank``, ``enqueued_at`` and ``id`` are fixed for the life of an
    # entry, so a compromised application cannot re-prioritise or re-assign a
    # queued task. No DELETE: cancelling is a status change and finished entries
    # are kept as history.
    grant_app_privileges(
        op,
        "queue_entries",
        select=True,
        insert=True,
        update_columns=(
            "status",
            "claimed_by",
            "claimed_at",
            "lease_expires_at",
            "claim_count",
            "finished_at",
        ),
    )

    op.create_table(
        "budget_usages",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("preset", sa.String(length=24), nullable=False),
        sa.Column(
            "consumed", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("limit_value", sa.BigInteger(), nullable=True),
        sa.Column("running_since", sa.DateTime(timezone=True), nullable=True),
        # The runtime session generation (fencing token of the runtime timer):
        # every start_runtime adds 1, stop_runtime must present the current value.
        # It only grows and is kept when the timer stops.
        sa.Column(
            "runtime_generation",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        # The cutoff of the last stop_runtime: the time up to which the runtime is
        # charged. start_runtime never sets running_since before it, so a start
        # that read its clock earlier than an older session's stop cannot make the
        # interval in between count twice.
        sa.Column("settled_through", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("task_id", "kind", name=op.f("pk_budget_usages")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"], name=op.f("fk_budget_usages_task_id_tasks")
        ),
        _in("kind", BUDGET_KINDS, "ck_budget_usages_kind_valid"),
        _in("preset", BUDGET_PRESETS, "ck_budget_usages_preset_valid"),
        sa.CheckConstraint(
            f"consumed >= 0 AND consumed <= {MAX_CONSUMED}",
            name=op.f("ck_budget_usages_consumed_in_range"),
        ),
        sa.CheckConstraint(
            "limit_value IS NULL OR limit_value >= 0",
            name=op.f("ck_budget_usages_limit_not_negative"),
        ),
        sa.CheckConstraint(
            "(preset = 'unlimited') = (limit_value IS NULL)",
            name=op.f("ck_budget_usages_limit_matches_preset"),
        ),
        sa.CheckConstraint(
            "running_since IS NULL OR kind = 'runtime_seconds'",
            name=op.f("ck_budget_usages_running_only_for_runtime"),
        ),
        sa.CheckConstraint(
            "runtime_generation >= 0"
            " AND (kind = 'runtime_seconds' OR runtime_generation = 0)",
            name=op.f("ck_budget_usages_runtime_generation_valid"),
        ),
        sa.CheckConstraint(
            "settled_through IS NULL OR kind = 'runtime_seconds'",
            name=op.f("ck_budget_usages_settled_only_for_runtime"),
        ),
        sa.CheckConstraint(
            "running_since IS NULL OR settled_through IS NULL"
            " OR running_since >= settled_through",
            name=op.f("ck_budget_usages_running_not_before_settled"),
        ),
    )
    # set_preset upserts (INSERT ... ON CONFLICT DO UPDATE SET preset,
    # limit_value); record and stop_runtime add to ``consumed`` (atomic
    # ``UPDATE ... SET consumed = ...``); start_runtime and stop_runtime set
    # ``running_since``, start_runtime adds 1 to ``runtime_generation``, and
    # stop_runtime sets ``settled_through`` (the cutoff start_runtime honours).
    # Nothing else changes: ``task_id`` and ``kind`` are the key and
    # ``created_at`` is history. No DELETE: a budget is never removed.
    grant_app_privileges(
        op,
        "budget_usages",
        select=True,
        insert=True,
        update_columns=(
            "preset",
            "limit_value",
            "consumed",
            "running_since",
            "runtime_generation",
            "settled_through",
        ),
    )

    op.create_table(
        "loop_failure_signatures",
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        # The task attempt (``tasks.attempt``, which Restart increments) the
        # failure was reported for: only the task's current attempt is assessed,
        # and the cleanup after a Restart deletes only older attempts.
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("approach", sa.Integer(), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("seq", name=op.f("pk_loop_failure_signatures")),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            name=op.f("fk_loop_failure_signatures_task_id_tasks"),
        ),
        sa.CheckConstraint(
            "attempt >= 1",
            name=op.f("ck_loop_failure_signatures_attempt_positive"),
        ),
        sa.CheckConstraint(
            f"approach >= 0 AND approach <= {MAX_APPROACH}",
            name=op.f("ck_loop_failure_signatures_approach_in_range"),
        ),
        sa.CheckConstraint(
            "signature ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_loop_failure_signatures_signature_format"),
        ),
    )
    op.create_index(
        op.f("ix_loop_failure_signatures_task_id"),
        "loop_failure_signatures",
        ["task_id", "seq"],
    )
    # record_failure INSERTs a row and DELETEs the rows that fall out of the
    # bounded window; ``clear_previous_attempts`` DELETEs the rows of the attempts
    # before the task's current one (cleanup after a Restart). That is the only
    # place in PAW-033 where the application deletes, because this table is a
    # sliding window of hashes, not history (the durable record of what happened
    # is ``task_events``). No UPDATE: a stored failure is never edited.
    # ``record_failure`` also reads ``tasks.attempt`` and locks that row
    # ``FOR SHARE`` (attempt fence), and the reads and the cleanup compare with
    # ``tasks.attempt``; that needs only the SELECT and the column-level UPDATE
    # that revision 0032 already granted on ``tasks``.
    grant_app_privileges(
        op, "loop_failure_signatures", select=True, insert=True, delete=True
    )


def downgrade() -> None:
    op.drop_table("loop_failure_signatures")
    op.drop_table("budget_usages")
    op.drop_table("queue_entries")
