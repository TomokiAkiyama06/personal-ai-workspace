"""Command line entry point for the Memory Worker benchmark.

The worker is supplied as ``module:factory``. The factory is called without
arguments and must return an object with ``extract(input_text) -> str``. Only
point ``--worker`` at trusted code: the module is imported and the factory runs
with the caller's permissions.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from benchmarks.memory_worker_runner import load_cases, run_benchmark


def _create_worker(spec: str):
    module_name, separator, factory_name = spec.partition(":")
    if not module_name or not separator or not factory_name:
        raise ValueError("worker must be given as module:factory")
    factory = getattr(importlib.import_module(module_name), factory_name)
    return factory()


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
    arguments = parser.parse_args(argv)

    try:
        cases = load_cases(str(arguments.cases))
    except (OSError, ValueError) as error:
        print(f"{arguments.cases}: {error}", file=sys.stderr)
        return 1

    try:
        worker = _create_worker(arguments.worker)
    except (ImportError, AttributeError, ValueError, TypeError) as error:
        print(f"worker is unusable ({type(error).__name__})", file=sys.stderr)
        return 2

    report = run_benchmark(worker, cases)
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
