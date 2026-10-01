"""Stored notifications in PostgreSQL (issue #188, Decision 0070 Proposed).

Producers add a notification inside their own transaction (:func:`add_in`,
:func:`resolve_in`): the notification commits with what it reports, or not at
all. The routes read and change one user's view through :class:`NotificationStore`.

What a user sees is decided in SQL, on every statement, from the user's id and
the audiences their role holds now (``domain.audience_capabilities``): their own
notifications and those of an audience they belong to, not resolved. Anything
else (another user's notification, an audience they left) is invisible: reading
it, marking it read or dismissing it behaves as if it did not exist.

* :meth:`NotificationStore.page`: the newest notifications that are not
  dismissed, and how many are unread in all.
* :meth:`NotificationStore.mark_read`: the given visible notifications (or every
  visible one) become read for this user; the others are ignored. How many are
  unread after it is counted in the same transaction.
* :meth:`NotificationStore.dismiss`: the notification and the earlier ones of its
  key (one entry of the Notification Center) are dismissed (and read) for this
  user. A newer notification of the key is listed again.
* :meth:`NotificationStore.purge`: notifications older than the retention period
  go (their receipts with them).
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence

import psycopg
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database, DatabaseDisposedError, DatabaseNotConfiguredError
from paw_backend.notifications.domain import (
    Category,
    NewNotification,
    NotificationPage,
    ReadResult,
    Severity,
    StoredNotification,
    params_json,
)

logger = logging.getLogger(__name__)

# The newest notifications a page holds at most (and by default).
MAX_PAGE = 200
DEFAULT_PAGE = 100
# How many ids one "mark read" names at most.
MAX_READ_IDS = 200
# Notifications are kept this long after they were created (Decision 0070, 7).
RETENTION_DAYS = 90
MAX_PURGE_ROWS = 1000


class NotificationsUnavailableError(Exception):
    """PostgreSQL did not answer in time or failed (only the type is logged)."""


# The notifications ``:user`` sees: theirs and their audiences', not resolved.
_VISIBLE = """
n.resolved_at IS NULL
AND (n.recipient_user_id = :user
     OR n.audience_capability = ANY(CAST(:audiences AS text[])))
"""

_PAGE = f"""
SELECT n.id, n.key, n.kind, n.severity, n.category, n.project_id, n.params,
       n.created_at, r.read_at IS NOT NULL AS read
FROM notifications n
LEFT JOIN notification_receipts r ON r.notification_id = n.id AND r.user_id = :user
WHERE {_VISIBLE} AND r.dismissed_at IS NULL
ORDER BY n.created_at DESC, n.id DESC
LIMIT :limit
"""

_UNREAD = f"""
SELECT count(*)
FROM notifications n
LEFT JOIN notification_receipts r ON r.notification_id = n.id AND r.user_id = :user
WHERE {_VISIBLE} AND r.read_at IS NULL
"""

_MARK_READ = f"""
INSERT INTO notification_receipts AS r (notification_id, user_id, read_at)
SELECT n.id, :user, now()
FROM notifications n
WHERE {_VISIBLE} AND (CAST(:every AS boolean) OR n.id = ANY(CAST(:ids AS uuid[])))
ON CONFLICT (notification_id, user_id) DO UPDATE SET read_at = EXCLUDED.read_at
WHERE r.read_at IS NULL
RETURNING 1
"""

_TARGET = f"""
SELECT n.key, n.created_at FROM notifications n WHERE n.id = :id AND {_VISIBLE}
"""

_DISMISS = f"""
INSERT INTO notification_receipts AS r
    (notification_id, user_id, read_at, dismissed_at)
SELECT n.id, :user, now(), now()
FROM notifications n
WHERE {_VISIBLE} AND n.key = :key AND n.created_at <= :created_at
ON CONFLICT (notification_id, user_id) DO UPDATE SET
    read_at = COALESCE(r.read_at, EXCLUDED.read_at),
    dismissed_at = COALESCE(r.dismissed_at, EXCLUDED.dismissed_at)
"""

_ADD = """
INSERT INTO notifications
    (key, kind, severity, category, recipient_user_id, audience_capability,
     project_id, params)
VALUES (:key, :kind, :severity, :category, :recipient, :audience, :project,
        CAST(:params AS jsonb))
RETURNING id
"""

_RESOLVE = """
UPDATE notifications SET resolved_at = now()
WHERE key = :key AND resolved_at IS NULL
RETURNING id
"""

_PURGE = """
WITH doomed AS (
    SELECT id FROM notifications
    WHERE created_at < now() - make_interval(days => :days)
    ORDER BY created_at
    LIMIT :limit
), gone AS (
    DELETE FROM notifications WHERE id IN (SELECT id FROM doomed) RETURNING 1
)
SELECT count(*) FROM gone
"""


async def add_in(session: AsyncSession, notification: NewNotification) -> uuid.UUID:
    """Add ``notification`` in the caller's transaction; return its id."""
    if not isinstance(notification, NewNotification):
        raise TypeError("notification must be a NewNotification")
    result = await session.execute(
        text(_ADD),
        {
            "key": notification.key,
            "kind": notification.kind,
            "severity": notification.severity.value,
            "category": notification.category.value,
            "recipient": notification.recipient_user_id,
            "audience": None
            if notification.audience_capability is None
            else notification.audience_capability.value,
            "project": notification.project_id,
            "params": params_json(notification.params),
        },
    )
    return result.scalar_one()


