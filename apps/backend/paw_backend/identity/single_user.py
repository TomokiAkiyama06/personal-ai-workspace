"""The deployment's single active user, checked under one advisory lock
(Decision 0099, 2, condition 3; issue #297).

The production ``CloudPolicy`` sends a task's content to a cloud agent only
while the deployment has exactly one ``active`` user. ``allows`` asks first, but
a second user may become active between that answer and the send, so the
transaction that records the send (before anything is sent: Decision 0048, 4)
asks again:

* :func:`recheck_single_user_in` takes the deployment-wide advisory lock
  **shared** (``pg_advisory_xact_lock_shared``) in the record's transaction and
  then counts the active users; anything but exactly one is ``False`` and the
  caller writes nothing and sends nothing. The node attempt's placement record
  (``DagStore.record_placement``) calls it through the policy, and the planner
  call's record (Decision 0099, 3) is to call it the same way.
* :func:`lock_user_activation` takes the same lock **exclusively**; every
  transaction that creates a user or makes one ``active`` takes it before it
  writes the user.

So a recount and an activation never overlap: a send whose record committed
was the send of a deployment that had one active user at that moment. A session
that already started runs to its end (as with the kill switch, Decision 0099,
2).

The lock is transaction-level: it is released when the transaction ends, never
held across an ``await`` outside it. Both functions take the caller's open
``AsyncSession`` (or ``AsyncConnection``) and run inside its transaction.
"""

from sqlalchemy import text

#: The advisory lock key of the single-user check (a fixed, arbitrary bigint:
#: ``PAW_1USR`` in ASCII). Shared by the recount, exclusive by an activation.
SINGLE_USER_LOCK_KEY = 0x5041575F31555352

_LOCK_SHARED = text("SELECT pg_advisory_xact_lock_shared(:key)")
_LOCK_EXCLUSIVE = text("SELECT pg_advisory_xact_lock(:key)")
_COUNT_ACTIVE = text("SELECT count(*) FROM users WHERE status = 'active'")


async def lock_user_activation(session) -> None:
    """Take the single-user lock exclusively in ``session``'s transaction:
    call it before creating a user or making one ``active``."""
    await session.execute(_LOCK_EXCLUSIVE, {"key": SINGLE_USER_LOCK_KEY})


async def count_active_users(session) -> int:
    """The number of ``active`` users (no lock)."""
    return int((await session.execute(_COUNT_ACTIVE)).scalar_one())


async def recheck_single_user_in(session) -> bool:
    """Whether the deployment has exactly one active user, counted under the
    single-user lock taken shared in ``session``'s transaction (see the
    module). The lock stays held until that transaction ends, so no user can
    become active before the caller's record commits."""
    await session.execute(_LOCK_SHARED, {"key": SINGLE_USER_LOCK_KEY})
    return await count_active_users(session) == 1


__all__ = [
    "SINGLE_USER_LOCK_KEY",
    "count_active_users",
    "lock_user_activation",
    "recheck_single_user_in",
]
