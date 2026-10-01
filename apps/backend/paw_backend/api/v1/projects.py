"""Projects, their repositories and members over HTTP (issue #184, Decision 0066,
Proposed).

``/api/v1/projects/*`` and ``/api/v1/admin/projects``: the routes the Web App's
プロジェクト screen (PAW-061, ``apps/web/src/projects``) reads and changes
projects with. The behaviour is in ``paw_backend.projects`` (``ProjectService``)
and ``paw_backend.repositories`` (``RepositoryService``); this module only
translates, and composes the screen's list and detail out of their reads.

Authorization (Decision 0066, 1 and 2): every route is guarded by
``require_capability`` with the capability the service itself checks, on the
project resource built from the **stored** project (``_project_of``: the id of the
URL, the state of the row); the service then authorizes again on the state it
reads in its own transaction (the services never rely on the HTTP path, Decision
0058, 3). A user who is not a member, a project that does not exist and one that
is Deleted are therefore the same 403 ``forbidden`` (nothing about the project is
disclosed); the service's own "not found" (404) is left for a change between the
two decisions. The list of one's own projects is a read of the user's own
memberships (no capability in ``ProjectService``): its guard is ``account.read``,
which every human role holds, and an Agent never.

* ``GET /projects``: the user's projects in the three statuses (Active, Archived,
  and the Pending deletion projects the user manages), each with the user's role
  and the names of the repositories the user may read;
* ``POST /projects``: create (``project.create``; the creator is the first Manager);
* ``GET /projects/{id}``: the project with its repositories (ACL override
  included; a repository the override closes for the user is left out) and its
  members with their login names (open invitations too, for a Manager);
* ``GET /projects/{id}/repositories``, ``GET /projects/{id}/members``: the same
  two lists on their own;
* ``POST /projects/{id}/archive`` / ``unarchive`` / ``begin-deletion`` (the
  project's exact name in ``confirm_name``) / ``restore``
  (``project.lifecycle.manage``);
* ``PUT /projects/{id}/members/{user_id}/role`` (``project.members.manage``);
* ``POST /projects/{id}/repositories``: register a repository in one of the four
  ways (``project.repo.add``). It runs in the request: a clone takes as long as
  git does (bounded by the repository policy's clone timeout);
* ``GET /admin/projects``: every project that is not Deleted, for an Owner / Admin
  (``admin.projects.manage``, keyset pages; issue #84).

``503 projects_unavailable`` when the deployment has no database. Every error body
is fixed (the services' messages are built from closed vocabularies only).
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr
from starlette import status
from starlette.requests import HTTPConnection

from paw_backend.authz import (
    Capability,
    Principal,
    ProjectState,
    Reason,
    Resource,
    require_capability,
)
from paw_backend.authz.capabilities import RepoPermission
from paw_backend.authz.roles import ProjectRole
from paw_backend.db import Database
from paw_backend.errors import ApiError
from paw_backend.projects import (
    AccountNotActiveError,
    ConfirmationMismatchError,
    DeletionWindowClosedError,
    IllegalTransitionError,
    InvalidProjectInputError,
    LastManagerError,
    MemberNotFoundError,
    NamedMember,
    NoManagerError,
    Project,
    ProjectBusyError,
    ProjectError,
    ProjectNotFoundError,
    ProjectPermissionDeniedError,
    ProjectService,
    ProjectStateError,
    ProjectStatus,
)
from paw_backend.projects import store as project_store
from paw_backend.projects.limits import (
    MAX_DESCRIPTION_CHARS,
    MAX_LIST_LIMIT,
    MAX_NAME_CHARS,
    RAW_TEXT_FACTOR,
)
from paw_backend.projects.transaction import transaction
from paw_backend.repositories import RepositoryService
from paw_backend.repositories.errors import (
    CheckoutExistsError,
    CheckoutGoneError,
    CheckoutInProgressError,
    GhCommandError,
    GitCommandError,
    GitHubUnavailableError,
    InvalidRepositoryInputError,
    LinuxAccountUnavailableError,
    PathRejectedError,
    ProjectNotActiveError,
    ProjectUnavailableError,
    RemoteAlreadyRegisteredError,
    RemoteError,
    RepositoryBusyError,
    RepositoryError,
    RepositoryLimitError,
    RepositoryNameTakenError,
    RepositoryNotFoundError,
    RepositoryPermissionDeniedError,
)
from paw_backend.repositories.limits import (
    MAX_BRANCH_CHARS,
    MAX_PATH_CHARS,
    MAX_REMOTE_URL_CHARS,
    MAX_REPOSITORIES_PER_PROJECT,
)
from paw_backend.repositories.limits import MAX_NAME_CHARS as MAX_REPOSITORY_NAME
from paw_backend.repositories.records import Repository

router = APIRouter(tags=["projects"])

# The statuses of the user's own list, in the order of the screen (Decision 0066, 3).
_OWN_STATUSES = (
    ProjectStatus.ACTIVE,
    ProjectStatus.ARCHIVED,
    ProjectStatus.PENDING_DELETION,
)
# At most this many projects of one status are listed (``truncated`` says more
# exist); five pages of the service's largest page.
OWN_LIST_MAX_PER_STATUS = 5 * MAX_LIST_LIMIT
_AUTHZ_STATE = {
    ProjectStatus.ACTIVE: ProjectState.ACTIVE,
    ProjectStatus.ARCHIVED: ProjectState.ARCHIVED,
    ProjectStatus.PENDING_DELETION: ProjectState.PENDING_DELETION,
}
# The order an ACL override's permissions are listed in.
_PERMISSION_ORDER = tuple(RepoPermission)
# The project's lock wait of the guard's read (it takes no lock: a bound anyway).
_GUARD_LOCK_TIMEOUT_MS = 3000
# A raw text may be this long before the service's own validation (it counts
# characters after normalising, and refuses longer raw input itself).
_NAME_INPUT_MAX = MAX_NAME_CHARS * RAW_TEXT_FACTOR
_DESCRIPTION_INPUT_MAX = MAX_DESCRIPTION_CHARS * RAW_TEXT_FACTOR


# -- the services and the guard --------------------------------------------------------


class _Unavailable(ValueError):
    """The resolver's own "no project": the guard denies (and audits) it, and
    logs it as a client's mistake (like a malformed id), not as a bug."""


