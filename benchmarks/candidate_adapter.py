"""Provider-neutral contract for benchmark coding-agent candidates.

This module defines data and control boundaries only.  It deliberately does not
load credentials, connect to a provider, execute tools, or manage worktrees.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from numbers import Real
from threading import Lock
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _copy_json_value(value: Any) -> Any:
    """Copy a JSON value while rejecting non-JSON mapping keys and values."""

    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("input_schema mappings must use string keys")
        return {key: _copy_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_copy_json_value(item) for item in value]
    return value


_INVALID_SCHEMA_MESSAGE = (
    "input_schema must be a valid JSON-serializable Draft 2020-12 JSON Schema"
)


def _snapshot_json_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Return a validated private copy of a tool input schema.

    Raises ``TypeError`` for non-string mapping keys and ``ValueError`` for any
    value that is not a finite, acyclic, valid Draft 2020-12 JSON Schema.
    """

    try:
        snapshot = _copy_json_value(schema)
    except RecursionError:
        raise ValueError(_INVALID_SCHEMA_MESSAGE) from None
    try:
        json.dumps(snapshot, allow_nan=False)
        Draft202012Validator.check_schema(snapshot)
    except (SchemaError, TypeError, ValueError, RecursionError):
        raise ValueError(_INVALID_SCHEMA_MESSAGE) from None
    return snapshot


def _freeze_json_value(value: Any) -> Any:
    """Recursively make a copied JSON value read-only."""

    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json_value(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """Public candidate identity suitable for benchmark results and logs.

    Authentication material and provider client objects have no place in this
    structure.  A concrete adapter must obtain them from a private backend
    dependency instead.
    """

    candidate_id: str
    model: str
    runtime: str
    quantization: str = "none"

    def __post_init__(self) -> None:
        for field_name in ("candidate_id", "model", "runtime", "quantization"):
            _require_text(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class PromptConfig:
    """The exact prompts supplied under a fair benchmark configuration."""

    system: str
    task: str

    def __post_init__(self) -> None:
        _require_text(self.system, "system")
        _require_text(self.task, "task")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Provider-neutral tool declaration using a JSON Schema input contract.

    ``input_schema`` is validated and replaced by a deep read-only snapshot, so
    later changes to the supplied mapping never reach a candidate.  ``TypeError``
    is raised for a non-mapping schema or non-string keys; ``ValueError`` for a
    schema that is not a valid, finite, acyclic JSON Schema of type ``object``.

    The read-only snapshot is not directly JSON- or ``dataclasses.asdict``-
    serializable.  Adapters obtain a JSON-ready, mutable, isolated copy with
    :meth:`input_schema_as_dict` (schema only) or :meth:`to_dict` (whole tool).
    ``copy.deepcopy`` and ``pickle`` are supported and re-validate the copy;
    ``dataclasses.asdict`` is not supported and raises ``TypeError``.
    """

    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        _require_text(self.name, "name")
        _require_text(self.description, "description")
        if not isinstance(self.input_schema, Mapping):
            raise TypeError("input_schema must be a mapping")
        snapshot = _snapshot_json_schema(self.input_schema)
        if snapshot.get("type") != "object":
            raise ValueError("input_schema must declare an object JSON Schema")
        object.__setattr__(self, "input_schema", _freeze_json_value(snapshot))

    def input_schema_as_dict(self) -> dict[str, Any]:
        """Return a fresh, deep, plain ``dict``/``list`` copy of the schema.

        The result is JSON-serializable and may be mutated freely; each call
        returns an independent copy and the frozen snapshot is never affected.
        """

        return _copy_json_value(self.input_schema)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable ``name``/``description``/``input_schema`` copy."""

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema_as_dict(),
        }

    def __reduce__(self) -> tuple[Any, tuple[str, str, dict[str, Any]]]:
        # Rebuild from a plain copy so deepcopy/pickle re-validate and re-freeze.
        return (
            ToolDefinition,
            (self.name, self.description, self.input_schema_as_dict()),
        )


@dataclass(frozen=True, slots=True)
class ContextLimits:
    """Explicit token limits shared by every candidate in a comparison."""

    max_context_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_context_tokens, bool)
            or not isinstance(self.max_context_tokens, int)
            or self.max_context_tokens < 1
        ):
            raise ValueError("max_context_tokens must be a positive integer")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if self.max_output_tokens > self.max_context_tokens:
            raise ValueError("max_output_tokens cannot exceed max_context_tokens")


