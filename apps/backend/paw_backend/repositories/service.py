"""The repository service: registration and per-user checkouts (PAW-027).

What it does
------------
A **repository** belongs to a project (a logical, shared record). What a user or an
agent edits is a **checkout**: that user's own working copy in their own Linux
account (``REQUIREMENTS.md``, "Project / Repository registration and per-user
checkout"). Three ways to add a repository (all need ``project.repo.add``: a
Manager):

* ``clone_from_github``: ``git clone`` of a repository on an allowed host into the
  actor's workspace, then registered;
* ``register_existing``: an existing Git repository in the actor's own Linux
  account, verified (``paths.check_existing_repository`` and ``git rev-parse``)
  and registered as it is;
* ``create_local`` / ``create_github``: a new empty repository (``git init``),
  local only, or also on GitHub through the :class:`GitHubGateway` seam (PAW-028:
  the default gateway refuses).

Every other member with ``project.read`` (and the repository's ``read`` ACL) makes
their own checkout with ``create_checkout`` (a clone from a registered remote).
**No file is ever written into a repository that was not written by git itself**:
nothing is added, committed or configured in the working tree of a project
repository (``AGENTS.md``, ``MEMORY.md``, ``.personal-ai/`` and the like belong to
the workspace's own storage, ``REQUIREMENTS.md``).

Who can call it
---------------
The service is not exposed over HTTP by this issue (sessions come with PAW-022).
Every method that changes or reads a repository takes the acting
:class:`~paw_backend.authz.Principal` and asks the
:class:`~paw_backend.authz.Authorizer` (default deny, audit by the Authorizer) on
the **stored** state of the project. The ``project_roles`` of the Principal the
caller passes are **ignored**: the role of the actor in the project is read from
``project_members`` in the same transaction. Only ``Principal.user_id`` and
``Principal.system_role`` are taken from the caller.

Authorization and audit (Decision 0004 and Decision 0017, both Approved):

* ``clone_from_github``, ``register_existing``, ``create_local``,
  ``create_github``, ``remove_repository``, ``add_remote``, ``remove_remote``:
  ``project.repo.add`` (a Manager; audit ``REQUIRED``, so an allowed action is
  refused when the audit write fails);
* ``set_acl``: ``project.settings.manage`` (a Manager; audit ``REQUIRED``);
* ``create_checkout``: ``project.read`` on the **repository** (its ACL override can
  hide it) and ``workspace.use`` on the actor's own checkout (audit ``REQUIRED``);
* ``remove_checkout``: ``workspace.use`` on the actor's own checkout;
* ``get_repository``, ``list_repositories``: ``project.read``;
* ``list_my_checkouts``: the actor's own rows, no capability (self service);
* ``scope_entries``, ``purge_projects``: backend-internal, not for users.

The audit records the decision, not the outcome: an allowed action can still fail
afterwards (git, the file system, a rule) and the event stays. The Authorizer
writes the events; this module writes none.

The project must be **Active** to add or remove a repository, a remote or an ACL,
and to create a checkout (``ProjectNotActiveError``); an Archived project is
read-only. ``remove_checkout`` (unregistering one's own directory) works in every
state.

Errors that do not disclose existence: after the Authorizer denied an action the
service raises :class:`ProjectUnavailableError` (exactly as for a missing project)
when the actor is not an accepted member; a member whose repository ACL forbids
``read`` gets :class:`RepositoryNotFoundError`.

Order of checks (every method)
------------------------------
1. the actor (``RepositoryPermissionDeniedError(UNAUTHENTICATED)``); the internal
   ``system`` identity cannot use the methods that write;
2. the arguments, in signature order, all before the database or the file system is
   touched (``InvalidRepositoryInputError``);
3. the Linux account of the actor (``LinuxAccountUnavailableError``);
4. for ``register_existing``: the file-system and git checks of the path (they read
   the actor's own directory as the actor's own user and disclose nothing about the
   project);
5. one transaction that locks the project row, loads the project, reads the actor's
   membership, authorizes, and applies the rules of the method.

Long work (a clone) never holds a transaction: a **pending** checkout row reserves
the name and the path first (committed), git runs, and a second transaction marks it
``ready``. A failure or a cancellation of the call's own work removes the reservation
and the directory the call created, but **only while its own pending row still
exists** (``_abandon``): when ``remove_checkout`` / ``remove_repository`` unregistered
it meanwhile, the call ends with ``CheckoutGoneError`` and never deletes the directory
(unregistering promises not to touch files, and the user may have added work since).
A pending row whose process died is *stale* after twice the clone
timeout; the next ``create_checkout`` of the same user replaces it (a directory the
dead attempt left is never deleted: the owner removes it).

Concurrency: all changes of one project's registry are serialised by the row lock on
the project (``FOR UPDATE``), like ``ProjectService``; a checkout is made under
``FOR SHARE`` (the project cannot be archived under it). The database has the final
word: names are unique per project without case, a URL belongs to one repository of a
project, a user has one checkout per repository and a directory belongs to one
checkout.

Deletion never touches files or GitHub (``REQUIREMENTS.md``): ``remove_repository``,
``remove_checkout`` and ``purge_projects`` remove registrations only.

Errors and logging: every error is a :class:`RepositoryError` with a fixed message
that contains no caller content, no path and nothing git printed. Database errors the
service does not handle propagate unchanged (their text can contain SQL parameters:
never show ``str(error)``). The service logs only the type of an unexpected error.
"""

