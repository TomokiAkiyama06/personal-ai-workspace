"""The task scope and the normalisation of everything a call may touch.

A task may touch only the paths, hosts, projects and credential handles its
scope lists. A tool call names its targets in arguments the model wrote, so
each target is *normalised first* (one canonical spelling, tricks refused) and
only then compared with the scope. Comparison is exact on the canonical form:
there is no prefix, wildcard or suffix matching that a crafted spelling could
slip through.

Paths
    * absolute, POSIX; a relative path is resolved against the first root of
      the scope; ``.`` and empty segments are dropped;
    * any ``..`` segment (also ``...`` and space/dot variants) is **refused**,
      never collapsed: collapsing it lexically would disagree with what the
      file system does behind a symlink (``link/..``);
    * backslashes, control / format / separator characters, ``~`` prefixes,
      percent-encoded ``.`` ``/`` ``\\`` and text that is not NFKC-stable
      (full-width ``．．／`` and other look-alikes) are refused;
    * containment is ``path == root or path.startswith(root + "/")``, so
      ``/srv/w/task-evil`` is not inside ``/srv/w/task``;
    * comparison is case-**sensitive**: ``/srv/W/task`` is not ``/srv/w/task``.
      A case-insensitive file system can only make that a false denial, never
      an escape;
    * real symlinks are resolved by a :class:`PathResolver` (by default
      ``os.path.realpath`` in a worker thread) and the resolved path must be
      inside the resolved roots. That check happens *before* the call and the
      file system can change afterwards (time of check / time of use): the
      executor must still open files confined to the roots.

Hosts
    ASCII only (an internationalised name must be given as ``xn--`` punycode),
    lower-cased, one trailing dot removed, labels validated; numeric hosts must
    be a strict dotted-quad IPv4 (``0x7f.1``, ``2130706433``, ``127.1`` are
    refused); URLs may not carry user information and only use ``http`` /
    ``https`` on their default port. A host is in scope only when it is
    *exactly* one of the scope's hosts (no wildcard, no subdomain matching).
"""

import asyncio
import ipaddress
import os
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from paw_backend.authz import ProjectState
from paw_backend.authz.subjects import to_uuid
from paw_backend.tools.capabilities import ScopeStatus
from paw_backend.tools.credentials import is_credential_handle

MAX_PATH_LENGTH = 1024
MAX_URL_LENGTH = 2048
MAX_HOST_LENGTH = 253
MAX_ROOTS = 32
MAX_HOSTS = 128
MAX_PROJECTS = 32
MAX_CREDENTIAL_HANDLES = 64

_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_PERCENT_SEPARATOR = re.compile(r"%(?:2[eEfF]|5[cC])")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_URL = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?P<authority>[^/?#@\\\s]+)"
    r"(?P<path>/[^\s?#]*)?(?P<query>\?[^\s#]*)?(?:#\S*)?"
)
_DEFAULT_PORTS = {"http": 80, "https": 443}


class TargetError(ValueError):
    """A target that is malformed or a trick. The message never echoes the value."""


def _check_text(value: object, max_length: int) -> str:
    if type(value) is not str or not value or len(value) > max_length:
        raise TargetError("target is not a valid text")
    for char in value:
        category = unicodedata.category(char)
        if category in _FORBIDDEN_CATEGORIES or (category == "Zs" and char != " "):
            raise TargetError("target contains a forbidden character")
    if unicodedata.normalize("NFKC", value) != value:
        raise TargetError("target is not in normal form")
    return value


def normalise_path(value: object, *, base: str | None = None) -> str:
    """The canonical absolute path for ``value``, or :class:`TargetError`.

    ``base`` (an absolute, already normalised path) is what a relative
    ``value`` is resolved against; without it a relative path is refused.
    """
    text = _check_text(value, MAX_PATH_LENGTH)
    if "\\" in text or text.startswith("~") or _PERCENT_SEPARATOR.search(text):
        raise TargetError("target path is not allowed")
    if text.startswith("/"):
        raw = text
    elif base is not None and base.startswith("/"):
        raw = f"{base}/{text}"
    else:
        raise TargetError("target path is not absolute")
    segments: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if not segment.strip(" ."):  # "..", "...", ". ." and the like
            raise TargetError("target path has a parent-directory segment")
        segments.append(segment)
    return "/" + "/".join(segments)