async def resolve_in(session: AsyncSession, key: str) -> int:
    """Resolve every open notification of ``key`` in the caller's transaction;
    return how many."""
    result = await session.execute(text(_RESOLVE), {"key": key})
    return len(result.fetchall())


async def purge_in(session: AsyncSession, *, days: int = RETENTION_DAYS) -> int:
    """Delete at most ``MAX_PURGE_ROWS`` notifications older than ``days``."""
    result = await session.execute(
        text(_PURGE), {"days": days, "limit": MAX_PURGE_ROWS}
    )
    return int(result.scalar_one())


def _params(value: object) -> dict:
    if isinstance(value, str):  # a driver that hands JSON over as text
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


class NotificationStore:
    def __init__(self, database: Database, *, timeout_seconds: float) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._timeout = float(timeout_seconds)

    async def _run(self, work):
        try:
            async with asyncio.timeout(self._timeout):
                return await self._database.run_abortable(work)
        except TimeoutError:
            logger.warning("Notification database work timed out")
            raise NotificationsUnavailableError from None
        except (DatabaseNotConfiguredError, DatabaseDisposedError):
            raise NotificationsUnavailableError from None
        except (SQLAlchemyError, psycopg.Error, OSError) as error:
            # Only the type: a driver message can name the host or the user.
            logger.warning(
                "Notification database work failed (%s)", type(error).__name__
            )
            raise NotificationsUnavailableError from None

    async def page(
        self,
        user_id: uuid.UUID,
        audiences: Sequence[str],
        *,
        limit: int = DEFAULT_PAGE,
    ) -> NotificationPage:
        if not 1 <= limit <= MAX_PAGE:
            raise ValueError("limit is out of range")
        who = {"user": user_id, "audiences": list(audiences)}

        async def work(session: AsyncSession) -> NotificationPage:
            rows = (await session.execute(text(_PAGE), {**who, "limit": limit})).all()
            unread = (await session.execute(text(_UNREAD), who)).scalar_one()
            return NotificationPage(
                items=tuple(
                    StoredNotification(
                        id=row.id,
                        key=row.key,
                        kind=row.kind,
                        severity=Severity(row.severity),
                        category=Category(row.category),
                        project_id=row.project_id,
                        params=_params(row.params),
                        created_at=row.created_at,
                        read=bool(row.read),
                    )
                    for row in rows
                ),
                unread=int(unread),
            )

        return await self._run(work)

    async def unread(self, user_id: uuid.UUID, audiences: Sequence[str]) -> int:
        who = {"user": user_id, "audiences": list(audiences)}

        async def work(session: AsyncSession) -> int:
            return int((await session.execute(text(_UNREAD), who)).scalar_one())

        return await self._run(work)

    async def mark_read(
        self,
        user_id: uuid.UUID,
        audiences: Sequence[str],
        ids: Sequence[uuid.UUID] | None,
    ) -> ReadResult:
        """Mark ``ids`` (``None``: every visible notification) read; return how
        many became read and how many are unread after it (one transaction: the
        answer never disagrees with what was saved). Ids the user cannot see are
        ignored."""
        if ids is not None and len(ids) > MAX_READ_IDS:
            raise ValueError("too many ids")
        params = {
            "user": user_id,
            "audiences": list(audiences),
            "every": ids is None,
            "ids": [] if ids is None else list(ids),
        }

        async def work(session: AsyncSession) -> ReadResult:
            updated = len((await session.execute(text(_MARK_READ), params)).fetchall())
            unread = (
                await session.execute(
                    text(_UNREAD), {"user": user_id, "audiences": params["audiences"]}
                )
            ).scalar_one()
            return ReadResult(updated=updated, unread=int(unread))

        return await self._run(work)

    async def dismiss(
        self, user_id: uuid.UUID, audiences: Sequence[str], notification_id: uuid.UUID
    ) -> bool:
        """Dismiss the entry of ``notification_id``; ``False`` if the user cannot
        see it (unknown, another user's, resolved, an audience they left)."""
        who = {"user": user_id, "audiences": list(audiences)}

        async def work(session: AsyncSession) -> bool:
            target = (
                await session.execute(text(_TARGET), {**who, "id": notification_id})
            ).first()
            if target is None:
                return False
            await session.execute(
                text(_DISMISS),
                {**who, "key": target.key, "created_at": target.created_at},
            )
            return True

        return await self._run(work)

    async def purge(self, *, days: int = RETENTION_DAYS) -> int:
        async def work(session: AsyncSession) -> int:
            return await purge_in(session, days=days)

        return await self._run(work)
