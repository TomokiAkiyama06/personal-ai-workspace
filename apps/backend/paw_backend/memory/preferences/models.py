"""ORM model of the Inferred Preference flow (revision ``0192``, PAW-044).

``memory_preference_resolutions``: the person's answer to a HELD candidate. A held
candidate is an item of a consolidated journal entry's outcome (PAW-041, Decision
0018: the consolidator keeps it there instead of writing a memory); the outcome is
the consolidator's record and is not rewritten, so the answer is a row of its own.
One row per held item answered (the rows are written for every unanswered held item
of the key up to the one the person answered, so an older one does not come back);
a NEWER held item of the same key is a new question.

The row goes with its journal entry (``ON DELETE CASCADE``): deleting the
conversation deletes the entry, its held candidate and the answer. ``memory_id`` is
the memory a confirmation wrote (``ON DELETE SET NULL``: a memory outlives nothing
here). Rows are never changed: the application role may SELECT and INSERT only.
The user is a plain UUID like the rest of the Memory layer; the erasure of a user
deletes the rows with the user's journal entries.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from paw_backend.db import Base
from paw_backend.memory.preferences.domain import Resolution


def _one_of(column: str, values: type[Resolution]) -> str:
    listed = ", ".join(f"'{member.value}'" for member in values)
    return f"{column} IN ({listed})"


class PreferenceResolution(Base):
    """The person's answer to one held candidate (an outcome item of an entry)."""

    __tablename__ = "memory_preference_resolutions"
    __table_args__ = (
        CheckConstraint(_one_of("resolution", Resolution), name="resolution_valid"),
        CheckConstraint("item_index >= 0", name="item_index_not_negative"),
        CheckConstraint(
            "resolution = 'confirmed' OR memory_id IS NULL",
            name="only_confirmed_has_memory",
        ),
        # The foreign key's referential action needs it; also "the answers of a
        # memory".
        Index(
            "ix_memory_preference_resolutions_memory_id",
            "memory_id",
            postgresql_where=text("memory_id IS NOT NULL"),
        ),
        Index("ix_memory_preference_resolutions_owner_user_id", "owner_user_id"),
    )

    entry_id: Mapped[UUID] = mapped_column(
        ForeignKey("memory_journal_entries.id", ondelete="CASCADE"), primary_key=True
    )
    # ``outcome.items[*].index`` of the entry: the item's place in the worker output.
    item_index: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    # The entry's owner, copied: the person who answered (only the owner can).
    owner_user_id: Mapped[UUID]
    resolution: Mapped[str] = mapped_column(Text)
    memory_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


TABLE_NAMES: tuple[str, ...] = (PreferenceResolution.__tablename__,)
