"""Which module handles a ``python -m paw_backend.cli`` command line.

``owner`` (PAW-021) and ``retention`` (Issue #117) keep their own parsers, exit
codes and database URLs (``PAW_OPERATOR_DATABASE_URL`` vs.
``PAW_MIGRATION_DATABASE_URL``): the first argument picks one.
"""

import sys
from collections.abc import Sequence
from types import ModuleType

from paw_backend.cli import owner, retention


def command_module(argv: Sequence[str]) -> ModuleType:
    """``retention`` for its commands, ``owner`` for everything else (and help)."""
    if argv and argv[0] in retention.COMMANDS:
        return retention
    return owner


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return command_module(arguments).main(arguments)
