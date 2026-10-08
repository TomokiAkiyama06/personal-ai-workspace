"""``python -m paw_backend.cli deploy-*`` (PAW-068, Issue #54, Decision 0079).

The database side of an update, run by the release tool
(``apps/backend/deploy/release/paw_release.py``) with the venv of a release, or
by hand:

* ``deploy-status``: read-only; prints JSON (schema revision, maintenance, open
  tasks, drain) on stdout.
* ``deploy-precheck``: read-only; the pre-update health / recovery checks that
  need the database: it is reachable and migrated, no maintenance is on, the
  ``audit_events`` partitions reach the end of next month (Decision 0031, the
  same check as ``audit-retention-check``), the last Recovery backup completed
  within ``--backup-max-age-minutes`` (as ``recovery-backup-check``) and
  ``PAW_RECOVERY_REPOSITORY_DIR`` is set. Prints JSON; exit 3 when one failed.
* ``deploy-maintenance-begin`` / ``deploy-drain`` / ``deploy-maintenance-end``:
  stop starting tasks, hold and drain the running ones, resume them
  (``paw_backend.deploy.maintenance``).
* ``deploy-restore-point-create`` / ``-verify`` / ``-restore``: the database
  restore point (``paw_backend.deploy.restore_points``).

Exit codes (the convention of the other commands): ``0`` success; ``1`` refused
(usage, an invalid value, a restore point that is not verified); ``2``
environment (configuration, ``PAW_MIGRATION_DATABASE_URL`` /
``PAW_DEPLOY_ADMIN_DATABASE_URL`` not set, the database cannot be reached); ``3``
failed (a check failed, the drain timed out, a restore point could not be made,
verified or restored).

Everything connects with ``PAW_MIGRATION_DATABASE_URL`` (the table owner: the
maintenance row is the owner's to write, and a restore point must contain every
table). URLs and exception messages are never printed.
"""

import argparse
import asyncio
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn, TextIO

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from paw_backend.authz.retention import AuditRetentionService, first_uncovered_moment
from paw_backend.authz.retention.runner import DEFAULT_REQUIRED_MONTHS_AHEAD
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.deploy.audit import DeployAction, record_deploy_event
from paw_backend.deploy.maintenance import (
    DeployMaintenance,
    InvalidReleaseNameError,
    MaintenanceState,
    task_counts,
)
from paw_backend.deploy.restore_points import RestorePointError, RestorePoints
from paw_backend.projects.task_gate import ProjectStateGate
from paw_backend.recovery.audit import RecoveryAction, backup_status
from paw_backend.tasks import TaskService
from paw_backend.tasks.queueing import TaskQueue

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2
EXIT_FAILED = 3

STATUS_COMMAND = "deploy-status"
PRECHECK_COMMAND = "deploy-precheck"
BEGIN_COMMAND = "deploy-maintenance-begin"
DRAIN_COMMAND = "deploy-drain"
END_COMMAND = "deploy-maintenance-end"
CREATE_COMMAND = "deploy-restore-point-create"
VERIFY_COMMAND = "deploy-restore-point-verify"
RESTORE_COMMAND = "deploy-restore-point-restore"
COMMANDS = frozenset(
    {
        STATUS_COMMAND,
        PRECHECK_COMMAND,
        BEGIN_COMMAND,
        DRAIN_COMMAND,
        END_COMMAND,
        CREATE_COMMAND,
        VERIFY_COMMAND,
        RESTORE_COMMAND,
    }
)

# The Recovery backup timer runs every 30 minutes (Decision 0054 5): three runs.
DEFAULT_BACKUP_MAX_AGE_MINUTES = 90

_DATABASE_HINT = (
    "check PAW_MIGRATION_DATABASE_URL, that PostgreSQL is reachable and that the "
    "migrations are applied."
)