@dataclass(frozen=True, slots=True)
class CandidateRequest:
    """One candidate-independent coding-agent request."""

    task_id: str
    prompt: PromptConfig
    tools: tuple[ToolDefinition, ...]
    context_limits: ContextLimits

    def __post_init__(self) -> None:
        _require_text(self.task_id, "task_id")
        if not isinstance(self.prompt, PromptConfig):
            raise TypeError("prompt must be a PromptConfig")
        if not isinstance(self.tools, tuple):
            raise TypeError("tools must be a tuple")
        if not all(isinstance(tool, ToolDefinition) for tool in self.tools):
            raise TypeError("tools must contain only ToolDefinition values")
        if not isinstance(self.context_limits, ContextLimits):
            raise TypeError("context_limits must be ContextLimits")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Backend-owned retry budget; adapters never retry an attempt themselves."""

    max_attempts: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")

    def allows_attempt(self, attempt: int) -> bool:
        """Return whether the backend may start the one-based attempt number."""

        return (
            isinstance(attempt, int)
            and not isinstance(attempt, bool)
            and (1 <= attempt <= self.max_attempts)
        )

    def should_retry(self, completed_attempts: int, result: CandidateResult) -> bool:
        """Return whether the backend may retry a retryable failed attempt."""

        if not isinstance(result, CandidateResult):
            raise TypeError("result must be a CandidateResult")
        return result.retryable and self.allows_attempt(completed_attempts + 1)


class CancellationReason(StrEnum):
    """Public cancellation classifications safe to place in task logs."""

    USER_REQUESTED = "user_requested"
    POLICY = "policy"
    SUPERSEDED = "superseded"
    BACKEND_SHUTDOWN = "backend_shutdown"


class CancellationToken:
    """Thread-safe, backend-owned cooperative cancellation signal."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._cancelled = False
        self._reason: CancellationReason | None = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def reason(self) -> CancellationReason | None:
        with self._lock:
            return self._reason

    def cancel(
        self, reason: CancellationReason = CancellationReason.USER_REQUESTED
    ) -> bool:
        """Signal cancellation once and return whether this call changed state."""

        if not isinstance(reason, CancellationReason):
            raise TypeError("reason must be a CancellationReason")
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._reason = reason
            return True


@dataclass(frozen=True, slots=True)
class AttemptControl:
    """Controls supplied by the backend for exactly one adapter attempt."""

    attempt: int
    timeout_seconds: float
    cancellation: CancellationToken

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("attempt must be a positive integer")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, Real)
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("cancellation must be a CancellationToken")


class CandidateStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CandidateErrorCode(StrEnum):
    """Stable public failure classifications that never contain provider detail."""

    BACKEND_CANCELLED = "backend_cancelled"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    INVALID_REQUEST = "invalid_request"
    TOOL_FAILURE = "tool_failure"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True, slots=True)
class Usage:
    """Normalized usage reported only when the runtime makes it available."""

    input_tokens: int | None = None
    output_tokens: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("input_tokens", "output_tokens"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Provider-neutral outcome of a single attempt.

    ``error_code`` is a stable public classification, rather than a raw provider
    error that could expose credentials or private prompt content.
    """

    status: CandidateStatus
    output: str | None = None
    error_code: CandidateErrorCode | None = None
    retryable: bool = False
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self) -> None:
        if not isinstance(self.status, CandidateStatus):
            raise TypeError("status must be a CandidateStatus")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be a bool")
        if not isinstance(self.usage, Usage):
            raise TypeError("usage must be Usage")
        if self.status is CandidateStatus.COMPLETED:
            if not isinstance(self.output, str):
                raise ValueError("a completed result requires output")
            if self.error_code is not None or self.retryable:
                raise ValueError("a completed result cannot contain failure details")
        else:
            if self.output is not None:
                raise ValueError("a non-completed result cannot contain output")
            if self.error_code is None:
                raise ValueError("a non-completed result requires error_code")
            if not isinstance(self.error_code, CandidateErrorCode):
                raise TypeError("error_code must be a CandidateErrorCode")
        if self.status is CandidateStatus.CANCELLED and self.retryable:
            raise ValueError("a cancelled attempt cannot be retryable")


class CandidateAdapter(ABC):
    """Common boundary implemented by future Local, Codex, and Claude adapters."""

    @property
    @abstractmethod
    def identity(self) -> CandidateIdentity:
        """Return public, credential-free identity for this candidate."""

    @abstractmethod
    async def run_attempt(
        self, request: CandidateRequest, control: AttemptControl
    ) -> CandidateResult:
        """Run exactly one attempt and honor timeout/cancellation controls.

        Implementations must not retry.  The backend compares ``retryable`` with
        its ``RetryPolicy`` before creating another ``AttemptControl``.
        """
