"""Which module handles a ``python -m paw_backend.cli`` command line.

``owner`` (PAW-021), ``retention`` (Issue #117), ``erasure`` (Issue #127) and
``memory_projection`` (PAW-045) keep their own parsers, exit codes and database
URLs (``PAW_OPERATOR_DATABASE_URL`` vs. ``PAW_MIGRATION_DATABASE_URL`` vs.
``PAW_DATABASE_URL``); ``compute`` (PAW-036) reads the GPU and uses no
database: the first argument picks one.
"""

import sys
from collections.abc import Sequence
from types import ModuleType

from paw_backend.cli import compute, erasure, memory_projection, owner, retention


def command_module(argv: Sequence[str]) -> ModuleType:
    """``retention`` / ``erasure`` / ``memory_projection`` / ``compute`` for their
    commands, ``owner`` for everything else (and help)."""
    if argv and argv[0] in retention.COMMANDS:
        return retention
    if argv and argv[0] in erasure.COMMANDS:
        return erasure
    if argv and argv[0] in memory_projection.COMMANDS:
        return memory_projection
    if argv and argv[0] in compute.COMMANDS:
        return compute
    return owner


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return command_module(arguments).main(arguments)