class _Parser(argparse.ArgumentParser):
    """A parser that never echoes what it was given (see ``owner._Parser``)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: invalid arguments (see --help).", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return number


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("must be an absolute path")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description=(
            "The database side of an update (Decision 0079). Reads "
            "PAW_MIGRATION_DATABASE_URL (and, for verify / restore, "
            "PAW_DEPLOY_ADMIN_DATABASE_URL) from the environment."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        STATUS_COMMAND, help="read-only: JSON status", allow_abbrev=False
    )
    precheck = commands.add_parser(
        PRECHECK_COMMAND,
        help="read-only: the pre-update checks (exit 3 when one failed)",
        allow_abbrev=False,
    )
    precheck.add_argument(
        "--months-ahead", type=int, default=DEFAULT_REQUIRED_MONTHS_AHEAD
    )
    precheck.add_argument(
        "--backup-max-age-minutes",
        type=_positive,
        default=DEFAULT_BACKUP_MAX_AGE_MINUTES,
    )
    begin = commands.add_parser(
        BEGIN_COMMAND, help="no task starts until the end", allow_abbrev=False
    )
    begin.add_argument("--from-release")
    begin.add_argument("--to-release")
    drain = commands.add_parser(
        DRAIN_COMMAND,
        help="hold the running tasks and wait until nothing runs (exit 3: timeout)",
        allow_abbrev=False,
    )
    drain.add_argument("--timeout-seconds", type=_positive, required=True)
    drain.add_argument("--poll-seconds", type=_positive, default=5)
    commands.add_parser(
        END_COMMAND, help="resume the held tasks and the queue", allow_abbrev=False
    )
    for name, help_text in (
        (CREATE_COMMAND, "pg_dump the database into the restore point directory"),
        (VERIFY_COMMAND, "restore a point into a scratch database and check it"),
        (RESTORE_COMMAND, "put a verified point back as the workspace database"),
    ):
        command = commands.add_parser(name, help=help_text, allow_abbrev=False)
        command.add_argument("--dir", type=_absolute, required=True)
        command.add_argument("--label", required=True)
        command.add_argument("--pg-dump", default="pg_dump")
        command.add_argument("--pg-restore", default="pg_restore")
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
        except SystemExit as stop:
            return stop.code if isinstance(stop.code, int) else EXIT_OK
        try:
            return _run(arguments, out, err)
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


def _emit(stream: TextIO, value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True), file=stream)


def _run(arguments: argparse.Namespace, out: TextIO, err: TextIO) -> int:
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
            "PAW_MIGRATION_DATABASE_URL is not set. The deploy commands run as the "
            "table owner (the migration role).",
        )
        return EXIT_ENVIRONMENT
    owner = settings.model_copy(
        update={"database_url": settings.migration_database_url}
    )
    command = arguments.command
    try:
        if command in (CREATE_COMMAND, VERIFY_COMMAND, RESTORE_COMMAND):
            return _restore_point(arguments, settings, out, err)
        return asyncio.run(_database_command(arguments, owner, out, err))
    except InvalidReleaseNameError:
        _say(err, "Refused: a release name is [A-Za-z0-9][A-Za-z0-9._-]{0,63}.")
        return EXIT_REFUSED
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        _say(err, f"Database error ({type(error).__name__}): {_DATABASE_HINT}")
        return EXIT_ENVIRONMENT


def _maintenance(database: Database) -> DeployMaintenance:
    gate = ProjectStateGate()
    return DeployMaintenance(
        database,
        TaskService(database, project_gate=gate),
        TaskQueue(database, project_gate=gate),
    )


def _maintenance_json(state: MaintenanceState | None) -> object:
    if state is None:
        return None
    return {
        "started_at": state.started_at.isoformat(timespec="seconds"),
        "from_release": state.from_release,
        "to_release": state.to_release,
    }


async def _status(database: Database, maintenance: DeployMaintenance) -> dict:
    async with database.session() as session:
        revisions = list(
            (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalars()
        )
    drain = await maintenance.drain_status()
    return {
        "schema_revision": revisions[0] if len(revisions) == 1 else None,
        "schema_revisions": sorted(revisions),
        "maintenance": _maintenance_json(await maintenance.state()),
        "tasks": await task_counts(database),
        "drain": {
            "active_claims": drain.active_claims,
            "running": drain.running,
            "held": drain.held,
            "drained": drain.drained,
        },
    }


async def _database_command(
    arguments: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO
) -> int:
    database = Database(settings)
    try:
        maintenance = _maintenance(database)
        command = arguments.command
        if command == STATUS_COMMAND:
            _emit(out, await _status(database, maintenance))
            return EXIT_OK
        if command == PRECHECK_COMMAND:
            return await _precheck(arguments, settings, database, maintenance, out)
        if command == BEGIN_COMMAND:
            created = await maintenance.begin(
                from_release=arguments.from_release, to_release=arguments.to_release
            )
            held = await maintenance.hold_running()
            if created and not await _record_quietly(
                database,
                DeployAction.MAINTENANCE_STARTED,
                f"from={arguments.from_release or '-'} "
                f"to={arguments.to_release or '-'} held={held}",
            ):
                _say(err, "The start could not be recorded in audit_events.")
            _say(
                err,
                ("Maintenance started" if created else "Maintenance was already on")
                + f": no task starts; {held} running task(s) held now.",
            )
            return EXIT_OK
        if command == DRAIN_COMMAND:
            if await maintenance.state() is None:
                _say(err, f"Refused: no maintenance is on (run {BEGIN_COMMAND}).")
                return EXIT_REFUSED
            status = await maintenance.drain(
                arguments.timeout_seconds, poll_seconds=arguments.poll_seconds
            )
            counts = (
                f"active_claims={status.active_claims} running={status.running} "
                f"held={status.held}"
            )
            if status.drained:
                _say(err, f"Drained: {counts}.")
                return EXIT_OK
            _say(
                err,
                f"FAILED: not drained after {arguments.timeout_seconds} s ({counts}). "
                "The maintenance stays on.",
            )
            return EXIT_FAILED
        # END_COMMAND
        report = await maintenance.end()
        # The row is gone and the queue runs: a failed audit row must not make
        # this look like a failed end (the release tool would report the system
        # as still in maintenance).
        if not await _record_quietly(
            database,
            DeployAction.MAINTENANCE_ENDED,
            f"resumed={report.resumed} remaining={report.remaining}",
        ):
            _say(err, "The end could not be recorded in audit_events.")
        _say(
            err,
            f"Maintenance ended: {report.resumed} task(s) resumed, "
            f"{report.remaining} still held (they are looked at again on the next "
            f"{END_COMMAND}).",
        )
        return EXIT_OK
    finally:
        await database.dispose()


async def _record_quietly(
    database: Database, action: DeployAction, reason: str
) -> bool:
    """Record ``action``; ``False`` (not an error) when the row could not be
    written: the maintenance itself already changed."""
    try:
        await record_deploy_event(database, action, reason)
    except (SQLAlchemyError, OSError):
        return False
    return True


async def _precheck(
    arguments: argparse.Namespace,
    settings: Settings,
    database: Database,
    maintenance: DeployMaintenance,
    out: TextIO,
) -> int:
    checks: list[dict[str, object]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    status = await _status(database, maintenance)
    check(
        "database",
        status["schema_revision"] is not None,
        f"revision={status['schema_revision'] or 'unknown'}",
    )
    check(
        "maintenance_off",
        status["maintenance"] is None,
        "off" if status["maintenance"] is None else "on (end it or roll back first)",
    )
    # Decision 0031 / the comment on Issue #54: the partitions must reach the end
    # of next month, as audit-retention-check checks.
    retention = AuditRetentionService(database)
    uncovered = first_uncovered_moment(
        retention._now(), await retention.existing_partitions(), arguments.months_ahead
    )
    check(
        "audit_retention",
        uncovered is None,
        f"covered {arguments.months_ahead} month(s) ahead"
        if uncovered is None
        else f"not covered from {uncovered.isoformat(timespec='seconds')}: run "
        "audit-retention-run and check paw-audit-retention.timer",
    )
    backup = await backup_status(database)
    fresh = (
        backup.last_action != RecoveryAction.BACKUP_FAILED.value
        and backup.last_completed_recorded_at is not None
        and (backup.checked_at - backup.last_completed_recorded_at).total_seconds()
        <= arguments.backup_max_age_minutes * 60
    )
    check(
        "recovery_backup",
        fresh,
        f"last completed {backup.last_completed_at.isoformat(timespec='seconds')}"
        if fresh and backup.last_completed_at is not None
        else f"no completed backup in the last {arguments.backup_max_age_minutes} "
        "minute(s) or the last run failed: check paw-recovery-backup.timer",
    )
    check(
        "recovery_repository",
        settings.recovery_repository_dir is not None,
        "configured"
        if settings.recovery_repository_dir is not None
        else "PAW_RECOVERY_REPOSITORY_DIR is not set",
    )
    ok = all(entry["ok"] for entry in checks)
    _emit(out, {"ok": ok, "checks": checks, "status": status})
    return EXIT_OK if ok else EXIT_FAILED


def _restore_point(
    arguments: argparse.Namespace, settings: Settings, out: TextIO, err: TextIO
) -> int:
    url = make_url(settings.migration_database_url.get_secret_value())
    points = RestorePoints(
        arguments.dir,
        recovery_directory=settings.recovery_repository_dir,
        pg_dump=arguments.pg_dump,
        pg_restore=arguments.pg_restore,
    )
    command = arguments.command
    admin = None
    if command != CREATE_COMMAND:
        if settings.deploy_admin_database_url is None:
            _say(
                err,
                "PAW_DEPLOY_ADMIN_DATABASE_URL is not set: verify / restore create "
                "and rename databases with it (a CREATEDB role that owns the "
                "workspace database).",
            )
            return EXIT_ENVIRONMENT
        admin = make_url(settings.deploy_admin_database_url.get_secret_value())
    try:
        if command == CREATE_COMMAND:
            point = points.create(url, arguments.label)
            action, reason = (
                DeployAction.RESTORE_POINT_CREATED,
                f"revision={point.revision} size={point.size}",
            )
            message = f"Restore point created at revision {point.revision}."
        elif command == VERIFY_COMMAND:
            point = points.verify(points.load(arguments.label), url, admin)
            action, reason = (
                DeployAction.RESTORE_POINT_VERIFIED,
                f"revision={point.revision} tables={point.verified['tables']}",
            )
            message = (
                f"Restore point verified: it restores to revision {point.revision}."
            )
        else:
            point = points.load(arguments.label)
            replaced = points.restore(point, url, admin)
            action, reason = (
                DeployAction.RESTORE_POINT_RESTORED,
                f"revision={point.revision}",
            )
            message = (
                f"Restored to revision {point.revision}. The replaced database is "
                f"kept as {replaced}: inspect it, then drop it."
            )
    except RestorePointError as error:
        code = EXIT_REFUSED if error.code in _REFUSALS else EXIT_FAILED
        _say(err, f"FAILED: {error.code}.")
        return code
    except OSError as error:
        # The directory or a file of the restore point (no path is shown).
        _say(err, f"FAILED: file_error ({type(error).__name__}).")
        return EXIT_FAILED
    _emit(out, point.as_json())
    _say(err, message)
    try:
        asyncio.run(_record(settings, action, reason))
    except (SQLAlchemyError, OSError):
        _say(err, "The outcome could not be recorded in audit_events.")
    return EXIT_OK


_REFUSALS = frozenset(
    {
        "invalid_label",
        "label_exists",
        "not_found",
        "not_verified",
        "users_deleted_since_the_point",
        "other_database",
        "directory_not_absolute",
        "directory_in_recovery_repository",
        "directory_not_private",
        "directory_not_a_directory",
        "directory_missing",
    }
)


async def _record(settings: Settings, action: DeployAction, reason: str) -> None:
    database = Database(
        settings.model_copy(update={"database_url": settings.migration_database_url})
    )
    try:
        await record_deploy_event(database, action, reason)
    finally:
        await database.dispose()
