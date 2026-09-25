"""Value types, the storage protocol and the shared rules of tool approvals.

An approval belongs to **one exact call**: its ``call_hash`` covers the tool,
the normalised arguments, the task and the requester (user and agent). It is

* created ``pending`` by the broker when a call needs a human (at most
  ``OpenLimits.max_pending`` open ones per task and user, and none for a call
  that was just rejected),
* ``approved`` or ``rejected`` once, by the human user the agent works for
  (never by the agent); a ``STRONG_APPROVAL`` is approved only with a step-up,
* ``consumed`` at most once, by the very call it was granted for,
* ``revoked`` by that user (or an Admin / Owner) or when its task ends, and
* ``expired`` when its time runs out, whatever it was before.

``rejected``, ``consumed``, ``revoked`` and ``expired`` are final. The history
of every change is append-only.

What the human is shown is the ``summary``: every argument of the call, by name,
with a bounded, redacted value (the hash binds the *full* values; a value cut for
display carries its length and a hash prefix).
"""

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from paw_backend.tools.capabilities import ApprovalLevel
from paw_backend.tools.credentials import contains_credential_plaintext, redact_text
from paw_backend.tools.scope import Target

MAX_APPROVAL_TOOL_LENGTH = 64
_HASH = re.compile(r"[0-9a-f]{64}")
_SUMMARY_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
APPROVAL_LEVELS = (ApprovalLevel.APPROVAL, ApprovalLevel.STRONG_APPROVAL)
# The approver's view of a call (see ``SummaryItem``).
MAX_SUMMARY_ITEMS = 16
MAX_SUMMARY_VALUE_CHARS = 256  # of the shown content; a cut adds a short suffix
MAX_SUMMARY_TEXT_CHARS = 320  # a stored value, suffix included
MAX_SUMMARY_TOTAL_CHARS = 6144
# Limits on open approvals (configurable within these bounds on the broker).
MIN_MAX_PENDING, MAX_MAX_PENDING = 1, 100
MIN_COOLDOWN, MAX_COOLDOWN = timedelta(minutes=1), timedelta(hours=24)
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class ApprovalEventKind(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    CONSUMED = "consumed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class OpenOutcome(StrEnum):
    CREATED = "created"
    EXISTING = "existing"  # an open request for this exact call already exists
    TOO_MANY_PENDING = "too_many_pending"  # the task / user has too many open ones
    COOLING_DOWN = "cooling_down"  # this exact call was rejected a moment ago
    # Only with ``require_active_task``: the task has ended, or is not known;
    # nothing was created (and no existing request is handed out either).
    TASK_NOT_ACTIVE = "task_not_active"
    TASK_UNKNOWN = "task_unknown"


class DecideOutcome(StrEnum):
    DECIDED = "decided"
    NOT_FOUND = "not_found"
    NOT_PENDING = "not_pending"  # already decided, consumed, revoked or expired
    EXPIRED = "expired"
    NOT_AUTHORISED = "not_authorised"  # not the delegating user, or the agent itself
    STEP_UP_REQUIRED = "step_up_required"  # a strong approval without a step-up


class ConsumeOutcome(StrEnum):
    CONSUMED = "consumed"
    NOT_FOUND = "not_found"
    MISMATCH = "mismatch"  # granted for a different call
    EXPIRED = "expired"
    ALREADY_USED = "already_used"
    REJECTED = "rejected"
    REVOKED = "revoked"
    PENDING = "pending"  # no human decision yet
    # Only with ``require_active_task``: the task of the approval has ended, or
    # is not known; nothing was consumed.
    TASK_NOT_ACTIVE = "task_not_active"
    TASK_UNKNOWN = "task_unknown"


class RevokeOutcome(StrEnum):
    REVOKED = "revoked"
    NOT_FOUND = "not_found"
    NOT_OPEN = "not_open"  # already decided against, used, revoked or expired


def _aware(value: object, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _escape(text: str) -> str:
    """Control, format and separator characters as visible escapes (so that a
    value cannot forge a line or hide text in the approver's view)."""
    return "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c) in _UNSAFE_CATEGORIES else c
        for c in text
    )


@dataclass(frozen=True, slots=True)
class SummaryItem:
    """One argument as the approver sees it: its name, its kind and a bounded,
    redacted, escaped value. Never carries credential plaintext."""

    name: str
    kind: str
    value: str

    def __post_init__(self) -> None:
        if type(self.name) is not str or _SUMMARY_NAME.fullmatch(self.name) is None:
            raise ValueError("a summary item needs a valid argument name")
        if type(self.kind) is not str or _SUMMARY_NAME.fullmatch(self.kind) is None:
            raise ValueError("a summary item needs a valid kind")
        if type(self.value) is not str or len(self.value) > MAX_SUMMARY_TEXT_CHARS:
            raise ValueError("a summary value is too long")
        if contains_credential_plaintext(self.value):
            raise ValueError("a summary must not hold credential plaintext")


def summary_value(text: str) -> str:
    """The value the approver is shown: redacted, escaped, cut at
    ``MAX_SUMMARY_VALUE_CHARS`` with the full length and a hash prefix so that
    a long value cannot hide behind its beginning unnoticed."""
    redacted, _ = redact_text(text)
    shown = _escape(redacted)
    if len(shown) <= MAX_SUMMARY_VALUE_CHARS:
        return shown
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    return f"{shown[:MAX_SUMMARY_VALUE_CHARS]}...[{len(text)} chars, sha256:{digest}]"


@dataclass(frozen=True, slots=True)
class OpenLimits:
    """How many approvals may be open, and how soon a rejected call may be
    asked again. Both are bounded so that they cannot be configured away."""

    max_pending: int = 10
    rejection_cooldown: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if (
            type(self.max_pending) is not int
            or not MIN_MAX_PENDING <= self.max_pending <= MAX_MAX_PENDING
        ):
            raise ValueError("max_pending must be between 1 and 100")
        if not isinstance(self.rejection_cooldown, timedelta) or not (
            MIN_COOLDOWN <= self.rejection_cooldown <= MAX_COOLDOWN
        ):
            raise ValueError("rejection_cooldown must be between 1 minute and 24 hours")


@dataclass(frozen=True, slots=True)
class NewApproval:
    approval_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str  # a registered tool name
    level: ApprovalLevel
    call_hash: str
    targets: tuple[Target, ...]
    # What the approver sees. Never empty: an approval nobody can read is refused.
    summary: tuple[SummaryItem, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        for name in ("approval_id", "task_id", "project_id", "agent_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if not isinstance(self.requester_user_id, uuid.UUID):
            raise TypeError("requester_user_id must be a UUID")
        if self.agent_id == self.requester_user_id:
            raise ValueError("an agent cannot be the user it acts for")
        if (
            type(self.tool) is not str
            or not 1 <= len(self.tool) <= MAX_APPROVAL_TOOL_LENGTH
        ):
            raise ValueError("tool is not valid")
        if self.level not in APPROVAL_LEVELS:
            raise ValueError(
                "only APPROVAL and STRONG_APPROVAL are approved by a human"
            )
        if not isinstance(self.call_hash, str) or not _HASH.fullmatch(self.call_hash):
            raise ValueError("call_hash is not a SHA-256 hex digest")
        _aware(self.expires_at, "expires_at")
        if (
            not isinstance(self.summary, tuple)
            or not 1 <= len(self.summary) <= MAX_SUMMARY_ITEMS
            or not all(isinstance(item, SummaryItem) for item in self.summary)
            or sum(len(item.value) for item in self.summary) > MAX_SUMMARY_TOTAL_CHARS
        ):
            raise ValueError("an approval needs a bounded, non-empty summary")


@dataclass(frozen=True, slots=True)
class ApprovalBinding:
    """The call an approval is used for: all of it must match what was granted."""

    task_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str
    level: ApprovalLevel
    call_hash: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: uuid.UUID
    task_id: uuid.UUID
    project_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    tool: str
    level: ApprovalLevel
    call_hash: str
    targets: tuple[Target, ...]
    summary: tuple[SummaryItem, ...]
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    approver_id: uuid.UUID | None = None
    decided_at: datetime | None = None
    consumed_at: datetime | None = None
    # A strong approval was granted with a confirmed step-up.
    step_up_verified: bool = False
    revoked_at: datetime | None = None
    # ``None`` with ``revoked_at`` set: revoked by the system (the task ended).
    revoked_by: uuid.UUID | None = None

    def binding(self) -> ApprovalBinding:
        return ApprovalBinding(
            self.task_id,
            self.agent_id,
            self.requester_user_id,
            self.tool,
            self.level,
            self.call_hash,
        )


@dataclass(frozen=True, slots=True)
class ApprovalHistoryEntry:
    seq: int
    approval_id: uuid.UUID
    kind: ApprovalEventKind
    actor_user_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    occurred_at: datetime
    # What was requested (only on the ``requested`` entry).
    summary: tuple[SummaryItem, ...] | None = None


@dataclass(frozen=True, slots=True)
class OpenResult:
    outcome: OpenOutcome
    # Set for ``CREATED`` and ``EXISTING``.
    record: ApprovalRecord | None = None

    @property
    def created(self) -> bool:
        return self.outcome is OpenOutcome.CREATED


@dataclass(frozen=True, slots=True)
class DecideResult:
    outcome: DecideOutcome
    record: ApprovalRecord | None = None


@dataclass(frozen=True, slots=True)
class ApprovalEvent:
    """What listeners are told (after the change was stored): ids and enums only."""

    kind: ApprovalEventKind
    approval_id: uuid.UUID
    task_id: uuid.UUID
    tool: str
    level: ApprovalLevel
    occurred_at: datetime


class ApprovalStore(Protocol):
    """Durable approval state. Every method is one atomic change.

    ``open_request`` returns the open (pending or approved, unexpired) request
    for the same ``call_hash`` when there is one, so a repeated request does not
    create a second approval; it refuses a new one while the task and user hold
    ``limits.max_pending`` open approvals or the same call was rejected within
    ``limits.rejection_cooldown``. ``decide``, ``consume`` and ``revoke`` succeed
    for at most one of any number of concurrent callers.

    ``consume(..., require_active_task=True)`` must check that the approval's
    task can still act **in the same atomic step as the consumption** and
    consume nothing otherwise (``TASK_NOT_ACTIVE`` / ``TASK_UNKNOWN``); and
    ``open_request(..., require_active_task=True)`` must do the same **in the
    same atomic step as the insert**, creating (or handing out) nothing for an
    ended or unknown task (``OpenOutcome.TASK_NOT_ACTIVE`` / ``TASK_UNKNOWN``).
    A check made before the call is not enough: the task can end in between. A
    consumption could then win from the revocation that follows the end, and a
    request created after that revocation would be one that nothing revokes
    (Decision 0006, section 9).

    ``get``, ``decide`` and ``revoke`` are what a human's decision calls
    (``ApprovalService``, which bounds them with one deadline and cancels them at
    it): a store on a database should run them so that the cancellation really
    ends the query (``PostgresApprovalStore``: an abortable connection), and each
    must stay atomic when it is cut off (Decision 0006, section 4).
    """

    async def open_request(
        self,
        new: NewApproval,
        *,
        now: datetime,
        limits: OpenLimits,
        require_active_task: bool = False,
    ) -> OpenResult: ...

    async def get(self, approval_id: uuid.UUID) -> ApprovalRecord | None: ...

    async def decide(
        self,
        approval_id: uuid.UUID,
        *,
        approver_id: uuid.UUID,
        approve: bool,
        now: datetime,
        step_up_verified: bool = False,
    ) -> DecideResult: ...

    async def consume(
        self,
        approval_id: uuid.UUID,
        binding: ApprovalBinding,
        *,
        now: datetime,
        require_active_task: bool = False,
    ) -> ConsumeOutcome: ...

    async def revoke(
        self, approval_id: uuid.UUID, *, actor_id: uuid.UUID | None, now: datetime
    ) -> RevokeOutcome: ...

    async def revoke_task(
        self, task_id: uuid.UUID, *, now: datetime
    ) -> list[uuid.UUID]: ...

    async def history(self, approval_id: uuid.UUID) -> list[ApprovalHistoryEntry]: ...


def is_expired(record: ApprovalRecord, now: datetime) -> bool:
    return record.expires_at <= now


def diagnose_decide(
    record: ApprovalRecord | None,
    approver_id: uuid.UUID,
    now: datetime,
    *,
    approve: bool = True,
    step_up_verified: bool = False,
) -> DecideOutcome:
    """What ``decide`` does with the record in its current state.

    Both stores use it: the in-memory one to decide, PostgreSQL to explain why
    its atomic conditional update matched nothing. The authorisation check
    comes before any state, so a stranger learns nothing about the approval.
    """
    if record is None:
        return DecideOutcome.NOT_FOUND
    if approver_id != record.requester_user_id or approver_id == record.agent_id:
        return DecideOutcome.NOT_AUTHORISED
    if record.status is not ApprovalStatus.PENDING:
        return DecideOutcome.NOT_PENDING
    if is_expired(record, now):
        return DecideOutcome.EXPIRED
    if (
        approve
        and record.level is ApprovalLevel.STRONG_APPROVAL
        and not step_up_verified
    ):
        return DecideOutcome.STEP_UP_REQUIRED
    return DecideOutcome.DECIDED


def diagnose_consume(
    record: ApprovalRecord | None, binding: ApprovalBinding, now: datetime
) -> ConsumeOutcome:
    """What ``consume`` does with the record in its current state (see above).

    ``CONSUMED`` means the record is consumable right now.
    """
    if record is None:
        return ConsumeOutcome.NOT_FOUND
    if record.binding() != binding:
        return ConsumeOutcome.MISMATCH
    status = record.status
    if status is ApprovalStatus.CONSUMED:
        return ConsumeOutcome.ALREADY_USED
    if status is ApprovalStatus.REJECTED:
        return ConsumeOutcome.REJECTED
    if status is ApprovalStatus.REVOKED:
        return ConsumeOutcome.REVOKED
    if status is ApprovalStatus.EXPIRED or is_expired(record, now):
        return ConsumeOutcome.EXPIRED
    if status is ApprovalStatus.PENDING:
        return ConsumeOutcome.PENDING
    return ConsumeOutcome.CONSUMED  # approved and unexpired


def diagnose_revoke(record: ApprovalRecord | None, now: datetime) -> RevokeOutcome:
    if record is None:
        return RevokeOutcome.NOT_FOUND
    if record.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED) and (
        not is_expired(record, now)
    ):
        return RevokeOutcome.REVOKED
    return RevokeOutcome.NOT_OPEN
