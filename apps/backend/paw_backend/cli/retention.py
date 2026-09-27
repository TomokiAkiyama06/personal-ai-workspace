"""``python -m paw_backend.cli audit-retention-run | audit-retention-check`` (#117).

The scheduled side of ``audit_events`` retention (Decision 0027, Decision 0031):
the systemd timer of ``apps/backend/deploy/systemd`` (or a cron entry) runs
``audit-retention-run`` once a day; ``audit-retention-check`` is a read-only
probe for monitoring.

Exit codes (the convention of ``paw_backend.cli.owner``):

* ``0``  success: every step ran, the live partitions cover the checked span,
  and (``run``) the outcome was recorded in ``audit_events``;
* ``1``  refused or invalid: a usage error, an invalid policy value, or another
  run holds the maintenance lock (nothing was done);
* ``2``  environment error: the configuration is invalid,
  ``PAW_MIGRATION_DATABASE_URL`` is not set, or the database cannot be reached
  (nothing was done, and nothing could be recorded);
* ``3``  the maintenance FAILED: a step raised, or the partitions do not cover
  the checked span, or (``run``) the outcome could not be recorded. ``run``
  records the failure as ``audit.retention.maintenance_failed`` whenever the
  database accepts the row, and says on stderr whether it did.

Every non-zero code is a failure to the scheduler (systemd ``OnFailure=``, cron
mail), which is how a failed or missing run is noticed before the partitions
run out.

It connects with ``PAW_MIGRATION_DATABASE_URL`` only (Decision 0027 section 6:
partition DDL needs the table owner). There is deliberately no fallback to
``PAW_DATABASE_URL``: in the split-role deployment that is the application's
role, which can run none of the steps, and a single-role setup can set both
variables to the same URL. As in ``owner``, the URL is never an argument and no
exception message is printed (only its type): what is printed goes to stderr.
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Sequence
from typing import NoReturn, TextIO

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from paw_backend.authz.retention import (
    AuditRetentionService,
    MaintenanceAlreadyRunningError,
    MaintenanceRunResult,
    RetentionPolicy,
    default_policy,
    first_uncovered_moment,
    run_scheduled_maintenance,
)
from paw_backend.authz.retention.runner import DEFAULT_REQUIRED_MONTHS_AHEAD
from paw_backend.config import Settings
from paw_backend.db import Database

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2
EXIT_MAINTENANCE_FAILED = 3

RUN_COMMAND = "audit-retention-run"
CHECK_COMMAND = "audit-retention-check"
COMMANDS = frozenset({RUN_COMMAND, CHECK_COMMAND})

_DATABASE_HINT = (
    "check PAW_MIGRATION_DATABASE_URL, that PostgreSQL is reachable, that the "
    "migrations are applied (alembic upgrade head) and that the URL names the "
    "table owner (the migration role), not the application's role."
)


class _Parser(argparse.ArgumentParser):
    """A parser that never echoes what it was given (see ``owner._Parser``)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(
            f"{self.prog}: invalid arguments (see --help). The database is "
            "configured with PAW_MIGRATION_DATABASE_URL, never with an argument.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_REFUSED)


def _non_negative(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description=(
            "Scheduled audit_events retention (Decision 0027 / 0031). Reads "
            "PAW_MIGRATION_DATABASE_URL (the table owner) from the environment."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        RUN_COMMAND,
        help=(
            "create the next months' partitions, archive (and, if enabled, purge) "
            "old ones, verify the coverage and record the outcome"
        ),
        allow_abbrev=False,
    )
    run.add_argument(
        "--purge-after-days",
        type=_non_negative,
        default=None,
        help=(
            "drop archived partitions whose window ended this many days ago "
            "(irreversible; off unless given, as Decision 0027 recommends)"
        ),
    )
    check = commands.add_parser(
        CHECK_COMMAND,
        help="read-only: exit 3 unless the live partitions cover the coming months",
        allow_abbrev=False,
    )
    check.add_argument(
        "--months-ahead",
        type=_non_negative,
        default=DEFAULT_REQUIRED_MONTHS_AHEAD,
        help=(
            "calendar months after the current one that must be covered "
            f"(default {DEFAULT_REQUIRED_MONTHS_AHEAD})"
        ),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            arguments = build_parser().parse_args(argv)
        except SystemExit as stop:  # --help (0) or a usage error (1)
            return stop.code if isinstance(stop.code, int) else EXIT_OK
        try:
            return _run(arguments, err)
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


def _policy(arguments: argparse.Namespace) -> RetentionPolicy:
    recommended = default_policy()
    return RetentionPolicy(
        archive_after_days=recommended.archive_after_days,
        purge_after_days=arguments.purge_after_days,
        horizon_months=recommended.horizon_months,
    )


def _run(arguments: argparse.Namespace, err: TextIO | None) -> int:
    policy = None
    if arguments.command == RUN_COMMAND:
        try:
            policy = _policy(arguments)
        except ValueError:
            _say(
                err,
                "Refused: --purge-after-days must be at least the archive period "
                f"({default_policy().archive_after_days} days).",
            )
            return EXIT_REFUSED
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
    if settings.migration_database_url is None:
        _say(
            err,
            "PAW_MIGRATION_DATABASE_URL is not set. Retention runs as the table "
            "owner (the migration role); PAW_DATABASE_URL is not used.",
        )
        return EXIT_ENVIRONMENT
    connection = settings.model_copy(
        update={"database_url": settings.migration_database_url}
    )
    try:
        if arguments.command == RUN_COMMAND:
            result = asyncio.run(_maintain(connection, policy))
        else:
            return asyncio.run(_check(connection, arguments.months_ahead, err))
    except MaintenanceAlreadyRunningError:
        _say(
            err,
            "Refused: another audit retention run is already running (it holds "
            "the maintenance lock). Nothing was done.",
        )
        return EXIT_REFUSED
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        _say(err, f"Database error ({type(error).__name__}): {_DATABASE_HINT}")
        return EXIT_ENVIRONMENT
    return _show(result, err)


async def _maintain(
    settings: Settings, policy: RetentionPolicy | None
) -> MaintenanceRunResult:
    database = Database(settings)
    try:
        return await run_scheduled_maintenance(AuditRetentionService(database), policy)
    finally:
        await database.dispose()


async def _check(settings: Settings, months_ahead: int, err: TextIO | None) -> int:
    database = Database(settings)
    try:
        service = AuditRetentionService(database)
        existing = await service.existing_partitions()
        uncovered = first_uncovered_moment(service._now(), existing, months_ahead)
    finally:
        await database.dispose()
    if uncovered is not None:
        _say(
            err,
            f"FAILED: audit_events is not covered from "
            f"{uncovered.isoformat(timespec='seconds')} (checked {months_ahead} "
            "month(s) ahead): an INSERT then would fail. Run audit-retention-run "
            "and check its timer.",
        )
        return EXIT_MAINTENANCE_FAILED
    _say(err, f"OK: audit_events partitions cover {months_ahead} month(s) ahead.")
    return EXIT_OK


def _show(result: MaintenanceRunResult, err: TextIO | None) -> int:
    report = result.report
    counts = (
        f"created={len(report.created)} archived={len(report.archived)} "
        f"purged={len(report.purged)}"
    )
    recorded = (
        "The outcome was recorded in audit_events."
        if result.audited
        else "The outcome could not be recorded in audit_events."
    )
    if result.ok:
        _say(err, f"Audit retention completed: {counts}. {recorded}")
        return EXIT_OK
    if result.failed_step is None:
        _say(
            err,
            f"FAILED: every step ran ({counts}), but the outcome could not be "
            "recorded in audit_events.",
        )
        return EXIT_MAINTENANCE_FAILED
    detail = (
        f"not covered from {result.uncovered_from.isoformat(timespec='seconds')}"
        if result.uncovered_from is not None
        else result.error_type
    )
    _say(
        err,
        f"FAILED at {result.failed_step.value} ({detail}); before that: {counts}. "
        f"{recorded} {_DATABASE_HINT[0].upper()}{_DATABASE_HINT[1:]}",
    )
    return EXIT_MAINTENANCE_FAILED
