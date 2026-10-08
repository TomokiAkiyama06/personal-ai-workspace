"""The free-text answer [その他...] and its structured preview (PAW-044).

REQUIREMENTS.md: "`その他...` では自然文入力を許可する" and the LLM turns the text
into a structure (scope, the rule, exceptions, default or mandatory, risk, expiry),
for example::

    「開発系のProjectだけ適用して。ただしmainへのMergeは毎回確認して」
    -> scope: project_group, apply_to: development projects, exception: confirm
       before a merge to main, risk: high

Two interpreters produce the same structure:

* a :class:`PreferenceInterpreter` (an LLM behind a port, like the Memory Worker
  of PAW-041): ``interpret(text, candidate) -> str`` returns the text of a JSON
  object of the contract below. What it returns is untrusted model output and is
  validated as a whole (:func:`parse_interpreter_output`); it never names an id
  (a project or repository is resolved by the service from the candidate's own
  evidence) and its risk level can only RAISE the risk the backend computes.
* :class:`RuleInterpreter`, the deterministic fallback used when no model is
  configured, or the model failed or answered outside the contract: a few
  phrases for the scope, "ただし" / "except" for exceptions, "必ず" / "must" for a
  mandatory rule. Decision 0081 point 11.

The preview is shown to the person and comes back with the confirmation (the client
may have corrected it). The service validates it again (:class:`StructuredPreference`)
and recomputes the risk from the text it will store; a high-risk preview is only
applied with an explicit acknowledgement. Nothing here is stored.

Contract (``preference-interpretation-v1``)::

    {"scope": "repo" | "project" | "user" | "project_group",
     "apply_to": str | null,      # required for project_group, else null
     "rule": str,                 # the preference itself
     "exceptions": [str, ...],
     "strength": "default" | "required",
     "risk_level": "low" | "high",
     "expires_at": "<ISO 8601 with offset>" | null}
"""

import inspect
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from paw_backend.memory.journal.rules import is_high_risk
from paw_backend.memory.preferences import limits
from paw_backend.memory.preferences.rules import RiskLevel, TargetScope
from paw_backend.memory.versioning.errors import InputProblem, InvalidMemoryInputError
from paw_backend.memory.versioning.validation import (
    reject,
    validate_aware_datetime,
    validate_enum,
    validate_text,
    validate_uuid,
)

CONTRACT = "preference-interpretation-v1"


class InterpretedScope(StrEnum):
    """The scopes an interpretation may name. ``project_group`` ("the development
    projects") has no entity behind it yet; the service stores it as the person's
    own User Memory with the condition in ``apply_to`` (Decision 0081 point 12)."""

    REPO = "repo"
    PROJECT = "project"
    USER = "user"
    PROJECT_GROUP = "project_group"


class Strength(StrEnum):
    DEFAULT = "default"  # the usual behaviour, may be overridden by an instruction
    # Meant as a rule that must hold. A memory cannot enforce anything (the Tool
    # Broker and Approval decide), so it is kept as a statement and treated as high
    # risk: the person confirms it explicitly.
    REQUIRED = "required"


class Interpreter(StrEnum):
    MODEL = "model"
    RULES = "rules"


@runtime_checkable
class PreferenceInterpreter(Protocol):
    """Turns the person's free text into ``preference-interpretation-v1``.

    ``candidate`` is the candidate's own text (what the preference is about), or
    ``None``. It is given nothing else: no user id, no project, no other memory.
    Raising anything means "no answer": the rule interpreter answers instead.
    """

    async def interpret(self, text: str, candidate: str | None) -> str: ...


def check_interpreter(interpreter: object) -> None:
    """``TypeError`` unless ``interpreter.interpret`` is an ``async`` callable."""
    method = getattr(interpreter, "interpret", None)
    if not callable(method) or not inspect.iscoroutinefunction(method):
        raise TypeError("interpreter.interpret must be an async callable")


# ---------------------------------------------------------------------------
# The structure
# ---------------------------------------------------------------------------


def _clean(field_name: str, value: object, max_chars: int) -> str:
    text = validate_text(field_name, value, max_chars=max_chars)
    if not text.strip():
        raise reject(field_name, InputProblem.BLANK)
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise reject(field_name, InputProblem.INVALID_CHARACTERS)
    return text.strip()