def _projects(request: Request) -> ProjectService:
    service: ProjectService | None = getattr(request.app.state, "projects", None)
    if service is None:
        raise ApiError(503, "projects_unavailable", "Projects are not available")
    return service


def _repositories(request: Request) -> RepositoryService:
    service: RepositoryService | None = getattr(request.app.state, "repositories", None)
    if service is None:
        raise ApiError(503, "projects_unavailable", "Projects are not available")
    return service


async def _project_of(connection: HTTPConnection) -> Resource:
    """The project of the URL as a ``Resource``, from the stored row.

    A malformed id is a ``ValueError`` and a missing or Deleted project
    ``_Unavailable``: ``require_capability`` turns both into an audited denial
    (403), the same answer as for a project the user is not a member of.
    """
    project_id = uuid.UUID(str(connection.path_params["project_id"]))
    database: Database = connection.app.state.database
    async with transaction(database, _GUARD_LOCK_TIMEOUT_MS) as session:
        project = await project_store.get_project(session, project_id)
    if project is None or project.status not in _AUTHZ_STATE:
        raise _Unavailable()
    return Resource.project(project.id, _AUTHZ_STATE[project.status])


def _on_project(capability: Capability):
    return Depends(require_capability(capability, _project_of))


_READER = Annotated[Principal, _on_project(Capability.PROJECT_READ)]
_LIFECYCLE = Annotated[Principal, _on_project(Capability.PROJECT_LIFECYCLE_MANAGE)]
_MEMBERS = Annotated[Principal, _on_project(Capability.PROJECT_MEMBERS_MANAGE)]
_REPO_ADD = Annotated[Principal, _on_project(Capability.PROJECT_REPO_ADD)]
_SELF = Annotated[Principal, Depends(require_capability(Capability.ACCOUNT_READ))]
_CREATOR = Annotated[Principal, Depends(require_capability(Capability.PROJECT_CREATE))]
_ADMIN = Annotated[
    Principal, Depends(require_capability(Capability.ADMIN_PROJECTS_MANAGE))
]


# -- errors ----------------------------------------------------------------------------


def _denied(reason: Reason) -> ApiError:
    if reason is Reason.AUDIT_UNAVAILABLE:
        return ApiError(503, "service_unavailable", "Service temporarily unavailable")
    return ApiError(403, "forbidden", "Permission denied")


