"""``python -m paw_backend.cli owner-setup | owner-recover`` (PAW-021).

Exit codes (the convention of ``benchmarks/validate_task.py``):

* ``0``  success;
* ``1``  refused or invalid: an Owner already exists, there is no Owner to
  recover, ``owner-recover`` run by a process that is not root (sudo), a bad
  login name, a missing confirmation flag, a usage error;
* ``2``  environment error: the configuration is invalid, the database is not
  configured, unreachable or not migrated, or the audit trail cannot be written
  (nothing was changed in that case);
* ``3``  the change WAS made (and audited) but the token could not be written to
  stdout (closed, a broken pipe, a full disk): run ``owner-recover`` for a new one.

What is printed, and where:

* the one-time token goes to **stdout**, once, as a line of its own, and
  nowhere else (not stderr, not a log, not a file);
* everything else goes to **stderr**: ids, expiry and hints, never a secret;
* no exception message is ever printed, only its type. The database URL is read
  from the settings and cannot be given as an argument: ``PAW_OPERATOR_DATABASE_URL``
  (a role that may create Owner tokens) or, when that is unset, ``PAW_DATABASE_URL``
  (a single-role setup; it is warned about, because then the web application's
  role can create tokens too).
"""

import argparse
import asyncio
import contextlib
import os
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
    LoginNameTakenError,
    OwnerAlreadyExistsError,
    OwnerNotFoundError,
    OwnerNotLiveError,
    RecoveryNotPrivilegedError,
)
from paw_backend.identity.operator import IssuedToken, OwnerOperator

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2
EXIT_TOKEN_NOT_DELIVERED = 3

CONFIRM_FLAG = "--confirm-owner-recovery"

_LOGIN_NAME_RULE = (
    "a login name is 3 to 64 characters: lower-case letters, digits and . _ - "
    "inside, starting and ending with a letter or digit"
)
REPLACE_FLAG = "--replace-non-live-owner"

