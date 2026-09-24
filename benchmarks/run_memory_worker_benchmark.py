"""Command line entry point for the Memory Worker benchmark.

The worker is supplied as ``module:factory``. The factory is called without
arguments and must return an object with ``extract(input_text) -> str``. Only
point ``--worker`` at trusted code: the module is imported and the factory runs
with the caller's permissions. Each ``extract`` call has a deadline
(``--timeout-seconds``); a call that misses it is a failed case.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from benchmarks.memory_worker_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    load_cases,
    run_benchmark,
    validate_timeout_seconds,
    validate_worker,
)
from benchmarks.metrics_collector import MetricsCollector, NvidiaSmiGpuSampler


def _create_worker(spec: str):
    module_name, separator, factory_name = spec.partition(":")
    if not module_name or not separator or not factory_name:
        raise ValueError("worker must be given as module:factory")
    factory = getattr(importlib.import_module(module_name), factory_name)
    worker = factory()
    validate_worker(worker)
    return worker


def _timeout_argument(text: str) -> float:
    try:
        return validate_timeout_seconds(float(text))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must be a finite number of seconds above zero"
        ) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Memory Worker benchmark.")
    parser.add_argument("--cases", required=True, type=Path, help="cases JSON file")
    parser.add_argument(
        "--worker",
        required=True,
        help="module:factory returning an object with extract(input_text) -> str",
    )
    parser.add_argument(
        "--output", type=Path, help="write the JSON report to this file, not stdout"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_timeout_argument,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="deadline for each extract() call; a call that misses it is a failed "
        f"case, not a hang (default: {DEFAULT_TIMEOUT_SECONDS:g}). Use the same "
        "value for every candidate",
    )
    parser.add_argument(
        "--collect-resources",
        action="store_true",
        help="record wall clock and, when nvidia-smi is available, peak VRAM / GPU "
        "utilization under 'resources'",
    )
    arguments = parser.parse_args(argv)

    try:
        cases = load_cases(str(arguments.cases))
    except (OSError, ValueError) as error:
        print(f"{arguments.cases}: {error}", file=sys.stderr)
        return 1

    try:
        worker = _create_worker(arguments.worker)
    except Exception as error:  # noqa: BLE001 - a factory may fail in any way; report only the type.
        print(f"worker is unusable ({type(error).__name__})", file=sys.stderr)
        return 2

    try:
        collector = (
            MetricsCollector(gpu_sampler=NvidiaSmiGpuSampler())
            if arguments.collect_resources
            else None
        )
        report = run_benchmark(
            worker,
            cases,
            metrics_collector=collector,
            timeout_seconds=arguments.timeout_seconds,
        )
    except (TypeError, ValueError) as error:
        print(
            f"worker returned an invalid result ({type(error).__name__})",
            file=sys.stderr,
        )
        return 2

    text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    if arguments.output is None:
        print(text)
        return 0
    try:
        arguments.output.write_text(text + "\n", encoding="utf-8")
    except OSError as error:
        print(f"cannot write the report ({type(error).__name__})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
