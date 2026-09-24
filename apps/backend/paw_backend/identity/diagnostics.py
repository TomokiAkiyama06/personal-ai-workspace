"""Startup diagnostic: can the application's database user mint an Owner token?

Whoever can INSERT into ``setup_tokens``, or change a user's role, can take over
the Owner. Migration ``0021`` gives the web application's role neither (that is
the operator's, ``PAW_OPERATOR_DATABASE_ROLE``); this warns when the role the
application actually connects as could, for instance in a single-role setup.
"""

import asyncio
import logging
from dataclasses import astuple, dataclass

from sqlalchemy import text

from paw_backend.db import Database

logger = logging.getLogger(__name__)

_QUERY = text(
    """
    SELECT
      pg_has_role(current_user, c.relowner, 'MEMBER') AS owns,
      has_table_privilege(current_user, 'setup_tokens', 'INSERT') AS insert_tokens,
      has_table_privilege(current_user, 'users', 'INSERT') AS insert_users,
      has_table_privilege(current_user, 'users', 'DELETE') AS delete_users,
      has_column_privilege(current_user, 'setup_tokens', 'secret_hash', 'UPDATE')
        AS update_hash,
      has_column_privilege(current_user, 'setup_tokens', 'revoked_at', 'UPDATE')
        AS update_revoked,
      has_column_privilege(current_user, 'users', 'system_role', 'UPDATE')
        AS update_role
    FROM pg_class c
    WHERE c.oid = to_regclass('setup_tokens')
    """
)


@dataclass(frozen=True, slots=True)
class TokenTableAccess:
    """What the connected database user may do to the Owner-token tables."""

    owns: bool
    insert_tokens: bool
    insert_users: bool
    delete_users: bool
    update_hash: bool
    update_revoked: bool
    update_role: bool

    @property
    def can_mint_owner_token(self) -> bool:
        """True when the user could forge a token or change who the Owner is."""
        return any(astuple(self))


async def read_token_table_access(database: Database) -> TokenTableAccess | None:
    """The access of the connected user, or ``None`` if the tables do not exist."""
    async with database.session() as session:
        row = (await session.execute(_QUERY)).one_or_none()
    if row is None:
        return None
    return TokenTableAccess(*row)


async def warn_if_tokens_can_be_minted(
    database: Database, timeout_seconds: float
) -> None:
    """Log a WARNING when the application user could mint an Owner token.

    Never raises and never fails startup: a warning only. It logs no role or
    connection details. If PostgreSQL cannot be reached, nothing is checked.
    """
    if not database.configured:
        return
    try:
        async with asyncio.timeout(timeout_seconds):
            access = await read_token_table_access(database)
    except Exception as error:
        logger.info("Owner token privilege check skipped (%s)", type(error).__name__)
        return
    if access is not None and access.can_mint_owner_token:
        logger.warning(
            "The application's database user can create Owner tokens or change "
            "roles (owner=%s insert_tokens=%s insert_users=%s update_role=%s "
            "update_hash=%s update_revoked=%s): a compromise of the application "
            "could take over the Owner. Run migrations with "
            "PAW_MIGRATION_DATABASE_URL, the application as PAW_APP_DATABASE_ROLE "
            "and the Owner commands as PAW_OPERATOR_DATABASE_ROLE "
            "(PAW_OPERATOR_DATABASE_URL).",
            access.owns,
            access.insert_tokens,
            access.insert_users,
            access.update_role,
            access.update_hash,
            access.update_revoked,
        )
