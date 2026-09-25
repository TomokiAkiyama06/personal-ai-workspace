"""Limits and time rules of the project module (PAW-026).

Every size here bounds something a caller controls. The database repeats the
name / description limits and the 30 day retention as CHECK constraints
(migration ``0026``); ``tests/test_projects_schema.py`` fails when the two
disagree. Raising ``MAX_NAME_CHARS`` or ``MAX_DESCRIPTION_CHARS`` therefore needs
a new migration that replaces the ``name_length`` / ``description_length`` CHECK
constraints (and ``models.py``): the constant alone would let validation accept
values PostgreSQL rejects. ``MAX_MEMBERS_PER_PROJECT`` is enforced only by
``ProjectService.invite_member`` and can be changed alone.

The invitation lifetime is a product choice that is **not** written into the
database (Decision 0008 approved it on 2026-09-25 as a provisional value):
changing it needs no migration. Its effective value is the literal in
``domain.invite_expiry``; ``INVITE_TTL`` records the same value and only the tests
read it, so change both together.
"""

from datetime import UTC, datetime, timedelta

# --- text (counted in characters, i.e. Unicode code points) --------------------

MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 2000
# A raw string longer than this many times the limit is refused before it is
# scanned (bounds the work done for a hostile caller).
RAW_TEXT_FACTOR = 10

# The text a project's name becomes when it is deleted (``REQUIREMENTS.md``:
# the audit trail keeps a minimum as ``Deleted Project``).
DELETED_PROJECT_NAME = "Deleted Project"

# --- lifecycle and membership ---------------------------------------------------

# Pending deletion lasts 30 days (``REQUIREMENTS.md`` "Project lifecycle"). The
# database enforces ``deletion_scheduled_at = deletion_started_at + 30 days``.
DELETION_RETENTION = timedelta(days=30)
# An invitation can be accepted for 14 days (Decision 0008: approved as a provisional
# value on 2026-09-25; the limits below are provisional as well).
INVITE_TTL = timedelta(days=14)
# Accepted members plus open invitations of one project.
MAX_MEMBERS_PER_PROJECT = 200

# --- listing, purging, locking ----------------------------------------------------

DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200
MAX_LIST_OFFSET = 100_000
DEFAULT_PURGE_BATCH_SIZE = 50
MAX_PURGE_BATCH_SIZE = 500
# Tasks one ``ProjectTaskStopper.stop_project_tasks`` call stops at most (and open
# requests one ``pending_project_ids`` call lists at most); same upper bound.
DEFAULT_TASK_STOP_BATCH_SIZE = 100
DEFAULT_LOCK_TIMEOUT_MS = 3000
MIN_LOCK_TIMEOUT_MS = 1
MAX_LOCK_TIMEOUT_MS = 60_000


def utc_now() -> datetime:
    """The default clock: the current instant as an aware UTC datetime."""
    return datetime.now(UTC)
