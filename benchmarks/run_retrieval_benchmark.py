"""Command line entry point for the retrieval (embedding / reranker) benchmark.

The retriever is supplied as ``module:factory``. The factory is called without
arguments and must return an object with
``retrieve(query_text, requester_principals, k) -> Sequence[str]``. Only point
``--retriever`` at trusted code: the module is imported and the factory runs with
the caller's permissions.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from benchmarks.retrieval_runner import (
    load_dataset,
    run_benchmark,
    validate_retriever,
)


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _create_retriever(spec: str):
    module_name, separator, factory_name = spec.partition(":")
    if not module_name or not separator or not factory_name:
        raise ValueError("retriever must be given as module:factory")
    factory = getattr(importlib.import_module(module_name), factory_name)
    retriever = factory()
    validate_retriever(retriever)
    return retriever


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the retrieval benchmark.")
    parser.add_argument("--dataset", required=True, type=Path, help="dataset JSON file")
    parser.add_argument(
        "--retriever",
        required=True,
        help="module:factory returning an object with "
        "retrieve(query_text, requester_principals, k) -> ids",
    )
    parser.add_argument(
        "-k", type=_positive_int, default=5, help="cutoff for ranking metrics"
    )
    parser.add_argument(
        "--output", type=Path, help="write the JSON report to this file, not stdout"
    )
    arguments = parser.parse_args(argv)

    try:
        dataset = load_dataset(arguments.dataset)
    except (OSError, ValueError) as error:
        print(f"{arguments.dataset}: {error}", file=sys.stderr)
        return 1

    try:
        retriever = _create_retriever(arguments.retriever)
    except Exception as error:  # noqa: BLE001 - a factory may fail in any way; report only the type.
        print(f"retriever is unusable ({type(error).__name__})", file=sys.stderr)
        return 2

    try:
        report = run_benchmark(retriever, dataset, k=arguments.k)
    except (TypeError, ValueError) as error:
        print(
            f"retriever returned an invalid result ({type(error).__name__})",
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