# error type -> (HTTP status, code); the message is the error's own fixed text.
_PROJECT_ERRORS: tuple[tuple[type[ProjectError], int, str], ...] = (
    (InvalidProjectInputError, 422, "validation_error"),
    (ProjectNotFoundError, 404, "not_found"),
    (MemberNotFoundError, 404, "not_found"),
    (ProjectStateError, 409, "project_state"),
    (IllegalTransitionError, 409, "project_state"),
    (ConfirmationMismatchError, 422, "confirmation_mismatch"),
    (DeletionWindowClosedError, 409, "deletion_window_closed"),
    (NoManagerError, 409, "no_manager"),
    (LastManagerError, 409, "last_manager"),
    (AccountNotActiveError, 409, "account_not_active"),
    (ProjectBusyError, 503, "project_busy"),
)
_REPOSITORY_ERRORS: tuple[tuple[type[RepositoryError], int, str], ...] = (
    (InvalidRepositoryInputError, 422, "validation_error"),
    (ProjectUnavailableError, 404, "not_found"),
    (RepositoryNotFoundError, 404, "not_found"),
    (ProjectNotActiveError, 409, "project_state"),
    (RepositoryNameTakenError, 409, "repository_name_taken"),
    (RepositoryLimitError, 409, "repository_limit"),
    (RemoteAlreadyRegisteredError, 409, "remote_already_registered"),
    (RemoteError, 422, "remote_rejected"),
    (CheckoutExistsError, 409, "checkout_exists"),
    (CheckoutInProgressError, 409, "checkout_in_progress"),
    (CheckoutGoneError, 409, "checkout_gone"),
    (PathRejectedError, 422, "path_rejected"),
    (LinuxAccountUnavailableError, 409, "linux_account_unavailable"),
    (GitCommandError, 502, "git_failed"),
    (GhCommandError, 502, "gh_command_failed"),
    (GitHubUnavailableError, 503, "github_unavailable"),
    (RepositoryBusyError, 503, "repository_busy"),
)


@contextmanager
def _errors() -> Iterator[None]:
    """The services' typed errors as the API's fixed error responses."""
    try:
        yield
    except (ProjectPermissionDeniedError, RepositoryPermissionDeniedError) as error:
        raise _denied(error.reason) from None
    except ProjectError as error:
        for kind, http_status, code in _PROJECT_ERRORS:
            if isinstance(error, kind):
                raise ApiError(http_status, code, str(error)) from None
        raise ApiError(409, "conflict", "The request conflicts") from None
    except RepositoryError as error:
        for kind, http_status, code in _REPOSITORY_ERRORS:
            if isinstance(error, kind):
                raise ApiError(http_status, code, str(error)) from None
        raise ApiError(409, "conflict", "The request conflicts") from None


# -- request and response bodies -------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


Role = Literal["manager", "contributor", "viewer"]
Status = Literal["active", "archived", "pending_deletion"]


class ProjectSummaryOut(BaseModel):
    id: uuid.UUID
    name: str
    status: Status
    # The signed-in user's role (``null`` only for an invitation, never listed).
    my_role: Role | None
    # The repositories the user may read, by name (none while Pending deletion).
    repository_names: list[str]
    deletion_scheduled_at: datetime | None


class ProjectListResponse(BaseModel):
    projects: list[ProjectSummaryOut]
    # More than ``OWN_LIST_MAX_PER_STATUS`` projects of one status exist.
    truncated: bool


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: Status
    created_at: datetime
    updated_at: datetime
    deletion_scheduled_at: datetime | None


class RepositoryOut(BaseModel):
    id: uuid.UUID
    name: str
    default_branch: str
    source: Literal["github_clone", "existing_path", "new_local", "new_github"]
    # ``null``: the project role applies (inherit); otherwise the override (empty =
    # no access), which narrows the role for every member alike.
    acl: list[Literal["read", "write", "agent"]] | None
    updated_at: datetime


class MemberOut(BaseModel):
    user_id: uuid.UUID
    login_name: str
    role: Role
    status: Literal["active", "invited"]
    creator: bool
    invite_expires_at: datetime | None


class ProjectDetailOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    status: Status
    my_role: Role | None
    created_at: datetime
    deletion_scheduled_at: datetime | None
    repositories: list[RepositoryOut]
    members: list[MemberOut]


class RepositoryListResponse(BaseModel):
    repositories: list[RepositoryOut]


