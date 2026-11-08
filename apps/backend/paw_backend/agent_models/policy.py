"""The Admin's policy of the CLI agents' models: its vocabulary and its rules
(Decision 0103, 2 A, 3 A and 9 A; approved 2026-11-08; issue #341).

Per connection kind (Codex, Claude) the Admin (Owner included) allows models
out of the repository's verified ones (:mod:`.verified`) and names exactly one
of them the default. Nothing here does I/O.

* :class:`AllowedModel`: one allowed model, whether it is the default, and the
  Admin's short description (for the selection of Decision 0103, 11; plain
  text, at most :data:`MAX_DESCRIPTION_CHARS`, no control character);
* :class:`KindPolicy`: a kind's allowed models (at least one, at most
  :data:`MAX_MODELS_PER_KIND`, no model twice, exactly one default) with who
  wrote them and when (the database's clock);
* :func:`check_policy`: the rules a new policy must pass before it is stored;
  a model outside the verified ones is :class:`ModelNotVerifiedError` (the
  API's 422 ``model_not_verified``), any other break of the rules
  :class:`InvalidModelPolicyError` (422 ``validation_error``).

While a kind has no policy, its only allowed model and its default is the
runtime configuration's ``[agents.<kind>].model`` (Decision 0103, 9 A,
superseding the meaning of Decision 0099, 7): everything works as before.
"""

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from paw_backend.agent_models.verified import verified_models
from paw_backend.connections.domain import ConnectionKind
from paw_backend.connections.errors import InvalidConnectionInputError
from paw_backend.connections.validation import validate_model

#: The models one kind may allow (a handful are verified; a bound for the API).
MAX_MODELS_PER_KIND: Final = 16
#: The longest description of an allowed model.
MAX_DESCRIPTION_CHARS: Final = 120
#: Decision 0103, 7 A: the audit action of a change of the policy (ids and
#: enums only; the names are in the policy's own table and its history).
ACTION_UPDATE: Final = "admin.agent_models.update"
#: The audit resource of the policy.
RESOURCE_KIND: Final = "agent_model_policy"


class InvalidModelPolicyError(ValueError):
    """The policy breaks a rule (see the module). No value is in the message."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid model policy ({reason})")
        self.reason = reason


class ModelNotVerifiedError(InvalidModelPolicyError):
    """A model the repository has not verified for its kind (Decision 0103, 3
    A): the Admin cannot allow it."""

    def __init__(self) -> None:
        super().__init__("model_not_verified")


@dataclass(frozen=True, slots=True)
class AllowedModel:
    """One model a kind allows."""

    model: str
    default: bool = False
    description: str | None = None


@dataclass(frozen=True, slots=True)
class KindPolicy:
    """A kind's stored policy (see the module)."""

    kind: ConnectionKind
    models: tuple[AllowedModel, ...]
    updated_by: uuid.UUID | None = None
    updated_at: datetime | None = None

    @property
    def default_model(self) -> str | None:
        """The default (``None`` only for rows the service never writes)."""
        defaults = [entry.model for entry in self.models if entry.default]
        return defaults[0] if len(defaults) == 1 else None

    def allows(self, model: object) -> bool:
        return any(entry.model == model for entry in self.models)


def _description(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str:
        raise InvalidModelPolicyError("description")
    if not value or len(value) > MAX_DESCRIPTION_CHARS or value != value.strip():
        raise InvalidModelPolicyError("description")
    if any(unicodedata.category(char) in ("Cc", "Cf", "Cs") for char in value):
        raise InvalidModelPolicyError("description")
    return value


def check_policy(kind: object, models: object) -> tuple[AllowedModel, ...]:
    """The allowed models of a new policy of ``kind`` once they passed every
    rule (see the module), in the order given."""
    if type(kind) is not ConnectionKind:
        raise InvalidModelPolicyError("kind")
    if (
        not isinstance(models, tuple | list)
        or not 1 <= len(models) <= MAX_MODELS_PER_KIND
        or not all(type(entry) is AllowedModel for entry in models)
    ):
        raise InvalidModelPolicyError("models")
    checked: list[AllowedModel] = []
    for entry in models:
        try:
            name = validate_model("model", entry.model)
        except InvalidConnectionInputError:
            raise InvalidModelPolicyError("model") from None
        if type(entry.default) is not bool:
            raise InvalidModelPolicyError("default")
        checked.append(
            AllowedModel(name, entry.default, _description(entry.description))
        )
    names = [entry.model for entry in checked]
    if len(set(names)) != len(names):
        raise InvalidModelPolicyError("duplicate_model")
    if sum(entry.default for entry in checked) != 1:
        raise InvalidModelPolicyError("default")
    if not set(names) <= verified_models(kind):
        raise ModelNotVerifiedError()
    return tuple(checked)


__all__ = [
    "ACTION_UPDATE",
    "MAX_DESCRIPTION_CHARS",
    "MAX_MODELS_PER_KIND",
    "RESOURCE_KIND",
    "AllowedModel",
    "InvalidModelPolicyError",
    "KindPolicy",
    "ModelNotVerifiedError",
    "check_policy",
]
