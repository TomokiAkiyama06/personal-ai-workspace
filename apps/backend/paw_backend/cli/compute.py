"""``python -m paw_backend.cli compute-status`` (PAW-036).

A read-only look at the GPU through the probe the Compute Resource Scheduler uses
(the two ``nvidia-smi --query-*`` commands, nothing else): for each GPU, the
total and used memory, the safety headroom and what would be available to the
workspace, the utilization, and how many processes hold memory. It does not
print the pids or names of those processes (they may be other users'), does not
connect to the database and changes nothing.

Exit codes follow ``paw_backend.cli.owner``: ``0`` success, ``1`` a usage error,
``2`` the GPU could not be read.
"""

import argparse
import asyncio
import contextlib
import json
import math
import sys
from collections.abc import Sequence
from typing import NoReturn, TextIO

from paw_backend.compute.accounting import account, headroom_bytes
from paw_backend.compute.errors import ProbeUnavailableError
from paw_backend.compute.limits import (
    DEFAULT_HEADROOM_FRACTION,
    DEFAULT_HEADROOM_MIN_BYTES,
    MAX_HEADROOM_FRACTION,
)
from paw_backend.compute.probe import MIB, NvidiaSmiProbe

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_ENVIRONMENT = 2

STATUS_COMMAND = "compute-status"
COMMANDS = frozenset({STATUS_COMMAND})


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        print(f"{self.prog}: invalid arguments (see --help).", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED)


def _mib(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not an integer") from None
    if not 0 <= number <= 1 << 30:
        raise argparse.ArgumentTypeError("out of range")
    return number


def _fraction(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("not a number") from None
    if not math.isfinite(number) or not 0 <= number <= MAX_HEADROOM_FRACTION:
        raise argparse.ArgumentTypeError("out of range")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m paw_backend.cli",
        description=(
            "Read-only GPU status for the Compute Resource Scheduler (PAW-036)."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser(
        STATUS_COMMAND,
        help=(
            "read the GPUs (nvidia-smi --query-*) and print the VRAM accounting as JSON"
        ),
        allow_abbrev=False,
    )
    status.add_argument(
        "--headroom-min-mib",
        type=_mib,
        default=DEFAULT_HEADROOM_MIN_BYTES // MIB,
        help="the fixed part of the safety headroom in MiB",
    )
    status.add_argument(
        "--headroom-fraction",
        type=_fraction,
        default=DEFAULT_HEADROOM_FRACTION,
        help="the share of the GPU kept as safety headroom",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    probe: object | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            arguments = build_parser().parse_args(argv)
        except SystemExit as stop:
            return stop.code if isinstance(stop.code, int) else EXIT_OK
        probe = probe if probe is not None else NvidiaSmiProbe()
        try:
            sample = asyncio.run(probe.sample())
        except ProbeUnavailableError:
            print(
                "The GPU could not be read (nvidia-smi is missing, timed out or "
                "printed something unexpected).",
                file=err,
            )
            return EXIT_ENVIRONMENT
        devices = []
        for device in sample.devices:
            headroom = headroom_bytes(
                device.total_bytes,
                minimum_bytes=arguments.headroom_min_mib * MIB,
                fraction=arguments.headroom_fraction,
            )
            processes = sample.processes_on(device)
            view = account(device, processes, (), headroom=headroom)
            devices.append(
                {
                    "index": device.index,
                    "name": device.name,
                    "total_mib": view.total // MIB,
                    "used_mib": view.actual // MIB,
                    "headroom_mib": headroom // MIB,
                    "available_mib": view.available // MIB,
                    "utilization_percent": device.utilization_percent,
                    "processes": len(processes),
                }
            )
        print(json.dumps({"read_only": True, "devices": devices}, indent=2), file=out)
        return EXIT_OK
