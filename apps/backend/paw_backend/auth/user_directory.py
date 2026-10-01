"""The workspace's users, for the administration screens (issue #187).

A read of ``users`` only: id, login name, system role, status and when the account
was created. No credential, session, Passkey or e-mail (there is none). Who may read
it is the HTTP layer's check (``GET /api/v1/admin/users``: ``admin.users.manage``,
Decision 0069, Proposed); this module does not authorize.

A deleted user (``status = 'deleted'``) is not listed: the account is gone and
cannot be restored (Decision 0033). One pending deletion is (the Owner may still
restore it).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from paw_backend.db import Database

# Owner first, then the Admins, then the Users; by login name within a role.
LIST_USERS_SQL = (
    "SELECT id, login_name, system_role, status, created_at FROM users"
    " WHERE status <> 'deleted'"
    " ORDER BY CASE system_role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1"
    " ELSE 2 END, login_name"
)


@dataclass(frozen=True, slots=True)
class UserSummary:
    user_id: uuid.UUID
    login_name: str
    system_role: str
    status: str
    created_at: datetime


async def list_users(
    database: Database, *, timeout_seconds: float
) -> tuple[UserSummary, ...]:
    """Every user that is not deleted. ``TimeoutError`` at the deadline."""
    rows = await database.fetch_abortable(
        LIST_USERS_SQL, {}, timeout_seconds=timeout_seconds
    )
    return tuple(UserSummary(*row) for row in rows)


async def user_exists(
    database: Database, user_id: uuid.UUID, *, timeout_seconds: float
) -> bool:
    """Whether a user row exists (any status, a deleted one too)."""
    rows = await database.fetch_abortable(
        "SELECT 1 FROM users WHERE id = %(id)s",
        {"id": user_id},
        timeout_seconds=timeout_seconds,
    )
    return bool(rows)