class MemberListResponse(BaseModel):
    members: list[MemberOut]


class CreateProjectRequest(_Body):
    name: StrictStr = Field(min_length=1, max_length=_NAME_INPUT_MAX)
    description: StrictStr | None = Field(
        default=None, max_length=_DESCRIPTION_INPUT_MAX
    )


class BeginDeletionRequest(_Body):
    # The project's exact name, typed by the person (Decision 0008).
    confirm_name: StrictStr = Field(min_length=1, max_length=_NAME_INPUT_MAX)


class ChangeRoleRequest(_Body):
    role: Role


class MemberRoleResponse(BaseModel):
    user_id: uuid.UUID
    role: Role


class _Registration(_Body):
    name: StrictStr | None = Field(default=None, max_length=MAX_REPOSITORY_NAME * 10)


class ExistingPathRegistration(_Registration):
    source: Literal["existing_path"]
    path: StrictStr = Field(min_length=1, max_length=MAX_PATH_CHARS * 10)


class GitHubCloneRegistration(_Registration):
    source: Literal["github_clone"]
    # ``owner/repo`` or an ``https`` URL on an allowed host.
    url: StrictStr = Field(min_length=1, max_length=MAX_REMOTE_URL_CHARS * 10)
    branch: StrictStr | None = Field(default=None, max_length=MAX_BRANCH_CHARS * 10)


class NewLocalRegistration(_Body):
    source: Literal["new_local"]
    name: StrictStr = Field(min_length=1, max_length=MAX_REPOSITORY_NAME * 10)
    default_branch: StrictStr | None = Field(
        default=None, max_length=MAX_BRANCH_CHARS * 10
    )


class NewGitHubRegistration(_Body):
    source: Literal["new_github"]
    name: StrictStr = Field(min_length=1, max_length=MAX_REPOSITORY_NAME * 10)
    private: StrictBool = True
    default_branch: StrictStr | None = Field(
        default=None, max_length=MAX_BRANCH_CHARS * 10
    )


RegistrationRequest = Annotated[
    ExistingPathRegistration
    | GitHubCloneRegistration
    | NewLocalRegistration
    | NewGitHubRegistration,
    Field(discriminator="source"),
]


class AdminProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    status: Status
    created_at: datetime
    deletion_scheduled_at: datetime | None


class AdminProjectPageResponse(BaseModel):
    projects: list[AdminProjectOut]
    # Pass back as ``cursor`` (with the same ``status``) for the next page.
    next_cursor: str | None


# -- composing the answers -------------------------------------------------------------


def _role_of(principal: Principal, project_id: uuid.UUID) -> ProjectRole | None:
    """The user's role in the project, as the session read it from storage (for
    display only: every decision is the services')."""
    return principal.project_roles.get(project_id)


def _project_out(project: Project) -> ProjectOut:
    return ProjectOut(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status.value,
        created_at=project.created_at,
        updated_at=project.updated_at,
        deletion_scheduled_at=project.deletion_scheduled_at,
    )


def _repository_out(repository: Repository) -> RepositoryOut:
    allowed = repository.acl.allowed
    return RepositoryOut(
        id=repository.id,
        name=repository.name,
        default_branch=repository.default_branch,
        source=repository.source.value,
        acl=None
        if allowed is None
        else [p.value for p in _PERMISSION_ORDER if p in allowed],
        updated_at=repository.updated_at,
    )


def _member_out(named: NamedMember, creator: uuid.UUID | None) -> MemberOut:
    member = named.member
    return MemberOut(
        user_id=member.user_id,
        login_name=named.login_name,
        role=member.role.value,
        status=member.status.value,
        creator=creator is not None and member.user_id == creator,
        invite_expires_at=member.invite_expires_at,
    )


async def _members(
    request: Request,
    principal: Principal,
    project_id: uuid.UUID,
    creator: uuid.UUID | None,
) -> list[MemberOut]:
    """The accepted members, then (for a Manager) the open invitations."""
    service = _projects(request)
    members = await service.list_members_named(principal, project_id)
    found = [_member_out(m, creator) for m in members]
    if _role_of(principal, project_id) is ProjectRole.MANAGER:
        # Only a Manager may see invitations (``project.members.manage``); a
        # Manager who was demoted meanwhile simply sees none.
        try:
            invites = await service.list_invites_named(principal, project_id)
        except ProjectPermissionDeniedError as error:
            if error.reason is Reason.AUDIT_UNAVAILABLE:
                raise
            invites = ()
        except (ProjectNotFoundError, ProjectStateError):
            invites = ()
        found.extend(_member_out(m, creator) for m in invites)
    return found


