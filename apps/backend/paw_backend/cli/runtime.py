"""``python -m paw_backend.cli runtime-config-check <path>`` (Decision 0093, 7).

Checks a runtime configuration file (``/etc/paw/runtime.toml``) with the same
parser the backend's start uses (``paw_backend/runtime_config.py``): the file's
owner and mode and its directories, every command's ``argv[0]`` (it must exist
on this host: run it on the deployment host, as the backend's user ``paw``), the
URLs, the sections and their dependencies. It prints what the file enables,
without a value (no path, command or URL), and ends: it does not start the
backend, connect to the database or run any command of the file. Run it after
the file changed and before the backend is restarted.

The settings (``PAW_REPOSITORY_SSH_*``, and ``PAW_DATABASE_URL``, which
``[orchestrator]`` needs) come from the environment, as for the backend: run it
with the backend's environment file, e.g.
``systemd-run --uid=paw --pipe --wait --quiet -p
EnvironmentFile=/etc/paw/backend.env /opt/paw/current/venv/bin/python -m
paw_backend.cli runtime-config-check /etc/paw/runtime.toml`` (the database is
not connected to).

Exit codes follow ``paw_backend.cli.owner``: ``0`` the file can be used, ``1`` a
usage error, ``2`` the settings or the file are refused (the backend would not
start with it).
"""

import argparse
import contextlib
import sys
from collections.abc import Callable, Sequence
from typing import NoReturn, TextIO

from pydantic import ValidationError

from paw_backend.config import Settings
from paw_backend.runtime_config import (
    RuntimeConfigError,
    RuntimeWiring,
    check_environment,
    load_runtime_config,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 2

CHECK_COMMAND = "runtime-config-check"
COMMANDS = frozenset({CHECK_COMMAND})


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: invalid arguments (see --help).", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description="Check the backend's runtime configuration file (Decision 0093).",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        CHECK_COMMAND,
        help=(
            "check a runtime configuration file (PAW_RUNTIME_CONFIG_FILE) and "
            "print what it enables"
        ),
        allow_abbrev=False,
    )
    check.add_argument("path", help="the file, e.g. /etc/paw/runtime.toml")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    load: Callable[[str, Settings], RuntimeWiring] | None = None,
) -> int:
    """``load``: tests pass one (over a fake file system)."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            arguments = build_parser().parse_args(argv)
        except SystemExit as stop:
            return stop.code if isinstance(stop.code, int) else EXIT_OK
        try:
            settings = Settings()
        except ValidationError as error:
            # No setting values in it (see Settings.model_config).
            print(f"Invalid configuration: {error}", file=err)
            return EXIT_REFUSED
        try:
            wiring = (load_runtime_config if load is None else load)(
                arguments.path, settings
            )
            check_environment(wiring, settings)
        except RuntimeConfigError as error:
            print(f"Invalid runtime configuration: {error}", file=err)
            return EXIT_REFUSED
        print("The runtime configuration can be used. It enables:", file=out)
        if not wiring.parts:
            print("  nothing", file=out)
        for part in wiring.parts:
            print(f"  {part}", file=out)
        return EXIT_OK
