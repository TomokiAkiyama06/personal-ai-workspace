"""The health probes of the shared connections (issue #306, Decision 0100).

Revision ID: 0306
Revises: 0282
Create Date: 2026-10-29

Decision 0100 (approved 2026-10-29) checks a Claude / Codex connection with a
real, short CLI session (a fixed prompt, no tools, no user data). A probe
belongs to no task and no user, so it is not a ``connection_usage`` row and is
counted into no quota (Decision 0100, 3; the human's answer 1 A):

* ``connection_probes``: one row per probe, inserted with its
  ``connection.probe`` audit event in one transaction BEFORE anything is sent
  (``running``), finished with its outcome (``connected`` / ``expired`` /
  ``unavailable`` / ``busy`` / ``cancelled``), its failure code, the tokens the
  CLI reported and the time it took (the database's clock), or ``abandoned`` by
  the reaper when the process that ran it died. ``connection_id`` is a plain
  UUID (the history outlives the connection); ``secret_handle`` is the opaque
  handle of the credential that was checked (never the credential), with the
  secret store's ``credential_version``; ``actor_id`` the Admin who caused it.
  No column holds a prompt, an answer or a credential.
* a partial unique index: one ``running`` probe per credential (the
  ``probe_running`` refusal, Decision 0100, 7); an index on
  ``(connection_id, started_at)`` for the spacing of rechecks (Decision 0100, 8).

Privileges of the application role (``PAW_APP_DATABASE_ROLE``): SELECT, INSERT
and UPDATE of the columns a probe's end writes; never DELETE (the probes are
kept like the usage rows). ``paw_compatibility`` is ``expand`` (Decision 0079,
4): an older release never names the table. ``downgrade()`` drops it (the
probes' history is lost: development and test only).

The revision names the issue (0306 after 0282) rather than the next number of
the chain: whichever of this and another revision written on the same head
merges second re-chains its ``down_revision``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from paw_backend.db_roles import grant_app_privileges

revision: str = "0306"
down_revision: str | Sequence[str] | None = "0282"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Decision 0079 4: what an older release can do with this schema.
paw_compatibility: str = "expand"

# Written out here (not imported): a migration keeps the values it was written
# with. ``tests/test_connection_probes_migration.py`` compares them with the
# model.
TABLE = "connection_probes"
KINDS = ("codex", "claude")
TRIGGERS = ("attach", "replace", "manual")
STATES = ("running", "finished", "abandoned")
OUTCOMES = ("connected", "expired", "unavailable", "busy", "cancelled")
FAILURE_CODES = (
    "rate_limited",
    "unavailable",
    "expired",
    "timeout",
    "invalid_response",
    "internal_error",
)
HANDLE_PATTERN = "^cred_[0-9a-f]{32}$"
MODEL_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$"
MAX_TOKENS = 10**9
MAX_DURATION_MS = 10**12
UPDATE_COLUMNS = (
    "state",
    "outcome",
    "failure_code",
    "model",
    "input_tokens",
    "output_tokens",
    "finished_at",
    "duration_ms",
)


def _listed(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "connection_probes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("secret_handle", sa.Text(), nullable=False),
        sa.Column("credential_version", sa.BigInteger(), nullable=True),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            f"kind IN ({_listed(KINDS)})", name=op.f("ck_connection_probes_kind_valid")
        ),
        sa.CheckConstraint(
            f"trigger IN ({_listed(TRIGGERS)})",
            name=op.f("ck_connection_probes_trigger_valid"),
        ),
        sa.CheckConstraint(
            f"state IN ({_listed(STATES)})",
            name=op.f("ck_connection_probes_state_valid"),
        ),
        sa.CheckConstraint(
            f"outcome IS NULL OR outcome IN ({_listed(OUTCOMES)})",
            name=op.f("ck_connection_probes_outcome_valid"),
        ),
        sa.CheckConstraint(
            f"failure_code IS NULL OR failure_code IN ({_listed(FAILURE_CODES)})",
            name=op.f("ck_connection_probes_failure_code_valid"),
        ),
        sa.CheckConstraint(
            f"secret_handle ~ '{HANDLE_PATTERN}'",
            name=op.f("ck_connection_probes_handle_shape"),
        ),
        sa.CheckConstraint(
            f"model IS NULL OR model ~ '{MODEL_PATTERN}'",
            name=op.f("ck_connection_probes_model_shape"),
        ),
        sa.CheckConstraint(
            "(state = 'finished') = (outcome IS NOT NULL)",
            name=op.f("ck_connection_probes_outcome_matches_state"),
        ),
        sa.CheckConstraint(
            "(state = 'running') = (finished_at IS NULL)",
            name=op.f("ck_connection_probes_finished_matches_state"),
        ),
        sa.CheckConstraint(
            "(finished_at IS NULL) = (duration_ms IS NULL)",
            name=op.f("ck_connection_probes_duration_matches_finish"),
        ),
        sa.CheckConstraint(
            "failure_code IS NULL OR (outcome IS NOT NULL AND outcome <> 'connected')",
            name=op.f("ck_connection_probes_failure_only_when_not_connected"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_connection_probes_finish_after_start"),
        ),
        sa.CheckConstraint(
            f"duration_ms IS NULL OR duration_ms BETWEEN 0 AND {MAX_DURATION_MS}",
            name=op.f("ck_connection_probes_duration_range"),
        ),
        sa.CheckConstraint(
            f"input_tokens IS NULL OR input_tokens BETWEEN 0 AND {MAX_TOKENS}",
            name=op.f("ck_connection_probes_input_tokens_range"),
        ),
        sa.CheckConstraint(
            f"output_tokens IS NULL OR output_tokens BETWEEN 0 AND {MAX_TOKENS}",
            name=op.f("ck_connection_probes_output_tokens_range"),
        ),
        sa.CheckConstraint(
            "credential_version IS NULL OR credential_version >= 1",
            name=op.f("ck_connection_probes_version_positive"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connection_probes")),
    )
    op.create_index(
        "uq_connection_probes_running",
        "connection_probes",
        ["connection_id", "secret_handle"],
        unique=True,
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index(
        "ix_connection_probes_connection_id_started_at",
        "connection_probes",
        ["connection_id", "started_at"],
    )
    grant_app_privileges(
        op,
        "connection_probes",
        select=True,
        insert=True,
        update_columns=UPDATE_COLUMNS,
    )


def downgrade() -> None:
    op.drop_index("ix_connection_probes_connection_id_started_at", table_name=TABLE)
    op.drop_index("uq_connection_probes_running", table_name=TABLE)
    op.drop_table(TABLE)
