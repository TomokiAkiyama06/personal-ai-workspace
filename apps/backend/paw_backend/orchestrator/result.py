"""The structured result a node passes to the nodes that depend on it (PAW-034).

``REQUIREMENTS.md`` ("Result passing"): sub-agents do not talk to each other
without limit; the backend orchestrator passes **structured results**, and only
the context the next node needs (never the whole conversation). The fields are
the ones the requirements list ("Subtask output例"). Everything is bounded
(Decision 0021, section 5): a result is at most ``MAX_RESULT_BYTES`` of JSON, and
a node receives the results of its direct dependencies only, at most
``MAX_DEPENDENCIES`` of them (so at most ``MAX_UPSTREAM_BYTES`` in all).

``NodeResult`` is validated when it is built (unknown fields, wrong types, texts
with a NUL or a surrogate, lists that are too long, a JSON that is too large are
refused with ``InvalidNodeResultError``) and is immutable and detached from the
caller's containers. ``to_json`` / ``from_json`` are the storage form.
"""

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from paw_backend.orchestrator.errors import InvalidNodeResultError, ResultReason
from paw_backend.orchestrator.jsonvalue import (
    JsonProblem,
    check_json_object,
    encoded_size,
)
from paw_backend.orchestrator.limits import (
    MAX_CHANGED_FILES,
    MAX_COMMIT_CHARS,
    MAX_ITEM_CHARS,
    MAX_LIST_ITEMS,
    MAX_PATH_CHARS,
    MAX_RESULT_BYTES,
    MAX_SUMMARY_CHARS,
    MAX_TEST_RESULT_BYTES,
    MAX_TEST_RESULT_DEPTH,
)

_SURROGATE = re.compile("[\ud800-\udfff]")
_LINE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]")
_TEXT_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

RESULT_FIELDS = frozenset(
    {
        "summary",
        "changed_files",
        "commit",
        "test_result",
        "discovered_facts",
        "dependency_notes",
        "unresolved_questions",
        "confidence",
        "artifacts",
    }
)


def _refuse(reason: ResultReason):
    raise InvalidNodeResultError(reason)


def _text(value: object, *, limit: int, multiline: bool) -> str:
    if type(value) is not str:
        _refuse(ResultReason.BAD_TYPE)
    if len(value) > limit * 4:
        _refuse(ResultReason.BAD_TEXT)
    forbidden = _TEXT_FORBIDDEN if multiline else _LINE_FORBIDDEN
    if forbidden.search(value) or _SURROGATE.search(value):
        _refuse(ResultReason.BAD_TEXT)
    stripped = value.strip()
    if not stripped or len(stripped) > limit:
        _refuse(ResultReason.BAD_TEXT)
    return stripped


def _texts(
    value: object, *, count: int, limit: int, multiline: bool
) -> tuple[str, ...]:
    if isinstance(value, str | bytes | Mapping) or not isinstance(value, Iterable):
        _refuse(ResultReason.BAD_TYPE)
    items = list(value)
    if len(items) > count:
        _refuse(ResultReason.TOO_MANY_ITEMS)
    return tuple(_text(item, limit=limit, multiline=multiline) for item in items)


@dataclass(frozen=True, slots=True)
class NodeResult:
    """What a node reports. Only ``summary`` is required.

    ``changed_files`` and ``artifacts`` are paths / opaque references (one line
    each); ``commit`` a revision; ``test_result`` a small JSON object;
    ``discovered_facts``, ``dependency_notes`` and ``unresolved_questions`` are
    texts; ``confidence`` is a number from 0 to 1.
    """

    summary: str
    changed_files: tuple[str, ...] = ()
    commit: str | None = None
    test_result: Mapping[str, Any] | None = None
    discovered_facts: tuple[str, ...] = ()
    dependency_notes: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    confidence: float | None = None
    artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        set_(
            self,
            "summary",
            _text(self.summary, limit=MAX_SUMMARY_CHARS, multiline=True),
        )
        set_(
            self,
            "changed_files",
            _texts(
                self.changed_files,
                count=MAX_CHANGED_FILES,
                limit=MAX_PATH_CHARS,
                multiline=False,
            ),
        )
        if self.commit is not None:
            set_(
                self,
                "commit",
                _text(self.commit, limit=MAX_COMMIT_CHARS, multiline=False),
            )
        if self.test_result is not None:
            if not isinstance(self.test_result, dict):
                _refuse(ResultReason.BAD_TYPE)
            try:
                checked = check_json_object(
                    self.test_result,
                    max_bytes=MAX_TEST_RESULT_BYTES,
                    max_depth=MAX_TEST_RESULT_DEPTH,
                )
            except JsonProblem as problem:
                _refuse(
                    ResultReason.TOO_LARGE
                    if problem.kind == "size"
                    else ResultReason.BAD_TYPE
                )
            set_(self, "test_result", checked)
        for name in ("discovered_facts", "dependency_notes", "unresolved_questions"):
            set_(
                self,
                name,
                _texts(
                    getattr(self, name),
                    count=MAX_LIST_ITEMS,
                    limit=MAX_ITEM_CHARS,
                    multiline=True,
                ),
            )
        set_(
            self,
            "artifacts",
            _texts(
                self.artifacts,
                count=MAX_LIST_ITEMS,
                limit=MAX_PATH_CHARS,
                multiline=False,
            ),
        )
        if self.confidence is not None:
            confidence = self.confidence
            if type(confidence) not in (int, float):
                _refuse(ResultReason.BAD_NUMBER)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                _refuse(ResultReason.BAD_NUMBER)
            set_(self, "confidence", float(confidence))
        if encoded_size(self.to_json()) > MAX_RESULT_BYTES:
            _refuse(ResultReason.TOO_LARGE)

    def to_json(self) -> dict[str, Any]:
        """The storage form: a JSON object with every field."""
        return {
            "summary": self.summary,
            "changed_files": list(self.changed_files),
            "commit": self.commit,
            "test_result": None if self.test_result is None else dict(self.test_result),
            "discovered_facts": list(self.discovered_facts),
            "dependency_notes": list(self.dependency_notes),
            "unresolved_questions": list(self.unresolved_questions),
            "confidence": self.confidence,
            "artifacts": list(self.artifacts),
        }

    @classmethod
    def from_json(cls, data: object) -> "NodeResult":
        """Read the storage form (or an agent's mapping). Unknown fields are refused."""
        if not isinstance(data, Mapping):
            _refuse(ResultReason.NOT_A_RESULT)
        if any(type(name) is not str or name not in RESULT_FIELDS for name in data):
            _refuse(ResultReason.UNKNOWN_FIELD)
        if "summary" not in data:
            _refuse(ResultReason.MISSING_FIELD)
        return cls(**dict(data))


def upstream_size(results: Mapping[str, NodeResult]) -> int:
    """The bytes of JSON a node would receive for these dependency results."""
    return sum(encoded_size(result.to_json()) for result in results.values())
