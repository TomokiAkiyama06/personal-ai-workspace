"""Validate benchmark task JSON without executing or resolving its references."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

SCHEMA_PATH = Path(__file__).parent / "schemas" / "task-v1.schema.json"


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    """Load the bundled schema and verify that the schema itself is valid."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _error_path(error) -> str:
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _error_reason(error) -> str:
    """Describe a schema failure without including the rejected input value."""
    if error.validator == "required":
        missing = [name for name in error.validator_value if name not in error.instance]
        return f"required property is missing: {', '.join(missing)}"
    if error.validator == "additionalProperties":
        return "unexpected property is not allowed"
    if error.validator == "type":
        expected = error.validator_value
        if isinstance(expected, list):
            expected = " or ".join(expected)
        return f"expected {expected}"
    if error.validator == "enum":
        return "value is not an allowed enum member"
    if error.validator == "const":
        return "value does not match the supported schema version"
    if error.validator == "minLength":
        return "string must not be empty"
    if error.validator == "minItems":
        return "array must contain at least one item"
    return f"validation failed ({error.validator})"


def validate_document(document: Any, schema: dict[str, Any] | None = None) -> list[str]:
    """Return stable, value-free errors for a decoded benchmark task."""
    validator = Draft202012Validator(schema if schema is not None else load_schema())
    errors = sorted(
        validator.iter_errors(document),
        key=lambda error: tuple(
            f"{type(part).__name__}:{part}" for part in error.absolute_path
        ),
    )
    return [f"{_error_path(error)}: {_error_reason(error)}" for error in errors]


def _load_document(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except json.JSONDecodeError as error:
        return None, f"JSON syntax error at line {error.lineno}, column {error.colno}"
    except (OSError, UnicodeError) as error:
        return None, f"cannot read UTF-8 JSON ({type(error).__name__})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate benchmark task JSON files.")
    parser.add_argument("files", nargs="+", type=Path)
    arguments = parser.parse_args(argv)

    try:
        schema = load_schema()
    except (OSError, UnicodeError, json.JSONDecodeError, SchemaError) as error:
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