_RECOVERY_WARNING = (
    "owner-recover issues a recovery token for the existing Owner and revokes "
    "every outstanding setup / recovery token of the Owner. Run it only on the "
    "server, as root (sudo), when the Owner cannot log in. "
    f"Repeat with {CONFIRM_FLAG} to proceed."
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
            "configured with PAW_OPERATOR_DATABASE_URL or PAW_DATABASE_URL, "
            "never with an argument.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_REFUSED)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description=(
            "Server-local Owner management. Reads PAW_OPERATOR_DATABASE_URL (or "
            "PAW_DATABASE_URL) and the other PAW_* settings from the environment."
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
    setup.add_argument(
        REPLACE_FLAG,
        dest="replace_non_live_owner",
        action="store_true",
        help=(
            "only when the Owner account is pending deletion or deleted: demote it "
            "to a plain user and create a new Owner (audited). A live Owner is "
            "never replaced"
        ),
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
            _say(err, _RECOVERY_WARNING)
            return EXIT_REFUSED
        try:
            return _run(arguments, out, err)
        except Exception as error:
            # The last resort of a command that must never print a message that
            # could carry a secret: the type only, no traceback.
            _say(
                err,
                f"Unexpected error ({type(error).__name__}); details are not shown.",
            )
            return EXIT_ENVIRONMENT


def _say(stream: TextIO | None, message: str) -> None:
    """Print a line to ``stream``; a stream that cannot be written is ignored."""
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        print(message, file=stream)


def _connection_settings(settings: Settings, err: TextIO | None) -> Settings | None:
    """The settings the command connects with: the operator's database URL.

    Falls back to ``PAW_DATABASE_URL`` (a single-role setup) with a warning:
    there the web application's role can create Owner tokens as well.
    """
    if settings.operator_database_url is not None:
        return settings.model_copy(
            update={"database_url": settings.operator_database_url}
        )
    if settings.database_url is None:
        return None
    _say(
        err,
        "Warning: PAW_OPERATOR_DATABASE_URL is not set, so PAW_DATABASE_URL is "
        "used. If the web application runs as that role too, it can create Owner "
        "tokens: use a separate operator role (see the README).",
    )
    return settings


def _run(arguments: argparse.Namespace, out: TextIO | None, err: TextIO | None) -> int:
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
        _say(err, f"Invalid configuration: check {', '.join(names) or 'PAW_*'}")
        return EXIT_ENVIRONMENT
    connection = _connection_settings(settings, err)
    if connection is None:
        _say(err, "PAW_OPERATOR_DATABASE_URL (or PAW_DATABASE_URL) is not set.")
        return EXIT_ENVIRONMENT
    try:
        issued = asyncio.run(_issue(connection, arguments))
    except InvalidLoginNameError:
        _say(err, f"Invalid login name: {_LOGIN_NAME_RULE}.")
        return EXIT_REFUSED
    except OwnerAlreadyExistsError:
        _say(
            err,
            "Refused: an Owner already exists, so the initial setup is closed. If "
            "the Owner cannot log in, run owner-recover on this server.",
        )
        return EXIT_REFUSED
    except OwnerNotLiveError as error:
        _say(
            err,
            f"Refused: the Owner account exists but its status is {error.status}, "
            f"so owner-recover cannot help. To replace it, run owner-setup with "
            f"{REPLACE_FLAG}: the old account is demoted to a plain user and a "
            "new Owner is created (audited).",
        )
        return EXIT_REFUSED
    except LoginNameTakenError:
        _say(err, "Refused: that login name is already in use.")
        return EXIT_REFUSED
    except OwnerNotFoundError:
        _say(err, "Refused: there is no Owner to recover. Run owner-setup first.")
        return EXIT_REFUSED
    except RecoveryNotPrivilegedError:
        _say(
            err,
            "Refused: owner-recover must be run as root, for example with sudo. "
            "The database credential alone is not enough (SUDO_UID is not checked).",
        )
        return EXIT_REFUSED
    except AuditUnavailableError:
        _say(
            err,
            "The audit trail could not be written, so nothing was changed. Check "
            "that the audit_events table is migrated and that the operator's "
            "database role may INSERT into it.",
        )
        return EXIT_ENVIRONMENT
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        _say(
            err,
            f"Database error ({type(error).__name__}): check the database URL, "
            "that PostgreSQL is reachable, that the migrations are applied "
            "(alembic upgrade head) and that the role is the operator's "
            "(PAW_OPERATOR_DATABASE_ROLE).",
        )
        return EXIT_ENVIRONMENT
    return _show(issued, arguments.command, out, err)


async def _issue(settings: Settings, arguments: argparse.Namespace) -> IssuedToken:
    """Run the command. The service reads who runs it (the uid) from this process."""
    database = Database(settings)
    try:
        service = OwnerOperator.from_settings(
            settings, database, PostgresAuditSink(database)
        )
        if arguments.command == "owner-setup":
            return await service.setup_owner(
                arguments.login_name,
                replace_non_live_owner=arguments.replace_non_live_owner,
            )
        return await service.recover_owner()
    finally:
        await database.dispose()


def _show(
    issued: IssuedToken, command: str, out: TextIO | None, err: TextIO | None
) -> int:
    """Hand the token over (stdout, once) and say what was done (stderr)."""
    what = "Owner created" if command == "owner-setup" else "Recovery token issued"
    if not _write_token(issued.token, out):
        _say(
            err,
            f"{what}, but the token could not be written to stdout, so it is lost. "
            "Run owner-recover --confirm-owner-recovery for a new one (it revokes "
            "the lost one).",
        )
        return EXIT_TOKEN_NOT_DELIVERED
    who = ""
    operator = issued.operator  # what the service saw, and stored on the token
    if operator is not None:
        sudo = f" sudo_uid={operator.sudo_uid}" if operator.sudo_uid is not None else ""
        who = f" operator uid={operator.uid}{sudo}."
    _say(
        err,
        f"{what}. owner_id={issued.user_id} login_name={issued.login_name}.{who}\n"
        f"The {issued.purpose.value} token printed on stdout is valid until "
        f"{issued.expires_at.isoformat(timespec='seconds')}, works once, and is "
        "shown only now: it is not stored and cannot be displayed again.",
    )
    return EXIT_OK


def _write_token(token: str, out: TextIO | None) -> bool:
    """Write ``token`` to ``out`` and flush; ``False`` if that did not work."""
    if out is None:  # stdout was closed before the command started
        return False
    try:
        print(token, file=out)
        out.flush()
    except (OSError, ValueError):  # a broken pipe, a full disk, a closed stream
        _detach_from_stdout(out)
        return False
    return True


def _detach_from_stdout(out: TextIO) -> None:
    """Point a real stdout at /dev/null after a failed write.

    The buffer still holds the token; the interpreter would try to flush it again
    at exit, print a traceback and replace the exit code with 120.
    """
    with contextlib.suppress(OSError, ValueError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, out.fileno())
        finally:
            os.close(devnull)
