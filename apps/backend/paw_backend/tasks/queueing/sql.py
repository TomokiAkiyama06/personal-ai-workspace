"""Small helpers to tell database errors apart without reading their text.

The services translate a few PostgreSQL error codes into typed errors. They look
at the SQLSTATE and the constraint name only: the message of a driver error may
contain values from the statement and must never be shown or logged.
"""

from sqlalchemy.exc import DBAPIError

FOREIGN_KEY_VIOLATION = "23503"
UNIQUE_VIOLATION = "23505"


def sqlstate(error: DBAPIError) -> str | None:
    """The SQLSTATE of the driver error behind ``error`` (``None`` if unknown)."""
    return getattr(error.orig, "sqlstate", None)


def constraint_name(error: DBAPIError) -> str | None:
    """The violated constraint's name reported by the server, if any."""
    diagnostics = getattr(error.orig, "diag", None)
    return getattr(diagnostics, "constraint_name", None)
