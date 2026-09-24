"""Startup diagnostic: can the application's database user rewrite the audit trail?"""

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
