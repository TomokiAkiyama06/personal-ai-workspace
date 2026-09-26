"""Partition ``audit_events`` by month and add an archive parent (#86, Decision 0027).

Revision ID: 0086
Revises: 0071
Create Date: 2026-09-27

Decision 0004 (PAW-025) left the retention period, the partitioning scheme and
where old rows go undecided (its 5.5). Decision 0027 (proposed;
``docs/decisions/0027-audit-retention-and-partitioning.md``) answers this:
monthly ``PARTITION BY RANGE (recorded_at)``, a second parent
``audit_events_archive`` that old partitions move to (a metadata-only
``DETACH`` / ``ATTACH``, never a row copy), and a bookkeeping table,
``audit_retention_partitions``, that ``paw_backend.authz.retention`` uses to
track each partition instead of parsing PostgreSQL's own catalog. This
revision only *builds the shapes*; nothing runs the ongoing job by itself (no
scheduler is added — see Decision 0027 and ``AuditRetentionService``).

What changes physically:

* The table Migration 0025 created (and Migration 0087 added ``details`` to) is
  renamed to ``audit_events_p_legacy`` and becomes the partition that holds
  every row recorded before this migration ran (``FOR VALUES FROM (MINVALUE)
  TO (<cutover>)``, ``cutover`` = ``now()`` when this migration is applied).
  Its own CHECK constraints (``decision_valid``, ``details_registered``,
  ``external_send_details``) are untouched; only its primary key changes, from
  ``id`` alone to ``(id, recorded_at)`` (PostgreSQL requires the partition key
  in every unique / primary key constraint of a partitioned table's members).
* A new table named ``audit_events`` — the partitioned parent — takes its
  place, with the identical column set and CHECK constraints, ``PARTITION BY
  RANGE (recorded_at)``. The legacy table is attached to it, and one more
  partition is created immediately, covering from the cutover to the start of
  the next calendar month (a short first partition; every later one, made by
  ``AuditRetentionService.ensure_partitions``, is a full calendar month). The
  three append-only triggers (Migration 0025) are recreated on this new parent
  with the **same names** they had before: PostgreSQL clones a row-level
  trigger (the reject-update/delete one, the recorded_at-forcing one) onto
  every partition automatically, both the two that exist already and any
  created later — proved in ``tests/test_retention_postgres.py``. The
  **statement-level** one (reject-truncate) is *not* cloned by PostgreSQL, so it
  is created explicitly on the first live partition too (the legacy table kept
  its own pre-existing copy through the rename); ``AuditRetentionService``
  does the same for every partition it creates later, or a direct
  ``TRUNCATE`` of one partition would bypass the guarantee PostgreSQL does not
  extend to it automatically.
* ``audit_events_archive`` is a second, empty, identically-shaped partitioned
  parent with its own copies of the two append-only triggers (so anything
  later attached to it is protected the moment it arrives, the same cloning
  behaviour). Nothing is archived by this migration; ``AuditRetentionService.
  archive_due_partitions`` moves partitions into it later, by ``DETACH`` /
  ``ATTACH`` (no row is read or rewritten).
* ``audit_retention_partitions`` (an ordinary, mutable table — not part of the
  append-only trail; see ``paw_backend/authz/retention/models.py``) gets one
  row for the legacy partition and one for the first live partition, both
  ``status = 'live'``.

Grants: the application role keeps exactly what it had (``INSERT`` and
``SELECT`` on ``audit_events``, now the partitioned parent — PostgreSQL checks
privileges against whichever name a statement addresses, so a grant on the
parent alone covers every partition reached through it; nothing is granted on
individual partitions). It additionally gets ``SELECT`` (never ``INSERT``) on
``audit_events_archive``, so a future admin view could read archived history
without a second migration; ``audit_retention_partitions`` gets no
application-role grant at all (only the migration / retention-maintenance role
touches it — see ``NO_APP_GRANTS`` below and Decision 0027). Creating,
archiving and purging a partition are all DDL (``CREATE TABLE ... PARTITION
OF``, ``ATTACH``/``DETACH PARTITION``, ``DROP TABLE``) that only the table's
owner may run; ``AuditRetentionService`` therefore needs the same privileged
connection migrations use (``PAW_MIGRATION_DATABASE_URL``), never the
application's role (proved by the non-superuser-role tests in
``tests/test_retention_postgres.py``).

``downgrade()`` folds every partition's rows back into the legacy table (by
``INSERT ... SELECT``, not by touching ``recorded_at`` — the legacy table is
detached from its parent, which drops its *cloned* row-level triggers, before
any row is copied into it, so the recorded-at-forcing trigger cannot overwrite
history being restored), drops the two parents and the bookkeeping table, and
restores the original unpartitioned shape and trigger names exactly. It
assumes ``audit_events_p_legacy`` itself was never purged (nothing in this
revision or ``AuditRetentionService`` purges automatically; the default
policy's ``purge_after_days`` is ``None`` — see Decision 0027); if an operator
manually purged even that partition, a downgrade must be recovered by hand.
Like Migration 0025's own ``downgrade()``, this is a development / test tool,
not something to run in production.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from paw_backend.authz.retention.rules import (
    month_start,
    next_month_start,
    partition_name,
)
from paw_backend.config import Settings
from paw_backend.db_roles import grant_app_privileges, validate_role_name

logger = logging.getLogger("paw_backend.migrations.0086")

revision: str = "0086"
down_revision: str | Sequence[str] | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY = "audit_events_p_legacy"
ARCHIVE = "audit_events_archive"
BOOKKEEPING = "audit_retention_partitions"

# ``audit_retention_partitions`` is bookkeeping the migration / retention role
# alone reads and writes (``paw_backend.authz.retention``); the application
# never queries it, so it gets no grant at all (tests/test_migration_grants.py
# requires this explicit opt-out for a created table with no grant call).
NO_APP_GRANTS = {
    "audit_retention_partitions": (
        "only the migration / retention-maintenance role manages partitions; "
        "the application role never queries the bookkeeping table"
    ),
}

# The three CHECK constraints of ``audit_events`` as they exist today (Migration
# 0025's ``decision_valid``, Migration 0087's ``details_registered`` and
# ``external_send_details``), repeated here so the newly-created parent (and the
# archive parent) get them from the start. A migration is a frozen snapshot
# (see Migration 0087's own docstring): this repeats 0087's literals rather
# than importing ``paw_backend.authz.models``, and
# ``tests/test_retention_postgres.py`` fails if the two drift apart.
_DECISION_VALID = "decision IN ('allow', 'deny')"
_DETAILS_ACTIONS = ("research.external_send",)
_DETAILS_REGISTERED = (
    "details IS NULL OR (action IN ("
    + ", ".join(f"'{action}'" for action in _DETAILS_ACTIONS)
    + ") AND jsonb_typeof(details) = 'object' AND octet_length(details::text) <= 2048)"
)
_EXTERNAL_SEND_ACTION = "research.external_send"
_EXTERNAL_SEND_REASON = "send_authorized"
_EXTERNAL_SEND_KEYS = (
    "query_fingerprint",
    "query_chars",
    "provider_kinds",
    "withheld",
    "credentials_removed",
    "pieces_matched",
    "abstractions",
    "truncated",
)
_WITHHELD_KEYS = ("private_source", "private_memory", "raw_conversation", "secret")
_PROVIDER_KINDS = ("web", "docs", "github", "opencode")
_QUERY_CHARS_PATTERN = "'^([1-9][0-9]?|1[0-9]{2}|2[0-4][0-9]|25[0-6])$'"
_PIECES_PATTERN = "'^([0-9]|[12][0-9]|3[0-2])$'"
_COUNT_PATTERN = "'^(0|[1-9][0-9]{0,5}|1000000)$'"


def _keys(names: tuple[str, ...]) -> str:
    return "ARRAY[" + ", ".join(f"'{name}'" for name in names) + "]"


def _external_send_check() -> str:
    kind = "(" + "|".join(_PROVIDER_KINDS) + ")"
    more = len(_PROVIDER_KINDS) - 1
    kinds_pattern = f'\'^\\["{kind}"(, "{kind}"){{0,{more}}}\\]$\''
    conditions = [
        "decision = 'allow'",
        f"reason = '{_EXTERNAL_SEND_REASON}'",
        "project_id IS NOT NULL",
        "jsonb_typeof(details) = 'object'",
        f"details - {_keys(_EXTERNAL_SEND_KEYS)} = '{{}}'::jsonb",
        "jsonb_typeof(details -> 'query_fingerprint') = 'string'",
        "details ->> 'query_fingerprint' ~ '^sha256:[0-9a-f]{64}$'",
        "jsonb_typeof(details -> 'query_chars') = 'number'",
        f"details ->> 'query_chars' ~ {_QUERY_CHARS_PATTERN}",
        "jsonb_typeof(details -> 'provider_kinds') = 'array'",
        f"(details -> 'provider_kinds')::text ~ {kinds_pattern}",
        "jsonb_typeof(details -> 'withheld') = 'object'",
        f"(details -> 'withheld') - {_keys(_WITHHELD_KEYS)} = '{{}}'::jsonb",
    ]
    for label in _WITHHELD_KEYS:
        conditions.append(
            f"jsonb_typeof(details -> 'withheld' -> '{label}') = 'number'"
        )
        conditions.append(f"details -> 'withheld' ->> '{label}' ~ {_PIECES_PATTERN}")
    for name, pattern in (
        ("credentials_removed", _COUNT_PATTERN),
        ("pieces_matched", _PIECES_PATTERN),
        ("abstractions", _COUNT_PATTERN),
    ):
        conditions.append(f"jsonb_typeof(details -> '{name}') = 'number'")
        conditions.append(f"details ->> '{name}' ~ {pattern}")
    conditions.append("jsonb_typeof(details -> 'truncated') = 'boolean'")
    return (
        f"action <> '{_EXTERNAL_SEND_ACTION}' OR COALESCE("
        + " AND ".join(conditions)
        + ", false)"
    )


def _columns() -> list[sa.Column]:
    """The full column list of ``audit_events`` today, for a fresh ``CREATE TABLE``."""
    return [
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
        sa.Column("repo_acl", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("old_role", sa.Text(), nullable=True),
        sa.Column("new_role", sa.Text(), nullable=True),
        sa.Column("client_request_id", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(none_as_null=True), nullable=True),
    ]


def _decision_check() -> sa.CheckConstraint:
    """The one CHECK constraint that was never ``NOT VALID`` (Migration 0025).

    Named ``ck_audit_events_decision_valid`` on *every* table it is declared
    on (the live parent, the archive parent — never ``ck_<table>_...``):
    PostgreSQL only recognises a child's constraint as already satisfying a
    parent's identical one, and skips re-validating it, when the **names**
    match, not just the expressions (proved empirically: a differently-named
    but identical constraint makes ``ATTACH PARTITION`` fail with "child table
    is missing constraint ..."). A partition moves between ``audit_events``
    and ``audit_events_archive`` over its life (Decision 0027), so both
    parents must declare every constraint under the exact same name.
    """
    return sa.CheckConstraint(
        _DECISION_VALID, name=op.f("ck_audit_events_decision_valid")
    )


def _add_details_checks(table: str) -> None:
    """Add the two Migration-0087 CHECK constraints to ``table``, ``NOT VALID``.

    Only for ``audit_events``, and only *after* ``audit_events_p_legacy`` (which
    already carries its own, real-rows-behind-them ``NOT VALID`` copies) is
    attached: PostgreSQL refuses to attach a partition whose matching
    constraint is present but not validated under a parent whose own copy
    *is* already validated (as an empty, freshly-``CREATE TABLE``d parent's
    would trivially be) — the two would disagree about whether every row has
    been checked. Adding the constraint to the parent **after** the attach, in
    the same ``NOT VALID`` form and the same name Migration 0087 used, merges
    with the child's existing copy instead: neither side is scanned or
    validated (proved empirically against a real PostgreSQL 18; see
    ``tests/test_retention_postgres.py``).
    """
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT ck_audit_events_details_registered "
        f"CHECK ({_DETAILS_REGISTERED}) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT ck_audit_events_external_send_details "
        f"CHECK ({_external_send_check()}) NOT VALID"
    )


def _grant_operator_insert_on(table: str) -> None:
    """Re-grant Migration 0021's ``INSERT ON audit_events`` to the operator role.

    Migration 0021 grants this once, to whichever relation is named
    ``audit_events`` when 0021 itself runs (a role's grant is tied to the
    relation, not the name — renaming ``audit_events`` to
    ``audit_events_p_legacy`` here, below, carries that old grant with it, not
    to the new relation this migration creates under the old name). Without
    repeating the grant here, the server-local Owner commands
    (``PAW_OPERATOR_DATABASE_ROLE``) would lose the ability to write their own
    audit trail the moment this migration is applied (``tests/
    test_retention_postgres.py``, and the existing ``tests/
    test_owner_token_roles.py``, which failed exactly this way while writing
    this migration).
    """
    role = Settings().operator_database_role
    if role is None:
        return
    role = validate_role_name(role)
    context = op.get_context()
    if not context.as_sql:
        found = op.get_bind().execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        if found.first() is None:
            raise RuntimeError(
                "PAW_OPERATOR_DATABASE_ROLE names a PostgreSQL role that does not "
                "exist; create it before running the migration."
            )
    quoted = context.dialect.identifier_preparer.quote_identifier(role)
    op.execute(f"GRANT INSERT ON {table} TO {quoted}")


def _iso(moment) -> str:
    return moment.isoformat()


def _row_triggers(table: str) -> None:
    """The two row-level append-only triggers, on ``table`` itself.

    PostgreSQL clones a row-level trigger created on a partitioned table onto
    every partition that exists when it is created *and* onto every partition
    created or attached later — so creating these once, here, is enough for
    every partition ``AuditRetentionService`` ever makes.
    """
    op.execute(
        f"CREATE TRIGGER tr_audit_events_reject_update_delete "
        f"BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION paw_reject_audit_events_change()"
    )
    op.execute(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER "
        "tr_audit_events_reject_update_delete"
    )


def _force_recorded_at_trigger(table: str) -> None:
    """The recorded_at-forcing trigger (Migration 0025), on ``table`` itself.

    Only ever ``audit_events`` (the live parent): ``audit_events_archive``
    never receives a fresh INSERT (nothing writes to it directly), so it has
    no need to force anything.
    """
    op.execute(
        f"CREATE TRIGGER tr_audit_events_force_recorded_at BEFORE INSERT ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION paw_force_audit_events_recorded_at()"
    )
    op.execute(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER tr_audit_events_force_recorded_at"
    )


def _truncate_trigger(table: str) -> None:
    """The statement-level append-only trigger. **Not** cloned by PostgreSQL:

    create this one explicitly on every individual partition, never only on a
    parent (``AuditRetentionService`` does the same for every partition it
    creates later; see its docstring and Decision 0027's risk list).
    """
    op.execute(
        f"CREATE TRIGGER tr_audit_events_reject_truncate BEFORE TRUNCATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION paw_reject_audit_events_change()"
    )
    op.execute(
        f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER tr_audit_events_reject_truncate"
    )


def _fold_back_into_legacy_sql() -> str:
    """The dynamic-SQL body of ``downgrade()``'s fold-back, as one ``DO`` block.

    *Why one PL/pgSQL block instead of Python-side introspection*: which
    partitions exist, and under which parent, is exactly the kind of fact an
    offline (``--sql``) render cannot know — there is no live database to ask.
    A ``DO`` block defers that question to whenever the generated SQL is
    actually run (a real ``downgrade()``, or a human running the rendered
    script later): ``EXECUTE format(...)`` builds each statement from the
    catalog at that moment, online or not. It is one transaction either way
    (the block itself, plus everything ``downgrade()`` runs around it).

    Detaching ``audit_events_p_legacy`` first, before folding any other
    partition's rows into it, matters: detaching drops its *cloned* row-level
    triggers (in particular the recorded_at-forcing one — see the module
    docstring), so the ``INSERT ... SELECT`` that follows keeps every row's
    original ``recorded_at`` instead of overwriting it with "now".
    """
    return f"""
DO $$
DECLARE
    moved_partition text;
    holding_parent text;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = '{LEGACY}') THEN
        RAISE EXCEPTION '% is missing: it was purged, so this downgrade has '
            'nothing to fold history back into (recover by hand, as with '
            'Migration 0025''s own downgrade)', '{LEGACY}';
    END IF;

    SELECT inhparent::regclass::text INTO holding_parent
      FROM pg_inherits WHERE inhrelid = '{LEGACY}'::regclass;
    IF holding_parent IS NOT NULL THEN
        EXECUTE format('ALTER TABLE %I DETACH PARTITION {LEGACY}', holding_parent);
    END IF;
    ALTER TABLE {LEGACY} DROP CONSTRAINT ck_{LEGACY}_upper_bound;

    FOR moved_partition, holding_parent IN
        SELECT c.relname, i.inhparent::regclass::text
          FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent IN ('audit_events'::regclass, '{ARCHIVE}'::regclass)
    LOOP
        EXECUTE format(
            'ALTER TABLE %I DETACH PARTITION %I', holding_parent, moved_partition
        );
        EXECUTE format('INSERT INTO {LEGACY} SELECT * FROM %I', moved_partition);
        EXECUTE format('DROP TABLE %I', moved_partition);
    END LOOP;

    DROP TABLE {ARCHIVE};
    DROP TABLE audit_events;

    ALTER TABLE {LEGACY} DROP CONSTRAINT pk_{LEGACY};
    ALTER TABLE {LEGACY} ADD CONSTRAINT pk_audit_events PRIMARY KEY (id);
    DROP TRIGGER IF EXISTS tr_audit_events_reject_truncate ON {LEGACY};
    ALTER TABLE {LEGACY} RENAME TO audit_events;
    ALTER INDEX ix_{LEGACY}_occurred_at RENAME TO ix_audit_events_occurred_at;
END $$;
"""


def upgrade() -> None:
    # Python's own clock, not a live query (``SELECT now()`` against ``op.get_
    # bind()``): a partition *name* has to be a concrete identifier at the
    # moment this function runs, whether that is a real upgrade or an offline
    # ``--sql`` render (``op.get_bind().execute(...).scalar()`` returns ``None``
    # then, since nothing is actually sent to a server). Using the process
    # clock instead makes both paths agree, and differs from a real ``now()``
    # by, at most, the time this function takes to run.
    cutover = datetime.now(UTC)
    first_upper = next_month_start(cutover)
    first_name = partition_name(month_start(cutover))

    # 1. The 0025/0087 table becomes the partition holding every pre-existing row.
    op.execute(f"ALTER TABLE audit_events RENAME TO {LEGACY}")
    op.execute(f"ALTER TABLE {LEGACY} RENAME CONSTRAINT pk_audit_events TO pk_{LEGACY}")
    # The index keeps its old name through the table rename too; free the name
    # for the new parent's own (partitioned) index of the same name below.
    op.execute(
        f"ALTER INDEX ix_audit_events_occurred_at RENAME TO ix_{LEGACY}_occurred_at"
    )
    op.execute(
        f"ALTER TABLE {LEGACY} ADD CONSTRAINT ck_{LEGACY}_upper_bound "
        f"CHECK (recorded_at < '{_iso(cutover)}'::timestamptz)"
    )
    op.execute(f"ALTER TABLE {LEGACY} DROP CONSTRAINT pk_{LEGACY}")
    op.execute(
        f"ALTER TABLE {LEGACY} ADD CONSTRAINT pk_{LEGACY} PRIMARY KEY (id, recorded_at)"
    )
    # Its own pre-partitioning row triggers must go so the parent's (below) clone
    # a single, uniform copy onto it instead of colliding by name; its statement
    # trigger (reject-truncate) is not cloned either way, so it is left as-is.
    op.execute(f"DROP TRIGGER tr_audit_events_reject_update_delete ON {LEGACY}")
    op.execute(f"DROP TRIGGER tr_audit_events_force_recorded_at ON {LEGACY}")

    # 2. The new partitioned parent, same shape, same append-only protection.
    # The two Migration-0087 CHECK constraints are added *after* the attach
    # below (``_add_details_checks``), not here: ``audit_events_p_legacy``
    # already has its own real-rows-behind-them ``NOT VALID`` copies, and
    # ``ATTACH PARTITION`` refuses a child whose matching constraint is
    # present but not validated under a parent whose own (trivially, being
    # empty) already counts as validated.
    op.create_table(
        "audit_events",
        *_columns(),
        _decision_check(),
        sa.PrimaryKeyConstraint("id", "recorded_at", name=op.f("pk_audit_events")),
        postgresql_partition_by="RANGE (recorded_at)",
    )
    op.create_index(
        op.f("ix_audit_events_occurred_at"), "audit_events", ["occurred_at"]
    )
    grant_app_privileges(op, "audit_events", select=True, insert=True)
    _grant_operator_insert_on("audit_events")

    op.execute(
        f"ALTER TABLE audit_events ATTACH PARTITION {LEGACY} "
        f"FOR VALUES FROM (MINVALUE) TO ('{_iso(cutover)}'::timestamptz)"
    )
    op.execute(
        f"CREATE TABLE {first_name} PARTITION OF audit_events "
        f"FOR VALUES FROM ('{_iso(cutover)}'::timestamptz) "
        f"TO ('{_iso(first_upper)}'::timestamptz)"
    )
    _add_details_checks("audit_events")

    _row_triggers("audit_events")
    _force_recorded_at_trigger("audit_events")
    # The statement-level trigger on the parent covers a TRUNCATE that names the
    # parent directly; a TRUNCATE naming one partition needs its own copy
    # (PostgreSQL does not clone a statement-level trigger — see the module
    # docstring), so the first live partition gets one explicitly too.
    _truncate_trigger("audit_events")
    _truncate_trigger(first_name)

    # 3. The archive parent: empty, ready, protected from the moment anything
    # is ever attached to it. It does *not* declare the two Migration-0087
    # constraints (unlike ``audit_events``): a fresh, childless parent's own
    # copy of a NOT VALID constraint is trivially "validated" (nothing exists
    # yet to violate it), which then conflicts with the real, still-unvalidated
    # copy on whatever partition is archived here later (same failure as
    # attaching legacy — see ``_add_details_checks``, and there is no
    # partition to attach first here the way legacy lets ``audit_events`` defer
    # to). The archived partition keeps enforcing its own copy regardless
    # (CHECK constraints are not affected by DETACH / ATTACH); the parent
    # simply does not redeclare it. ``decision_valid`` was never ``NOT VALID``
    # and has no such conflict, so it stays.
    op.create_table(
        "audit_events_archive",
        *_columns(),
        _decision_check(),
        sa.PrimaryKeyConstraint("id", "recorded_at", name=op.f(f"pk_{ARCHIVE}")),
        postgresql_partition_by="RANGE (recorded_at)",
    )
    grant_app_privileges(op, "audit_events_archive", select=True)
    _row_triggers(ARCHIVE)
    _truncate_trigger(ARCHIVE)

    # 4. Bookkeeping: one row per partition that exists right now.
    op.create_table(
        "audit_retention_partitions",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("lower_bound", sa.DateTime(timezone=True), nullable=True),
        sa.Column("upper_bound", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("name", name=op.f(f"pk_{BOOKKEEPING}")),
        sa.CheckConstraint(
            "status IN ('live', 'archived', 'purged')",
            name=op.f(f"ck_{BOOKKEEPING}_status_valid"),
        ),
    )
    op.execute(f'REVOKE ALL ON "{BOOKKEEPING}" FROM PUBLIC')
    op.execute(
        sa.text(
            f"INSERT INTO {BOOKKEEPING} "
            "(name, lower_bound, upper_bound, status, created_at) VALUES "
            "(:name, NULL, :upper, 'live', :now)"
        ).bindparams(name=LEGACY, upper=cutover, now=cutover)
    )
    op.execute(
        sa.text(
            f"INSERT INTO {BOOKKEEPING} "
            "(name, lower_bound, upper_bound, status, created_at) VALUES "
            "(:name, :lower, :upper, 'live', :now)"
        ).bindparams(name=first_name, lower=cutover, upper=first_upper, now=cutover)
    )


def downgrade() -> None:
    # DESTROYS the partition / archive shape and, for anything not folded back
    # into the legacy table, its cloned append-only triggers along the way
    # (development / test only — see the module docstring). The fold-back
    # itself is one dynamic-SQL block (``_fold_back_into_legacy_sql``): which
    # partitions exist cannot be known from Python when this is rendered
    # offline (``--sql``), only from whatever database the generated script is
    # later run against.
    op.execute(_fold_back_into_legacy_sql())
    op.drop_table(BOOKKEEPING)

    _row_triggers("audit_events")
    _force_recorded_at_trigger("audit_events")
    _truncate_trigger("audit_events")
