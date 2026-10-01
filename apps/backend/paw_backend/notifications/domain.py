"""The vocabulary of stored notifications (issue #188, Decision 0070 Approved).

A notification is what the Notification Center lists (``docs/NOTIFICATION_POLICY.md``):
a severity, a category, the subsystem's ``kind`` code, the aggregation ``key``
(notifications with the same key are one entry with a count in the Web App) and
``params``. It holds codes and numbers only, never a sentence: the Web App turns
``kind`` and ``params`` into its own language (the i18n catalog), and nothing a
user wrote or a dependency answered is stored here.

Who receives it (the Notification Policy's "Role-aware Notification") is one of:

* ``recipient_user_id``: one user (their task, their project, their device);
* ``audience_capability``: every user whose system role holds that capability
  (``Scope.SYSTEM``) *when they read*: an Admin who is demoted stops seeing the
  System Health notifications at once, a new Admin sees the open ones.
"""

import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from paw_backend.authz.capabilities import CAPABILITIES, Capability, Scope
from paw_backend.authz.policy import DEFAULT_POLICY, Policy, decide
from paw_backend.authz.subjects import Principal, Resource


class Severity(StrEnum):
    """The Notification Policy's levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Category(StrEnum):
    """The Notification Center's "タスク" filter lists ``task``."""

    TASK = "task"
    SYSTEM = "system"


# ``system_health.component_changed``: a subsystem and what happened, as codes.
KIND_PATTERN = r"^[a-z][a-z0-9_]{0,40}(\.[a-z][a-z0-9_]{0,40}){1,3}$"
# A parameter's name, as a code.
PARAM_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,40}$"
KEY_MAX_CHARS = 200
PARAM_TEXT_MAX_CHARS = 200
PARAM_LIST_MAX_ITEMS = 20
PARAMS_MAX_BYTES = 2000

_KIND = re.compile(KIND_PATTERN)
_PARAM_NAME = re.compile(PARAM_NAME_PATTERN)
# Printable ASCII without spaces: keys are codes and ids ("system_health:database").
_KEY = re.compile(rf"^[!-~]{{1,{KEY_MAX_CHARS}}}$")

type ParamValue = str | int | float | bool | None | tuple[str, ...]


class InvalidNotificationError(ValueError):
    """A notification a producer built does not fit the rules above."""


def _param(name: str, value: object) -> ParamValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidNotificationError(f"params.{name} is not finite")
        return value
    if isinstance(value, str):
        if len(value) > PARAM_TEXT_MAX_CHARS:
            raise InvalidNotificationError(f"params.{name} is too long")
        return value
    if isinstance(value, list | tuple):
        if len(value) > PARAM_LIST_MAX_ITEMS:
            raise InvalidNotificationError(f"params.{name} has too many items")
        items = tuple(value)
        for item in items:
            if not isinstance(item, str) or len(item) > PARAM_TEXT_MAX_CHARS:
                raise InvalidNotificationError(f"params.{name} holds a non-code")
        return items
    raise InvalidNotificationError(f"params.{name} has an unsupported type")


def check_params(params: Mapping[str, object]) -> Mapping[str, ParamValue]:
    """The params as an immutable mapping of codes, numbers and code lists."""
    if not isinstance(params, Mapping):
        raise InvalidNotificationError("params must be a mapping")
    checked: dict[str, ParamValue] = {}
    for name, value in params.items():
        if not isinstance(name, str) or not _PARAM_NAME.fullmatch(name):
            raise InvalidNotificationError("a parameter name is not a code")
        checked[name] = _param(name, value)
    if len(params_json(checked).encode()) > PARAMS_MAX_BYTES:
        raise InvalidNotificationError("params are too large")
    return MappingProxyType(checked)


def params_json(params: Mapping[str, ParamValue]) -> str:
    return json.dumps(
        {k: list(v) if isinstance(v, tuple) else v for k, v in params.items()},
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True, slots=True)
class NewNotification:
    """A notification a producer adds (``store.add_in``)."""

    key: str
    kind: str
    severity: Severity
    category: Category
    recipient_user_id: uuid.UUID | None = None
    audience_capability: Capability | None = None
    project_id: uuid.UUID | None = None
    params: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not _KEY.fullmatch(self.key):
            raise InvalidNotificationError("key must be 1-200 printable characters")
        if not isinstance(self.kind, str) or not _KIND.fullmatch(self.kind):
            raise InvalidNotificationError("kind is not a code")
        object.__setattr__(self, "severity", Severity(self.severity))
        object.__setattr__(self, "category", Category(self.category))
        if (self.recipient_user_id is None) == (self.audience_capability is None):
            raise InvalidNotificationError(
                "a notification has one recipient user or one audience capability"
            )
        if self.recipient_user_id is not None and not isinstance(
            self.recipient_user_id, uuid.UUID
        ):
            raise InvalidNotificationError("recipient_user_id must be a UUID")
        if self.audience_capability is not None:
            if not isinstance(self.audience_capability, Capability):
                raise InvalidNotificationError("audience_capability is not known")
            # Only a capability the system role alone decides: the audience is
            # read from the role, never from a project or an owned resource.
            if CAPABILITIES[self.audience_capability].scope is not Scope.SYSTEM:
                raise InvalidNotificationError("audience_capability is not system-wide")
        if self.project_id is not None and not isinstance(self.project_id, uuid.UUID):
            raise InvalidNotificationError("project_id must be a UUID")
        object.__setattr__(self, "params", check_params(self.params))


@dataclass(frozen=True, slots=True)
class StoredNotification:
    """A notification as one user sees it."""

    id: uuid.UUID
    key: str
    kind: str
    severity: Severity
    category: Category
    project_id: uuid.UUID | None
    params: Mapping[str, object]
    created_at: datetime
    read: bool


@dataclass(frozen=True, slots=True)
class NotificationPage:
    items: tuple[StoredNotification, ...]
    # Every unread notification the user can see (not only those of the page).
    unread: int


@dataclass(frozen=True, slots=True)
class ReadResult:
    # How many notifications became read.
    updated: int
    # Every unread notification the user can see after the read (same transaction).
    unread: int


def audience_capabilities(
    principal: Principal, policy: Policy = DEFAULT_POLICY
) -> tuple[str, ...]:
    """The system-wide capabilities ``principal``'s role holds now: the audiences
    whose notifications they receive. A pure decision of the policy (nothing is
    audited: it filters a read the route already authorized)."""
    system = Resource.system()
    return tuple(
        capability.value
        for capability, info in CAPABILITIES.items()
        if info.scope is Scope.SYSTEM
        and decide(principal, capability, system, policy=policy).allowed
    )
