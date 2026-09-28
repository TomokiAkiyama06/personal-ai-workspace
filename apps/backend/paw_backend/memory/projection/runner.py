"""One run of the Memory Markdown Projection (PAW-045, Decision 0038).

1. **check_target**: verify the projection directory, claim it, take its lock
   (``writer.open_target``). A second run at the same time gets
   ``ProjectionBusyError`` before anything is done (not recorded: nothing ran).
2. **read_database**: read the current versions in one snapshot
   (``MemoryProjectionSource``).
3. **render**: the pure renderer (``render.render_projection``).
4. **write_files**: make the directory hold exactly that (``LockedTarget.sync``).
5. Record the outcome (``audit.record_projection_outcome``) in a transaction of
   its own: ``memory.projection.completed``, or ``memory.projection.failed`` with
   the step and a closed code. Recording that fails makes the run not ``ok``.

The lock is held from step 1 until the outcome is recorded, so an older snapshot
can never be written over a newer one, and a reader that takes the lock (PAW-047)
always sees the outcome of the files it finds. Recording is not interrupted by a
cancellation: it finishes, then the cancellation propagates.

The file-system work runs in a thread; a cancellation (SIGTERM from systemd)
waits for the thread's current step to finish, so a file is never left
half-written and the lock is never released while a write is going on, then
records ``<step>:CancelledError`` and propagates.
A lock the thread took after the cancellation is released at once (the runner
may live in a long-lived process, Decision 0038 8).

Each file is replaced atomically, but a run that fails while writing can leave
some directories of the new snapshot next to others of the old one; the next
successful run repairs that. A reader that copies the directory (PAW-047) takes
the marker's lock and copies only after a completed run (Decision 0038 9).

The caller (``paw_backend.cli.memory_projection``) turns ``ok`` into the exit
code the scheduler watches (Decision 0038 6).
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from paw_backend.db import Database
from paw_backend.memory.projection.audit import (
    ProjectionAction,
    record_projection_outcome,
)
from paw_backend.memory.projection.records import (
    ProjectedMemory,
    ProjectionPlan,
    ProjectionRunResult,
    ProjectionStep,
    WriteReport,
)
from paw_backend.memory.projection.render import render_projection
from paw_backend.memory.projection.source import MemoryProjectionSource
from paw_backend.memory.projection.writer import (
    LockedTarget,
    ProjectionBusyError,
    ProjectionTargetError,
    open_target,
    system_home_directories,
)

Clock = Callable[[], datetime]


class ProjectionSource(Protocol):
    async def current_versions(self) -> Sequence[ProjectedMemory]: ...


class OutcomeRecorder(Protocol):
    def __call__(
        self, action: ProjectionAction, reason: str, *, occurred_at: datetime
    ) -> Awaitable[None]: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _in_thread[T](
    function: Callable[..., T],
    *arguments: object,
    discard: Callable[[T], object] | None = None,
) -> T:
    """Run ``function`` in a thread; a cancellation waits for it, then propagates.

    What the thread returns after the cancellation never reaches the caller, so
    ``discard`` gets it (``LockedTarget.close``: the lock the thread took must
    not stay held by a long-lived process).
    """
    future = asyncio.ensure_future(asyncio.to_thread(function, *arguments))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if (
            discard is not None
            and not future.cancelled()
            and future.exception() is None
        ):
            with contextlib.suppress(Exception):
                discard(future.result())
        raise


async def _to_the_end(
    awaitable: Awaitable[object],
) -> tuple[bool, asyncio.CancelledError | None]:
    """Await ``awaitable`` to its end even if the caller is cancelled meanwhile.

    Returns whether it succeeded and the caller's cancellation (to re-raise once
    the rest is done). A failure of ``awaitable`` itself (an exception, or its
    own cancellation) is ``False``, never raised.
    """
    task = asyncio.ensure_future(awaitable)
    interrupted: asyncio.CancelledError | None = None
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as failure:
            if task.cancelled():
                return False, interrupted
            interrupted = interrupted or failure
            continue
        except Exception:
            return False, interrupted
        return True, interrupted


def _close(target: LockedTarget) -> None:
    target.close()


def _code(error: BaseException) -> str:
    if isinstance(error, ProjectionTargetError):
        return error.problem.value
    return type(error).__name__


class MemoryProjectionRunner:
    """Project PostgreSQL's memories into ``root`` (see the module docstring)."""

    def __init__(
        self,
        database: Database,
        root: str | Path,
        *,
        protected_homes: Collection[str] | None = None,
        clock: Clock = _utc_now,
        source: ProjectionSource | None = None,
        recorder: OutcomeRecorder | None = None,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._root = str(root)
        self._protected = (
            system_home_directories()
            if protected_homes is None
            else tuple(protected_homes)
        )
        self._clock = clock
        self._source = source or MemoryProjectionSource(database)
        self._recorder = recorder or self._record

    async def _record(
        self, action: ProjectionAction, reason: str, *, occurred_at: datetime
    ) -> None:
        await record_projection_outcome(
            self._database, action, reason, occurred_at=occurred_at
        )

    async def run(self) -> ProjectionRunResult:
        """One run. Raises ``ProjectionBusyError`` (nothing done); returns the rest."""
        failed_step: ProjectionStep | None = None
        error: str | None = None
        cancelled: asyncio.CancelledError | None = None
        plan: ProjectionPlan | None = None
        report = WriteReport()
        target: LockedTarget | None = None
        step = ProjectionStep.CHECK_TARGET
        audited = False
        try:
            try:
                target = await _in_thread(
                    open_target, self._root, self._protected, discard=_close
                )
                step = ProjectionStep.READ_DATABASE
                memories = await self._source.current_versions()
                step = ProjectionStep.RENDER
                plan = render_projection(memories)
                step = ProjectionStep.WRITE_FILES
                report = await _in_thread(target.sync, plan)
            except ProjectionBusyError:
                raise
            except Exception as failure:
                failed_step, error = step, _code(failure)
            except asyncio.CancelledError as failure:
                failed_step, error, cancelled = step, _code(failure), failure
            if failed_step is None and plan is not None:
                action = ProjectionAction.COMPLETED
                reason = (
                    f"memories={plan.memories} written={report.written} "
                    f"removed={report.removed} redacted={plan.redactions}"
                )
                if plan.truncations:
                    reason += f" truncated={plan.truncations}"
            else:
                action = ProjectionAction.FAILED
                reason = f"{failed_step.value}:{error}"
            # Still holding the lock (see the module docstring).
            audited, interrupted = await _to_the_end(
                self._recorder(action, reason, occurred_at=self._clock())
            )
            cancelled = cancelled or interrupted
        finally:
            if target is not None:
                try:
                    await _in_thread(target.close)
                except asyncio.CancelledError as failure:
                    cancelled = cancelled or failure
                except OSError:
                    pass
        if cancelled is not None:
            raise cancelled
        return ProjectionRunResult(
            memories=0 if plan is None else plan.memories,
            redactions=0 if plan is None else plan.redactions,
            truncations=0 if plan is None else plan.truncations,
            report=report,
            failed_step=failed_step,
            error=error,
            audited=audited,
        )


__all__ = [
    "MemoryProjectionRunner",
    "OutcomeRecorder",
    "ProjectionSource",
]