async def _readable_repositories(
    request: Request, principal: Principal, project_id: uuid.UUID
) -> list[Repository]:
    return list(
        await _repositories(request).list_repositories(
            principal, project_id, limit=MAX_REPOSITORIES_PER_PROJECT
        )
    )


# -- the user's own projects -----------------------------------------------------------


@router.get(
    "/projects",
    response_model=ProjectListResponse,
    summary="The signed-in user's projects (Active, Archived, Pending deletion)",
)
async def list_projects(request: Request, principal: _SELF) -> ProjectListResponse:
    projects = _projects(request)
    found: list[ProjectSummaryOut] = []
    truncated = False
    with _errors():
        for project_status in _OWN_STATUSES:
            listed: list[Project] = []
            while len(listed) < OWN_LIST_MAX_PER_STATUS:
                page = await projects.list_projects(
                    principal,
                    status=project_status,
                    limit=MAX_LIST_LIMIT,
                    offset=len(listed),
                )
                listed.extend(page)
                if len(page) < MAX_LIST_LIMIT:
                    break
            else:
                more = await projects.list_projects(
                    principal, status=project_status, limit=1, offset=len(listed)
                )
                truncated = truncated or bool(more)
            for project in listed:
                names: list[str] = []
                if project_status is not ProjectStatus.PENDING_DELETION:
                    try:
                        repositories = await _readable_repositories(
                            request, principal, project.id
                        )
                    except (ProjectUnavailableError, ProjectNotActiveError):
                        repositories = []  # changed since it was listed
                    except RepositoryPermissionDeniedError as error:
                        if error.reason is Reason.AUDIT_UNAVAILABLE:
                            raise
                        repositories = []
                    names = [r.name for r in repositories]
                found.append(
                    ProjectSummaryOut(
                        id=project.id,
                        name=project.name,
                        status=project.status.value,
                        my_role=_role_of(principal, project.id),
                        repository_names=names,
                        deletion_scheduled_at=project.deletion_scheduled_at,
                    )
                )
    return ProjectListResponse(projects=found, truncated=truncated)


@router.post(
    "/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=ProjectOut,
    summary="Create a project (without a repository); the creator manages it",
)
async def create_project(
    body: CreateProjectRequest, request: Request, principal: _CREATOR
) -> ProjectOut:
    with _errors():
        project = await _projects(request).create_project(
            principal, body.name, body.description
        )
    return _project_out(project)


# -- one project -----------------------------------------------------------------------


@router.get(
    "/projects/{project_id}",
    response_model=ProjectDetailOut,
    summary="A project with its repositories and members",
)
async def project_detail(
    project_id: uuid.UUID, request: Request, principal: _READER
) -> ProjectDetailOut:
    with _errors():
        project = await _projects(request).get_project(principal, project_id)
        repositories = await _readable_repositories(request, principal, project_id)
        members = await _members(request, principal, project_id, project.created_by)
    return ProjectDetailOut(
        id=project.id,
        name=project.name,
        description=project.description,
        status=project.status.value,
        my_role=_role_of(principal, project.id),
        created_at=project.created_at,
        deletion_scheduled_at=project.deletion_scheduled_at,
        repositories=[_repository_out(r) for r in repositories],
        members=members,
    )


@router.get(
    "/projects/{project_id}/repositories",
    response_model=RepositoryListResponse,
    summary="The project's repositories the user may read, with their ACL",
)
async def list_repositories(
    project_id: uuid.UUID, request: Request, principal: _READER
) -> RepositoryListResponse:
    with _errors():
        repositories = await _readable_repositories(request, principal, project_id)
    return RepositoryListResponse(
        repositories=[_repository_out(r) for r in repositories]
    )


@router.get(
    "/projects/{project_id}/members",
    response_model=MemberListResponse,
    summary="The members (and, for a Manager, the open invitations)",
)
async def list_members(
    project_id: uuid.UUID, request: Request, principal: _READER
) -> MemberListResponse:
    with _errors():
        project = await _projects(request).get_project(principal, project_id)
        members = await _members(request, principal, project_id, project.created_by)
    return MemberListResponse(members=members)


