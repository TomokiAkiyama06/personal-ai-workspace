"""``python -m paw_backend.cli connection-credential put --kind codex|claude``
(Decision 0087, 2; #208).

The only way a Codex / Claude credential's plaintext enters the secret store:
on the server, from standard input, never from an argument, the environment,
the shell history or a web form. The command stores it under a new handle and
prints the handle (nothing else); an Owner / Admin then registers the handle
(``ConnectionService.connect`` / ``replace_credential``), which checks under
the handle's lock that the store holds it for that kind.

The master key is the systemd credential ``paw-secret-key``: the command runs
in a one-shot unit that loads it, through the deployment's wrapper
(``deploy/secret-store/paw-connection-credential-put``, run as root), with the
backend's environment file. ``PAW_SECRET_STORE_DIR`` names the store. When a
database is configured the store is audited (``connection.credential_store``,
the ``system`` role; the reason holds the kind, the version and the handle,
``codex:v1:cred_...``; never the value).

Standard input is read whole (at most ``MAX_SECRET_CHARS`` characters); one
trailing line break is dropped (``printf`` / ``echo`` / a file all work).
Exit codes follow ``paw_backend.cli.owner``: ``0`` success, ``1`` a usage
error or a value that is refused, ``2`` the store or the database cannot be
used.
"""

import argparse
import asyncio
import contextlib
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NoReturn, TextIO

from paw_backend.authz import AuditEvent, PostgresAuditSink
from paw_backend.config import Settings
from paw_backend.connections.domain import ConnectionKind
from paw_backend.connections.errors import InvalidConnectionInputError
from paw_backend.connections.limits import MAX_SECRET_CHARS
from paw_backend.connections.secret_store import SecretStoreError
from paw_backend.connections.wiring import build_secret_store
from paw_backend.db import Database

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2

PUT_COMMAND = "connection-credential"
COMMANDS = frozenset({PUT_COMMAND})
ACTION_STORE = "connection.credential_store"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: invalid arguments (see --help).", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description="Store a Codex / Claude credential (Decision 0087).",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    credential = commands.add_parser(
        PUT_COMMAND, help="the secret store of the shared connections"
    )
    actions = credential.add_subparsers(dest="action", required=True)
    put = actions.add_parser(
        "put",
        help=("store the credential read from standard input and print its handle"),
    )
    put.add_argument(
        "--kind", required=True, choices=[kind.value for kind in ConnectionKind]
    )
    return parser


def _read_value(stream: TextIO) -> str | None:
    data = stream.read(MAX_SECRET_CHARS + 2)
    if data.endswith("\r\n"):
        data = data[:-2]
    elif data.endswith("\n"):
        data = data[:-1]
    if not data or len(data) > MAX_SECRET_CHARS:
        return None
    return data


async def _audit(database: Database, kind: ConnectionKind, handle: str) -> None:
    """Decision 0087, 2: the handle, the kind and the version (a put is
    version 1), never the value; the system's role (the operator ran a
    server-local command)."""
    event = AuditEvent(
        event_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        actor_id=None,
        actor_role="system",
        action=ACTION_STORE,
        resource_kind="connection_credential",
        decision="allow",
        reason=f"{kind.value}:v1:{handle}",
    )
    await PostgresAuditSink(database).record(event)


async def _put(kind: ConnectionKind, value: str, settings: Settings) -> str:
    store = build_secret_store(settings)
    if store is None:
        raise SecretStoreError("No secret store is configured")
    handle = await store.put(kind, value)
    database = Database(settings)
    if database.configured:
        try:
            await _audit(database, kind, handle)
        finally:
            await database.dispose()
    return handle


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    settings: Settings | None = None,
) -> int:
    source = stdin if stdin is not None else sys.stdin
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            arguments = build_parser().parse_args(argv)
        except SystemExit as stop:  # --help (0) or a usage error (1)
            return stop.code if isinstance(stop.code, int) else EXIT_OK
        value = _read_value(source)
        if value is None:
            _say(err, "Refused: standard input is empty or too long.")
            return EXIT_REFUSED
        try:
            handle = asyncio.run(
                _put(ConnectionKind(arguments.kind), value, settings or Settings())
            )
        except InvalidConnectionInputError:
            _say(err, "Refused: the value is not a credential this store keeps.")
            return EXIT_REFUSED
        except SecretStoreError as error:
            _say(err, f"The secret store cannot be used: {error}")
            return EXIT_ENVIRONMENT
        except Exception as error:
            _say(
                err,
                f"Unexpected error ({type(error).__name__}); details are not shown.",
            )
            return EXIT_ENVIRONMENT
        _say(out, handle)
        return EXIT_OK


def _say(stream: TextIO | None, message: str) -> None:
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        print(message, file=stream)