import asyncio
import logging
import os
import pwd
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from types import MappingProxyType

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from paw_backend.authz import Authorizer, Capability, Principal, ProjectState, Resource
from paw_backend.authz.capabilities import REPO_PERMISSION_OF
from paw_backend.authz.policy import Reason
from paw_backend.authz.roles import SystemRole
from paw_backend.db import Database
from paw_backend.projects import store as projects_store
from paw_backend.projects.records import MemberStatus, Project, ProjectStatus
from paw_backend.repositories import store
from paw_backend.repositories.accounts import (
    AccountDirectory,
    LoginNameAccountDirectory,
)
from paw_backend.repositories.errors import (
    CheckoutChangedError,
    CheckoutExistsError,
    CheckoutGoneError,
    CheckoutInProgressError,
    CheckoutNotFoundError,
    GitHubUnavailableError,
    InvalidRepositoryInputError,
    NoCloneSourceError,
    PathProblem,
    PathRejectedError,
    ProjectNotActiveError,
    ProjectUnavailableError,
    RemoteAlreadyRegisteredError,
    RemoteError,
    RemoteNotFoundError,
    RemoteProblem,
    RepositoryLimitError,
    RepositoryNameTakenError,
    RepositoryNotFoundError,
    RepositoryPermissionDeniedError,
    TooManyCheckoutsError,
)
from paw_backend.repositories.git import GitClient, GitRunner
from paw_backend.repositories.github import (
    GitHubGateway,
    GitHubRepo,
    UnavailableGitHubGateway,
    check_created_repository,
    parse_github_source,
    remote_urls_from_origin,
)
from paw_backend.repositories.limits import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_LOCK_TIMEOUT_MS,
    MAX_IDENTITY_SCAN_DIRECTORIES,
    MAX_LOCK_TIMEOUT_MS,
    MAX_REMOTES_PER_REPOSITORY,
    MAX_REPOSITORIES_PER_PROJECT,
    MAX_SCOPE_CHECKOUTS,
    MIN_LOCK_TIMEOUT_MS,
)
from paw_backend.repositories.paths import (
    LinuxAccount,
    check_existing_repository,
    create_checkout_directory,
    inspect_checkout_root,
    locate_directory_identity,
    plan_checkout_path,
    project_directory_name,
    read_checkout_identity,
    remove_directory,
)
from paw_backend.repositories.policy import RepositoryPolicy
from paw_backend.repositories.records import (
    Checkout,
    CheckoutState,
    Registered,
    Remote,
    Repository,
    RepositoryDetail,
    RepositorySource,
)
from paw_backend.repositories.transaction import transaction
from paw_backend.repositories.validation import (
    validate_bool,
    validate_branch,
    validate_limit,
    validate_name,
    validate_offset,
    validate_path_text,
    validate_permissions,
    validate_project_ids,
    validate_remote_url,
    validate_uuid,
)
from paw_backend.tools.scope import ScopedRepository, path_within

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

_HUMAN_ROLES = (SystemRole.OWNER, SystemRole.ADMIN, SystemRole.USER)
_AUTHZ_STATE = MappingProxyType(
    {
        ProjectStatus.ACTIVE: ProjectState.ACTIVE,
        ProjectStatus.ARCHIVED: ProjectState.ARCHIVED,
        ProjectStatus.PENDING_DELETION: ProjectState.PENDING_DELETION,
    }
)
_READ = REPO_PERMISSION_OF[Capability.PROJECT_READ]
# A branch a new repository starts on until git says otherwise.
_DEFAULT_BRANCH = "main"

# constraint (or unique index) name -> the error a violation means
_VIOLATIONS: MappingProxyType[str, Callable[[], Exception]] = MappingProxyType(
    {
        "uq_repositories_project_id_lower_name": RepositoryNameTakenError,
        "uq_repository_remotes_project_id": RemoteAlreadyRegisteredError,
        "pk_repository_remotes": RemoteAlreadyRegisteredError,
        "uq_repository_checkouts_path": CheckoutExistsError,
        "uq_repository_checkouts_repository_id": CheckoutExistsError,
        # Unreachable through the service (the path is validated first); a defence.
        "ck_repository_checkouts_path_valid": lambda: PathRejectedError(
            PathProblem.NOT_CANONICAL
        ),
        # The repository was removed while an operation of its own was running.
        "fk_repository_remotes_repository_id_repositories": CheckoutGoneError,
        "fk_repository_checkouts_repository_id_repositories": CheckoutGoneError,
    }
)


class _Lock(Enum):
    NONE = "none"
    SHARE = "share"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class _Populated:
    """What phase 2 of a new checkout learned: branch, head and extra remote URLs."""

    default_branch: str | None
    head: str | None
    remotes: tuple[str, ...] = ()


def _violation(error: IntegrityError) -> Exception:
    """The typed error of a unique violation, else the original error."""
    name = getattr(getattr(error.orig, "diag", None), "constraint_name", None)
    make = _VIOLATIONS.get(name) if isinstance(name, str) else None
    return error if make is None else make()


