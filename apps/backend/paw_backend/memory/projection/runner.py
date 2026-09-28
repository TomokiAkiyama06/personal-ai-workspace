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

The lock is held from step 1 to the end of step 4, so an older snapshot can never
be written over a newer one. The file-system work runs in a thread; a
cancellation (SIGTERM from systemd) waits for the thread's current step to
finish, so a file is never left half-written and the lock is never released
while a write is going on, then records ``<step>:CancelledError`` and propagates.

The caller (``paw_backend.cli.memory_projection``) turns ``ok`` into the exit
code the scheduler watches (Decision 0038 6).
"""

import asyncio
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


async def _in_thread[T](function: Callable[..., T], *arguments: object) -> T:
    """Run ``function`` in a thread; a cancellation waits for it, then propagates."""
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
        raise


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
        try:
            try:
                target = await _in_thread(open_target, self._root, self._protected)
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
        finally:
            if target is not None:
                try:
                    await _in_thread(target.close)
                except asyncio.CancelledError as failure:
                    cancelled = cancelled or failure
                except OSError:
                    pass
        if failed_step is None and plan is not None:
            action = ProjectionAction.COMPLETED
            reason = (
                f"memories={plan.memories} written={report.written} "
                f"removed={report.removed} redacted={plan.redactions}"
            )
        else:
            action = ProjectionAction.FAILED
            reason = f"{failed_step.value}:{error}"
        try:
            await self._recorder(action, reason, occurred_at=self._clock())
            audited = True
        except Exception:
            audited = False
        if cancelled is not None:
            raise cancelled
        return ProjectionRunResult(
            memories=0 if plan is None else plan.memories,
            redactions=0 if plan is None else plan.redactions,
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
