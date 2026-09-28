"""``python -m paw_backend.cli memory-projection-run | memory-projection-check``.

The Memory Markdown Projection (PAW-045, Decision 0038 Proposed): the systemd
timer of ``apps/backend/deploy/systemd`` (or a cron entry) runs
``memory-projection-run`` every few minutes; ``memory-projection-check`` is a
read-only probe for monitoring.

Exit codes (the convention of ``paw_backend.cli.retention``):

* ``0``  success: the directory holds the projection of the current database
  and (``run``) the outcome was recorded in ``audit_events``; (``check``) the
  last run succeeded and a run completed within ``--max-age-minutes``;
* ``1``  refused: a usage error, or another run holds the projection's lock
  (nothing was done);
* ``2``  environment error: the configuration is invalid, ``PAW_DATABASE_URL``
  or ``PAW_MEMORY_PROJECTION_DIR`` is not set, or the database cannot be reached
  (nothing could be recorded);
* ``3``  the projection FAILED: the directory was refused (inside a git work
  tree or a home directory, not empty and not the projection's, not owned by this
  user, ...), the read, the rendering or the writing failed, the run was
  terminated (SIGTERM), or the outcome could not be recorded; (``check``) the
  last run failed or none completed recently. ``run`` records the failure as
  ``memory.projection.failed`` whenever the database accepts the row.

Every non-zero code is a failure to the scheduler (systemd ``OnFailure=``, cron
mail): that is the failure notification of Decision 0038 6, next to the audit row.

It connects with ``PAW_DATABASE_URL`` (the application's role: SELECT on the
memory tables and INSERT on ``audit_events`` are all it needs). Neither the URL
nor the directory is ever an argument, and no exception message or path is
printed (only closed codes and exception types): what is printed goes to stderr.
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
from paw_backend.memory.projection import (
    MemoryProjectionRunner,
    ProjectionAction,
    ProjectionBusyError,
    ProjectionRunResult,
    projection_status,
    system_home_directories,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2
EXIT_PROJECTION_FAILED = 3

RUN_COMMAND = "memory-projection-run"
CHECK_COMMAND = "memory-projection-check"
COMMANDS = frozenset({RUN_COMMAND, CHECK_COMMAND})

# The timer runs every 5 minutes; a monitor that sees no completed run for 30
# minutes (six runs) reports it. The Git backup of PAW-047 runs every 30 minutes.
DEFAULT_MAX_AGE_MINUTES = 30

_DATABASE_HINT = (
    "check PAW_DATABASE_URL, that PostgreSQL is reachable and that the "
    "migrations are applied (alembic upgrade head)."
)


class _Parser(argparse.ArgumentParser):
    """A parser that never echoes what it was given (see ``owner._Parser``)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(
            f"{self.prog}: invalid arguments (see --help). The directory is "
            "configured with PAW_MEMORY_PROJECTION_DIR and the database with "
            "PAW_DATABASE_URL, never with an argument.",
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
        description=(
            "Memory Markdown Projection (PAW-045, Decision 0038). Reads "
            "PAW_DATABASE_URL and PAW_MEMORY_PROJECTION_DIR from the environment."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        RUN_COMMAND,
        help=(
            "write the Markdown projection of every memory's current version "
            "into PAW_MEMORY_PROJECTION_DIR and record the outcome"
        ),
        allow_abbrev=False,
    )
    check = commands.add_parser(
        CHECK_COMMAND,
        help=(
            "read-only: exit 3 if the last run failed or none completed within "
            "--max-age-minutes"
        ),
        allow_abbrev=False,
    )
    check.add_argument(
        "--max-age-minutes",
        type=_positive,
        default=DEFAULT_MAX_AGE_MINUTES,
        help=(
            f"how old the last completed run may be (default {DEFAULT_MAX_AGE_MINUTES})"
        ),
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
        except SystemExit as stop:  # --help (0) or a usage error (1)
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
    if settings.database_url is None:
        _say(err, "PAW_DATABASE_URL is not set.")
        return EXIT_ENVIRONMENT
    clock = clock or _now
    try:
        if arguments.command == CHECK_COMMAND:
            return asyncio.run(_check(settings, arguments.max_age_minutes, clock, err))
        if settings.memory_projection_dir is None:
            _say(
                err,
                "PAW_MEMORY_PROJECTION_DIR is not set: the projection needs a "
                "dedicated directory (outside every repository and home directory).",
            )
            return EXIT_ENVIRONMENT
        homes = (
            system_home_directories(settings.repository_min_linux_uid)
            if protected_homes is None
            else tuple(protected_homes)
        )
        result = asyncio.run(_project(settings, homes, clock))
    except asyncio.CancelledError:
        _say(
            err,
            "FAILED: the run was terminated (SIGTERM, e.g. systemd's "
            "TimeoutStartSec) before it finished. It was recorded as "
            "memory.projection.failed if the database accepted it.",
        )
        return EXIT_PROJECTION_FAILED
    except ProjectionBusyError:
        _say(
            err,
            "Refused: another memory projection run is in progress (it holds the "
            "projection's lock). Nothing was done.",
        )
        return EXIT_REFUSED
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        _say(err, f"Database error ({type(error).__name__}): {_DATABASE_HINT}")
        return EXIT_ENVIRONMENT
    return _show(result, err)


async def _project(settings: Settings, homes, clock) -> ProjectionRunResult:
    database = Database(settings)
    runner = MemoryProjectionRunner(
        database, settings.memory_projection_dir, protected_homes=homes, clock=clock
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
        status = await projection_status(database)
    finally:
        await database.dispose()
    if status.last_action == ProjectionAction.FAILED.value:
        _say(
            err,
            f"FAILED: the last memory projection run ({_time(status.last_run_at)}) "
            f"failed ({status.last_reason}); last completed: "
            f"{_time(status.last_completed_at)}.",
        )
        return EXIT_PROJECTION_FAILED
    limit = clock() - timedelta(minutes=max_age_minutes)
    if status.last_completed_at is None or status.last_completed_at < limit:
        _say(
            err,
            "FAILED: no memory projection run completed in the last "
            f"{max_age_minutes} minute(s) (last completed: "
            f"{_time(status.last_completed_at)}). Check the timer "
            "(paw-memory-projection.timer).",
        )
        return EXIT_PROJECTION_FAILED
    _say(
        err,
        f"OK: the last memory projection run completed at "
        f"{_time(status.last_completed_at)} ({status.last_reason}).",
    )
    return EXIT_OK


def _show(result: ProjectionRunResult, err: TextIO | None) -> int:
    report = result.report
    counts = (
        f"memories={result.memories} written={report.written} "
        f"unchanged={report.unchanged} removed={report.removed} "
        f"unmanaged={report.unmanaged} redacted={result.redactions}"
    )
    recorded = (
        "The outcome was recorded in audit_events."
        if result.audited
        else "The outcome could not be recorded in audit_events."
    )
    if result.ok:
        _say(err, f"Memory projection completed: {counts}. {recorded}")
        return EXIT_OK
    if result.failed_step is None:
        _say(
            err,
            f"FAILED: the projection was written ({counts}), but the outcome could "
            "not be recorded in audit_events.",
        )
        return EXIT_PROJECTION_FAILED
    _say(
        err,
        f"FAILED at {result.failed_step.value} ({result.error}). {recorded}",
    )
    return EXIT_PROJECTION_FAILED