class RepositoryService:
    """Repositories of projects and the users' own checkouts (module docstring)."""

    def __init__(
        self,
        database: Database,
        authorizer: Authorizer,
        accounts: AccountDirectory,
        git: GitClient,
        *,
        policy: RepositoryPolicy | None = None,
        github: GitHubGateway | None = None,
        clock: Clock | None = None,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> None:
        """``TypeError`` for a wrong type, ``ValueError`` for a bad lock timeout."""
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        if not isinstance(authorizer, Authorizer):
            raise TypeError("authorizer must be an Authorizer")
        if not hasattr(accounts, "account_of"):
            raise TypeError("accounts must be an AccountDirectory")
        if not isinstance(git, GitClient):
            raise TypeError("git must be a GitClient")
        if policy is not None and not isinstance(policy, RepositoryPolicy):
            raise TypeError("policy must be a RepositoryPolicy")
        if (
            isinstance(accounts, LoginNameAccountDirectory)
            and accounts.min_uid != (policy or RepositoryPolicy()).min_uid
        ):
            # One value for "the lowest uid of a person": the policy's.
            raise ValueError("the account directory and the policy disagree on min_uid")
        if github is not None and not hasattr(github, "create_repository"):
            raise TypeError("github must be a GitHubGateway")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(lock_timeout_ms, bool) or not isinstance(lock_timeout_ms, int):
            raise TypeError("lock_timeout_ms must be an int")
        if not MIN_LOCK_TIMEOUT_MS <= lock_timeout_ms <= MAX_LOCK_TIMEOUT_MS:
            raise ValueError("lock_timeout_ms is out of range")
        self._database = database
        self._authorizer = authorizer
        self._accounts = accounts
        self._git = git
        self._policy = policy or RepositoryPolicy()
        self._github = github or UnavailableGitHubGateway()
        self._clock = clock or _utc_now
        self._lock_timeout_ms = lock_timeout_ms

    @classmethod
    def from_policy(
        cls,
        database: Database,
        authorizer: Authorizer,
        runner: GitRunner,
        policy: RepositoryPolicy,
        *,
        accounts: AccountDirectory | None = None,
        account_lookup: Callable[[str], pwd.struct_passwd] | None = None,
        github: GitHubGateway | None = None,
        clock: Clock | None = None,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    ) -> "RepositoryService":
        """The production wiring: **one** ``RepositoryPolicy`` for every part.

        ``RepositoryPolicy.from_settings(settings)`` is the usual ``policy``. The git
        client and (unless ``accounts`` is given) the ``LoginNameAccountDirectory`` are
        built from it, so ``PAW_REPOSITORY_MIN_LINUX_UID`` is applied when a Linux
        account is looked up, whatever else is configured. ``account_lookup``
        replaces ``pwd.getpwnam`` (tests only); ``runner`` is the ``GitRunner`` of the
        deployment (``SubprocessGitRunner`` unless git must run as another user).
        """
        if not isinstance(policy, RepositoryPolicy):
            raise TypeError("policy must be a RepositoryPolicy")
        if accounts is None:
            options = {} if account_lookup is None else {"lookup": account_lookup}
            accounts = LoginNameAccountDirectory(database, policy=policy, **options)
        elif account_lookup is not None:
            raise TypeError("account_lookup is for the default account directory")
        return cls(
            database,
            authorizer,
            accounts,
            GitClient(runner, policy),
            policy=policy,
            github=github,
            clock=clock,
            lock_timeout_ms=lock_timeout_ms,
        )

    # -- plumbing --------------------------------------------------------------

    @staticmethod
    def _actor(actor: object) -> Principal:
        if not isinstance(actor, Principal):
            raise RepositoryPermissionDeniedError(Reason.UNAUTHENTICATED)
        return actor

    @classmethod
    def _human(cls, actor: object) -> Principal:
        principal = cls._actor(actor)
        if principal.system_role not in _HUMAN_ROLES:
            raise RepositoryPermissionDeniedError(Reason.CAPABILITY_NOT_GRANTED)
        return principal

    def _now(self) -> datetime:
        now = self._clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("the clock must return an aware datetime")
        return now

    def _transaction(self) -> AbstractAsyncContextManager[AsyncSession]:
        return transaction(self._database, self._lock_timeout_ms)

    @staticmethod
    async def _load_project(
        session: AsyncSession, project_id: uuid.UUID, lock: _Lock
    ) -> Project:
        if lock is _Lock.UPDATE:
            project = await projects_store.get_project(
                session, project_id, for_update=True
            )
        elif lock is _Lock.SHARE:
            project = await projects_store.get_project_for_share(session, project_id)
        else:
            project = await projects_store.get_project(session, project_id)
        if project is None or project.status is ProjectStatus.DELETED:
            raise ProjectUnavailableError()
        return project

    async def _guard(
        self,
        session: AsyncSession,
        actor: Principal,
        capability: Capability,
        project_id: uuid.UUID,
        *,
        lock: _Lock,
        repository_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        repo_scoped: bool = False,
        project_resource: bool = False,
        require_active: bool = True,
    ) -> tuple[Project, Repository | None]:
        """Lock and load the project, authorize, and load the repository if named.

        ``repo_scoped`` authorizes on the repository resource (its ACL applies;
        only the capabilities that are about one repository allow that);
        otherwise the resource is the project (``project_resource``) or a
        ``repository`` resource that names ``resource_id`` (the audit then says
        which repository, or which new one, the decision was about). A named
        repository that is not in the project is ``RepositoryNotFoundError`` only
        after the actor was allowed: a non-member learns nothing.
        """
        project = await self._load_project(session, project_id, lock)
        repository = (
            None
            if repository_id is None
            else await store.get_repository(session, project.id, repository_id)
        )
        member = await projects_store.get_member(session, project.id, actor.user_id)
        active = member is not None and member.status is MemberStatus.ACTIVE
        principal = Principal(
            actor.user_id,
            actor.system_role,
            {project.id: member.role} if active and member is not None else {},
        )
        state = _AUTHZ_STATE[project.status]
        if repo_scoped and repository is not None:
            resource = Resource.repository(project.id, state, repository.acl)
        elif project_resource:
            resource = Resource.project(project.id, state)
        else:
            resource = Resource(
                kind="repository",
                id=resource_id if resource_id is not None else repository_id,
                project_id=project.id,
                project_state=state,
            )
        decision = await self._authorizer.authorize(principal, capability, resource)
        if not decision.allowed:
            if decision.reason is Reason.AUDIT_UNAVAILABLE:
                raise RepositoryPermissionDeniedError(decision.reason)
            if not active:
                raise ProjectUnavailableError()
            if decision.reason is Reason.PROJECT_STATE_FORBIDS:
                raise ProjectNotActiveError(project.status)
            if decision.reason is Reason.REPO_ACL_FORBIDS:
                raise RepositoryNotFoundError()
            raise RepositoryPermissionDeniedError(decision.reason)
        if require_active and project.status is not ProjectStatus.ACTIVE:
            raise ProjectNotActiveError(project.status)
        if repository_id is not None and repository is None:
            raise RepositoryNotFoundError()
        return project, repository

    async def _require_active_project(
        self, session: AsyncSession, project_id: uuid.UUID
    ) -> Project:
        """Lock the project ``FOR SHARE`` and require it to be Active *now*.

        The first statement of every transaction that completes a pending checkout
        (``_finish``, the end of ``create_checkout``). A clone or a GitHub creation
        runs for minutes between the reservation and the completion, and repository
        changes are allowed only in an Active project, so the state is read again
        here, under a lock that an Archive or a Delete start (``FOR UPDATE``) waits
        for. Lock order as everywhere: the project first, then the repository, then
        the checkout. ``ProjectUnavailableError`` for a missing or Deleted project,
        ``ProjectNotActiveError`` otherwise; the caller's own work is then undone by
        ``_abandon`` (only what it still owns).
        """
        project = await self._load_project(session, project_id, _Lock.SHARE)
        if project.status is not ProjectStatus.ACTIVE:
            raise ProjectNotActiveError(project.status)
        return project

    async def _check_capacity(self, session: AsyncSession, project_id: uuid.UUID):
        if (
            await store.count_repositories(session, project_id)
            >= MAX_REPOSITORIES_PER_PROJECT
        ):
            raise RepositoryLimitError()

    # -- adding a repository ---------------------------------------------------

    async def register_existing(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        path: str,
        *,
        name: str | None = None,
    ) -> Registered:
        """Register an existing Git repository of the actor's own account.

        ``path`` is absolute in the canonical spelling and passes
        ``paths.check_existing_repository`` (resolved, below one of the actor's
        roots, not hidden, owned by the actor's account, a real ``.git``) and the
        comparison with what ``git rev-parse`` reports. ``name`` defaults to the
        directory's name. The repository's default branch is what ``origin/HEAD``
        points at (else the current branch), its ``origin`` URL is registered as a
        remote when it is an ``https`` URL (or a GitHub ``ssh`` one on an allowed
        host; a URL with credentials is refused), and the directory becomes the
        actor's ``ready`` checkout. Nothing is written into the repository.
        ``project.repo.add``; the project must be Active.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        path = validate_path_text(path)
        name = validate_name(os.path.basename(path) if name is None else name)
        account = await self._accounts.account_of(principal.user_id)
        await asyncio.to_thread(
            check_existing_repository,
            path,
            account,
            self._policy.existing_roots,
            self._policy.workspace_subdir,
        )
        facts = await self._git.inspect(path, account)
        remotes = remote_urls_from_origin(facts.origin_url, self._policy.clone_hosts)
        # Which directory this is, for every later scope (a replaced root differs).
        identity = await asyncio.to_thread(read_checkout_identity, path, account)
        now = self._now()
        repository_id, checkout_id = uuid.uuid4(), uuid.uuid4()
        try:
            async with self._transaction() as session:
                await self._guard(
                    session,
                    principal,
                    Capability.PROJECT_REPO_ADD,
                    project_id,
                    lock=_Lock.UPDATE,
                    resource_id=repository_id,
                )
                await self._check_capacity(session, project_id)
                repository = await store.insert_repository(
                    session,
                    repository_id=repository_id,
                    project_id=project_id,
                    name=name,
                    default_branch=facts.default_branch,
                    source=RepositorySource.EXISTING_PATH,
                    created_by=principal.user_id,
                    now=now,
                )
                for url in remotes:
                    await store.insert_remote(
                        session,
                        repository_id=repository_id,
                        project_id=project_id,
                        url=url,
                        now=now,
                    )
                checkout = await store.insert_checkout(
                    session,
                    checkout_id=checkout_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    user_id=principal.user_id,
                    path=path,
                    state=CheckoutState.READY,
                    now=now,
                    identity=identity,
                )
        except IntegrityError as error:
            raise _violation(error) from None
        return Registered(repository, checkout, remotes, facts.head)

    async def clone_from_github(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        source: str,
        *,
        name: str | None = None,
        branch: str | None = None,
    ) -> Registered:
        """Clone a GitHub repository into the actor's workspace and register it.

        ``source`` is ``owner/repo`` or ``https://<host>/<owner>/<repo>[.git]`` on
        an allowed host (``github.parse_github_source``). ``name`` defaults to the
        repository's name; ``branch`` (optional) is checked out instead of the
        remote's default. The clone runs as the actor's Linux user with the actor's
        own git configuration and no credential from this backend (PAW-028): a
        private repository needs the user's own ``gh auth``. Both ``https``
        spellings of the URL are registered as remotes. ``project.repo.add``.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        ref = parse_github_source(source, self._policy.clone_hosts)
        name = validate_name(ref.repo if name is None else name)
        branch = None if branch is None else validate_branch(branch)
        account = await self._accounts.account_of(principal.user_id)

        async def populate(path: str) -> _Populated:
            await self._git.clone(ref.clone_url, path, account, branch=branch)
            facts = await self._git.inspect(path, account)
            return _Populated(facts.default_branch, facts.head)

        return await self._create_managed(
            principal,
            account,
            project_id,
            name=name,
            source=RepositorySource.GITHUB_CLONE,
            default_branch=branch or _DEFAULT_BRANCH,
            remotes=ref.remote_urls,
            populate=populate,
        )

    async def create_local(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        name: str,
        *,
        default_branch: str = _DEFAULT_BRANCH,
    ) -> Registered:
        """Create a new, empty, local-only repository in the actor's workspace.

        ``git init`` with ``default_branch`` and nothing else: no file, no commit,
        no remote. Only its creator has a checkout, and nobody else can clone it
        until a remote is added (Decision 0017). ``project.repo.add``.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        name = validate_name(name)
        branch = validate_branch(default_branch, "default_branch")
        account = await self._accounts.account_of(principal.user_id)

        async def populate(path: str) -> _Populated:
            await self._git.init(path, account, initial_branch=branch)
            return _Populated(branch, None)

        return await self._create_managed(
            principal,
            account,
            project_id,
            name=name,
            source=RepositorySource.NEW_LOCAL,
            default_branch=branch,
            remotes=(),
            populate=populate,
        )

    async def create_github(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        name: str,
        *,
        private: bool = True,
        default_branch: str = _DEFAULT_BRANCH,
    ) -> Registered:
        """Create a new repository locally and on GitHub, and register it.

        The local part is ``create_local``. The GitHub part is the
        :class:`GitHubGateway` (PAW-028; the default refuses with
        :class:`GitHubUnavailableError`): it creates the repository as the actor's
        own GitHub user, the repository must be on an allowed host, and the result
        becomes ``origin`` and the registered remotes. Nothing is pushed. If a step
        fails everything created here is removed again; a GitHub repository that
        was created and then could not be registered is **not** deleted (the
        backend has no delete permission, and an error is logged).
        ``project.repo.add``.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        name = validate_name(name)
        private = validate_bool("private", private)
        branch = validate_branch(default_branch, "default_branch")
        account = await self._accounts.account_of(principal.user_id)

        async def populate(path: str) -> _Populated:
            await self._git.init(path, account, initial_branch=branch)
            created = await self._create_on_github(principal.user_id, name, private)
            try:
                await self._git.add_origin(path, created.clone_url, account)
            except BaseException:
                self._log_orphaned_github_repository()
                raise
            return _Populated(branch, None, created.remote_urls)

        return await self._create_managed(
            principal,
            account,
            project_id,
            name=name,
            source=RepositorySource.NEW_GITHUB,
            default_branch=branch,
            remotes=(),
            populate=populate,
        )

    async def _create_on_github(
        self, user_id: uuid.UUID, name: str, private: bool
    ) -> GitHubRepo:
        try:
            created = await self._github.create_repository(
                user_id=user_id, name=name, private=private
            )
        except GitHubUnavailableError:
            raise
        except Exception as error:
            # A gateway is foreign code: its message can hold anything.
            logger.warning("GitHub gateway failed (%s)", type(error).__name__)
            raise GitHubUnavailableError() from None
        try:
            return check_created_repository(created, name, self._policy.clone_hosts)
        except InvalidRepositoryInputError:
            # The repository may exist on GitHub, but what came back cannot be
            # registered (nothing of it is logged: it can be anything).
            logger.error(
                "The GitHub gateway returned an invalid repository; a GitHub "
                "repository may exist that was not registered"
            )
            raise GitHubUnavailableError() from None

    async def _create_managed(
        self,
        principal: Principal,
        account: LinuxAccount,
        project_id: uuid.UUID,
        *,
        name: str,
        source: RepositorySource,
        default_branch: str,
        remotes: tuple[str, ...],
        populate: Callable[[str], Awaitable[_Populated]],
    ) -> Registered:
        """Reserve, populate and finish a repository whose checkout is created here."""
        now = self._now()
        repository_id, checkout_id = uuid.uuid4(), uuid.uuid4()
        try:
            async with self._transaction() as session:
                project, _ = await self._guard(
                    session,
                    principal,
                    Capability.PROJECT_REPO_ADD,
                    project_id,
                    lock=_Lock.UPDATE,
                    resource_id=repository_id,
                )
                await self._check_capacity(session, project_id)
                path = await asyncio.to_thread(
                    plan_checkout_path,
                    account,
                    self._policy.workspace_subdir,
                    project_directory_name(project.name, project.id),
                    name,
                )
                repository = await store.insert_repository(
                    session,
                    repository_id=repository_id,
                    project_id=project_id,
                    name=name,
                    default_branch=default_branch,
                    source=source,
                    created_by=principal.user_id,
                    now=now,
                )
                for url in remotes:
                    await store.insert_remote(
                        session,
                        repository_id=repository_id,
                        project_id=project_id,
                        url=url,
                        now=now,
                    )
                await store.insert_checkout(
                    session,
                    checkout_id=checkout_id,
                    repository_id=repository_id,
                    project_id=project_id,
                    user_id=principal.user_id,
                    path=path,
                    state=CheckoutState.PENDING,
                    now=now,
                )
        except IntegrityError as error:
            raise _violation(error) from None

        created_directory = False
        populated: _Populated | None = None
        try:
            await asyncio.to_thread(create_checkout_directory, path, account)
            created_directory = True
            populated = await populate(path)
            identity = await asyncio.to_thread(read_checkout_identity, path, account)
            checkout, stored = await self._finish(
                repository,
                checkout_id,
                populated.default_branch,
                populated.remotes,
                identity,
            )
        except BaseException:
            if source is RepositorySource.NEW_GITHUB and populated is not None:
                self._log_orphaned_github_repository()
            # What this call made must not outlive a failure or a cancellation of
            # its own work (shielded, so a second cancel cannot skip it). What a
            # concurrent unregistration took away is not cleaned up again: see
            # ``_abandon``.
            await asyncio.shield(
                self._abandon(
                    account,
                    path,
                    created_directory,
                    checkout_id=checkout_id,
                    repository_id=repository_id,
                )
            )
            raise
        return Registered(stored, checkout, remotes + populated.remotes, populated.head)

    @staticmethod
    def _log_orphaned_github_repository() -> None:
        """The repository exists on GitHub but was not registered (nothing else)."""
        logger.error(
            "A GitHub repository was created but could not be registered; "
            "it was not deleted"
        )

    async def _finish(
        self,
        repository: Repository,
        checkout_id: uuid.UUID,
        default_branch: str | None,
        extra_remotes: tuple[str, ...],
        identity: tuple[int, int],
    ) -> tuple[Checkout, Repository]:
        """The second transaction: branch, remotes, then the checkout is ``ready``.

        The project must still be Active (``_require_active_project``: locked ``FOR
        SHARE`` before anything else). Something may also have unregistered while
        git or GitHub was working:
        ``CheckoutGoneError`` for a repository that is gone (nothing is inserted
        below a missing parent) and for a checkout that is gone (the branch and the
        remotes are still stored: the repository stays registered). The repository
        row is locked ``FOR SHARE`` first, so it cannot disappear under the inserts.
        """
        now = self._now()
        try:
            async with self._transaction() as session:
                await self._require_active_project(session, repository.project_id)
                stored = await store.get_repository(
                    session, repository.project_id, repository.id, for_share=True
                )
                if stored is None:
                    raise CheckoutGoneError()
                if default_branch is not None and (
                    default_branch != stored.default_branch
                ):
                    await store.update_default_branch(
                        session, stored.id, default_branch, now
                    )
                for url in extra_remotes:
                    await store.insert_remote(
                        session,
                        repository_id=stored.id,
                        project_id=stored.project_id,
                        url=url,
                        now=now,
                    )
                checkout = await store.mark_ready(session, checkout_id, now, identity)
                stored = await store.get_repository(
                    session, stored.project_id, stored.id
                )
        except IntegrityError as error:
            raise _violation(error) from None
        if checkout is None or stored is None:
            raise CheckoutGoneError()
        return checkout, stored

    async def _abandon(
        self,
        account: LinuxAccount,
        path: str,
        created_directory: bool,
        *,
        checkout_id: uuid.UUID,
        repository_id: uuid.UUID | None = None,
    ) -> None:
        """Undo what a failed or cancelled creation made, if it still owns it.

        The call owns its work as long as **its own ``pending`` row still exists**.
        The row is deleted first, in a transaction of its own, and only when that
        deleted it are the directory and (for a new repository nobody else has a
        checkout of) the repository removed. If the row is gone, a concurrent
        ``remove_checkout`` / ``remove_repository`` unregistered it, and those
        operations promise not to touch files: the directory stays, whatever the
        user put into it since. A row that already became ``ready`` is a finished
        registration and stays too. Best effort: a database error leaves the
        directory and the row (a stale reservation).
        """
        try:
            async with self._transaction() as session:
                owned = await store.delete_checkout(
                    session, checkout_id, only_pending=True
                )
                if owned and repository_id is not None:
                    await store.delete_repository_if_unused(session, repository_id)
            if owned and created_directory:
                await asyncio.to_thread(remove_directory, path, account)
        except Exception as error:
            # Only the type is logged.
            logger.error(
                "Cleanup of a failed checkout failed (%s)", type(error).__name__
            )

    # -- checkouts -------------------------------------------------------------

    async def create_checkout(
        self, actor: Principal, project_id: uuid.UUID, repository_id: uuid.UUID
    ) -> Checkout:
        """Make the actor's own checkout of a registered repository (a clone).

        The actor needs ``project.read`` on the repository (its ACL can hide it)
        and ``workspace.use`` on their own workspace. The clone source is the first
        registered remote whose host may be cloned from (``NoCloneSourceError``
        otherwise: a local-only repository cannot be cloned by others). A user has
        one checkout per repository: ``CheckoutExistsError`` for a ``ready`` one,
        ``CheckoutInProgressError`` for a fresh pending one; a stale pending one
        (its process died) is replaced. The directory it left behind is **not**
        deleted: the new attempt stops with ``PathProblem.EXISTS`` until its owner
        removes it. The project must be Active.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        account = await self._accounts.account_of(principal.user_id)
        now = self._now()
        checkout_id = uuid.uuid4()
        try:
            async with self._transaction() as session:
                project, repository = await self._guard(
                    session,
                    principal,
                    Capability.PROJECT_READ,
                    project_id,
                    lock=_Lock.SHARE,
                    repository_id=repository_id,
                    repo_scoped=True,
                )
                assert repository is not None
                await self._authorize_workspace(principal, checkout_id, project.id)
                source = self._clone_source(
                    await store.list_remotes(session, repository.id)
                )
                existing = await store.get_checkout_of(
                    session, repository.id, principal.user_id, for_update=True
                )
                if existing is not None:
                    if existing.state is CheckoutState.READY:
                        raise CheckoutExistsError()
                    age = now - existing.created_at
                    if age < timedelta(seconds=self._policy.pending_timeout_s):
                        raise CheckoutInProgressError()
                    await store.delete_checkout(session, existing.id)
                path = await asyncio.to_thread(
                    plan_checkout_path,
                    account,
                    self._policy.workspace_subdir,
                    project_directory_name(project.name, project.id),
                    repository.name,
                )
                await store.insert_checkout(
                    session,
                    checkout_id=checkout_id,
                    repository_id=repository.id,
                    project_id=project.id,
                    user_id=principal.user_id,
                    path=path,
                    state=CheckoutState.PENDING,
                    now=now,
                )
        except IntegrityError as error:
            raise _violation(error) from None

        created_directory = False
        try:
            # A directory a dead attempt left behind is never deleted here (it could
            # hold anything by now): creating this one fails with ``exists`` and the
            # owner removes it.
            await asyncio.to_thread(create_checkout_directory, path, account)
            created_directory = True
            await self._git.clone(source, path, account)
            identity = await asyncio.to_thread(read_checkout_identity, path, account)
            now = self._now()
            async with self._transaction() as session:
                await self._require_active_project(session, project.id)
                checkout = await store.mark_ready(session, checkout_id, now, identity)
            if checkout is None:
                raise CheckoutGoneError()
        except BaseException:
            await asyncio.shield(
                self._abandon(account, path, created_directory, checkout_id=checkout_id)
            )
            raise
        return checkout

    async def _authorize_workspace(
        self, principal: Principal, checkout_id: uuid.UUID, project_id: uuid.UUID
    ) -> None:
        """``workspace.use`` on the actor's own checkout: a personal resource."""
        resource = Resource(
            kind="checkout",
            id=checkout_id,
            project_id=project_id,
            owner_id=principal.user_id,
        )
        decision = await self._authorizer.authorize(
            principal, Capability.WORKSPACE_USE, resource
        )
        if not decision.allowed:
            raise RepositoryPermissionDeniedError(decision.reason)

    def _clone_source(self, remotes: list[Remote]) -> str:
        hosts = set(self._policy.clone_hosts)
        for remote in remotes:  # by URL: the same one every time
            host = remote.url.split("/", 3)[2]
            if host in hosts:
                return remote.url
        raise NoCloneSourceError()

    async def remove_checkout(
        self, actor: Principal, project_id: uuid.UUID, repository_id: uuid.UUID
    ) -> None:
        """Unregister the actor's own checkout. **The directory is not deleted.**

        ``workspace.use`` on the actor's own checkout (a member who left the project
        can still unregister). Works in every project state. A pending checkout can
        be removed too: the clone that is running ends with ``CheckoutGoneError`` and
        **leaves the directory as it is** (it is no longer the clone's to delete).
        ``CheckoutNotFoundError`` if there is none.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        async with self._transaction() as session:
            checkout = await store.get_checkout_of(
                session, repository_id, principal.user_id, for_update=True
            )
            if checkout is None or checkout.project_id != project_id:
                raise CheckoutNotFoundError()
            await self._authorize_workspace(principal, checkout.id, project_id)
            await store.delete_checkout(session, checkout.id)

    async def list_my_checkouts(
        self,
        actor: Principal,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> tuple[Checkout, ...]:
        """The actor's own checkouts, newest first (self service, no Audit event)."""
        principal = self._human(actor)
        limit = validate_limit(limit)
        offset = validate_offset(offset)
        async with self._transaction() as session:
            found = await store.list_checkouts_of(
                session, principal.user_id, limit, offset
            )
        return tuple(found)

    # -- managing a repository -------------------------------------------------

    async def remove_repository(
        self, actor: Principal, project_id: uuid.UUID, repository_id: uuid.UUID
    ) -> Repository:
        """Remove the registration: the repository, remotes and everyone's checkouts.

        **Nothing on disk and nothing on GitHub is deleted** (``REQUIREMENTS.md``);
        the users' directories stay and are simply no longer registered.
        ``project.repo.add``; the project must be Active. Returns the removed record.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        async with self._transaction() as session:
            _, repository = await self._guard(
                session,
                principal,
                Capability.PROJECT_REPO_ADD,
                project_id,
                lock=_Lock.UPDATE,
                repository_id=repository_id,
            )
            assert repository is not None
            await store.delete_repository(session, repository.id)
        return repository

    async def add_remote(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        repository_id: uuid.UUID,
        url: str,
    ) -> Remote:
        """Register one more ``https`` URL that addresses the repository.

        ``url`` is canonicalised as the Tool Broker does (``validate_remote_url``);
        each spelling an executor may be given is registered separately, up to 8. A
        URL belongs to one repository of a project. ``project.repo.add``; Active.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        url = validate_remote_url(url)
        now = self._now()
        try:
            async with self._transaction() as session:
                _, repository = await self._guard(
                    session,
                    principal,
                    Capability.PROJECT_REPO_ADD,
                    project_id,
                    lock=_Lock.UPDATE,
                    repository_id=repository_id,
                )
                assert repository is not None
                if (
                    len(await store.list_remotes(session, repository.id))
                    >= MAX_REMOTES_PER_REPOSITORY
                ):
                    raise RemoteError(RemoteProblem.TOO_MANY)
                return await store.insert_remote(
                    session,
                    repository_id=repository.id,
                    project_id=project_id,
                    url=url,
                    now=now,
                )
        except IntegrityError as error:
            raise _violation(error) from None

    async def remove_remote(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        repository_id: uuid.UUID,
        url: str,
    ) -> None:
        """Unregister one URL of the repository (``RemoteNotFoundError`` if unknown)."""
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        url = validate_remote_url(url)
        async with self._transaction() as session:
            _, repository = await self._guard(
                session,
                principal,
                Capability.PROJECT_REPO_ADD,
                project_id,
                lock=_Lock.UPDATE,
                repository_id=repository_id,
            )
            assert repository is not None
            if not await store.delete_remote(session, repository.id, url):
                raise RemoteNotFoundError()

    async def set_acl(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        repository_id: uuid.UUID,
        allowed: object,
    ) -> Repository:
        """Set the repository's ACL override; ``None`` restores ``inherit``.

        ``allowed`` is a collection of :class:`RepoPermission` (an empty one denies
        everything). An override only **narrows** the project role
        (Decision 0004): the policy never grants a permission the role lacks.
        ``project.settings.manage`` (Decision 0017: a Manager); Active.
        """
        principal = self._human(actor)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        permissions = validate_permissions(allowed)
        now = self._now()
        async with self._transaction() as session:
            _, repository = await self._guard(
                session,
                principal,
                Capability.PROJECT_SETTINGS_MANAGE,
                project_id,
                lock=_Lock.UPDATE,
                repository_id=repository_id,
            )
            assert repository is not None
            updated = await store.set_acl(session, repository.id, permissions, now)
        assert updated is not None  # the repository is locked with its project
        return updated

    # -- reading ---------------------------------------------------------------

    async def get_repository(
        self, actor: Principal, project_id: uuid.UUID, repository_id: uuid.UUID
    ) -> RepositoryDetail:
        """The repository and its remotes (``project.read``; Archived is readable).

        A repository whose ACL override denies ``read`` is ``RepositoryNotFoundError``.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        async with self._transaction() as session:
            _, repository = await self._guard(
                session,
                principal,
                Capability.PROJECT_READ,
                project_id,
                lock=_Lock.NONE,
                repository_id=repository_id,
                repo_scoped=True,
                require_active=False,
            )
            assert repository is not None
            remotes = await store.list_remotes(session, repository.id)
        return RepositoryDetail(repository, tuple(r.url for r in remotes))

    async def list_repositories(
        self,
        actor: Principal,
        project_id: uuid.UUID,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> tuple[Repository, ...]:
        """The project's repositories the actor may read, by name (``project.read``).

        Repositories whose ACL override denies ``read`` are left out.
        """
        principal = self._actor(actor)
        project_id = validate_uuid("project_id", project_id)
        limit = validate_limit(limit)
        offset = validate_offset(offset)
        async with self._transaction() as session:
            await self._guard(
                session,
                principal,
                Capability.PROJECT_READ,
                project_id,
                lock=_Lock.NONE,
                project_resource=True,
                require_active=False,
            )
            found = await store.list_repositories(
                session, project_id, limit, offset, permission=_READ
            )
        return tuple(found)

    # -- backend-internal ----------------------------------------------------------

    async def scope_entries(
        self, user_id: uuid.UUID, project_id: uuid.UUID, repository_id: uuid.UUID
    ) -> tuple[ScopedRepository, ...]:
        """What the Tool Broker's ``TaskScope.repositories`` needs for one repository.

        Backend-internal (the orchestrator calls it; it takes no actor and asks no
        Authorizer, like ``ProjectService.roles_of``): the caller has already decided
        that the user may work on this repository. Returns, first, the entry of the
        user's own ``ready`` checkout (root, the stored ACL, the registered remotes),
        then an entry for every **other** checkout of the same user that encloses it
        or lies inside it (Decision 0006 section 8(b): nested repositories obey both
        ACLs). Each entry is the repository's own, with the ACL of *its* project. A
        pending checkout is not usable: ``CheckoutNotFoundError``.

        **Fails closed on a changed root.** The Tool Broker resolves every root with
        ``realpath``, so the nesting must come from the directories the roots lead to
        now, not from the text stored at registration. Every ``ready`` checkout of the
        user is inspected (``paths.inspect_checkout_root``: its real path is the
        stored path, a directory of the user's account, and the ``(st_dev, st_ino)``
        recorded when it became ready). The requested checkout must be exactly what
        was registered, else :class:`CheckoutChangedError`. Another checkout is
        related to it when its stored path *or* the path it now resolves to is the
        same, above or below; a related checkout that changed makes the whole scope
        :class:`CheckoutChangedError` (a link that now leads into another checkout, a
        checkout renamed into another one's place), while a changed checkout that has
        nothing to do with it is left out. More than ``MAX_SCOPE_CHECKOUTS`` checkouts
        are :class:`TooManyCheckoutsError` (never a partial scope).

        What this does **not** close: the answer is a fact of the moment it is read.
        The Broker resolves again when a tool runs, and a root replaced in between
        is not seen here (Decision 0017, "Remaining limit").
        """
        user_id = validate_uuid("user_id", user_id)
        project_id = validate_uuid("project_id", project_id)
        repository_id = validate_uuid("repository_id", repository_id)
        account = await self._accounts.account_of(user_id)
        async with self._transaction() as session:
            repository = await store.get_repository(session, project_id, repository_id)
            if repository is None:
                raise RepositoryNotFoundError()
            checkout = await store.get_checkout_of(session, repository_id, user_id)
            if checkout is None or checkout.state is not CheckoutState.READY:
                raise CheckoutNotFoundError()
            ready = await store.list_ready_checkouts_of(
                session, user_id, MAX_SCOPE_CHECKOUTS + 1
            )
            if len(ready) > MAX_SCOPE_CHECKOUTS:
                raise TooManyCheckoutsError()
            related = await self._related_checkouts(account, checkout, ready)
            entries = [await self._entry(session, repository, checkout)]
            for other in related:
                found = await store.get_repository_any_project(
                    session, other.repository_id
                )
                if found is not None:
                    entries.append(await self._entry(session, found, other))
        return tuple(entries)

    @staticmethod
    async def _related_checkouts(
        account: LinuxAccount, checkout: Checkout, ready: list[Checkout]
    ) -> list[Checkout]:
        """The verified checkouts enclosing or inside ``checkout``; else refuse."""

        def inspect_all():
            return {
                item.id: inspect_checkout_root(
                    item.path,
                    account.uid,
                    (item.root_device, item.root_inode)
                    if item.root_device is not None and item.root_inode is not None
                    else None,
                )
                for item in ready
            }

        states = await asyncio.to_thread(inspect_all)
        own = states[checkout.id]
        if own.problem is not None:
            _log_changed_root(checkout, own.problem)
            raise CheckoutChangedError(own.problem, checkout.id)
        root = checkout.path

        def related(path: str) -> bool:
            return path == root or path_within(path, root) or path_within(root, path)

        found: list[Checkout] = []
        vanished: list[Checkout] = []  # changed, and not (by path) related to the root
        for other in ready:
            if other.id == checkout.id:
                continue
            state = states[other.id]
            leads_to = {other.path} | ({state.resolved} if state.resolved else set())
            if not any(related(path) for path in leads_to):
                if state.problem is not None and other.root_device is not None:
                    vanished.append(other)
                continue
            if state.problem is not None:
                _log_changed_root(other, state.problem)
                raise CheckoutChangedError(PathProblem.CHANGED, other.id)
            found.append(other)
        if vanished:
            await RepositoryService._refuse_if_moved_into(root, vanished)
        return found

    @staticmethod
    async def _refuse_if_moved_into(root: str, vanished: list[Checkout]) -> None:
        """Refuse when a checkout that left its path may now be inside ``root``.

        Such a checkout (deleted, moved, replaced) is unrelated *by its paths*, but it
        may have been renamed into a directory of ``root``, and then the Tool Broker
        reaches its files through ``root`` and applies only ``root``'s ACL. Its
        identity, recorded when it became ready, is looked for in the directories below
        ``root``: found, or not provably absent (the search is bounded), and the scope
        is refused. Only runs when a checkout changed; no changed checkout, no search.
        """
        wanted = {(c.root_device, c.root_inode): c for c in vanished}
        located, complete = await asyncio.to_thread(
            locate_directory_identity,
            root,
            list(wanted),
            MAX_IDENTITY_SCAN_DIRECTORIES,
        )
        culprit = vanished[0] if located is None else wanted[located]
        if located is not None or not complete:
            _log_changed_root(culprit, PathProblem.CHANGED)
            raise CheckoutChangedError(PathProblem.CHANGED, culprit.id)

    @staticmethod
    async def _entry(
        session: AsyncSession, repository: Repository, checkout: Checkout
    ) -> ScopedRepository:
        remotes = await store.list_remotes(session, repository.id)
        return ScopedRepository(
            repo_id=repository.id,
            project_id=repository.project_id,
            root=checkout.path,
            acl=repository.acl,
            remotes=tuple(remote.url for remote in remotes),
        )

    async def purge_projects(self, project_ids: object) -> tuple[uuid.UUID, ...]:
        """Remove the registrations of projects that are **Deleted** (backend-internal).

        ``ProjectService.purge_expired`` returns the ids it marked Deleted; the
        orchestrator passes them here (Decision 0008 section 2: each area removes its
        own data). A project that is not a tombstone keeps its repositories, whatever
        the caller says. **No file and no GitHub repository is deleted**
        (``REQUIREMENTS.md``). Returns the ids that had registrations.
        """
        ids = validate_project_ids(project_ids)
        async with self._transaction() as session:
            purged = await store.delete_repositories_of_deleted_projects(session, ids)
        return tuple(purged)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _log_changed_root(checkout: Checkout, problem: PathProblem) -> None:
    """A checkout root is not what was registered (ids and the reason only)."""
    logger.warning(
        "Checkout root changed (checkout=%s, repository=%s, problem=%s)",
        checkout.id,
        checkout.repository_id,
        problem.value,
    )