def path_within(path: str, root: str) -> bool:
    """Whether the canonical ``path`` is ``root`` or below it (never for ``/``)."""
    if root == "/":
        return False
    return path == root or path.startswith(root + "/")


def normalise_host(value: object) -> str:
    """The canonical host name / IP literal for ``value``, or :class:`TargetError`."""
    if type(value) is not str or not value or len(value) > MAX_HOST_LENGTH + 2:
        raise TargetError("target host is not valid")
    if not value.isascii():
        raise TargetError("target host is not ASCII")
    host = value.lower()
    if host.startswith("["):
        if not host.endswith("]") or "%" in host:
            raise TargetError("target host is not valid")
        try:
            address = ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise TargetError("target host is not valid") from None
        return f"[{address.compressed}]"
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > MAX_HOST_LENGTH:
        raise TargetError("target host is not valid")
    labels = host.split(".")
    if not all(_LABEL.fullmatch(label) for label in labels):
        raise TargetError("target host is not valid")
    last = labels[-1]
    if last.isdigit() or last.startswith("0x"):
        # A numeric last label makes it an IPv4 address: only the strict
        # dotted-quad spelling is accepted, never an octal / hex / short form.
        try:
            return str(ipaddress.IPv4Address(host))
        except ValueError:
            raise TargetError("target host is not valid") from None
    return host