@dataclass(frozen=True, slots=True)
class StructuredPreference:
    """A preference as the person confirms it (validated on construction).

    ``project_id`` / ``repo_id`` are set by the service (a preview) or chosen by the
    person (a confirmation); an interpreter never sets them.
    """

    scope: InterpretedScope
    rule: str
    exceptions: tuple[str, ...] = ()
    apply_to: str | None = None
    strength: Strength = Strength.DEFAULT
    expires_at: datetime | None = None
    project_id: UUID | None = None
    repo_id: UUID | None = None

    def __post_init__(self) -> None:
        scope = validate_enum("scope", self.scope, InterpretedScope)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(
            self, "rule", _clean("rule", self.rule, limits.MAX_RULE_CHARS)
        )
        exceptions = self.exceptions
        if not isinstance(exceptions, list | tuple):
            raise reject("exceptions", InputProblem.WRONG_TYPE)
        if len(exceptions) > limits.MAX_EXCEPTIONS:
            raise reject("exceptions", InputProblem.TOO_MANY)
        object.__setattr__(
            self,
            "exceptions",
            tuple(
                _clean("exceptions", e, limits.MAX_EXCEPTION_CHARS) for e in exceptions
            ),
        )
        if scope is InterpretedScope.PROJECT_GROUP:
            object.__setattr__(
                self,
                "apply_to",
                _clean("apply_to", self.apply_to, limits.MAX_APPLY_TO_CHARS),
            )
        elif self.apply_to is not None:
            raise reject("apply_to", InputProblem.NOT_ALLOWED)
        object.__setattr__(
            self, "strength", validate_enum("strength", self.strength, Strength)
        )
        if self.expires_at is not None:
            validate_aware_datetime("expires_at", self.expires_at)
        needs_project = scope in (InterpretedScope.PROJECT, InterpretedScope.REPO)
        if self.project_id is not None:
            validate_uuid("project_id", self.project_id)
            if not needs_project:
                raise reject("project_id", InputProblem.NOT_ALLOWED)
        if self.repo_id is not None:
            validate_uuid("repo_id", self.repo_id)
            if scope is not InterpretedScope.REPO:
                raise reject("repo_id", InputProblem.NOT_ALLOWED)

    @property
    def target(self) -> TargetScope:
        """Where it is stored: ``project_group`` is the person's own memory."""
        if self.scope is InterpretedScope.PROJECT_GROUP:
            return TargetScope.USER
        return TargetScope(self.scope.value)

    def content(self) -> str:
        """The text of the confirmed memory version: the rule, then its conditions."""
        lines = [self.rule]
        if self.apply_to is not None:
            lines.append(f"適用対象: {self.apply_to}")
        lines.extend(f"例外: {exception}" for exception in self.exceptions)
        if self.strength is Strength.REQUIRED:
            lines.append("強さ: 必須（Memory は実行や権限を強制しない）")
        return "\n".join(lines)

    def risk_level(self, *others: str | None) -> RiskLevel:
        """High when any of its text (or ``others``) touches a high-risk area, or it
        is meant as a mandatory rule."""
        if self.strength is Strength.REQUIRED or is_high_risk(
            self.rule, self.apply_to, *self.exceptions, *others
        ):
            return RiskLevel.HIGH
        return RiskLevel.LOW

    def as_attributes(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "apply_to": self.apply_to,
            "rule": self.rule,
            "exceptions": list(self.exceptions),
            "strength": self.strength.value,
            "expires_at": None
            if self.expires_at is None
            else self.expires_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PreferencePreview:
    """What [その他...] shows before the person confirms."""

    preference: StructuredPreference
    risk_level: RiskLevel
    interpreted_by: Interpreter
    content: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", self.preference.content())

    @property
    def requires_acknowledgement(self) -> bool:
        return self.risk_level is RiskLevel.HIGH


# ---------------------------------------------------------------------------
# The model's answer
# ---------------------------------------------------------------------------

_FIELDS = frozenset(
    {"scope", "apply_to", "rule", "exceptions", "strength", "risk_level", "expires_at"}
)


class InterpreterOutputError(ValueError):
    """The interpreter's answer broke the contract. The message is a closed code."""


def parse_interpreter_output(raw: object) -> tuple[StructuredPreference, RiskLevel]:
    """The structure and the model's own risk level, or :class:`InterpreterOutputError`.

    One violation drops the whole answer (the rule interpreter then answers).
    """
    if not isinstance(raw, str) or len(raw) > limits.MAX_INTERPRETER_OUTPUT_CHARS:
        raise InterpreterOutputError("not_text")
    try:
        document = json.loads(raw)
    except ValueError:
        raise InterpreterOutputError("not_json") from None
    if not isinstance(document, Mapping) or set(document) != _FIELDS:
        raise InterpreterOutputError("fields")
    try:
        risk = RiskLevel(document["risk_level"])
        exceptions = document["exceptions"]
        if not isinstance(exceptions, list):
            raise InterpreterOutputError("exceptions")
        expires_raw = document["expires_at"]
        expires_at = None
        if expires_raw is not None:
            if not isinstance(expires_raw, str):
                raise InterpreterOutputError("expires_at")
            expires_at = datetime.fromisoformat(expires_raw)
        preference = StructuredPreference(
            scope=InterpretedScope(document["scope"]),
            rule=document["rule"],
            exceptions=tuple(exceptions),
            apply_to=document["apply_to"],
            strength=Strength(document["strength"]),
            expires_at=expires_at,
        )
    except InterpreterOutputError:
        raise
    except (ValueError, TypeError, KeyError, InvalidMemoryInputError):
        raise InterpreterOutputError("invalid") from None
    return preference, risk


# ---------------------------------------------------------------------------
# The deterministic fallback
# ---------------------------------------------------------------------------

_REPO = re.compile(r"この\s*(repo|repository|リポジトリ|レポジトリ)|this repo", re.I)
_PROJECT = re.compile(r"この\s*(project|プロジェクト)|this project", re.I)
_ALL = re.compile(
    r"(すべて|全て|全部|全)の?\s*(project|プロジェクト)"
    r"|どこでも|all projects|everywhere",
    re.I,
)
# "開発系のProject", "研究関連のプロジェクト": a group the person names.
_GROUP = re.compile(r"([^\s、。,.]{1,40}?(系|関連))の?\s*(project|プロジェクト)", re.I)
_EXCEPT = re.compile(r"ただし|但し|ただ、|except|but ", re.I)
_REQUIRED = re.compile(r"必ず|必須|絶対|must|required", re.I)
_TRIM = " \t\n、。,.!！"


class RuleInterpreter:
    """A few fixed phrases; anything else keeps the recommended scope.

    It never invents a rule: the rule is the candidate's own text (what the person
    is answering about), or the free text when there is no candidate.
    """

    def interpret_text(
        self, text: str, candidate: str | None, recommended: TargetScope
    ) -> StructuredPreference:
        folded = unicodedata.normalize("NFKC", text)
        marker = _EXCEPT.search(folded)
        main = folded[: marker.start()] if marker else folded
        exceptions = [folded[marker.end() :].strip(_TRIM)] if marker is not None else []
        exceptions = [e[: limits.MAX_EXCEPTION_CHARS] for e in exceptions if e]
        apply_to = None
        group = _GROUP.search(main)
        if _REPO.search(main):
            scope = InterpretedScope.REPO
        elif _PROJECT.search(main):
            scope = InterpretedScope.PROJECT
        elif _ALL.search(main):
            scope = InterpretedScope.USER
        elif group is not None:
            scope = InterpretedScope.PROJECT_GROUP
            apply_to = group.group(0).strip(_TRIM)
        else:
            scope = InterpretedScope(recommended.value)
        rule = candidate if candidate and candidate.strip() else main.strip(_TRIM)
        if not rule:
            rule = folded.strip(_TRIM)
        return StructuredPreference(
            scope=scope,
            rule=rule[: limits.MAX_RULE_CHARS],
            exceptions=tuple(exceptions),
            apply_to=apply_to,
            strength=Strength.REQUIRED if _REQUIRED.search(main) else Strength.DEFAULT,
        )


__all__ = [
    "CONTRACT",
    "InterpretedScope",
    "Interpreter",
    "InterpreterOutputError",
    "PreferenceInterpreter",
    "PreferencePreview",
    "RuleInterpreter",
    "Strength",
    "StructuredPreference",
    "check_interpreter",
    "parse_interpreter_output",
]
