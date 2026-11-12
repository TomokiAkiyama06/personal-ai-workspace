"""The Admin's policy of the CLI agents' models (issue #341, Decision 0103, 5
A, 7 A and 9 A).

Revision ID: 0341
Revises: 0325
Create Date: 2026-11-08

Decision 0103 (approved 2026-11-08 as recommended) lets the Admin allow, per
connection kind, models out of the repository's verified ones and name one of
them the default (``PUT /admin/agent-models``), without a restart:

* ``agent_model_policies``: one row per allowed model (primary key ``(kind,
  model)``), ``is_default`` on exactly one row of a kind (a partial unique
  index allows at most one; the service always writes one), the Admin's short
  ``description`` (1 to 120 characters) and ``updated_by`` / ``updated_at``
  (a plain UUID, as ``connection_probes.actor_id``). A kind without rows has
  no policy: the runtime configuration's model is its only allowed one;
* ``agent_model_policy_changes``: the history of the changes, append only:
  one row per allowed model of each kind a change wrote, sharing ``change_id``
  (the correlation id of the change's ``admin.agent_models.update`` audit
  event, which names no model: Decision 0103, 7 A);
* the probe's trigger ``policy`` (Decision 0103, 5 A, adding to Decision
  0100, 6): the probe that follows a change of a kind's default model. The
  ``trigger`` CHECK of ``connection_probes`` (revision 0306) is dropped and
  added again with it (nothing else changes).

Privileges of the application role (``PAW_APP_DATABASE_ROLE``):
``agent_model_policies`` SELECT, INSERT and DELETE (a change replaces a kind's
rows); ``agent_model_policy_changes`` SELECT and INSERT only (never UPDATE or
DELETE).

No ``paw_compatibility`` (Decision 0079, 4: not backward compatible; Codex
review of #350, P2). An older release never names the two tables, but it
reads a probe's ``trigger`` back for ``GET /admin/connections`` (the latest
probe of the credential) and knows no ``policy``: after an application-only
rollback that list would fail while that latest probe is a ``policy`` one. A
rollback over this revision therefore restores the database first. (Decision
0103, 9 named this revision ``expand``; Decision 0079's definition decides,
confirmed by the human 2026-11-12.)

``downgrade()`` drops both tables (the policy and its history are lost: the
runtime configuration's models apply again) and puts the old trigger CHECK
back; it FAILS while a probe's row holds ``policy``: development and test
only.

The revision names the issue (0341 after 0325) rather than the next number of
the chain: whichever of this and another revision written on the same head
merges second re-chains its ``down_revision``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0341"
down_revision: str | Sequence[str] | None = "0325"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: no ``paw_compatibility`` (not backward compatible; module
# docstring). A rollback over this revision restores the database first.

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_agent_model_policies_migration.py`` compares them with the
# model and with revision 0306.
POLICY_TABLE = "agent_model_policies"
CHANGE_TABLE = "agent_model_policy_changes"
KINDS = ("codex", "claude")
MODEL_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$"
MAX_DESCRIPTION_CHARS = 120
PREVIOUS_TRIGGERS = ("attach", "replace", "manual")
TRIGGERS = (*PREVIOUS_TRIGGERS, "policy")
TRIGGER_CHECK = "ck_connection_probes_trigger_valid"


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _checks(table: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(
            f"kind IN ({_listed(KINDS)})", name=op.f(f"ck_{table}_kind_valid")
        ),
        sa.CheckConstraint(
            f"model ~ '{MODEL_PATTERN}'", name=op.f(f"ck_{table}_model_shape")
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND "
            f"{MAX_DESCRIPTION_CHARS}",
            name=op.f(f"ck_{table}_description_length"),
        ),
    ]


def _trigger_check(values: Sequence[str]) -> None:
    op.drop_constraint(op.f(TRIGGER_CHECK), "connection_probes", type_="check")
    op.create_check_constraint(
        op.f(TRIGGER_CHECK), "connection_probes", f"trigger IN ({_listed(values)})"
    )


def upgrade() -> None:
    op.create_table(
        "agent_model_policies",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        *_checks(POLICY_TABLE),
        sa.PrimaryKeyConstraint("kind", "model", name=op.f(f"pk_{POLICY_TABLE}")),
    )
    op.create_index(
        "uq_agent_model_policies_default",
        POLICY_TABLE,
        ["kind"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )
    grant_app_privileges(
        op, "agent_model_policies", select=True, insert=True, delete=True
    )
    op.create_table(
        "agent_model_policy_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("change_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Uuid(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        *_checks(CHANGE_TABLE),
        sa.PrimaryKeyConstraint("id", name=op.f(f"pk_{CHANGE_TABLE}")),
    )
    op.create_index(
        "ix_agent_model_policy_changes_changed_at", CHANGE_TABLE, ["changed_at"]
    )
    grant_app_privileges(op, "agent_model_policy_changes", select=True, insert=True)
    _trigger_check(TRIGGERS)


def downgrade() -> None:
    # Fails while a probe's row holds ``policy`` (see the docstring).
    _trigger_check(PREVIOUS_TRIGGERS)
    op.drop_index("ix_agent_model_policy_changes_changed_at", table_name=CHANGE_TABLE)
    op.drop_table(CHANGE_TABLE)
    op.drop_index("uq_agent_model_policies_default", table_name=POLICY_TABLE)
    op.drop_table(POLICY_TABLE)
