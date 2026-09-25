"""The decisions of the consolidator, as pure functions (PAW-041). No database.

Keeping them apart from the SQL lets ``tests/test_journal_rules.py`` try every
branch with plain tables, and lets a reviewer read the policy in one place.

What a Memory Worker returns is a set of *claims* about a conversation: which
scope a memory should have, whether the user confirmed it. The backend decides
what happens (REQUIREMENTS.md "Inferred Preference / Confirmation Flow", "Memory
Conflict / Versioning", docs/MEMORY_ARCHITECTURE.md sections 8, 9 and 10), and
the choices the requirements leave open are proposed in Decision 0018:

* **Scope is narrowed, never widened.** Every candidate is written to the
  conversation owner's private scope (``user``). The worker's ``project`` /
  ``repo`` scope is a recommendation kept with the candidate; widening a memory to
  people other than its owner needs the user's confirmation ("User -> Project:
  確認必須") and is a new version made by the confirmation flow (PAW-044). The
  worker's ``shared`` scope is refused: Shared Memory is never written by a worker.
* **A worker cannot mint a confirmed memory.** ``inferred`` is stored as
  ``inferred``; ``confirmed`` is stored as ``observed`` (an explicit statement
  seen once), because only the user's confirmation makes a memory confirmed.
* **A candidate never replaces a confirmed memory,** whatever it says: it is held
  for the user (the requirements' priority order puts Confirmed above Inferred).
* **High-risk areas are held,** not written (Merge, Delete / destructive,
  publication, ACL / role / permission, credentials / secrets, sending outside).
  The word list below is a conservative heuristic; the structural guarantee is
  elsewhere: no memory of any state grants a permission or runs an operation
  (Tool Broker and Approval decide those), and a held item is stored only in the
  observation's outcome, not as a memory version.
* **Order is the event sequence,** not the time a worker finished: a candidate
  from an older turn does not overwrite the memory a newer turn produced.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from paw_backend.memory.journal import limits
from paw_backend.memory.journal.domain import ItemResult, WorkerScope, WorkerState
from paw_backend.memory.journal.errors import InputProblem, InvalidJournalInputError
from paw_backend.memory.journal.validation import validate_int
from paw_backend.memory.journal.worker import WorkerMemory
from paw_backend.memory.models import ConfirmationState, MemoryStatus

# ---------------------------------------------------------------------------
# Retry delay
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Backoff:
    """Delay before a failed job is claimed again: ``base * factor ** (n - 1)``.

    ``n`` is the number of failures so far (1 for the first). The delay never
    exceeds ``max_seconds``. No jitter: the delay is deterministic, so a test can
    assert it (a fleet of workers would add jitter; none exists yet).
    """

    base_seconds: int = limits.DEFAULT_BACKOFF_BASE_SECONDS
    factor: int = limits.DEFAULT_BACKOFF_FACTOR
    max_seconds: int = limits.DEFAULT_BACKOFF_MAX_SECONDS

    def __post_init__(self) -> None:
        validate_int(
            "base_seconds", self.base_seconds, low=1, high=limits.MAX_BACKOFF_SECONDS
        )
        validate_int("factor", self.factor, low=1, high=limits.MAX_BACKOFF_FACTOR)
        validate_int(
            "max_seconds", self.max_seconds, low=1, high=limits.MAX_BACKOFF_SECONDS
        )
        if self.max_seconds < self.base_seconds:
            raise InvalidJournalInputError("max_seconds", InputProblem.OUT_OF_RANGE)

    def delay_seconds(self, failures: int) -> int:
        """The delay after the ``failures``-th failure (an ``int`` of at least 1)."""
        validate_int("failures", failures, low=1, high=limits.MAX_CLAIM_COUNT)
        delay = self.base_seconds
        if self.factor == 1:
            return delay
        for _ in range(failures - 1):
            if delay >= self.max_seconds:
                break
            delay *= self.factor
        return min(delay, self.max_seconds)


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderKey:
    """Where an entry sits in time: enough to tell which of two is newer."""

    conversation_id: UUID
    event_sequence: int
    recorded_at: datetime


def is_newer(entry: OrderKey, applied: OrderKey) -> bool:
    """Is ``entry`` strictly newer than ``applied``, the entry behind a memory?

    In one conversation the event sequence decides (it is assigned under a row
    lock, so it is the true order of the events, and the recorded time could tie or
    step backwards with the clock). Across conversations the events have no shared
    sequence, so the time they were recorded decides (the database clock), and the
    conversation id breaks an exact tie so that the answer is total and stable.
    Equal means not newer: an entry never supersedes itself.
    """
    if entry.conversation_id == applied.conversation_id:
        return entry.event_sequence > applied.event_sequence
    return (entry.recorded_at, str(entry.conversation_id)) > (
        applied.recorded_at,
        str(applied.conversation_id),
    )


# ---------------------------------------------------------------------------
# Confirmation state
# ---------------------------------------------------------------------------


def stored_state(claimed: WorkerState) -> ConfirmationState:
    """The state a candidate is stored with. Never ``confirmed``.

    An ``inferred`` claim stays ``inferred`` (no auto-promotion). A ``confirmed``
    claim was made by a model about text a person may have pasted from elsewhere,
    so it does not confirm anything: it is stored as ``observed``, and the
    original claim is kept in the version's attributes for the confirmation UI.
    """
    if claimed is WorkerState.INFERRED:
        return ConfirmationState.INFERRED
    return ConfirmationState.OBSERVED


# ---------------------------------------------------------------------------
# High-risk areas
# ---------------------------------------------------------------------------

# The areas the requirements name (REQUIREMENTS.md "Inferred Preference /
# Confirmation Flow"): merge permission, delete / destructive operations,
# widening visibility, ACL / role / permission changes, credentials / secrets,
# sending or publishing outside. Whole words (a key ``allow_merge`` is split at
# the underscore); an over-match only holds a candidate for the user, which is the
# safe direction.
HIGH_RISK_WORDS = frozenset(
    {
        "merge", "merges", "merged", "merging",
        "delete", "deletes", "deleted", "deleting", "deletion",
        "destroy", "destructive", "purge", "wipe", "force",
        "acl", "acls", "role", "roles", "permission", "permissions",
        "privilege", "privileges", "admin", "sudo",
        "credential", "credentials", "secret", "secrets",
        "password", "passwords", "token", "tokens", "apikey",
        "publish", "published", "publishing", "public", "visibility", "expose",
        "external", "externally", "upload", "send", "sends", "sending",
    }
)  # fmt: skip
# Japanese has no word boundary: a substring match.
HIGH_RISK_PHRASES = (
    "マージ", "削除", "破壊", "強制", "権限", "認証", "秘密", "公開", "外部", "送信",
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def is_high_risk(*texts: str | None) -> bool:
    """Does any text touch a high-risk area (a word or phrase of the lists above)?

    Text is folded first (NFKC, camelCase split, lower case), so full-width
    ``Ｍｅｒｇｅ`` and ``MergePolicy`` count.
    """
    for text in texts:
        if not text:
            continue
        # NFKC first (full-width letters), then a break at a camelCase boundary
        # ("MergePolicy" holds the words "merge" and "policy"), then lower case.
        folded = _CAMEL_BOUNDARY.sub(" ", unicodedata.normalize("NFKC", text)).lower()
        if not HIGH_RISK_WORDS.isdisjoint(_WORD.findall(folded)):
            return True
        if any(phrase in folded for phrase in HIGH_RISK_PHRASES):
            return True
    return False


# ---------------------------------------------------------------------------
# One memory of a worker's output
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentMemory:
    """The latest version of the memory a key names, as far as a decision needs."""

    status: MemoryStatus
    confirmation_state: ConfirmationState
    content: str

    @property
    def is_active_confirmed(self) -> bool:
        return (
            self.status is MemoryStatus.ACTIVE
            and self.confirmation_state is ConfirmationState.CONFIRMED
        )


def plan_item(
    item: WorkerMemory,
    *,
    entry: OrderKey,
    current: CurrentMemory | None,
    applied: OrderKey | None,
    supersedes_target: CurrentMemory | None,
) -> ItemResult:
    """What to do with one memory of a worker's output.

    ``current`` is the latest version of the memory the item's key already names
    (``None``: the key is new). ``applied`` is the order key of the entry that
    produced it. ``supersedes_target`` is the latest version of the memory named by
    ``item.supersedes`` (``None`` when there is none, or it names the item's own
    key). The first matching rule wins:

    1. ``shared`` scope: refused. 2. No content: nothing to store.
    3. A high-risk area: held. 4. A new key: created (held instead if it would
    supersede a confirmed memory). 5. The user rejected or deleted the memory:
    blocked. 6. An older turn than the one behind the current version: stale.
    7. The current version says the same: duplicate. 8. The current version is
    confirmed, or the item supersedes a confirmed memory: held. 9. Otherwise a new
    version.
    """
    if item.scope is WorkerScope.SHARED:
        return ItemResult.REFUSED_SHARED
    if item.content is None:
        return ItemResult.NO_CONTENT
    if is_high_risk(item.key, item.content):
        return ItemResult.HELD_HIGH_RISK
    replaces_confirmed = (
        supersedes_target is not None and supersedes_target.is_active_confirmed
    )
    if current is None:
        return ItemResult.HELD_CONFIRMED if replaces_confirmed else ItemResult.CREATED
    if (
        current.status in (MemoryStatus.DEPRECATED, MemoryStatus.HISTORY)
        or current.confirmation_state is ConfirmationState.REJECTED
    ):
        return ItemResult.BLOCKED
    if applied is not None and not is_newer(entry, applied):
        return ItemResult.STALE
    if current.status is MemoryStatus.ACTIVE:
        if current.content.strip() == item.content.strip():
            return ItemResult.DUPLICATE
        if current.confirmation_state is ConfirmationState.CONFIRMED:
            return ItemResult.HELD_CONFIRMED
    return ItemResult.HELD_CONFIRMED if replaces_confirmed else ItemResult.UPDATED
