"""Argument checks of the orchestrator's public methods (PAW-034).

Every check raises ``InvalidOrchestratorArgumentError(parameter)`` and never
coerces: a ``bool`` is not an ``int``, a ``str`` is not a ``uuid.UUID``, a string
is not an enum member. The public methods call these before they touch the
database, so a wrong argument never reaches PostgreSQL and the error never holds
the value.
"""

import math
import re
import uuid
from enum import Enum
from typing import Any, NoReturn

from paw_backend.orchestrator.errors import InvalidOrchestratorArgumentError

_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]*")
_AGENT_LABEL = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_SIGNATURE = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SURROGATE = re.compile("[\ud800-\udfff]")
MAX_WORKER_ID_LENGTH = 100
MAX_INT32 = 2**31 - 1


def _reject(parameter: str) -> NoReturn:
    raise InvalidOrchestratorArgumentError(parameter)


def check_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    """``value`` must be an ``int`` (not a ``bool``) within [minimum, maximum]."""
    if isinstance(value, bool) or not isinstance(value, int):
        _reject(name)
    if not minimum <= value <= maximum:
        _reject(name)
    return value


def check_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        _reject(name)
    return value


def check_seconds(
    name: str, value: Any, *, minimum: float, maximum: float, allow_zero: bool = False
) -> float:
    """A finite ``int`` or ``float`` (not a ``bool``) within the bounds."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        _reject(name)
    if not math.isfinite(value) or not minimum <= value <= maximum:
        if not (allow_zero and value == 0):
            _reject(name)
    return float(value)


def check_uuid(name: str, value: Any) -> uuid.UUID:
    """Only a ``uuid.UUID`` instance is accepted (text is not parsed)."""
    if not isinstance(value, uuid.UUID):
        _reject(name)
    return value


def check_member[E: Enum](name: str, value: Any, enum_class: type[E]) -> E:
    """The member itself or its exact serialised ``str``, as the member."""
    if type(value) is enum_class:
        return value
    if type(value) is str:
        try:
            return enum_class(value)
        except ValueError:
            pass
    _reject(name)


def check_worker_id(value: Any, name: str = "worker_id") -> str:
    """1 to 100 ASCII characters ``[A-Za-z0-9._:@/-]``, first a letter or digit."""
    if (
        type(value) is not str
        or len(value) > MAX_WORKER_ID_LENGTH
        or _WORKER_ID.fullmatch(value) is None
    ):
        _reject(name)
    return value


def check_agent_label(name: str, value: Any) -> str:
    """The label of an agent of a ladder: ``[a-z][a-z0-9._-]{0,63}``."""
    if type(value) is not str or _AGENT_LABEL.fullmatch(value) is None:
        _reject(name)
    return value


def check_label(name: str, value: Any, *, maximum: int) -> str:
    """A non-blank ``str`` (not a subclass) of at most ``maximum`` characters with
    no control character and no surrogate (text that UTF-8 cannot encode)."""
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > maximum
        or _CONTROL.search(value) is not None
        or _SURROGATE.search(value) is not None
    ):
        _reject(name)
    return value


def check_signature(value: Any) -> str:
    """A failure signature: exactly 64 lowercase hexadecimal characters."""
    if type(value) is not str or _SIGNATURE.fullmatch(value) is None:
        _reject("signature")
    return value
