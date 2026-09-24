"""Repeated-failure (loop) detection (PAW-033).

REQUIREMENTS.md: when an agent repeats the same failure, stop the same approach,
try an alternative approach, and if it still fails escalate to a stronger agent
(Codex / Claude). A loop never fails the task by itself.

A failure is reduced to a SIGNATURE: a hash of the error class, the step and the
normalised message. Only signatures are stored, never the raw message.

Everything is independent of the budget preset: an Unlimited task is still
stopped by a detected loop.

The detector part is split in three pure functions (``normalize_failure_message``,
``failure_signature``, ``evaluate_loop``) and the database part
(``LoopDetector``) that persists a bounded window of ``FailureRecord`` per task
in ``loop_failure_signatures`` and calls the pure functions.
"""

import re
import uuid
from collections.abc import Sequence

from paw_backend.db import Database
from paw_backend.tasks.queueing.domain import (
    DEFAULT_LOOP_POLICY,
    FailureRecord,
    LoopAssessment,
    LoopPolicy,
)
from paw_backend.tasks.queueing.errors import InvalidQueueingArgumentError

# The patterns of ``normalize_failure_message`` (applied to case-folded text).
UUID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
PREFIXED_HEX_PATTERN = re.compile(r"\b0x[0-9a-f]+\b")
HEX_TOKEN_PATTERN = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,64}\b")
DIGITS_PATTERN = re.compile(r"\d+")


def normalize_failure_message(message: str) -> str:
    """Reduce a failure message to a stable form so that repeats compare equal.

    ``message`` must be a ``str`` (``InvalidQueueingArgumentError("message")``
    otherwise); it may be empty and may contain any characters. Steps, in this
    exact order:

    1. Keep only the first ``MAX_SIGNATURE_MESSAGE_CHARS`` (2000) characters.
    2. Unicode normalisation ``unicodedata.normalize("NFKC", text)``.
    3. ``text.casefold()``.
    4. Replace every match of ``UUID_PATTERN`` with ``<uuid>``.
    5. Replace every match of ``PREFIXED_HEX_PATTERN`` with ``<hex>``.
    6. Replace every match of ``HEX_TOKEN_PATTERN`` (a hexadecimal token of 7 to
       64 characters with at least one digit AND at least one letter a-f, for
       example a commit hash) with ``<hex>``.
    7. Replace every match of ``DIGITS_PATTERN`` (a run of digits) with ``<n>``.
    8. Collapse whitespace: ``" ".join(text.split())`` (this also strips).

    The result may be the empty string. Example:
    ``"Timeout after 30s  on 0x7FFF"`` becomes ``"timeout after <n>s on <hex>"``.
    """
    raise NotImplementedError("PAW-033 stub")


def failure_signature(error_class: str, step: str, message: str) -> str:
    """The signature of a failure: 64 lowercase hexadecimal characters.

    ``sha256`` (``hashlib``) over the UTF-8 encoding of
    ``error_class + "\\x1f" + step + "\\x1f" + normalize_failure_message(message)``
    (``\\x1f`` is the ASCII unit separator), as ``hexdigest()``. ``error_class``
    and ``step`` are used exactly as given (case-sensitive, not normalised).
    The signature never contains or reveals the raw message and is stable across
    processes and versions (it is stored).

    Validation (before hashing): ``validation.check_error_class``,
    ``validation.check_step_name``, ``validation.check_message``; failures raise
    ``InvalidQueueingArgumentError`` naming ``error_class`` / ``step`` /
    ``message``. Deterministic: the same inputs always give the same signature.
    """
    raise NotImplementedError("PAW-033 stub")


