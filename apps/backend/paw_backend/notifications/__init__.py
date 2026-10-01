"""Stored notifications (issue #188, Decision 0070 Approved).

``domain.py`` is the vocabulary (a notification is codes and numbers; its
audience is one user or the holders of a system-wide capability), ``models.py``
the tables (revision ``0188``), ``store.py`` the reads and writes. Producers add
notifications in their own transaction (``store.add_in``; System Health's
severity changes are the first, ``health/store.py``); ``/api/v1/notifications``
serves them, and the authenticated event stream (``/api/v1/events/*``) tells a
client that its notifications changed (``notification.changed``, no content).
"""

from paw_backend.notifications.domain import (
    Category,
    InvalidNotificationError,
    NewNotification,
    NotificationPage,
    ReadResult,
    Severity,
    StoredNotification,
    audience_capabilities,
)
from paw_backend.notifications.store import (
    NotificationStore,
    NotificationsUnavailableError,
    add_in,
    resolve_in,
)

__all__ = [
    "Category",
    "InvalidNotificationError",
    "NewNotification",
    "NotificationPage",
    "NotificationStore",
    "NotificationsUnavailableError",
    "ReadResult",
    "Severity",
    "StoredNotification",
    "add_in",
    "audience_capabilities",
    "resolve_in",
]
