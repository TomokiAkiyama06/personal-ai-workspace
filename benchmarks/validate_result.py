"""Validate benchmark evaluator result JSON without executing evaluator checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from benchmarks.json_input import decode_json
from benchmarks.schema_validation import validate_document as _validate_document

SCHEMA_PATH = Path(__file__).parent / "schemas" / "result-v1.schema.json"


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Load the bundled schema and verify that the schema itself is valid."""
    schema = decode_json(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def validate_document(document: Any, schema: dict[str, Any] | None = None) -> list[str]:
    """Return stable, value-free errors for a decoded evaluator result."""
    return _validate_document(document, schema if schema is not None else load_schema())


def _load_document(path: Path) -> tuple[Any | None, str | None]:
    try:
        return decode_json(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as error:
        return None, f"JSON syntax error at line {error.lineno}, column {error.colno}"
    except ValueError:
        return (
            None,
            "JSON contains a non-standard numeric constant or duplicate object key",
        )
    except (OSError, UnicodeError) as error:
        return None, f"cannot read UTF-8 JSON ({type(error).__name__})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate evaluator result JSON files."
    )
    parser.add_argument("files", nargs="+", type=Path)
    arguments = parser.parse_args(argv)

    try:
        schema = load_schema()
    except (OSError, UnicodeError, ValueError, SchemaError) as error:
        print(f"benchmark schema is unusable ({type(error).__name__})", file=sys.stderr)
        return 2

    failed = False
    for path in arguments.files:
        document, read_error = _load_document(path)
        if read_error is not None:
            print(f"{path}: {read_error}", file=sys.stderr)
            failed = True
            continue
        errors = validate_document(document, schema)
        if errors:
            for error in errors:
                print(f"{path}:{error}", file=sys.stderr)
            failed = True
        else:
            print(f"{path}: valid")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
