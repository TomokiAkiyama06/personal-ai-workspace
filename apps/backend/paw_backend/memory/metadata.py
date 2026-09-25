"""Naming the actor of an in-place change of a memory version (PAW-040, #90).

``memory_versions.pinned``, ``importance``, ``status`` and ``stale_since`` are
updated in place, and a trigger records each change in ``memory_metadata_changes``
with its actor (REQUIREMENTS.md "Manual Memory Editing": the change history is
kept; revision 0071 added the status and the stale state, so that a deprecation or
a stale marking can be explained). A trigger cannot know who asked, so the writer
names the actor first, in the same transaction and before the ``UPDATE``::

    session.execute(metadata_change_actor(ActorType.USER, user_id))
    session.execute(update(MemoryVersion)...values(pinned=True))
    session.execute(update(MemoryVersion)...values(status="deprecated"))

The two settings are transaction-local (``set_config(..., true)``), so a pooled
connection never carries one request's actor into the next. A change without an
actor is refused by the database (NOT NULL ``actor_type``). The Backend must
name only an actor it has authenticated: like ``actor_user_id`` of a version,
the id is asserted, not checked, until the users table exists (PAW-021).
"""

from uuid import UUID

from sqlalchemy import Select, func, select

from paw_backend.memory.models import ActorType

# The names the trigger function reads; keep them equal to the function in
# ``models.RECORD_METADATA_CHANGE_FUNCTION``.
ACTOR_TYPE_SETTING = "paw.actor_type"
ACTOR_USER_ID_SETTING = "paw.actor_user_id"


def metadata_change_actor(
    actor_type: ActorType | str, actor_user_id: UUID | None = None
) -> Select:
    """A statement that names who changes a version in place from now on.

    (``pinned``, ``importance``, ``status`` and ``stale_since``.)

    It applies to the current transaction only. A ``user`` needs ``actor_user_id``;
    an ``agent`` or the ``system`` may pass one or ``None``. Naming an actor again
    replaces the earlier one (including its user id).
    """
    try:
        actor = ActorType(actor_type)
    except ValueError:
        # The value is not echoed.
        raise ValueError("actor_type is not a known actor type") from None
    if actor_user_id is not None and not isinstance(actor_user_id, UUID):
        raise TypeError("actor_user_id must be a UUID or None")
    if actor is ActorType.USER and actor_user_id is None:
        raise ValueError("a user actor needs actor_user_id")
    return select(
        func.set_config(ACTOR_TYPE_SETTING, actor.value, True),
        func.set_config(
            ACTOR_USER_ID_SETTING,
            "" if actor_user_id is None else str(actor_user_id),
            True,
        ),
    )
