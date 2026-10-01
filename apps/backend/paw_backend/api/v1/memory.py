"""The Memory screen over HTTP (issue #186; PAW-063's ``MemorySource``, PR #178).

``/api/v1/memory/*``. The reads are ``paw_backend.memory.board.MemoryBoard``, the
writes ``MemoryVersioningService``; this module only translates. Decision 0068
(Proposed) records the choices.

* ``GET /scopes``: the Scope Tree with counts.
* ``GET /memories?scope=user|project|repo|shared[&project_id][&repo_id][&q]``:
  the current version of each memory of one scope (``truncated`` when there were
  more than the list holds).
* ``GET /memories/{id}/history``: the History Graph (versions, relations, the
  related versions of other memories, ``can_write``).
* ``GET /memories/{id}/versions/{n}/sources``: the sources of one version.
* ``POST /memories/{id}/edit``: a manual edit (``expected_version``).
* ``POST /memories/{id}/restore``: a new version with an old version's content
  (``expected_version``, ``source_version``).

Every route needs a session (not restricted by the Passkey policy) whose person
holds ``memory.read`` on their own memory (Decision 0024: ``DENIED_ONLY``, an
allowed read writes no audit row). What each call may then read or change is
decided by the services, per scope, with the roles and project states READ FROM
THE DATABASE. A memory the reader may not see answers 404 ``memory_not_found``,
exactly as one that does not exist. A change made on top of a version that is not
the current one answers 409 ``memory_version_conflict`` and writes nothing. The
writes are ordinary Memory changes (``memory.use`` / ``project.memory.use``,
audited): no Passkey Step-up (REQUIREMENTS.md asks it of Owner / Admin
operations), and the state-changing Origin check (Decision 0044) covers them.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from starlette.requests import HTTPConnection

from paw_backend.authz import Capability, Principal, Resource, require_capability
from paw_backend.authz.deps import get_principal_provider
from paw_backend.errors import ApiError
from paw_backend.memory.board import (
    BoardScope,
    BoardScopeKind,
    BoardSource,
    BoardVersion,
    MemoryBoard,
)
from paw_backend.memory.board import limits as board_limits
from paw_backend.memory.models import MemoryScope
from paw_backend.memory.versioning import (
    InvalidMemoryInputError,
    MemoryBusyError,
    MemoryChanges,
    MemoryDatabaseError,
    MemoryNotFoundError,
    MemoryPermissionError,
    MemoryScopeNotSupportedError,
    MemoryStateError,
    MemoryVersionConflictError,
    MemoryVersioningService,
)
from paw_backend.memory.versioning import limits as version_limits
from paw_backend.memory.versioning.records import MemoryVersionView
from paw_backend.memory.versioning.service import RESOURCE_MEMORY

router = APIRouter(prefix="/memory", tags=["memory"])


async def _own_memory(connection: HTTPConnection) -> Resource:
    """The person's own memory, for the route guard (``memory.read``).

    ``require_capability`` runs this only for an authenticated person; the
    provider is asked again because the guard does not pass the principal on.
    """
    principal = await get_principal_provider(connection).get_principal(connection)
    if principal is None:  # the session ended between the two lookups
        raise ValueError("no principal")
    return Resource.owned_by(principal.user_id, RESOURCE_MEMORY)


_READER = Annotated[
    Principal, Depends(require_capability(Capability.MEMORY_READ, _own_memory))
]


# -- answers ------------------------------------------------------------------------


class MemoryVersionOut(BaseModel):
    """One stored version (``MemoryVersionView``) and its writer's name."""

    memory_id: uuid.UUID
    version_id: uuid.UUID
    version_number: int
    scope: str
    owner_user_id: uuid.UUID | None
    project_id: uuid.UUID | None
    project_group_id: uuid.UUID | None
    repo_id: uuid.UUID | None
    memory_type: str
    title: str
    content: str
    importance: int
    pinned: bool
    status: str
    confirmation_state: str
    freshness_policy: str
    verified_at: datetime | None
    # Seconds.
    revalidate_after: int | None
    revalidate_triggers: list[str]
    expires_at: datetime | None
    commit_sha: str | None
    branch: str | None
    stale_since: datetime | None
    actor_type: str
    actor_user_id: uuid.UUID | None
    # The login name of the person who wrote it (null: not a person, or a
    # deleted account).
    actor_name: str | None
    change_reason: str | None
    created_at: datetime


class RepoScopeOut(BaseModel):
    repo_id: uuid.UUID
    name: str
    count: int


class ProjectScopeOut(BaseModel):
    project_id: uuid.UUID
    name: str
    count: int
    project_count: int
    repos: list[RepoScopeOut]


class ScopeTreeOut(BaseModel):
    user: int
    projects: list[ProjectScopeOut]
    shared: int


