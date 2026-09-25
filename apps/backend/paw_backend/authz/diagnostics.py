"""Startup diagnostics: can the application's database user rewrite its own evidence?

``audit_events`` (PAW-025) and the tool approval tables (PAW-031) are protected
by triggers, and, when the application connects as a role other than the one
that runs the migrations, by privileges. These checks only *warn* when the
connected user could get around that.
"""

import logging
from dataclasses import dataclass

from paw_backend.db import Database

logger = logging.getLogger(__name__)

_QUERY = """
    SELECT pg_has_role(current_user, c.relowner, 'MEMBER') AS owns,
           has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
           has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
           has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
           has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate
    FROM pg_class c
    WHERE c.oid = to_regclass('audit_events')
    """


@dataclass(frozen=True, slots=True)
class AuditTableAccess:
    """What the connected database user may do to ``audit_events``."""

    owns: bool
    can_insert: bool
    can_update: bool
    can_delete: bool
    can_truncate: bool

    @property
    def cannot_write(self) -> bool:
        """True when the user cannot append: every audited action would be refused."""
        return not self.can_insert

    @property
    def protected(self) -> bool:
        """True when only INSERT / SELECT are possible and the table is not owned."""
        return not (
            self.owns or self.can_update or self.can_delete or self.can_truncate
        )


async def read_audit_table_access(
    database: Database, timeout_seconds: float | None = None
) -> AuditTableAccess | None:
    """The access of the connected user, or ``None`` if the table does not exist.

    The query runs on a dedicated connection that is aborted, never cancelled
    on the server, when the time is up, the caller is cancelled or the
    database is disposed (``Database.fetch_abortable``): a stalled PostgreSQL
    must not be able to hold up shutdown.
    """
    rows = await database.fetch_abortable(_QUERY, timeout_seconds=timeout_seconds)
    return AuditTableAccess(*rows[0]) if rows else None


async def warn_if_audit_table_is_mutable(
    database: Database, timeout_seconds: float
) -> None:
    """Log a WARNING when the application user could bypass the append-only guard.

    Never raises and never fails startup: a warning only. It logs no role or
    connection details. If PostgreSQL cannot be reached, nothing is checked.
    """
    if not database.configured:
        return
    try:
        access = await read_audit_table_access(database, timeout_seconds)
    except Exception as error:
        logger.info("Audit table privilege check skipped (%s)", type(error).__name__)
        return
    if access is not None and access.cannot_write:
        logger.warning(
            "The application's database user cannot INSERT into audit_events: the "
            "audit trail cannot be written, so every audited action will be "
            "refused (503). Grant INSERT and SELECT to the application's role "
            "(run the migration with PAW_APP_DATABASE_ROLE set)."
        )
    if access is not None and not access.protected:
        logger.warning(
            "The application's database user is not restricted on audit_events "
            "(owner=%s update=%s delete=%s truncate=%s): the append-only guard "
            "can be bypassed. Run migrations with PAW_MIGRATION_DATABASE_URL and "
            "the application as the role named by PAW_APP_DATABASE_ROLE.",
            access.owns,
            access.can_update,
            access.can_delete,
            access.can_truncate,
        )


# --- tool approvals (PAW-031) -------------------------------------------------

_TOOL_TABLES = ("tool_approvals", "tool_approval_events")
# The columns of ``tool_approvals`` that ``PostgresApprovalStore`` sets (decide,
# consume, revoke, expire): the application needs UPDATE on EVERY one of them,
# because a missing one makes the transition that sets it fail with a permission
# error. Migration 0031 grants exactly these; tests check the list against the
# store's SQL and against the migrated grants.
APPROVAL_STATE_COLUMNS = (
    "status",
    "approver_id",
    "decided_at",
    "step_up_verified",
    "consumed_at",
    "revoked_at",
    "revoked_by",
)
_STATE_COLUMN_NAMES = ", ".join(f"'{name}'" for name in APPROVAL_STATE_COLUMNS)
_TOOL_QUERY = f"""
    SELECT c.relname AS name,
           pg_has_role(current_user, c.relowner, 'MEMBER') AS owns,
           has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
           has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
           has_any_column_privilege(current_user, c.oid, 'UPDATE') AS can_update_some,
           (SELECT count(*) = {len(APPROVAL_STATE_COLUMNS)}
              FROM pg_attribute a
             WHERE a.attrelid = c.oid
               AND a.attname IN ({_STATE_COLUMN_NAMES})
               AND NOT a.attisdropped
               AND has_column_privilege(current_user, c.oid, a.attnum, 'UPDATE')
           ) AS can_update_state,
           has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
           has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate
    FROM pg_class c
    WHERE c.oid = ANY (ARRAY[to_regclass('tool_approvals'),
                             to_regclass('tool_approval_events')])
"""


