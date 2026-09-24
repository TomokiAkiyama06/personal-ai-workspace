"""Startup diagnostics: can the application's database user rewrite its own evidence?

``audit_events`` (PAW-025) and the tool approval tables (PAW-031) are protected
by triggers, and, when the application connects as a role other than the one
that runs the migrations, by privileges. These checks only *warn* when the
connected user could get around that.
"""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import text

from paw_backend.db import Database

logger = logging.getLogger(__name__)

_QUERY = text(
    """
    SELECT pg_has_role(current_user, c.relowner, 'MEMBER') AS owns,
           has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
           has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
           has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
           has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate
    FROM pg_class c
    WHERE c.oid = to_regclass('audit_events')
    """
)


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


async def read_audit_table_access(database: Database) -> AuditTableAccess | None:
    """The access of the connected user, or ``None`` if the table does not exist."""
    async with database.session() as session:
        row = (await session.execute(_QUERY)).one_or_none()
    if row is None:
        return None
    return AuditTableAccess(*row)


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
        async with asyncio.timeout(timeout_seconds):
            access = await read_audit_table_access(database)
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
_TOOL_QUERY = text(
    """
    SELECT c.relname AS name,
           pg_has_role(current_user, c.relowner, 'MEMBER') AS owns,
           has_table_privilege(current_user, c.oid, 'INSERT') AS can_insert,
           has_table_privilege(current_user, c.oid, 'UPDATE') AS can_update,
           has_any_column_privilege(current_user, c.oid, 'UPDATE') AS can_update_some,
           has_table_privilege(current_user, c.oid, 'DELETE') AS can_delete,
           has_table_privilege(current_user, c.oid, 'TRUNCATE') AS can_truncate
    FROM pg_class c
    WHERE c.oid = ANY (ARRAY[to_regclass('tool_approvals'),
                             to_regclass('tool_approval_events')])
    """
)


@dataclass(frozen=True, slots=True)
class ToolTableAccess:
    """What the connected database user may do to one tool approval table.

    ``can_update`` is the privilege on the whole table; ``can_update_some`` is
    UPDATE of at least one column (the state columns of ``tool_approvals``).
    """

    name: str
    owns: bool
    can_insert: bool
    can_update: bool
    can_update_some: bool
    can_delete: bool
    can_truncate: bool

    @property
    def cannot_work(self) -> bool:
        """The application cannot request or use approvals with this access."""
        if not self.can_insert:
            return True
        return self.name == "tool_approvals" and not self.can_update_some

    @property
    def protected(self) -> bool:
        """Only what the application needs: no ownership, no DELETE / TRUNCATE,
        no whole-table UPDATE (the history table gets no UPDATE at all)."""
        return not (
            self.owns or self.can_update or self.can_delete or self.can_truncate
        ) and (self.name == "tool_approvals" or not self.can_update_some)


async def read_tool_table_access(database: Database) -> dict[str, ToolTableAccess]:
    """The access of the connected user to each existing tool approval table."""
    async with database.session() as session:
        rows = (await session.execute(_TOOL_QUERY)).all()
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
        async with asyncio.timeout(timeout_seconds):
            access = await read_tool_table_access(database)
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
                ", UPDATE of the state columns" if name == "tool_approvals" else "",
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
