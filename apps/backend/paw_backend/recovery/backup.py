"""One run of the Recovery Repository backup (PAW-047, Decision 0054 5).

1. **check_repository**: the checkout is a canonical directory outside every home
   and the projection, the top of a git work tree, with the marker (claimed when
   it is an empty clone), a branch with an upstream; take its lock. A second run
   at the same time gets ``RecoveryBusyError`` (not recorded: nothing ran).
2. **copy_memory**: take the projection's lock (waiting for a running
   projection), refuse its incomplete flag, check that the last recorded
   projection run completed, read its files (Decision 0038 9).
3. **read_database**: one snapshot (``RecoverySource``).
4. **render**: every file (``render_recovery``); the manifest is kept when
   nothing changed.
5. **write_files**: make the checkout's managed names hold exactly that.
6. **commit**: build the commit from the rendered bytes (never from the work
   tree or the checkout's index); commit only when the tree changed (one commit
   per run: the dirty check of REQUIREMENTS.md "Git schedule").
7. **push**: push when ``HEAD`` is ahead of what the remote last saw (a run
   after a failed push pushes the earlier commit: the retry). Never forced.

The outcome is recorded as ``recovery.backup.completed`` / ``failed``
(``audit.py``); the caller turns ``ok`` into the exit code the timer watches.
A write that fails midway leaves the checkout partly updated and uncommitted:
nothing is committed before every file is written, and the next run rewrites
the whole tree before its commit. A cancellation (SIGTERM) waits for the running
file-system or git step, records ``<step>:CancelledError`` and propagates.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import metadata
from pathlib import Path
from typing import Protocol

from paw_backend.db import Database
from paw_backend.memory.projection import ProjectionAction, projection_status
from paw_backend.memory.projection.records import ProjectionStatus
from paw_backend.memory.projection.runner import _in_thread, _to_the_end
from paw_backend.memory.projection.writer import system_home_directories
from paw_backend.recovery.audit import RecoveryAction, record_recovery_outcome
from paw_backend.recovery.files import (
    CheckoutProblem,
    ProjectionReader,
    RecoveryBusyError,
    RecoveryCheckout,
    RecoveryFilesError,
    check_directory_path,
    open_checkout,
    open_projection,
)
from paw_backend.recovery.format import MANAGED_ROOT_NAMES, MANIFEST_NAME
from paw_backend.recovery.git import (
    DEFAULT_TIMEOUT_SECONDS,
    RecoveryGit,
    RecoveryGitError,
    Upstream,
)
from paw_backend.recovery.render import RecoveryPlan, render_recovery
from paw_backend.recovery.schema import migration_head
from paw_backend.recovery.source import RecoverySnapshot, RecoverySource

Clock = Callable[[], datetime]


class BackupStep(StrEnum):
    CHECK_REPOSITORY = "check_repository"
    COPY_MEMORY = "copy_memory"
    READ_DATABASE = "read_database"
    RENDER = "render"
    WRITE_FILES = "write_files"
    COMMIT = "commit"
    PUSH = "push"


class ProjectionNotCompletedError(Exception):
    """The last recorded projection run did not complete: nothing is copied."""

    code = "projection_not_completed"

    def __init__(self) -> None:
        super().__init__("the last memory projection run did not complete")


class SnapshotSource(Protocol):
    async def snapshot(self) -> RecoverySnapshot: ...


class OutcomeRecorder(Protocol):
    def __call__(
        self, action: RecoveryAction, reason: str, *, occurred_at: datetime
    ) -> Awaitable[None]: ...


@dataclass(frozen=True, slots=True)
class BackupRunResult:
    """What one run did. ``ok`` only when every step succeeded and was recorded."""

    files: int = 0
    written: int = 0
    removed: int = 0
    committed: bool = False
    pushed: bool = False
    redactions: int = 0
    truncations: int = 0
    failed_step: BackupStep | None = None
    error: str | None = None
    audited: bool = False

    @property
    def ok(self) -> bool:
        return self.failed_step is None and self.audited


def _utc_now() -> datetime:
    return datetime.now(UTC)


def workspace_version() -> str:
    try:
        return metadata.version("paw-backend")
    except metadata.PackageNotFoundError:
        return "unknown"


def _code(error: BaseException) -> str:
    if isinstance(error, RecoveryFilesError):
        return error.problem.value
    if isinstance(error, RecoveryGitError):
        return error.problem.value
    if isinstance(error, ProjectionNotCompletedError):
        return error.code
    return type(error).__name__


def commit_message(plan: RecoveryPlan) -> str:
    counts = plan.counts
    return (
        "Recovery backup\n\n"
        f"users={counts['users']} deletions={counts['deletions']} "
        f"projects={counts['projects']} repos={counts['repos']} "
        f"memories={counts['memories']} memory_versions={counts['memory_versions']} "
        f"tasks={counts['tasks']} memory_files={counts['memory_files']}\n"
    )


class RecoveryBackupRunner:
    """Back the workspace up into the checkout (see the module docstring)."""

    def __init__(
        self,
        database: Database,
        repository_dir: str | Path,
        projection_dir: str | Path,
        *,
        protected_homes: Collection[str] | None = None,
        clock: Clock = _utc_now,
        source: SnapshotSource | None = None,
        recorder: OutcomeRecorder | None = None,
        git_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        projection_wait_seconds: float = 120.0,
        projection_poll_seconds: float = 1.0,
        schema_version: Callable[[], str] = migration_head,
        projection_state: Callable[[], Awaitable[ProjectionStatus]] | None = None,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database
        self._repository_dir = str(repository_dir)
        self._projection_dir = str(projection_dir)
        self._protected = (
            system_home_directories()
            if protected_homes is None
            else tuple(protected_homes)
        )
        self._clock = clock
        self._source = source or RecoverySource(database)
        self._recorder = recorder or self._record
        self._git_timeout = git_timeout
        self._projection_wait = projection_wait_seconds
        self._projection_poll = projection_poll_seconds
        self._schema_version = schema_version
        self._projection_state = projection_state or self._projection_completed

    async def _record(
        self, action: RecoveryAction, reason: str, *, occurred_at: datetime
    ) -> None:
        await record_recovery_outcome(
            self._database, action, reason, occurred_at=occurred_at
        )

    def _open(self) -> tuple[RecoveryGit, RecoveryCheckout, Upstream]:
        path = check_directory_path(
            self._repository_dir,
            self._protected,
            projection_dir=self._projection_dir,
        )
        git = RecoveryGit(path, timeout=self._git_timeout)
        git.check_top_level()
        upstream = git.upstream()
        checkout = open_checkout(
            path, self._protected, projection_dir=self._projection_dir, claim=True
        )
        return git, checkout, upstream

    async def _open_projection(self) -> ProjectionReader:
        """The projection's lock, waiting (without blocking a thread) for a run."""
        deadline = asyncio.get_running_loop().time() + self._projection_wait
        while True:
            try:
                return await _in_thread(
                    open_projection,
                    self._projection_dir,
                    discard=lambda value: value.close(),
                )
            except RecoveryFilesError as failure:
                if failure.problem is not CheckoutProblem.PROJECTION_BUSY:
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise
            await asyncio.sleep(self._projection_poll)

    async def _projection_completed(self) -> ProjectionStatus:
        return await projection_status(self._database)

    async def run(self) -> BackupRunResult:
        """One run. Raises ``RecoveryBusyError`` (nothing done); returns the rest."""
        step = BackupStep.CHECK_REPOSITORY
        failed_step: BackupStep | None = None
        error: str | None = None
        cancelled: asyncio.CancelledError | None = None
        checkout: RecoveryCheckout | None = None
        reader: ProjectionReader | None = None
        plan: RecoveryPlan | None = None
        written = removed = 0
        committed = pushed = False
        try:
            try:
                opened = await _in_thread(
                    self._open, discard=lambda value: value[1].close()
                )
                git, checkout, upstream = opened
                step = BackupStep.COPY_MEMORY
                reader = await self._open_projection()
                status = await self._projection_state()
                if status.last_action != ProjectionAction.COMPLETED.value:
                    raise ProjectionNotCompletedError()
                memory_files = await _in_thread(reader.read)
                await _in_thread(reader.close)
                reader = None
                step = BackupStep.READ_DATABASE
                snapshot = await self._source.snapshot()
                step = BackupStep.RENDER
                try:
                    previous = await _in_thread(checkout.read_file, MANIFEST_NAME)
                except RecoveryFilesError:
                    previous = None  # not a regular file: rewritten below
                plan = render_recovery(
                    snapshot,
                    memory_files,
                    schema_version=self._schema_version(),
                    workspace_version=workspace_version(),
                    now=self._clock(),
                    previous_manifest=previous,
                )
                step = BackupStep.WRITE_FILES
                written, removed = await _in_thread(checkout.sync, plan.files)
                step = BackupStep.COMMIT
                committed = await _in_thread(
                    git.commit_files,
                    plan.files,
                    commit_message(plan),
                    MANAGED_ROOT_NAMES,
                )
                step = BackupStep.PUSH
                if await _in_thread(git.needs_push, upstream):
                    await _in_thread(git.push, upstream)
                    pushed = True
            except RecoveryBusyError:
                raise
            except Exception as failure:
                failed_step, error = step, _code(failure)
            except asyncio.CancelledError as failure:
                failed_step, error, cancelled = step, _code(failure), failure
            if failed_step is None and plan is not None:
                action = RecoveryAction.BACKUP_COMPLETED
                reason = (
                    f"files={len(plan.files)} written={written} removed={removed} "
                    f"commit={int(committed)} push={int(pushed)} "
                    f"redacted={plan.redactions}"
                )
                if plan.truncations:
                    reason += f" truncated={plan.truncations}"
            else:
                action = RecoveryAction.BACKUP_FAILED
                reason = f"{failed_step.value}:{error}"
            audited, interrupted = await _to_the_end(
                self._recorder(action, reason, occurred_at=self._clock())
            )
            cancelled = cancelled or interrupted
        finally:
            for opened_resource in (reader, checkout):
                if opened_resource is None:
                    continue
                try:
                    await _in_thread(opened_resource.close)
                except asyncio.CancelledError as failure:
                    cancelled = cancelled or failure
                except OSError:
                    pass
        if cancelled is not None:
            raise cancelled
        return BackupRunResult(
            files=0 if plan is None else len(plan.files),
            written=written,
            removed=removed,
            committed=committed,
            pushed=pushed,
            redactions=0 if plan is None else plan.redactions,
            truncations=0 if plan is None else plan.truncations,
            failed_step=failed_step,
            error=error,
            audited=audited,
        )


__all__ = [
    "BackupRunResult",
    "BackupStep",
    "OutcomeRecorder",
    "ProjectionNotCompletedError",
    "RecoveryBackupRunner",
    "SnapshotSource",
    "commit_message",
    "workspace_version",
]