@dataclass(frozen=True, slots=True)
class ToolTableAccess:
    """What the connected database user may do to one tool approval table.

    ``can_update`` is the privilege on the whole table; ``can_update_some`` is
    UPDATE of at least one column; ``can_update_state`` is UPDATE of EVERY
    column in ``APPROVAL_STATE_COLUMNS`` (through the table or column by column;
    always false for the history table, which has no such columns).
    """

    name: str
    owns: bool
    can_insert: bool
    can_update: bool
    can_update_some: bool
    can_update_state: bool
    can_delete: bool
    can_truncate: bool

    @property
    def cannot_work(self) -> bool:
        """The application cannot request or use approvals with this access.

        INSERT is needed on both tables; ``tool_approvals`` also needs UPDATE of
        all of its state columns, not just of one of them (``UPDATE(status)``
        alone lets a request through and then fails every decision).
        """
        if not self.can_insert:
            return True
        return self.name == "tool_approvals" and not self.can_update_state

    @property
    def protected(self) -> bool:
        """Only what the application needs: no ownership, no DELETE / TRUNCATE,
        no whole-table UPDATE (the history table gets no UPDATE at all)."""
        return not (
            self.owns or self.can_update or self.can_delete or self.can_truncate
        ) and (self.name == "tool_approvals" or not self.can_update_some)


async def read_tool_table_access(
    database: Database, timeout_seconds: float | None = None
) -> dict[str, ToolTableAccess]:
    """The access of the connected user to each existing tool approval table.

    Like ``read_audit_table_access``, on a dedicated connection that is aborted
    (never cancelled on the server) at the deadline (``Database.fetch_abortable``).
    """
    rows = await database.fetch_abortable(_TOOL_QUERY, timeout_seconds=timeout_seconds)
    return {row[0]: ToolTableAccess(*row) for row in rows}


async def warn_if_tool_approval_tables_are_mutable(
    database: Database, timeout_seconds: float
) -> None:
    """Log a WARNING when the application user could bypass the approval guards.

    Never raises and never fails startup; logs no role or connection detail. If
    PostgreSQL cannot be reached, or the tables do not exist, nothing is checked.
    """
    if not database.configured:
        return
    try:
        access = await read_tool_table_access(database, timeout_seconds)
    except Exception as error:
        logger.info(
            "Tool approval table privilege check skipped (%s)", type(error).__name__
        )
        return
    for name in _TOOL_TABLES:
        table = access.get(name)
        if table is None:
            continue
        if table.cannot_work:
            logger.warning(
                "The application's database user cannot use %s (INSERT%s missing): "
                "tool approvals cannot be requested or used, so every call that "
                "needs an approval will be refused. Run the migration with "
                "PAW_APP_DATABASE_ROLE set to the application's role.",
                name,
                " or UPDATE of a state column" if name == "tool_approvals" else "",
            )
        if not table.protected:
            logger.warning(
                "The application's database user is not restricted on %s "
                "(owner=%s update=%s delete=%s truncate=%s): the approval guards "
                "(state machine, append-only history) can be bypassed. Run "
                "migrations with PAW_MIGRATION_DATABASE_URL and the application as "
                "the role named by PAW_APP_DATABASE_ROLE.",
                name,
                table.owns,
                table.can_update,
                table.can_delete,
                table.can_truncate,
            )


async def warn_about_loose_privileges(
    database: Database, timeout_seconds: float
) -> None:
    """The startup check: the audit trail, then the tool approval tables."""
    await warn_if_audit_table_is_mutable(database, timeout_seconds)
    await warn_if_tool_approval_tables_are_mutable(database, timeout_seconds)
