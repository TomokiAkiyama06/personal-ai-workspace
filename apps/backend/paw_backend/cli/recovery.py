"""``python -m paw_backend.cli recovery-backup-run | -check | recovery-restore``.

The Recovery Repository (PAW-047, Decision 0054 Proposed):

* ``recovery-backup-run``: one backup (the systemd timer of
  ``apps/backend/deploy/systemd`` runs it every 30 minutes; run by hand it is the
  manual backup). Connects with ``PAW_DATABASE_URL`` (SELECT is all it needs),
  reads ``PAW_MEMORY_PROJECTION_DIR`` and writes ``PAW_RECOVERY_REPOSITORY_DIR``.
* ``recovery-backup-check [--max-age-minutes N]``: read-only monitor.
* ``recovery-restore [--apply]``: a dry run by default; ``--apply`` restores
  into an **empty** workspace in one transaction. Connects with
  ``PAW_MIGRATION_DATABASE_URL`` (the table owner, as ``alembic upgrade`` of the
  fresh installation) and reads ``PAW_RECOVERY_REPOSITORY_DIR``.

Exit codes (the convention of ``paw_backend.cli.memory_projection``):

* ``0``  success: (``run``) backed up, committed and pushed as needed, and
  recorded; (``check``) the last run completed within ``--max-age-minutes``;
  (``restore``) the plan was shown (dry run) or the restore was applied;
* ``1``  refused: a usage error, another backup or restore holds the checkout's
  lock, or (``restore``) a check failed (source, format, checksums, schema, a
  target that is not empty): nothing was written;
* ``2``  environment error: invalid configuration, a URL or directory not set,
  the database cannot be reached;
* ``3``  FAILED: (``run``) a step failed or the outcome could not be recorded;
  (``check``) the last run failed or none completed recently; (``restore``) the
  write failed and was rolled back.

Neither a URL nor a directory is ever an argument; no exception message, path,
URL or git output is printed (only closed codes and exception types).
"""

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import NoReturn, TextIO

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.memory.projection import system_home_directories
from paw_backend.recovery import (
    BackupRunResult,
    RecoveryAction,
    RecoveryBackupRunner,
    RecoveryBusyError,
    RecoveryRestorer,
    RestoreResult,
    backup_status,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2
EXIT_FAILED = 3

RUN_COMMAND = "recovery-backup-run"
CHECK_COMMAND = "recovery-backup-check"
RESTORE_COMMAND = "recovery-restore"
COMMANDS = frozenset({RUN_COMMAND, CHECK_COMMAND, RESTORE_COMMAND})

# The timer runs every 30 minutes; a monitor that sees no completed run for 90
# minutes (three runs) reports it.
DEFAULT_MAX_AGE_MINUTES = 90

_DATABASE_HINT = (
    "check the database URL, that PostgreSQL is reachable and that the "
    "migrations are applied (alembic upgrade head)."
)


class _Parser(argparse.ArgumentParser):
    """A parser that never echoes what it was given (see ``owner._Parser``)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(
            f"{self.prog}: invalid arguments (see --help). The directories and the "
            "database URLs are configured in the environment, never with an "
            "argument.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_REFUSED)


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer") from None
    if not 1 <= number <= 10_080:
        raise argparse.ArgumentTypeError("must be from 1 to 10080")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description="Recovery Repository backup and restore (PAW-047, Decision 0054).",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        RUN_COMMAND,
        help=(
            "write the recovery files into PAW_RECOVERY_REPOSITORY_DIR, commit "
            "what changed and push it"
        ),
        allow_abbrev=False,
    )
    check = commands.add_parser(
        CHECK_COMMAND,
        help=(
            "read-only: exit 3 if the last backup failed or none completed "
            "within --max-age-minutes"
        ),
        allow_abbrev=False,
    )
    check.add_argument(
        "--max-age-minutes",
        type=_positive,
        default=DEFAULT_MAX_AGE_MINUTES,
        help=(
            "how old the last completed backup may be "
            f"(default {DEFAULT_MAX_AGE_MINUTES})"
        ),
    )
    restore = commands.add_parser(
        RESTORE_COMMAND,
        help=(
            "check PAW_RECOVERY_REPOSITORY_DIR and show what would be restored "
            "(a dry run); --apply restores into an empty workspace"
        ),
        allow_abbrev=False,
    )
    restore.add_argument(
        "--apply",
        action="store_true",
        help="write the rows (one transaction; the workspace must be empty)",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    protected_homes: Sequence[str] | None = None,
    clock=None,
) -> int:
    """Run one command. ``protected_homes`` and ``clock`` are for tests only."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            arguments = build_parser().parse_args(argv)
        except SystemExit as stop:
            return stop.code if isinstance(stop.code, int) else EXIT_OK
        try:
            return _run(arguments, err, protected_homes, clock)
        except Exception as error:
            _say(
                err,
                f"Unexpected error ({type(error).__name__}); details are not shown.",
            )
            return EXIT_ENVIRONMENT


def _say(stream: TextIO | None, message: str) -> None:
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        print(message, file=stream)


def _now() -> datetime:
    return datetime.now(UTC)


def _run(arguments, err, protected_homes, clock) -> int:
    try:
        settings = Settings()
    except ValidationError as error:
        names = sorted(
            {
                "PAW_" + str(entry["loc"][0]).upper()
                for entry in error.errors()
                if entry["loc"]
            }
        )
        _say(err, f"Invalid configuration: check {', '.join(names) or 'PAW_*'}")
        return EXIT_ENVIRONMENT
    clock = clock or _now
    homes = (
        system_home_directories(settings.repository_min_linux_uid)
        if protected_homes is None
        else tuple(protected_homes)
    )
    if arguments.command == RESTORE_COMMAND:
        return _restore(settings, arguments.apply, homes, clock, err)
    if settings.database_url is None:
        _say(err, "PAW_DATABASE_URL is not set.")
        return EXIT_ENVIRONMENT
    try:
        if arguments.command == CHECK_COMMAND:
            return asyncio.run(_check(settings, arguments.max_age_minutes, clock, err))
        missing = [
            name
            for name, value in (
                ("PAW_RECOVERY_REPOSITORY_DIR", settings.recovery_repository_dir),
                ("PAW_MEMORY_PROJECTION_DIR", settings.memory_projection_dir),
            )
            if value is None
        ]
        if missing:
            _say(err, f"{', '.join(missing)} not set: the backup needs both.")
            return EXIT_ENVIRONMENT
        result = asyncio.run(_backup(settings, homes, clock))
    except asyncio.CancelledError:
        _say(
            err,
            "FAILED: the backup was terminated (SIGTERM) before it finished. It "
            "was recorded as recovery.backup.failed if the database accepted it.",
        )
        return EXIT_FAILED
    except RecoveryBusyError:
        _say(
            err,
            "Refused: another recovery backup or restore is in progress (it holds "
            "the checkout's lock). Nothing was done.",
        )
        return EXIT_REFUSED
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        _say(err, f"Database error ({type(error).__name__}): {_DATABASE_HINT}")
        return EXIT_ENVIRONMENT
    return _show_backup(result, err)


async def _backup(settings: Settings, homes, clock) -> BackupRunResult:
    database = Database(settings)
    runner = RecoveryBackupRunner(
        database,
        settings.recovery_repository_dir,
        settings.memory_projection_dir,
        protected_homes=homes,
        clock=clock,
        git_timeout=settings.recovery_git_timeout_seconds,
    )
    try:
        with _cancel_on_sigterm():
            return await runner.run()
    finally:
        await database.dispose()


@contextlib.contextmanager
def _cancel_on_sigterm():
    """Turn SIGTERM into a cancellation of the running task (see ``retention``)."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    try:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError, ValueError):
        yield
        return
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def _time(value: datetime | None) -> str:
    return "never" if value is None else value.isoformat(timespec="seconds")


async def _check(settings: Settings, max_age_minutes: int, clock, err) -> int:
    database = Database(settings)
    try:
        status = await backup_status(database)
    finally:
        await database.dispose()
    if status.last_action == RecoveryAction.BACKUP_FAILED.value:
        _say(
            err,
            f"FAILED: the last recovery backup ({_time(status.last_run_at)}) failed "
            f"({status.last_reason}); last completed: "
            f"{_time(status.last_completed_at)}.",
        )
        return EXIT_FAILED
    limit = clock() - timedelta(minutes=max_age_minutes)
    if status.last_completed_at is None or status.last_completed_at < limit:
        _say(
            err,
            f"FAILED: no recovery backup completed in the last {max_age_minutes} "
            f"minute(s) (last completed: {_time(status.last_completed_at)}). Check "
            "the timer (paw-recovery-backup.timer).",
        )
        return EXIT_FAILED
    _say(
        err,
        f"OK: the last recovery backup completed at "
        f"{_time(status.last_completed_at)} ({status.last_reason}).",
    )
    return EXIT_OK


def _show_backup(result: BackupRunResult, err: TextIO | None) -> int:
    counts = (
        f"files={result.files} written={result.written} removed={result.removed} "
        f"committed={'yes' if result.committed else 'no'} "
        f"pushed={'yes' if result.pushed else 'no'} redacted={result.redactions}"
    )
    if result.truncations:
        counts += f" truncated={result.truncations}"
    recorded = (
        "The outcome was recorded in audit_events."
        if result.audited
        else "The outcome could not be recorded in audit_events."
    )
    if result.ok:
        _say(err, f"Recovery backup completed: {counts}. {recorded}")
        return EXIT_OK
    if result.failed_step is None:
        _say(
            err,
            f"FAILED: the backup was made ({counts}), but the outcome could not be "
            "recorded in audit_events.",
        )
        return EXIT_FAILED
    _say(err, f"FAILED at {result.failed_step.value} ({result.error}). {recorded}")
    return EXIT_FAILED


def _restore(settings: Settings, apply: bool, homes, clock, err) -> int:
    if settings.migration_database_url is None:
        _say(
            err,
            "PAW_MIGRATION_DATABASE_URL is not set. A restore writes the users and "
            "the workspace's rows as the table owner (the role that ran alembic).",
        )
        return EXIT_ENVIRONMENT
    if settings.recovery_repository_dir is None:
        _say(
            err,
            "PAW_RECOVERY_REPOSITORY_DIR is not set: point it at a fresh clone of "
            "the Recovery Repository.",
        )
        return EXIT_ENVIRONMENT
    owner = settings.model_copy(
        update={"database_url": settings.migration_database_url}
    )
    try:
        result = asyncio.run(_restore_run(owner, apply, homes, clock))
    except RecoveryBusyError:
        _say(
            err,
            "Refused: a recovery backup or restore is in progress (it holds the "
            "checkout's lock). Nothing was done.",
        )
        return EXIT_REFUSED
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        _say(err, f"Database error ({type(error).__name__}): {_DATABASE_HINT}")
        return EXIT_ENVIRONMENT
    return _show_restore(result, apply, err)


async def _restore_run(settings: Settings, apply: bool, homes, clock) -> RestoreResult:
    database = Database(settings)
    restorer = RecoveryRestorer(
        database,
        settings.recovery_repository_dir,
        protected_homes=homes,
        clock=clock,
        git_timeout=settings.recovery_git_timeout_seconds,
    )
    try:
        return await restorer.run(apply=apply)
    finally:
        await database.dispose()


def _show_restore(result: RestoreResult, apply: bool, err: TextIO | None) -> int:
    if result.refused is not None:
        _say(
            err,
            f"Refused ({result.refused}): nothing was written. See Decision 0054 "
            "and the README (Recovery Repository) for what each code means.",
        )
        return EXIT_REFUSED
    data = result.data
    assert data is not None
    if result.failed is not None:
        _say(
            err,
            f"FAILED ({result.failed}): the restore was rolled back; nothing was "
            "written.",
        )
        return EXIT_FAILED
    counts = " ".join(f"{key}={value}" for key, value in data.counts.items())
    header = (
        f"Recovery format {data.manifest.recovery_format_version}, schema "
        f"{data.manifest.workspace_schema_version}, commit {data.commit[:12]}, "
        f"generated {_time(data.manifest.generated_at)}."
    )
    _say(err, header)
    if not apply:
        _say(
            err,
            f"Dry run: would restore {counts} (tasks: {data.tasks} summaries, "
            "not restored).",
        )
        _say(
            err,
            (
                "No workspace data was written (only the recovery.restore.planned "
                "audit row). Run again with --apply to restore."
            ),
        )
    else:
        _say(err, f"Restored: {counts}. Recorded as recovery.restore.applied.")
    _say(err, "Then:" if apply else "After --apply:")
    for step in result.manual_steps:
        _say(err, f"- {step}")
    return EXIT_OK
