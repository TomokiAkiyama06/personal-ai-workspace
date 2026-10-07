"""The record of the local usage (issue #187 item 5, Decision 0077).

:class:`HybridRuntime` (``runtimes.py``) tells a :class:`LocalUsageSink` about
every call of a local model when it ended: the task, where the scheduler placed
it, whether it is a call (``calls`` 1) or the late time of one (``calls`` 0),
the tokens the local runtime reported to the task's budget while it ran and the
whole seconds the call held its lease (the figure charged to the budget's
``GPU_SECONDS``). :class:`PostgresLocalUsage` writes one row of ``local_usage``
(``models.py``) for it; the usage report reads them.

The record is not the budget: a failed write is the caller's to log, and the
node's outcome does not change for it.
"""

import uuid
from typing import Protocol

from paw_backend.compute.config import check_int, check_member
from paw_backend.compute.domain import Placement
from paw_backend.compute.errors import InvalidComputeArgumentError
from paw_backend.compute.models import (
    LOCAL_PLACEMENTS,
    MAX_LOCAL_SECONDS,
    MAX_LOCAL_TOKENS,
    LocalUsageRow,
)
from paw_backend.db import Database

__all__ = ["LocalUsageRow", "LocalUsageSink", "PostgresLocalUsage"]

# The row of a task that exists, attributed to its creator; nothing for a task
# that does not (the record never invents one). ``started_at`` is the database's
# time when the call ended, less the seconds it held its lease.
_INSERT = (
    "INSERT INTO local_usage (user_id, task_id, placement, calls, tokens, seconds,"
    " started_at) SELECT created_by, id, %(placement)s, %(calls)s, %(tokens)s,"
    " %(seconds)s, now() - make_interval(secs => %(seconds)s::float8)"
    " FROM tasks WHERE id = %(task)s"
)
# One write: never longer than this (the call that waits for it has ended).
WRITE_TIMEOUT_SECONDS = 5.0


class LocalUsageSink(Protocol):
    async def record(
        self,
        task_id: uuid.UUID,
        *,
        placement: Placement,
        calls: int,
        tokens: int,
        seconds: int,
    ) -> None:
        """Record one local call (``calls`` 1) or the late time of one
        (``calls`` 0). ``placement`` is ``Placement.LOCAL_GPU`` or
        ``LOCAL_CPU``."""
        ...


class PostgresLocalUsage:
    """A :class:`LocalUsageSink` that writes ``local_usage`` (one statement)."""

    def __init__(
        self, database: Database, *, timeout_seconds: float = WRITE_TIMEOUT_SECONDS
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._timeout = timeout_seconds

    async def record(
        self,
        task_id: uuid.UUID,
        *,
        placement: Placement,
        calls: int,
        tokens: int,
        seconds: int,
    ) -> None:
        if not isinstance(task_id, uuid.UUID):
            raise InvalidComputeArgumentError("task_id")
        placement = check_member("placement", placement, Placement)
        if placement not in LOCAL_PLACEMENTS:
            raise InvalidComputeArgumentError("placement")
        check_int("calls", calls, minimum=0, maximum=1)
        check_int("tokens", tokens, minimum=0, maximum=MAX_LOCAL_TOKENS)
        check_int("seconds", seconds, minimum=0, maximum=MAX_LOCAL_SECONDS)
        await self._database.execute_abortable(
            _INSERT,
            {
                "task": task_id,
                "placement": placement.value,
                "calls": calls,
                "tokens": tokens,
                "seconds": seconds,
            },
            timeout_seconds=self._timeout,
        )
