"""Privileges of the application's database role, for Alembic migrations.

In the documented split-role deployment the migrations run as the owner of the
schema (``PAW_MIGRATION_DATABASE_URL``) and the backend connects as a different,
unprivileged role (``PAW_APP_DATABASE_ROLE``). Tables are owned by the
migration role, so the application role gets ``permission denied`` unless the
migration that creates a table grants it exactly what it needs. **Every
migration that creates a table calls** :func:`grant_app_privileges` **for it**
(``apps/backend/README.md``; ``tests/test_migration_grants.py`` enforces this).

This module has no dependency on the application, so a version file can import
it: ``from paw_backend.db_roles import grant_app_privileges``.
"""

import logging
import os
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

logger = logging.getLogger(__name__)

APP_ROLE_VARIABLE = "PAW_APP_DATABASE_ROLE"
MIGRATION_URL_VARIABLE = "PAW_MIGRATION_DATABASE_URL"

# A plain identifier. Anything else (quotes, spaces, semicolons, ...) is refused
# before it gets near SQL; accepted names are still quoted when used.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
# Names PostgreSQL gives a special meaning even when quoted: "public" is the
# pseudo-role PUBLIC (everyone), and pg_* / postgres are reserved or built-in.
_RESERVED_ROLES = frozenset(
    {
        "public",
        "none",
        "postgres",
        "user",
        "current_user",
        "current_role",
        "session_user",
    }
)


class AppRoleNotFoundError(RuntimeError):
    """``PAW_APP_DATABASE_ROLE`` names a role that does not exist."""


def validate_role_name(value: str) -> str:
    """Return ``value`` if it is a usable application role name, else ``ValueError``.

    Letters, digits and underscore (63 characters at most); not ``public``
    (a quoted "public" is still the pseudo-role PUBLIC, so granting to it would
    grant everyone), not ``pg_*`` and not ``postgres`` or another reserved
    name. The value is not echoed in the error.
    """
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
        or value.lower() in _RESERVED_ROLES
        or value.lower().startswith("pg_")
    ):
        raise ValueError("app_database_role is not a valid PostgreSQL role name")
    return value


def configured_app_role() -> str | None:
    """The validated ``PAW_APP_DATABASE_ROLE``; ``None`` when unset or empty."""
    value = os.environ.get(APP_ROLE_VARIABLE, "")
    return validate_role_name(value) if value else None


def _identifier(name: str, what: str) -> str:
    if not isinstance(name, str) or _IDENTIFIER.fullmatch(name) is None:
        # The offending value is deliberately not echoed.
        raise ValueError(f"{what} is not a plain identifier")
    return name


def grant_app_privileges(
    op: Any,
    table: str,
    *,
    select: bool = True,
    insert: bool = False,
    update: bool = False,
    delete: bool = False,
    update_columns: Sequence[str] | None = None,
) -> str | None:
    """Give the application role exactly the requested privileges on ``table``.

    Call it right after ``op.create_table(table, ...)`` with the **least**
    privileges the application needs: ``select`` by default, ``insert`` /
    ``update`` / ``delete`` only when asked. ``update_columns=("a", "b")``
    grants ``UPDATE (a, b)`` on those columns only (and excludes ``update``).
    DELETE and TRUNCATE are never granted unless ``delete=True`` (TRUNCATE
    never); nothing else (ALTER, DROP, triggers, rules, GRANT OPTION) is ever
    given, so the application cannot change the schema.

    ``PAW_APP_DATABASE_ROLE`` is read from the environment. When it is unset
    (single-role development) no privilege is granted and nothing is raised;
    ``PUBLIC`` is still stripped of every privilege on the table. When it is
    set it is validated (:func:`validate_role_name`), and online it must exist
    (:class:`AppRoleNotFoundError` otherwise, before anything is executed).
    Identifiers are validated and quoted by the dialect; no name is ever
    formatted into SQL as text.

    ``op`` is Alembic's ``op`` (or an ``Operations`` object). Returns the role
    that was granted, or ``None``.
    """
    table = _identifier(table, "table")
    if isinstance(update_columns, str | bytes):
        raise TypeError("update_columns must be a sequence of column names")
    columns = None if update_columns is None else list(update_columns)
    if columns is not None:
        if update:
            raise ValueError("use either update=True or update_columns, not both")
        if not columns:
            raise ValueError("update_columns must not be empty")
        columns = [_identifier(column, "column") for column in columns]
    if not (select or insert or update or delete or columns):
        raise ValueError("no privilege requested")

    role = configured_app_role()
    context = op.get_context()
    preparer = context.dialect.identifier_preparer
    quote = preparer.quote_identifier  # always quoted: a role name keeps its case
    quoted_table = preparer.quote(table)  # quoted only where PostgreSQL needs it

    if role is None:
        if os.environ.get(MIGRATION_URL_VARIABLE):
            logger.warning(
                "%s is set but %s is not: no role is granted access to %s, so the "
                "application (a different role) cannot use it.",
                MIGRATION_URL_VARIABLE,
                APP_ROLE_VARIABLE,
                table,
            )
    elif not context.as_sql:  # online: fail loudly, before changing anything
        found = op.get_bind().execute(
            sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
        )
        if found.first() is None:
            raise AppRoleNotFoundError(
                f"{APP_ROLE_VARIABLE} names a PostgreSQL role that does not exist; "
                "create it before running the migration."
            )

    op.execute(f"REVOKE ALL ON {quoted_table} FROM PUBLIC")
    if role is None:
        return None

    quoted_role = quote(role)
    privileges = [
        name
        for name, wanted in (  # alphabetical: one canonical spelling
            ("DELETE", delete),
            ("INSERT", insert),
            ("SELECT", select),
            ("UPDATE", update),
        )
        if wanted
    ]
    if privileges:
        op.execute(f"GRANT {', '.join(privileges)} ON {quoted_table} TO {quoted_role}")
    if columns:
        listed = ", ".join(preparer.quote(column) for column in columns)
        op.execute(f"GRANT UPDATE ({listed}) ON {quoted_table} TO {quoted_role}")
    return role