class MemoryListOut(BaseModel):
    memories: list[MemoryVersionOut]
    truncated: bool


class MemoryRelationOut(BaseModel):
    from_version_id: uuid.UUID
    to_version_id: uuid.UUID
    relation: str
    reason: str | None


class MemoryHistoryOut(BaseModel):
    versions: list[MemoryVersionOut]
    relations: list[MemoryRelationOut]
    related: list[MemoryVersionOut]
    can_write: bool


class MemorySourceOut(BaseModel):
    source_type: str
    conversation_id: uuid.UUID | None
    message_id: uuid.UUID | None
    source_ref: str | None
    source_deleted_at: datetime | None
    created_at: datetime


class MemorySourcesOut(BaseModel):
    sources: list[MemorySourceOut]


# -- requests -----------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


_VERSION = Field(ge=1, le=version_limits.MAX_VERSION_NUMBER)


class EditRequest(_Body):
    expected_version: StrictInt = _VERSION
    title: StrictStr | None = Field(
        default=None, min_length=1, max_length=version_limits.MAX_TITLE_CHARS
    )
    content: StrictStr | None = Field(
        default=None, min_length=1, max_length=version_limits.MAX_CONTENT_CHARS
    )
    reason: StrictStr | None = Field(
        default=None, max_length=version_limits.MAX_REASON_CHARS
    )


class RestoreRequest(_Body):
    expected_version: StrictInt = _VERSION
    source_version: StrictInt = _VERSION
    reason: StrictStr | None = Field(
        default=None, max_length=version_limits.MAX_REASON_CHARS
    )


# -- translation ----------------------------------------------------------------------


def _version(entry: BoardVersion) -> MemoryVersionOut:
    version = entry.version
    after = version.revalidate_after
    return MemoryVersionOut(
        memory_id=version.memory_id,
        version_id=version.version_id,
        version_number=version.version_number,
        scope=version.scope.value,
        owner_user_id=version.owner_user_id,
        project_id=version.project_id,
        project_group_id=version.project_group_id,
        repo_id=version.repo_id,
        memory_type=version.memory_type,
        title=version.title,
        content=version.content,
        importance=version.importance,
        pinned=version.pinned,
        status=version.status.value,
        confirmation_state=version.confirmation_state.value,
        freshness_policy=version.freshness_policy.value,
        verified_at=version.verified_at,
        revalidate_after=None if after is None else int(after.total_seconds()),
        revalidate_triggers=list(version.revalidate_triggers),
        expires_at=version.expires_at,
        commit_sha=version.commit_sha,
        branch=version.branch,
        stale_since=version.stale_since,
        actor_type=version.actor_type.value,
        actor_user_id=version.actor_user_id,
        actor_name=entry.actor_name,
        change_reason=version.change_reason,
        created_at=version.created_at,
    )


def _source(source: BoardSource) -> MemorySourceOut:
    return MemorySourceOut(
        source_type=source.source_type.value,
        conversation_id=source.conversation_id,
        message_id=source.message_id,
        source_ref=source.source_ref,
        source_deleted_at=source.source_deleted_at,
        created_at=source.created_at,
    )


def _database(request: Request):
    database = request.app.state.database
    if not database.configured:
        raise ApiError(503, "service_unavailable", "Service temporarily unavailable")
    return database


def _board(request: Request) -> MemoryBoard:
    # The application's Authorizer, read per request (tests swap it).
    return MemoryBoard(_database(request), request.app.state.authorizer)


def _versioning(request: Request) -> MemoryVersioningService:
    return MemoryVersioningService(_database(request), request.app.state.authorizer)


async def _named(request: Request, written: MemoryVersionView) -> BoardVersion:
    """The version just written, with its writer's name.

    The version is committed: a failure to read the name must not turn the
    answer into an error (the client would retry and run into its own version),
    so the name is then left out.
    """
    try:
        return await _board(request).named(written)
    except MemoryDatabaseError:
        return BoardVersion(written, None)


@contextmanager
def _memory_errors() -> Iterator[None]:
    """The service errors as API errors (their messages are fixed strings)."""
    try:
        yield
    except MemoryNotFoundError:
        raise ApiError(404, "memory_not_found", "Memory not found") from None
    except MemoryVersionConflictError:
        raise ApiError(
            409,
            "memory_version_conflict",
            "The memory changed since the version you edited",
        ) from None
    except MemoryStateError as error:
        raise ApiError(
            409, "memory_state_conflict", f"State does not allow this: {error.problem}"
        ) from None
    except MemoryScopeNotSupportedError:
        raise ApiError(
            409, "memory_scope_not_supported", "This memory is managed elsewhere"
        ) from None
    except InvalidMemoryInputError as error:
        raise ApiError(
            422, "validation_error", f"Invalid {error.field}: {error.problem}"
        ) from None
    except MemoryPermissionError as error:
        if error.reason == "audit_unavailable":
            raise ApiError(
                503, "service_unavailable", "Service temporarily unavailable"
            ) from None
        raise ApiError(403, "forbidden", "Permission denied") from None
    except MemoryBusyError:
        raise ApiError(503, "memory_busy", "Memory is busy; retry later") from None
    except MemoryDatabaseError:
        raise ApiError(
            503, "service_unavailable", "Service temporarily unavailable"
        ) from None


