"""Shared value-free validation reporting for benchmark schemas."""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator


def error_path(error) -> str:
    path = "$"
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def error_reason(error) -> str:
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
    if error.validator == "minimum":
        return "number must not be negative"
    return f"validation failed ({error.validator})"


def validate_document(document: Any, schema: dict[str, Any]) -> list[str]:
    """Return stable, value-free errors for a decoded benchmark document."""
    errors = sorted(
        Draft202012Validator(schema).iter_errors(document),
        key=lambda error: tuple(
            f"{type(part).__name__}:{part}" for part in error.absolute_path
        ),
    )
    return [f"{error_path(error)}: {error_reason(error)}" for error in errors]
