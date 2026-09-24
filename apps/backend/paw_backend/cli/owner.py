"""``python -m paw_backend.cli owner-setup | owner-recover`` (PAW-021).

Exit codes (the convention of ``benchmarks/validate_task.py``):

* ``0``  success;
* ``1``  refused or invalid: an Owner already exists, there is no Owner to
  recover, a bad login name, a missing confirmation flag, a usage error;
* ``2``  environment error: the configuration is invalid, the database is not
  configured, unreachable or not migrated, or the audit trail cannot be written
  (nothing was changed in that case).

What is printed, and where:

* the one-time token goes to **stdout**, once, as a line of its own, and
  nowhere else (not stderr, not a log, not a file);
* everything else goes to **stderr**: ids, expiry and hints, never a secret;
* no exception message is ever printed, only its type. The database URL is read
  from the settings (``PAW_DATABASE_URL``) and cannot be given as an argument.
"""

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Sequence
from typing import NoReturn, TextIO

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from paw_backend.authz.audit import PostgresAuditSink
from paw_backend.config import Settings
from paw_backend.db import Database
from paw_backend.identity import (
    AuditUnavailableError,
    InvalidLoginNameError,
    IssuedToken,
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
    OwnerSetupService,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2

CONFIRM_FLAG = "--confirm-owner-recovery"

_LOGIN_NAME_RULE = (
    "a login name is 3 to 64 characters: lower-case letters, digits and . _ - "
    "inside, starting and ending with a letter or digit"
)
_RECOVERY_WARNING = (
    "owner-recover issues a recovery token for the existing Owner and revokes "
    "every outstanding setup / recovery token of the Owner. Run it only on the "
    f"server, when the Owner cannot log in. Repeat with {CONFIRM_FLAG} to proceed."
)


class _Parser(argparse.ArgumentParser):
    """A parser that never echoes what it was given.

    argparse quotes the offending argument in its errors; here that could be a
    database URL somebody wrongly passed on the command line.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(
            f"{self.prog}: invalid arguments (see --help). The database is "
            "configured with PAW_DATABASE_URL, never with an argument.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_REFUSED)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description=(
            "Server-local Owner management. Reads PAW_DATABASE_URL and the other "
            "PAW_* settings from the environment."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser(
        "owner-setup",
        help="create the initial Owner and print a one-time setup token",
        allow_abbrev=False,
    )
    setup.add_argument(
        "--login-name",
        required=True,
        help=f"the Owner's login name ({_LOGIN_NAME_RULE})",
    )
    recover = commands.add_parser(
        "owner-recover",
        help="print a one-time recovery token for the existing Owner",
        allow_abbrev=False,
    )
    recover.add_argument(
        CONFIRM_FLAG,
        dest="confirmed",
        action="store_true",
        help="required: acknowledges that outstanding tokens are revoked",
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
        if arguments.command == "owner-recover" and not arguments.confirmed:
            print(_RECOVERY_WARNING, file=err)
            return EXIT_REFUSED
        try:
            return _run(arguments, out, err)
        except Exception as error:
            # The last resort of a command that must never print a message that
            # could carry a secret: the type only, no traceback.
            print(
                f"Unexpected error ({type(error).__name__}); details are not shown.",
                file=err,
            )
            return EXIT_ENVIRONMENT


def _run(arguments: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        settings = Settings()
    except ValidationError as error:
        # Names of the invalid settings only: never a value (it may be a URL).
        names = sorted(
            {
                "PAW_" + str(entry["loc"][0]).upper()
                for entry in error.errors()
                if entry["loc"]
            }
        )
        print(f"Invalid configuration: check {', '.join(names) or 'PAW_*'}", file=err)
        return EXIT_ENVIRONMENT
    if settings.database_url is None:
        print("PAW_DATABASE_URL is not set.", file=err)
        return EXIT_ENVIRONMENT
    try:
        issued = asyncio.run(_issue(settings, arguments))
    except InvalidLoginNameError:
        print(f"Invalid login name: {_LOGIN_NAME_RULE}.", file=err)
        return EXIT_REFUSED
    except OwnerAlreadyExistsError:
        print(
            "Refused: an Owner already exists, so the initial setup is closed. If "
            "the Owner cannot log in, run owner-recover on this server.",
            file=err,
        )
        return EXIT_REFUSED
    except LoginNameTakenError:
        print("Refused: that login name is already in use.", file=err)
        return EXIT_REFUSED
    except OwnerNotFoundError:
        print("Refused: there is no Owner to recover. Run owner-setup first.", file=err)
        return EXIT_REFUSED
    except AuditUnavailableError:
        print(
            "The audit trail could not be written, so nothing was changed. Check "
            "that the audit_events table is migrated and writable by the "
            "configured database role.",
            file=err,
        )
        return EXIT_ENVIRONMENT
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        print(
            f"Database error ({type(error).__name__}): check PAW_DATABASE_URL, "
            "that PostgreSQL is reachable, and that the migrations are applied "
            "(alembic upgrade head).",
            file=err,
        )
        return EXIT_ENVIRONMENT
    _show(issued, arguments.command, out, err)
    return EXIT_OK


async def _issue(settings: Settings, arguments: argparse.Namespace) -> IssuedToken:
    database = Database(settings)
    try:
        service = OwnerSetupService.from_settings(
            settings, database, PostgresAuditSink(database)
        )
        if arguments.command == "owner-setup":
            return await service.setup_owner(arguments.login_name)
        return await service.recover_owner()
    finally:
        await database.dispose()


def _show(issued: IssuedToken, command: str, out: TextIO, err: TextIO) -> None:
    what = "Owner created" if command == "owner-setup" else "Recovery token issued"
    print(
        f"{what}. owner_id={issued.user_id} login_name={issued.login_name}\n"
        f"The {issued.purpose.value} token below is valid until "
        f"{issued.expires_at.isoformat(timespec='seconds')}, works once, and is "
        "shown only now: it is not stored and cannot be displayed again.",
        file=err,
    )
    err.flush()
    print(issued.token, file=out)
    out.flush()