def _scope(
    scope: MemoryScope | str, project_id: uuid.UUID | None, repo_id: uuid.UUID | None
) -> BoardScope:
    try:
        kind = BoardScopeKind(scope)
    except ValueError:
        raise ApiError(422, "validation_error", "Invalid scope") from None
    needs_project = kind in (BoardScopeKind.PROJECT, BoardScopeKind.REPO)
    needs_repo = kind is BoardScopeKind.REPO
    if needs_project != (project_id is not None) or needs_repo != (repo_id is not None):
        raise ApiError(422, "validation_error", "Invalid scope")
    return BoardScope(kind, project_id, repo_id)


# -- routes ---------------------------------------------------------------------------


@router.get(
    "/scopes",
    response_model=ScopeTreeOut,
    summary="The memory scopes the reader may see, with the number of memories",
)
async def memory_scopes(request: Request, principal: _READER) -> ScopeTreeOut:
    with _memory_errors():
        tree = await _board(request).scopes(principal)
    return ScopeTreeOut(
        user=tree.user,
        projects=[
            ProjectScopeOut(
                project_id=project.project_id,
                name=project.name,
                count=project.count,
                project_count=project.project_count,
                repos=[
                    RepoScopeOut(repo_id=repo.repo_id, name=repo.name, count=repo.count)
                    for repo in project.repos
                ],
            )
            for project in tree.projects
        ],
        shared=tree.shared,
    )


@router.get(
    "/memories",
    response_model=MemoryListOut,
    summary="The current version of each memory of one scope (with a search)",
)
async def list_memories(
    request: Request,
    principal: _READER,
    scope: Annotated[str, Query(max_length=16)],
    project_id: uuid.UUID | None = None,
    repo_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=board_limits.MAX_QUERY_CHARS)] = None,
) -> MemoryListOut:
    key = _scope(scope, project_id, repo_id)
    with _memory_errors():
        found = await _board(request).list_memories(principal, key, q)
    return MemoryListOut(
        memories=[_version(entry) for entry in found.memories],
        truncated=found.truncated,
    )


@router.get(
    "/memories/{memory_id}/history",
    response_model=MemoryHistoryOut,
    summary="The History Graph of one memory",
)
async def memory_history(
    request: Request, principal: _READER, memory_id: uuid.UUID
) -> MemoryHistoryOut:
    with _memory_errors():
        history = await _board(request).history(principal, memory_id)
    return MemoryHistoryOut(
        versions=[_version(entry) for entry in history.versions],
        relations=[
            MemoryRelationOut(
                from_version_id=edge.from_version_id,
                to_version_id=edge.to_version_id,
                relation=edge.relation.value,
                reason=edge.reason,
            )
            for edge in history.relations
        ],
        related=[_version(entry) for entry in history.related],
        can_write=history.can_write,
    )


@router.get(
    "/memories/{memory_id}/versions/{version_number}/sources",
    response_model=MemorySourcesOut,
    summary="Where one version of a memory came from",
)
async def memory_sources(
    request: Request,
    principal: _READER,
    memory_id: uuid.UUID,
    version_number: Annotated[int, _VERSION],
) -> MemorySourcesOut:
    with _memory_errors():
        sources = await _board(request).sources(principal, memory_id, version_number)
    return MemorySourcesOut(sources=[_source(source) for source in sources])


@router.post(
    "/memories/{memory_id}/edit",
    response_model=MemoryVersionOut,
    summary="Edit a memory: a new confirmed version after expected_version",
)
async def edit_memory(
    request: Request, principal: _READER, memory_id: uuid.UUID, body: EditRequest
) -> MemoryVersionOut:
    with _memory_errors():
        changes = MemoryChanges(
            title=body.title, content=body.content, reason=body.reason
        )
        written = await _versioning(request).edit_memory(
            principal, memory_id, body.expected_version, changes
        )
    return _version(await _named(request, written))


@router.post(
    "/memories/{memory_id}/restore",
    response_model=MemoryVersionOut,
    summary="A new active version with the content of an older version",
)
async def restore_memory(
    request: Request, principal: _READER, memory_id: uuid.UUID, body: RestoreRequest
) -> MemoryVersionOut:
    with _memory_errors():
        written = await _versioning(request).restore_version(
            principal,
            memory_id,
            body.expected_version,
            body.source_version,
            reason=body.reason,
        )
    return _version(await _named(request, written))