def evaluate_loop(
    history: Sequence[FailureRecord],
    policy: LoopPolicy = DEFAULT_LOOP_POLICY,
) -> LoopAssessment:
    """Judge a failure history (oldest first). Pure, deterministic, idempotent.

    ``history`` is a ``list`` or ``tuple`` of ``FailureRecord``; EVERY element is
    validated first (also those outside the window), and any other sequence type
    or element type raises ``InvalidQueueingArgumentError("history")``. ``policy``
    must be a ``LoopPolicy`` (``InvalidQueueingArgumentError("policy")``). The
    input is never modified.

    Rules:

    1. Empty history: ``LoopAssessment(CONTINUE, None, None, 0)``.
    2. ``window`` = the last ``policy.window_size`` records. ``last`` = the final
       record of the history.
    3. ``repeats`` = the number of records in ``window`` (``last`` included)
       whose ``signature`` AND ``approach`` equal those of ``last``. They need
       not be consecutive (A B A B A has three A).
    4. ``repeats < policy.repeat_threshold``: CONTINUE.
    5. Otherwise a loop is detected: if ``last.approach < policy.max_alternatives``
       the verdict is TRY_ALTERNATIVE, else ESCALATE.

    The result carries ``signature`` and ``approach`` of ``last`` and ``repeats``.
    Example with the default policy (3 repeats, 1 alternative): the third failure
    ``A`` in approach 0 gives TRY_ALTERNATIVE; the third ``A`` in approach 1
    gives ESCALATE; two ``A`` and then ``B`` gives CONTINUE (``repeats`` 1).
    """
    raise NotImplementedError("PAW-033 stub")


class LoopDetector:
    """Stores a bounded failure window per task and evaluates it.

    Nothing is kept in process memory: the window lives in
    ``loop_failure_signatures`` (columns ``seq``, ``task_id``, ``approach``,
    ``signature``, ``created_at``; ``seq`` orders the records). Unknown tasks
    raise ``TaskNotFoundError`` (foreign key violation SQLSTATE 23503; any other
    ``IntegrityError`` propagates).
    """

    def __init__(
        self, database: Database, policy: LoopPolicy = DEFAULT_LOOP_POLICY
    ) -> None:
        if not isinstance(policy, LoopPolicy):
            raise InvalidQueueingArgumentError("policy")
        self._database = database
        self._policy = policy

    @property
    def policy(self) -> LoopPolicy:
        return self._policy

    async def record_failure(
        self,
        task_id: uuid.UUID,
        *,
        error_class: str,
        step: str,
        message: str,
        approach: int = 0,
    ) -> LoopAssessment:
        """Record one failure and return the assessment including it.

        In ONE transaction: insert a row with ``failure_signature(error_class,
        step, message)`` and ``approach``; delete the task's oldest rows so that
        at most ``policy.window_size`` remain (the newest by ``seq`` are kept);
        then return ``evaluate_loop`` of the remaining rows (oldest first). The
        raw ``message`` (and ``error_class`` / ``step``) is never stored or
        logged. Concurrent calls for the SAME task must be serialised for the whole
        transaction (for example with ``SELECT pg_advisory_xact_lock(...)`` keyed by
        the task id): otherwise transactions that cannot see each other's rows
        would each skip the deletion and the window bound would be broken. Every
        argument is validated before the database is used
        (``validation.check_uuid("task_id", ...)``, ``check_approach``, and the
        checks named in ``failure_signature``). Not idempotent: calling twice
        records two failures.
        """
        raise NotImplementedError("PAW-033 stub")

    async def history(self, task_id: uuid.UUID) -> tuple[FailureRecord, ...]:
        """The stored window, oldest first (at most ``policy.window_size``).

        Empty for a task without failures (also for an unknown task). Read-only.
        """
        raise NotImplementedError("PAW-033 stub")

    async def assess(self, task_id: uuid.UUID) -> LoopAssessment:
        """``evaluate_loop`` of ``history(task_id)``. Read-only; calling it any
        number of times without new failures returns equal results."""
        raise NotImplementedError("PAW-033 stub")

    async def clear(self, task_id: uuid.UUID) -> int:
        """Delete the task's stored failures (for example on Restart) and return
        how many rows were deleted (0 when there were none)."""
        raise NotImplementedError("PAW-033 stub")