def normalise_url(value: object) -> tuple[str, str]:
    """``(canonical url, host)`` of an ``http(s)`` URL, or :class:`TargetError`.

    User information, non-default ports, other schemes, backslashes and white
    space are refused. The fragment is dropped (it is never sent).
    """
    if type(value) is not str or not value or len(value) > MAX_URL_LENGTH:
        raise TargetError("target url is not valid")
    if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise TargetError("target url is not valid")
    match = _URL.fullmatch(value)
    if match is None:
        raise TargetError("target url is not valid")
    scheme = match["scheme"].lower()
    if scheme not in _DEFAULT_PORTS:
        raise TargetError("target url scheme is not allowed")
    authority = match["authority"]
    port_text: str | None = None
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise TargetError("target url is not valid")
        host_text, rest = authority[: end + 1], authority[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                raise TargetError("target url is not valid")
            port_text = rest[1:]
    elif authority.count(":") > 1:
        raise TargetError("target url is not valid")
    elif ":" in authority:
        host_text, port_text = authority.split(":", 1)
    else:
        host_text = authority
    if port_text is not None:
        if not port_text.isascii() or not port_text.isdigit() or len(port_text) > 5:
            raise TargetError("target url port is not valid")
        if int(port_text) != _DEFAULT_PORTS[scheme]:
            raise TargetError("target url port is not allowed")
    host = normalise_host(host_text)
    path = match["path"] or ""
    query = match["query"] or ""
    return f"{scheme}://{host}{path}{query}", host


def normalise_project(value: object) -> uuid.UUID:
    try:
        return to_uuid(value, "project_id")
    except ValueError:
        raise TargetError("target project is not valid") from None


class TargetKind(StrEnum):
    PATH = "path"
    HOST = "host"
    PROJECT = "project"
    CREDENTIAL = "credential"


@dataclass(frozen=True, slots=True)
class Target:
    """One normalised thing a call touches (also what an approver is shown)."""

    kind: TargetKind
    value: str


def _collection(value: object, label: str) -> list:
    if isinstance(value, str | bytes) or not isinstance(value, Iterable):
        raise TypeError(f"{label} must be a collection")
    return list(value)


@dataclass(frozen=True, slots=True)
class TaskScope:
    """What a task may touch. Built by the backend from the task, never by a model.

    ``path_roots`` is ordered: the first is where a relative path resolves.
    ``projects`` maps every project the task may touch to its stored state
    (the authorization decision needs it). ``credential_handles`` maps each
    opaque handle the task may use to **the hosts that credential is valid
    for**: a call that names a host outside that set cannot use the handle,
    even when the task may talk to that host (a GitHub credential must not be
    sent to an unrelated service that the task also reaches). The plaintext
    behind a handle is never here.
    """

    path_roots: tuple[str, ...]
    hosts: frozenset[str]
    projects: Mapping[uuid.UUID, ProjectState]
    credential_handles: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        roots: list[str] = []
        for root in _collection(self.path_roots, "path_roots"):
            normalised = normalise_path(root)  # absolute only: no base
            if normalised == "/":
                raise ValueError("a task scope cannot contain the file system root")
            if normalised not in roots:
                roots.append(normalised)
        hosts = {normalise_host(h) for h in _collection(self.hosts, "hosts")}
        if not isinstance(self.projects, Mapping):
            raise TypeError("projects must map project ids to states")
        projects = {
            normalise_project(pid): ProjectState(state)
            for pid, state in self.projects.items()
        }
        if not isinstance(self.credential_handles, Mapping):
            raise TypeError("credential_handles must map handles to their hosts")
        handles: dict[str, frozenset[str]] = {}
        for handle, valid_hosts in self.credential_handles.items():
            if not is_credential_handle(handle):
                raise ValueError("credential_handles must be opaque handles")
            handles[handle] = frozenset(
                normalise_host(h) for h in _collection(valid_hosts, "hosts of a handle")
            )
        if (
            len(roots) > MAX_ROOTS
            or len(hosts) > MAX_HOSTS
            or len(projects) > MAX_PROJECTS
            or len(handles) > MAX_CREDENTIAL_HANDLES
            or any(len(valid) > MAX_HOSTS for valid in handles.values())
        ):
            raise ValueError("the task scope is too large")
        object.__setattr__(self, "path_roots", tuple(roots))
        object.__setattr__(self, "hosts", frozenset(hosts))
        object.__setattr__(self, "projects", MappingProxyType(projects))
        object.__setattr__(self, "credential_handles", MappingProxyType(handles))


class PathResolver(Protocol):
    """Maps an absolute path to the path the file system really means."""

    async def resolve(self, path: str) -> str: ...


class RealpathResolver:
    """``os.path.realpath`` (follows symlinks; nothing needs to exist) off the loop."""

    async def resolve(self, path: str) -> str:
        return await asyncio.to_thread(os.path.realpath, path)


class LexicalPathResolver:
    """No symlink resolution: the lexical form only. For tests and sandboxes
    that guarantee the tree holds no symlinks."""

    async def resolve(self, path: str) -> str:
        return path


class PathResolutionError(Exception):
    """The resolver failed or returned something unusable."""


@dataclass(frozen=True, slots=True)
class Classification:
    status: ScopeStatus
    # The first target kind that is outside the scope (``None`` when in scope).
    offending: TargetKind | None


async def _resolve(resolver: PathResolver, path: str, timeout_seconds: float) -> str:
    try:
        async with asyncio.timeout(timeout_seconds):
            resolved = await resolver.resolve(path)
        return normalise_path(resolved)
    except Exception as error:
        # Type name only is ever logged by the caller; an OS error can name paths.
        raise PathResolutionError(type(error).__name__) from None


async def classify_targets(
    targets: Iterable[Target],
    scope: TaskScope,
    resolver: PathResolver,
    *,
    timeout_seconds: float = 3.0,
) -> Classification:
    """Where ``targets`` lie relative to ``scope`` (may raise PathResolutionError)."""
    targets = list(targets)
    hosts_of_call = {t.value for t in targets if t.kind is TargetKind.HOST}
    outside: list[TargetKind] = []
    paths = [t for t in targets if t.kind is TargetKind.PATH]
    if paths:
        roots = [
            await _resolve(resolver, root, timeout_seconds) for root in scope.path_roots
        ]
        for target in paths:
            resolved = await _resolve(resolver, target.value, timeout_seconds)
            if not any(path_within(resolved, root) for root in roots):
                outside.append(TargetKind.PATH)
    for target in targets:
        if target.kind is TargetKind.PROJECT:
            if normalise_project(target.value) not in scope.projects:
                outside.append(TargetKind.PROJECT)
        elif target.kind is TargetKind.CREDENTIAL:
            valid_hosts = scope.credential_handles.get(target.value)
            # The handle must be the task's, and must be valid for every host
            # the same call reaches (a credential is never sent elsewhere).
            if valid_hosts is None or not hosts_of_call <= valid_hosts:
                outside.append(TargetKind.CREDENTIAL)
    if outside:
        return Classification(ScopeStatus.OUT_OF_SCOPE, outside[0])
    if any(t.kind is TargetKind.HOST and t.value not in scope.hosts for t in targets):
        return Classification(ScopeStatus.HOST_OUT_OF_SCOPE, TargetKind.HOST)
    return Classification(ScopeStatus.IN_SCOPE, None)
