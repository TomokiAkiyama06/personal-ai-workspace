"""Value objects returned by the Research Scratch Store."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class PromotionState(StrEnum):
    """Where an item is on its way to a Memory Candidate.

    ``pending`` (a confirmation is under way) defers deletion. The store never
    creates the Memory Candidate itself: it only records that a promotion was
    requested and how it ended.
    """

    NONE = "none"
    PENDING = "pending"
    PROMOTED = "promoted"
    REJECTED = "rejected"


class PromotionOutcome(StrEnum):
    """How a pending promotion ended (a subset of :class:`PromotionState`)."""

    PROMOTED = "promoted"
    REJECTED = "rejected"


class DeferralReason(StrEnum):
    """Why an item is exempt from TTL deletion."""

    PINNED = "pinned"
    IN_USE = "in_use"
    PROMOTION_PENDING = "promotion_pending"


@dataclass(frozen=True, slots=True)
class ScratchItem:
    """A snapshot of one item, as of the instant the store read it.

    ``in_use`` is True when at least one lease is active at that instant.
    ``expired`` is True when that instant is at or after ``expires_at``: an
    expired item is only ever returned while it is exempt from deletion (see
    ``deferral_reasons``), or as the result of the operation that ended its
    last exemption. Datetimes are timezone-aware.
    """

    id: UUID
    project_id: UUID
    task_id: UUID | None
    created_by: UUID
    query: str | None
    title: str | None
    summary: str | None
    content: str | None
    source_metadata: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    expired: bool
    pinned: bool
    in_use: bool
    promotion_state: PromotionState
    promotion_requested_at: datetime | None

    @property
    def deferral_reasons(self) -> tuple[DeferralReason, ...]:
        """The reasons TTL deletion is deferred right now, in a fixed order.

        The order is pinned, in_use, promotion_pending. Empty when the item is
        not exempt.
        """
        reasons = []
        if self.pinned:
            reasons.append(DeferralReason.PINNED)
        if self.in_use:
            reasons.append(DeferralReason.IN_USE)
        if self.promotion_state is PromotionState.PENDING:
            reasons.append(DeferralReason.PROMOTION_PENDING)
        return tuple(reasons)


@dataclass(frozen=True, slots=True)
class Lease:
    """One holder's right to keep an item alive, from ``leased_at`` until
    ``expires_at`` (exclusive: at ``expires_at`` the lease is over)."""

    item_id: UUID
    holder_id: UUID
    leased_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PurgeResult:
    """The outcome of one ``purge_expired`` call.

    ``purged``: rows this call deleted. ``deferred``: expired rows that are
    still present because they are exempt (pinned, in use or promotion
    pending) at ``now``; a row with several reasons counts once. ``has_more``:
    more purgeable rows exist beyond this call's batch, so the caller should
    call again.
    """

    purged: int
    deferred: int
    has_more: bool
