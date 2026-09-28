"""``python -m paw_backend.cli user-erasure-run`` (Issue #127, Decision 0043).

The scheduled erasure of the personal data of users deleted 30 or more days ago
(``paw_backend.auth.onboarding.erasure``). The systemd timer of
``apps/backend/deploy/systemd`` runs it once a day.

Exit codes (the convention of ``paw_backend.cli.retention``):

* ``0``  every due user was erased and marked ``deleted`` (or nobody was due);
* ``1``  refused or invalid: a usage error, or another run holds the lock (nothing
  was done);
* ``2``  environment error: invalid configuration, ``PAW_MIGRATION_DATABASE_URL``
  not set, the database unreachable (nothing was done);
* ``3``  at least one due user was NOT erased and marked ``deleted`` (active tasks,
  managed checkouts left, the copies outside the database not confirmed, a failed
  verification or step, a lock that was not released in time, or the run was
  terminated). Each is recorded as ``auth.user.erase`` / deny when the
  database accepts it, and the user stays ``pending_deletion`` (without access)
  for the next run. A non-zero code starts the ``OnFailure=`` unit, which is how
  the Owner hears of it.

It connects with ``PAW_MIGRATION_DATABASE_URL`` only (the table owner: the erasure
deletes rows the application's role may not delete, and records the ``deleted``
status that role may not set). The URL is never an argument and no exception
message is printed (only its type).

``--checkouts-removed USER_ID`` (repeatable): the operator removed that user's
managed checkouts from disk (they live in the user's own Linux account, which this
job does not touch); the job then deletes their ``repository_checkouts`` rows and
erases the user. Without it such a user is refused (``checkouts_remaining``).

``--copies-erased USER_ID`` (repeatable): the operator erased that user's copies
outside the database (the database backups and WAL that hold them, their files and
GitHub / SSH credentials in their Linux account, any recovery copy). REQUIREMENTS.md
"User Deletion Retention" does not allow ``Deleted`` before that, and this job
cannot reach or check those copies. Without it the database part is still erased
(``data_erased``) but the user stays ``pending_deletion`` (``copies_pending``,
exit 3), every day, until the operator confirms.
"""

import argparse
import asyncio
import contextlib
import signal
import sys
import uuid
from collections.abc import Sequence
from typing import NoReturn, TextIO

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from paw_backend.auth.onboarding.erasure import (
    ErasureAlreadyRunningError,
    ErasureOutcome,
    ErasureRunReport,
    UserErasureService,
)
from paw_backend.config import Settings
from paw_backend.db import Database

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2
EXIT_ERASURE_FAILED = 3

RUN_COMMAND = "user-erasure-run"
COMMANDS = frozenset({RUN_COMMAND})

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


def _user_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not a user id") from None


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description=(
            "Scheduled erasure of deleted users' personal data (Decision 0043). "
            "Reads PAW_MIGRATION_DATABASE_URL (the table owner) from the environment."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        RUN_COMMAND,
        help=(
            "erase the personal data of every user deleted 30 or more days ago "
            "(irreversible); mark deleted those whose copies outside the database "
            "were confirmed erased (--copies-erased)"
        ),
        allow_abbrev=False,
    )
    run.add_argument(
        "--checkouts-removed",
        metavar="USER_ID",
        type=_user_id,
        action="append",
        default=[],
        help=(
            "the managed checkouts of this user were removed from disk: delete their "
            "records and erase the user (repeatable)"
        ),
    )
    run.add_argument(
        "--copies-erased",
        metavar="USER_ID",
        type=_user_id,
        action="append",
        default=[],
        help=(
            "the copies of this user outside the database (backups / WAL, files and "
            "credentials in their Linux account, recovery copies) were erased: mark "
            "the user deleted once the database part is erased (repeatable)"
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


def _run(arguments: argparse.Namespace, err: TextIO | None) -> int:
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
            "PAW_MIGRATION_DATABASE_URL is not set. The erasure runs as the table "
            "owner (the migration role); PAW_DATABASE_URL is not used.",
        )
        return EXIT_ENVIRONMENT
    connection = settings.model_copy(
        update={"database_url": settings.migration_database_url}
    )
    try:
        report = asyncio.run(
            _erase(connection, arguments.checkouts_removed, arguments.copies_erased)
        )
    except asyncio.CancelledError:
        _say(
            err,
            "FAILED: the run was terminated (SIGTERM) before it finished. A user "
            "whose erasure was cut short was rolled back and stays pending deletion.",
        )
        return EXIT_ERASURE_FAILED
    except ErasureAlreadyRunningError:
        _say(
            err,
            "Refused: another user erasure run is already running (it holds the "
            "lock). Nothing was done.",
        )
        return EXIT_REFUSED
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        _say(err, f"Database error ({type(error).__name__}): {_DATABASE_HINT}")
        return EXIT_ENVIRONMENT
    return _show(report, err)


async def _erase(
    settings: Settings, checkouts_removed, copies_erased
) -> ErasureRunReport:
    database = Database(settings)
    service = UserErasureService(database)
    try:
        with _cancel_on_sigterm():
            return await service.run(
                checkouts_removed=checkouts_removed, copies_erased=copies_erased
            )
    finally:
        await database.dispose()


@contextlib.contextmanager
def _cancel_on_sigterm():
    """Turn SIGTERM into a cancellation of the running task (``retention``'s)."""
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


def _show(report: ErasureRunReport, err: TextIO | None) -> int:
    erased = report.count(ErasureOutcome.ERASED)
    if report.ok:
        _say(err, f"User erasure completed: erased={erased}.")
        return EXIT_OK
    for result in report.results:
        if result.outcome is ErasureOutcome.COPIES_PENDING:
            _say(
                err,
                f"NOT DELETED: user {result.user_id} (copies_pending). Its personal "
                "data in the database is erased; it stays pending deletion until the "
                "copies outside the database are erased and confirmed with "
                "--copies-erased.",
            )
        elif not result.ok:
            _say(
                err,
                f"NOT ERASED: user {result.user_id} ({result.outcome.value}"
                + (f", {result.error_type}" if result.error_type else "")
                + "). It stays pending deletion; see audit_events "
                "(action auth.user.erase).",
            )
    failed = sum(not result.ok for result in report.results)
    _say(err, f"FAILED: erased={erased} not_erased={failed}.")
    return EXIT_ERASURE_FAILED
