"""Where a node's attempt ran: placement, agent, model (issue #133, Decision 0037).

Revision ID: 0133
Revises: 0085
Create Date: 2026-09-28

Decision 0037 (approved 2026-09-28) lets ``HybridRuntime`` run a node on a cloud
agent (Codex / Claude) when the local GPU is busy, and decided in its 14 that
the orchestrator's record must say where each node actually ran before any
``CloudPolicy`` is injected. Until now an attempt row names only the rung of the
ladder (``agent_index``), which is the local agent's label even when the node
ran in the cloud. How this revision records it is proposed in Decision 0048
(``docs/decisions/0048-node-placement-audit.md``).

``agent_dag_node_attempts`` gets seven nullable columns:

* ``placement`` (``local_gpu`` / ``local_cpu`` / ``cloud``), ``placement_agent``
  (a ladder label or the cloud agent's name), ``placement_model`` (a model id)
  and ``placed_at`` (the database's clock): all four or none;
* for the cloud only, ``content_fingerprint`` (``sha256:`` and 64 hex digits of
  what the orchestrator handed the node: never the content), ``content_bytes``
  and ``placement_audit_id``, the id of the ``audit_events`` row of the external
  send, written in the same transaction (``DagStore.record_placement``).

CHECK constraints keep the columns identifiers, never text (the same patterns as
``orchestrator.limits``), and make a cloud placement without its audit row (or a
local one with one) impossible. Rows that exist when this revision runs have all
seven NULL, which every constraint accepts: they are added validated.

A placement is recorded once. The application role gets ``UPDATE`` on the seven
columns (column-level, as for the other columns of the table; the rest of the
grants of revision 0034 are repeated unchanged), and the trigger
``tr_agent_dag_node_attempts_placement_once`` refuses any change of them once
``placement`` is set, whatever role writes (the audit trail of the placement is
not rewritten). It is ``ENABLE ALWAYS``, like the triggers of ``audit_events``.

``audit_events`` is not changed: the send's row uses its existing columns only
(``details`` stays NULL; Decision 0023's registry of ``details`` actions is not
touched).

``downgrade()`` drops the trigger, its function and the columns: it DESTROYS
the recorded placements (the ``audit_events`` rows of the sends stay).
Development and test databases only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0133"
down_revision: str | Sequence[str] | None = "0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "agent_dag_node_attempts"
PLACEMENTS = ("local_gpu", "local_cpu", "cloud")
AGENT_LABEL_PATTERN = "[a-z][a-z0-9._-]{0,63}"
MAX_MODEL_CHARS = 128
MODEL_PATTERN = "[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}"
PLACEMENT_COLUMNS = (
    "placement",
    "placement_agent",
    "placement_model",
    "placed_at",
    "content_fingerprint",
    "content_bytes",
    "placement_audit_id",
)
# What revision 0034 granted, repeated so that the grant below replaces it with
# the same set plus the new columns.
_COLUMNS_0034 = ("state", "error_class", "failure_signature", "finished_at")

_FUNCTION = """
CREATE FUNCTION paw_keep_node_attempt_placement() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.placement IS NOT NULL AND (
        NEW.placement, NEW.placement_agent, NEW.placement_model, NEW.placed_at,
        NEW.content_fingerprint, NEW.content_bytes, NEW.placement_audit_id
    ) IS DISTINCT FROM (
        OLD.placement, OLD.placement_agent, OLD.placement_model, OLD.placed_at,
        OLD.content_fingerprint, OLD.content_bytes, OLD.placement_audit_id
    ) THEN
        RAISE EXCEPTION 'the placement of a node attempt is recorded once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$
"""


def _check(name: str, condition: str) -> None:
    op.create_check_constraint(op.f(f"ck_{TABLE}_{name}"), TABLE, sa.text(condition))


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("placement", sa.String(length=16), nullable=True))
    op.add_column(
        TABLE, sa.Column("placement_agent", sa.String(length=64), nullable=True)
    )
    op.add_column(
        TABLE,
        sa.Column("placement_model", sa.String(length=MAX_MODEL_CHARS), nullable=True),
    )
    op.add_column(
        TABLE, sa.Column("placed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        TABLE, sa.Column("content_fingerprint", sa.String(length=71), nullable=True)
    )
    op.add_column(TABLE, sa.Column("content_bytes", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("placement_audit_id", sa.Uuid(), nullable=True))

    listed = ", ".join(f"'{value}'" for value in PLACEMENTS)
    _check("placement_valid", f"placement IN ({listed})")
    _check(
        "placement_complete",
        "(placement IS NULL) = (placement_agent IS NULL)"
        " AND (placement IS NULL) = (placement_model IS NULL)"
        " AND (placement IS NULL) = (placed_at IS NULL)",
    )
    _check(
        "placement_agent_format",
        f"placement_agent IS NULL OR placement_agent ~ '^{AGENT_LABEL_PATTERN}$'",
    )
    _check(
        "placement_model_format",
        f"placement_model IS NULL OR placement_model ~ '^{MODEL_PATTERN}$'",
    )
    _check(
        "cloud_is_audited",
        "COALESCE(placement = 'cloud', false) = (placement_audit_id IS NOT NULL)"
        " AND (placement_audit_id IS NULL) = (content_fingerprint IS NULL)"
        " AND (placement_audit_id IS NULL) = (content_bytes IS NULL)",
    )
    _check(
        "content_fingerprint_format",
        "content_fingerprint IS NULL OR content_fingerprint ~ '^sha256:[0-9a-f]{64}$'",
    )
    _check("content_bytes_not_negative", "content_bytes IS NULL OR content_bytes >= 0")

    op.execute(_FUNCTION)
    op.execute(
        "CREATE TRIGGER tr_agent_dag_node_attempts_placement_once "
        f"BEFORE UPDATE ON {TABLE} "
        "FOR EACH ROW EXECUTE FUNCTION paw_keep_node_attempt_placement()"
    )
    op.execute(
        f"ALTER TABLE {TABLE} ENABLE ALWAYS TRIGGER "
        "tr_agent_dag_node_attempts_placement_once"
    )

    # The placement is written once, while the attempt runs.
    grant_app_privileges(
        op,
        "agent_dag_node_attempts",
        select=True,
        insert=True,
        update_columns=(*_COLUMNS_0034, *PLACEMENT_COLUMNS),
    )


def downgrade() -> None:
    # DESTROYS the recorded placements (see the docstring).
    op.execute(f"DROP TRIGGER tr_agent_dag_node_attempts_placement_once ON {TABLE}")
    op.execute("DROP FUNCTION paw_keep_node_attempt_placement()")
    for column in reversed(PLACEMENT_COLUMNS):
        # Dropping a column drops its CHECK constraints and its column grants.
        op.drop_column(TABLE, column)