# -- lifecycle -------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/archive",
    response_model=ProjectOut,
    summary="Archive the project (read-only); an Archived project is unchanged",
)
async def archive(
    project_id: uuid.UUID, request: Request, principal: _LIFECYCLE
) -> ProjectOut:
    with _errors():
        project = await _projects(request).archive(principal, project_id)
    return _project_out(project)


@router.post(
    "/projects/{project_id}/unarchive",
    response_model=ProjectOut,
    summary="Make an Archived project Active again",
)
async def unarchive(
    project_id: uuid.UUID, request: Request, principal: _LIFECYCLE
) -> ProjectOut:
    with _errors():
        project = await _projects(request).unarchive(principal, project_id)
    return _project_out(project)


@router.post(
    "/projects/{project_id}/begin-deletion",
    response_model=ProjectOut,
    summary=(
        "Start the deletion (Pending deletion for 30 days, restorable); needs the "
        "project's exact name"
    ),
)
async def begin_deletion(
    project_id: uuid.UUID,
    body: BeginDeletionRequest,
    request: Request,
    principal: _LIFECYCLE,
) -> ProjectOut:
    with _errors():
        project = await _projects(request).begin_deletion(
            principal, project_id, body.confirm_name
        )
    return _project_out(project)


@router.post(
    "/projects/{project_id}/restore",
    response_model=ProjectOut,
    summary="Restore a project Pending deletion (it comes back Archived)",
)
async def restore(
    project_id: uuid.UUID, request: Request, principal: _LIFECYCLE
) -> ProjectOut:
    with _errors():
        project = await _projects(request).restore(principal, project_id)
    return _project_out(project)


# -- members ---------------------------------------------------------------------------


@router.put(
    "/projects/{project_id}/members/{user_id}/role",
    response_model=MemberRoleResponse,
    summary="Change the role of an accepted member (the last Manager stays)",
)
async def change_role(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    body: ChangeRoleRequest,
    request: Request,
    principal: _MEMBERS,
) -> MemberRoleResponse:
    with _errors():
        member = await _projects(request).change_role(
            principal, project_id, user_id, ProjectRole(body.role)
        )
    return MemberRoleResponse(user_id=member.user_id, role=member.role.value)


# -- repositories ----------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/repositories",
    status_code=status.HTTP_201_CREATED,
    response_model=RepositoryOut,
    summary=(
        "Register a repository: clone from GitHub, an existing directory, a new "
        "local one or a new GitHub one (runs git in the request)"
    ),
)
async def register_repository(
    project_id: uuid.UUID,
    body: RegistrationRequest,
    request: Request,
    principal: _REPO_ADD,
) -> RepositoryOut:
    service = _repositories(request)
    with _errors():
        match body:
            case ExistingPathRegistration():
                registered = await service.register_existing(
                    principal, project_id, body.path, name=body.name
                )
            case GitHubCloneRegistration():
                registered = await service.clone_from_github(
                    principal, project_id, body.url, name=body.name, branch=body.branch
                )
            case NewLocalRegistration():
                options = {}
                if body.default_branch is not None:
                    options["default_branch"] = body.default_branch
                registered = await service.create_local(
                    principal, project_id, body.name, **options
                )
            case NewGitHubRegistration():
                options = {}
                if body.default_branch is not None:
                    options["default_branch"] = body.default_branch
                registered = await service.create_github(
                    principal, project_id, body.name, private=body.private, **options
                )
    return _repository_out(registered.repository)


# -- the administrator's list ----------------------------------------------------------


@router.get(
    "/admin/projects",
    response_model=AdminProjectPageResponse,
    summary="Every project that is not Deleted (Owner / Admin; keyset pages)",
)
async def admin_projects(
    request: Request,
    principal: _ADMIN,
    project_status: Annotated[Status | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
) -> AdminProjectPageResponse:
    with _errors():
        page = await _projects(request).list_all_projects(
            principal, status=project_status, limit=limit, cursor=cursor
        )
    return AdminProjectPageResponse(
        projects=[
            AdminProjectOut(
                id=p.id,
                name=p.name,
                status=p.status.value,
                created_at=p.created_at,
                deletion_scheduled_at=p.deletion_scheduled_at,
            )
            for p in page.projects
        ],
        next_cursor=page.next_cursor,
    )
