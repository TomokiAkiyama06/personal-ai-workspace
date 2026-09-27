"""The settings of the repository module as one validated value (PAW-027).

``RepositoryPolicy`` holds what an operator may configure: where checkouts live,
which existing directories may be registered, which hosts may be cloned from and
how long git may run. Every value is validated when the policy is built, so a
bad configuration fails at start, not at the first registration.

The defaults are the ones of Decision 0017 (Approved): checkouts under
``<home>/workspaces``, existing repositories anywhere in the user's own home,
clones from ``github.com`` only.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from paw_backend.config import Settings
from paw_backend.repositories.limits import (
    DEFAULT_CLONE_TIMEOUT_S,
    DEFAULT_GIT_TIMEOUT_S,
    DEFAULT_MIN_LINUX_UID,
    MAX_CLONE_HOSTS,
    MAX_GIT_TIMEOUT_S,
    MAX_ROOT_TEMPLATES,
    PENDING_TIMEOUT_FACTOR,
)
from paw_backend.tools.scope import TargetError, normalise_host, normalise_path

_SUBDIR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_MAX_UID = 4_294_967_295


def validate_workspace_subdir(value: object) -> str:
    """One safe directory name (``workspaces``): the checkout root below a home."""
    if not isinstance(value, str) or _SUBDIR.fullmatch(value) is None:
        raise ValueError("workspace_subdir is not a safe directory name")
    return value


def validate_root_template(value: object) -> str:
    """A root an existing repository may lie below, per user.

    An absolute path that contains ``{home}`` (only as the first part: the user's
    home directory) or ``{user}`` (the Linux user name), so that **every root
    belongs to one user**: a template without either would be one directory shared
    by all users, which per-user isolation forbids. The expansion of the
    placeholders is plain replacement, never string formatting.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("a root template must be a non-empty string")
    for name in _PLACEHOLDER.findall(value):
        if name not in ("home", "user"):
            raise ValueError("a root template can only use {home} and {user}")
    if "{" in _PLACEHOLDER.sub("", value) or "}" in _PLACEHOLDER.sub("", value):
        raise ValueError("a root template has a stray brace")
    if "{home}" not in value and "{user}" not in value:
        raise ValueError("a root template must contain {home} or {user}")
    if "{home}" in value and not value.startswith("{home}"):
        raise ValueError("{home} can only be the beginning of a root template")
    if value.count("{home}") > 1:
        raise ValueError("a root template has {home} more than once")
    probe = value.replace("{home}", "/home/probe").replace("{user}", "probe")
    try:
        canonical = normalise_path(probe)
    except TargetError:
        raise ValueError("a root template is not a valid absolute path") from None
    if canonical != probe or canonical == "/":
        raise ValueError("a root template must be a canonical absolute path")
    return value


def validate_clone_host(value: object) -> str:
    """A host that may be cloned from: a lower-case DNS name, no port, no address."""
    if not isinstance(value, str):
        raise ValueError("a clone host must be a string")
    try:
        host = normalise_host(value)
    except TargetError:
        raise ValueError("a clone host is not a valid host name") from None
    if host != value or host.startswith("[") or host.replace(".", "").isdigit():
        raise ValueError("a clone host must be a lower-case DNS name")
    return host


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    """Validated settings of the repository module (all fields have defaults)."""

    workspace_subdir: str = "workspaces"
    existing_roots: tuple[str, ...] = ("{home}",)
    clone_hosts: tuple[str, ...] = ("github.com",)
    min_uid: int = DEFAULT_MIN_LINUX_UID
    git_timeout_s: float = DEFAULT_GIT_TIMEOUT_S
    clone_timeout_s: float = DEFAULT_CLONE_TIMEOUT_S

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workspace_subdir", validate_workspace_subdir(self.workspace_subdir)
        )
        roots = _strings("existing_roots", self.existing_roots, MAX_ROOT_TEMPLATES)
        hosts = _strings("clone_hosts", self.clone_hosts, MAX_CLONE_HOSTS)
        object.__setattr__(
            self,
            "existing_roots",
            tuple(dict.fromkeys(validate_root_template(r) for r in roots)),
        )
        object.__setattr__(
            self,
            "clone_hosts",
            tuple(dict.fromkeys(validate_clone_host(h) for h in hosts)),
        )
        if (
            isinstance(self.min_uid, bool)
            or not isinstance(self.min_uid, int)
            or not 1 <= self.min_uid <= _MAX_UID
        ):
            raise ValueError("min_uid must be an int from 1 to 4294967295")
        for name in ("git_timeout_s", "clone_timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a number")
            if not 0 < value <= MAX_GIT_TIMEOUT_S:
                raise ValueError(f"{name} is out of range")
            object.__setattr__(self, name, float(value))

    @property
    def pending_timeout_s(self) -> float:
        """A pending checkout older than this is stale (its process died)."""
        return self.clone_timeout_s * PENDING_TIMEOUT_FACTOR

    @classmethod
    def from_settings(cls, settings: Settings) -> "RepositoryPolicy":
        """The policy of the ``PAW_REPOSITORY_*`` settings."""
        return cls(
            workspace_subdir=settings.repository_workspace_subdir,
            existing_roots=tuple(settings.repository_existing_roots),
            clone_hosts=tuple(settings.repository_clone_hosts),
            min_uid=settings.repository_min_linux_uid,
            git_timeout_s=settings.repository_git_timeout_seconds,
            clone_timeout_s=settings.repository_clone_timeout_seconds,
        )


def _strings(name: str, value: object, limit: int) -> list[str]:
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise ValueError(f"{name} must be a collection of strings")
    items = list(value)
    if not items or len(items) > limit:
        raise ValueError(f"{name} must hold 1 to {limit} entries")
    return items
