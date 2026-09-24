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

import hashlib
import re
import unicodedata
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.db import Database
from paw_backend.tasks.errors import StaleAttemptError, TaskNotFoundError
from paw_backend.tasks.models import TaskRow
from paw_backend.tasks.queueing.domain import (
    DEFAULT_LOOP_POLICY,
    FailureRecord,
    LoopAssessment,
    LoopPolicy,
    LoopVerdict,
)
from paw_backend.tasks.queueing.errors import InvalidQueueingArgumentError
from paw_backend.tasks.queueing.models import FailureSignatureRow
from paw_backend.tasks.queueing.validation import (
    MAX_SIGNATURE_MESSAGE_CHARS,
    check_approach,
    check_attempt,
    check_error_class,
    check_message,
    check_step_name,
    check_uuid,
)

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
    check_message(message)
    text = unicodedata.normalize("NFKC", message[:MAX_SIGNATURE_MESSAGE_CHARS])
    text = text.casefold()
    text = UUID_PATTERN.sub("<uuid>", text)
    text = PREFIXED_HEX_PATTERN.sub("<hex>", text)
    text = HEX_TOKEN_PATTERN.sub("<hex>", text)
    text = DIGITS_PATTERN.sub("<n>", text)
    return " ".join(text.split())


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
    check_error_class(error_class)
    check_step_name(step)
    check_message(message)
    text = "\x1f".join((error_class, step, normalize_failure_message(message)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
    if not isinstance(history, list | tuple) or not all(
        isinstance(record, FailureRecord) for record in history
    ):
        raise InvalidQueueingArgumentError("history")
    if not isinstance(policy, LoopPolicy):
        raise InvalidQueueingArgumentError("policy")
    if not history:
        return LoopAssessment(LoopVerdict.CONTINUE, None, None, 0)

    last = history[-1]
    repeats = sum(
        1
        for record in history[-policy.window_size :]
        if record.signature == last.signature and record.approach == last.approach
    )
    if repeats < policy.repeat_threshold:
        verdict = LoopVerdict.CONTINUE
    elif last.approach < policy.max_alternatives:
        verdict = LoopVerdict.TRY_ALTERNATIVE
    else:
        verdict = LoopVerdict.ESCALATE
    return LoopAssessment(verdict, last.signature, last.approach, repeats)


class LoopDetector:
    """Stores a bounded failure window per task and evaluates it.

    Nothing is kept in process memory: the window lives in
    ``loop_failure_signatures`` (columns ``seq``, ``task_id``, ``approach``,
    ``signature``, ``created_at``; ``seq`` orders the records). Unknown tasks
    raise ``TaskNotFoundError``.

    Attempts (Decision 0007, section 8). A failure belongs to one ATTEMPT of the
    task: ``record_failure`` names the attempt the reporting worker was started
    for (``TaskEvent.attempt``, the same number the PAW-032 step / log / tool
    writes carry) and is refused with ``StaleAttemptError`` unless it is the
    task's current attempt (``tasks.attempt``, which Restart increments). The
    counter is the existing PAW-032 one, so no new state exists. A delayed report
    of an abandoned attempt therefore never enters the new attempt's history.
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
        attempt: int,
        error_class: str,
        step: str,
        message: str,
        approach: int = 0,
    ) -> LoopAssessment:
        """Record one failure of ``attempt`` and return the assessment including it.

        ``attempt`` (required, an ``int`` from 1; ``validation.check_attempt``) is
        the task attempt the reporting worker works for. In ONE transaction: take
        the task's failure lock, lock the task row ``FOR SHARE`` and compare
        ``attempt`` with ``tasks.attempt``; raise ``TaskNotFoundError`` for an
        unknown task and ``StaleAttemptError`` when the attempt is not the current
        one (nothing is written). The share lock is held to the end of the
        transaction, so a Restart (which updates the task row) cannot commit
        between the check and the commit of the row: a stored failure always
        belongs to the attempt that was current when it committed. Then insert a
        row with ``failure_signature(error_class, step, message)`` and
        ``approach``; delete the task's oldest rows so that at most
        ``policy.window_size`` remain (the newest by ``seq`` are kept); then return
        ``evaluate_loop`` of the remaining rows (oldest first). The raw ``message``
        (and ``error_class`` / ``step``) is never stored or logged. Concurrent
        calls for the SAME task (``clear`` included) are serialised for the whole
        transaction by ``SELECT pg_advisory_xact_lock(...)`` keyed by the task id:
        otherwise transactions that cannot see each other's rows would each skip
        the deletion and the window bound would be broken. Every argument is
        validated before the database is used
        (``validation.check_uuid("task_id", ...)``, ``check_attempt``,
        ``check_approach``, and the checks named in ``failure_signature``). Not
        idempotent: calling twice records two failures.
        """
        check_uuid("task_id", task_id)
        check_attempt(attempt)
        check_approach(approach)
        signature = failure_signature(error_class, step, message)
        async with self._database.session() as session, session.begin():
            await self._lock_failures(session, task_id)
            await self._require_current_attempt(session, task_id, attempt)
            session.add(
                FailureSignatureRow(
                    task_id=task_id, approach=approach, signature=signature
                )
            )
            await session.flush()
            newest = (
                select(FailureSignatureRow.seq)
                .where(FailureSignatureRow.task_id == task_id)
                .order_by(FailureSignatureRow.seq.desc())
                .limit(self._policy.window_size)
            )
            await session.execute(
                delete(FailureSignatureRow).where(
                    FailureSignatureRow.task_id == task_id,
                    FailureSignatureRow.seq.not_in(newest),
                )
            )
            history = await self._read_history(session, task_id)
        return evaluate_loop(history, self._policy)

    @staticmethod
    async def _require_current_attempt(
        session: AsyncSession, task_id: uuid.UUID, attempt: int
    ) -> None:
        """Lock the task row ``FOR SHARE`` and require ``attempt`` to be current.

        ``FOR SHARE`` conflicts with the ``FOR NO KEY UPDATE`` that every PAW-032
        command (Restart included) takes, so the two run one after the other; it
        does not conflict with the ``FOR KEY SHARE`` of foreign-key checks or with
        other ``record_failure`` calls (which the failure lock serialises anyway).
        """
        current = (
            await session.execute(
                select(TaskRow.attempt)
                .where(TaskRow.id == task_id)
                .with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if current is None:
            raise TaskNotFoundError()
        if current != attempt:
            raise StaleAttemptError()

    @staticmethod
    async def _lock_failures(session: AsyncSession, task_id: uuid.UUID) -> None:
        """Take the task's failure lock until the end of the transaction.

        Every transaction that writes the task's rows (``record_failure`` and
        ``clear``) takes it first, so they run one after the other. Without it,
        transactions that cannot see each other's uncommitted rows would each skip
        the window deletion, and a ``clear`` would return before an in-flight
        ``record_failure`` commits and leave that (stale) failure behind.
        """
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"loop-failures:{task_id}", 0)
                )
            )
        )

    @staticmethod
    async def _read_history(
        session: AsyncSession, task_id: uuid.UUID
    ) -> tuple[FailureRecord, ...]:
        rows = await session.execute(
            select(FailureSignatureRow.signature, FailureSignatureRow.approach)
            .where(FailureSignatureRow.task_id == task_id)
            .order_by(FailureSignatureRow.seq)
        )
        return tuple(FailureRecord(signature, approach) for signature, approach in rows)

    async def history(self, task_id: uuid.UUID) -> tuple[FailureRecord, ...]:
        """The stored window, oldest first (at most ``policy.window_size``).

        Empty for a task without failures (also for an unknown task). Read-only.
        """
        check_uuid("task_id", task_id)
        async with self._database.session() as session, session.begin():
            return await self._read_history(session, task_id)

    async def assess(self, task_id: uuid.UUID) -> LoopAssessment:
        """``evaluate_loop`` of ``history(task_id)``. Read-only; calling it any
        number of times without new failures returns equal results."""
        return evaluate_loop(await self.history(task_id), self._policy)

    async def clear(self, task_id: uuid.UUID) -> int:
        """Delete the task's stored failures (for example on Restart) and return
        how many rows were deleted (0 when there were none).

        Serialised with ``record_failure`` of the SAME task by the same per-task
        lock, held for the whole transaction: a ``record_failure`` that is still
        in flight commits first and its row is deleted too, one that starts
        later waits for the clear. So ``clear`` never returns while an earlier
        failure is still going to appear (a failure recorded after the clear
        belongs to the new history).

        Call it AFTER the Restart command has committed (the new attempt is then
        the task's current one): a failure that an old attempt reports after that
        is refused by ``record_failure`` (``StaleAttemptError``), and one that
        was in flight when Restart ran committed before it and is deleted here.
        ``clear`` before Restart would leave a window in which the old attempt
        can still record and Restart then carries that failure into the new one."""
        check_uuid("task_id", task_id)
        async with self._database.session() as session, session.begin():
            await self._lock_failures(session, task_id)
            result = await session.execute(
                delete(FailureSignatureRow).where(
                    FailureSignatureRow.task_id == task_id
                )
            )
            return result.rowcount
