"""Pushing the checked integration branch and opening its pull request (issue #132).

Decision 0036 (10) left this out of PAW-035: once the integrated result of a
task passed the tests, the Evaluator and the review (``gate.py``), every
repository whose Working Set role is ``target`` gets its integration branch
pushed to GitHub and a pull request opened against its default branch, so that
the task can complete (Decision 0030, section 5: a ``target`` completes only with
a delivered pull request). Merging stays the human's (``AGENTS.md``). Decision
0052 lists the choices.

:class:`GitHubPullRequestPublisher` does it for one repository:

* **Authorized as the task's creator, audited.** ``project.pr.create`` on the
  repository (its ACL, the project's state now), decided by the Authorizer as an
  agent action on behalf of the task's creator (``authorize_agent_action``: the
  creator's current rights, intersected with a grant that holds this capability
  alone). Every decision is audited (``REQUIRED``: a failed audit write is a
  denial). ``REQUIREMENTS.md`` ("Tool approval boundary") makes a normal push to
  an AI branch and a pull request within the task's purpose ``SCOPED_AUTO``: no
  human approval is asked for; the ``target`` role is the task's purpose.
* **Only the commit that was checked, only a ``paw/`` branch.** The push is
  ``<checked commit>:refs/heads/paw/<task>/<attempt>/_integration``: a commit
  made on the branch after the checks is not pushed, and no other branch (the
  default branch least of all) is ever written. Never forced: a remote branch
  that moved elsewhere refuses the push.
* **To the registered GitHub repository, by its URL.** The URL is the one the
  backend registered for the repository (``ScopedRepository.remotes``, an
  ``https`` URL on an allowed host), never a remote name from the checkout's own
  configuration.
* **The user's own GitHub identity** (PAW-028, Decision 0029): git runs as the
  creator's Linux account through the deployment's ``GitRunner`` with gh's own
  credential helper (the one ``clone`` uses), and the pull request is made with
  ``gh api`` as that account (``GhRunner``). The backend never sees a token.
* **Idempotent.** A pull request of the branch against the default branch
  that already exists is reused (one against another base is not the one): an
  ``open`` one, or a ``merged`` one whose head is the checked commit, is
  delivered (a merged one of an older commit is not the one); a ``draft`` one
  is recorded as it is; a ``closed`` one that was not merged is recorded and
  **not** replaced (a human closed it). A second run pushes the same commit
  again (nothing to do).
* **Nothing it read is stored or logged.** Failures are a closed
  :class:`PublishProblem`; git's and gh's output never leaves this module, and
  the pull request's text carries only the task's title and which kinds of check
  passed (never what a check said).
"""

import json
import logging
import os
import re
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum

from paw_backend.authz import AgentGrant, Authorizer, Capability, ProjectState
from paw_backend.authz.subjects import Resource
from paw_backend.integration.coordinator import IntegrationTarget, default_branch
from paw_backend.integration.git import WorktreeGit
from paw_backend.integration.layout import INTEGRATION_KEY, branch_name, in_namespace
from paw_backend.orchestrator.errors import error_class_of
from paw_backend.orchestrator.workspaces import WorktreeUnavailableError
from paw_backend.repositories.accounts import AccountDirectory
from paw_backend.repositories.errors import (
    GitCommandError,
    InvalidRepositoryInputError,
    LinuxAccountUnavailableError,
)
from paw_backend.repositories.git import GitRunner
from paw_backend.repositories.github import GitHubRepo, parse_github_source
from paw_backend.repositories.github_connection import GhRunner
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.policy import RepositoryPolicy
from paw_backend.tasks import (
    PullRequestInfo,
    PullRequestState,
    RepoRole,
    TaskRun,
    TaskSnapshot,
)
from paw_backend.tools import ScopedRepository
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

# The publisher of a task's run acts as an agent of the task's creator; its id is
# derived, never random (the same run always has the same one on the audit trail).
_PUBLISHER_NAMESPACE = uuid.UUID("5e0c7a2d-9b41-4f63-8d17-2a6b3c9e1320")
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
# GitHub's own limit on a pull request's title.
MAX_TITLE_CHARS = 256
TITLE_PREFIX = "[PAW] "
# Larger than any pull request list of one branch; a longer answer is refused.
MAX_LISTED_PULL_REQUESTS = 100
_MAX_PULL_REQUEST_NUMBER = 2_147_483_647


class PublishProblem(StrEnum):
    """Why a repository's integration branch was not pushed or its pull request
    not made. A closed set: what git or gh said is never kept."""

    NOT_A_TARGET = "not_a_target"  # the Working Set role is not ``target`` now
    NOT_THE_INTEGRATION = "not_the_integration"  # not this run's integration branch
    NO_GITHUB_REMOTE = "no_github_remote"  # no registered GitHub repository
    NOT_AUTHORIZED = "not_authorized"  # ``project.pr.create`` was refused
    ACCOUNT_UNAVAILABLE = "account_unavailable"  # the creator has no Linux account
    BASE_UNKNOWN = "base_unknown"  # the checkout's default branch is not known
    PUSH_FAILED = "push_failed"  # git refused or failed the push
    GITHUB_FAILED = "github_failed"  # gh failed (not logged in, an HTTP error, ...)
    # The pull request's head is not the checked commit (the remote branch
    # moved after the push): it would propose what was not checked.
    BRANCH_MOVED = "branch_moved"
    INVALID_RESPONSE = "invalid_response"  # gh answered something not understood


class PullRequestNotPublishedError(Exception):
    """The repository's pull request was not made; ``problem`` says why."""

    def __init__(self, problem: PublishProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """One ``target`` repository of a task whose integrated result passed every
    check. ``checks`` are ``(kind, number of checks of that kind)`` in the order
    they ran (all passed); ``project_state`` is the project's state now."""

    task: TaskSnapshot
    run: TaskRun
    repository: ScopedRepository
    project_state: ProjectState | None
    target: IntegrationTarget
    checks: tuple[tuple[str, int], ...]


def publisher_agent_id(task_id: uuid.UUID, run: TaskRun) -> uuid.UUID:
    """The agent id the push and the pull request of ``run`` are decided for."""
    return uuid.uuid5(
        _PUBLISHER_NAMESPACE, f"{task_id}:{run.attempt}:{run.retry_count}"
    )


def pull_request_title(task: TaskSnapshot) -> str:
    """``[PAW] <task title>``, on one line, at most GitHub's limit."""
    title = " ".join(task.title.split())
    return (TITLE_PREFIX + title)[:MAX_TITLE_CHARS]


def pull_request_body(request: PublishRequest) -> str:
    """The pull request's text: the task, the checked commit and which kinds of
    check passed on it (never what a check said: its text is not stored either,
    ``gate.py``)."""
    rows = "\n".join(f"| {kind} | passed ({count}) |" for kind, count in request.checks)
    return (
        f"Personal AI Workspace task `{request.task.id}`"
        f" (attempt {request.run.attempt}).\n\n"
        f"Checked commit: `{request.target.head}`\n\n"
        "| Check | Result |\n| --- | --- |\n"
        f"{rows}\n\n"
        "Every check passed on this commit (Test -> Evaluator -> Review)."
        " Merging is the human's decision.\n"
    )


def _gh_credential_helper(gh_executable: str) -> str:
    # The same helper ``GitClient.clone`` adds (``gh auth setup-git``'s string).
    return f"credential.helper=!{gh_executable} auth git-credential"


def push_arguments(gh_executable: str, url: str, commit: str, branch: str) -> list[str]:
    """``git push`` of exactly ``commit`` to ``refs/heads/<branch>`` of ``url``,
    with gh's credential helper only (an empty ``credential.helper`` first drops
    every helper the repository's own configuration names). ``--no-follow-tags``
    and ``--no-recurse-submodules`` override the checkout's ``push.followTags``
    (an annotated tag of the commit would be pushed too) and
    ``push.recurseSubmodules`` (a submodule's commits would be pushed to its own
    remote): nothing but the one branch is written (Codex review of #159). The
    form is fixed: the SSH wrapper accepts nothing else (Decision 0052)."""
    return [
        "-c",
        "credential.helper=",
        "-c",
        _gh_credential_helper(gh_executable),
        "push",
        "--quiet",
        "--no-follow-tags",
        "--no-recurse-submodules",
        "--",
        url,
        f"{commit}:refs/heads/{branch}",
    ]


def _state_of(item: dict) -> PullRequestState:
    state = item.get("state")
    draft = item.get("draft", False)
    merged_at = item.get("merged_at")
    if type(draft) is not bool or not (merged_at is None or isinstance(merged_at, str)):
        raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
    if state == "open":
        return PullRequestState.DRAFT if draft else PullRequestState.OPEN
    if state == "closed":
        return PullRequestState.MERGED if merged_at else PullRequestState.CLOSED
    raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)


def parse_pull_request(
    item: object, repo: GitHubRepo, branch: str, base: str, commit: str
) -> PullRequestInfo | None:
    """One pull request of GitHub's REST API, checked: its head is ``branch`` of
    ``repo`` itself, its number is a positive integer and its URL is exactly
    ``https://<host>/<owner>/<repo>/pull/<number>`` (the case of the owner and
    the repository as GitHub spells them). ``None`` when it is against another
    branch than ``base`` (it does not propose the change to the default branch),
    and when it was merged with another head than ``commit`` (the checked
    commit is not in it; a new one proposes it). An open or draft one whose head
    is not ``commit`` is ``BRANCH_MOVED``: the branch moved after the push and
    the pull request proposes what was not checked (Codex review of #159)."""
    if not isinstance(item, dict):
        raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
    number = item.get("number")
    url = item.get("html_url")
    head = item.get("head")
    if (
        type(number) is not int
        or not 1 <= number <= _MAX_PULL_REQUEST_NUMBER
        or not isinstance(url, str)
        or not isinstance(head, dict)
        or head.get("ref") != branch
    ):
        raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
    expected = f"https://{repo.host}/{repo.owner}/{repo.repo}/pull/{number}"
    if url.lower() != expected.lower() or not url.isascii():
        raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
    head_repo = head.get("repo")
    full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
    if not isinstance(full_name, str) or (
        full_name.lower() != f"{repo.owner}/{repo.repo}".lower()
    ):
        raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
    head_commit = head.get("sha")
    if not isinstance(head_commit, str) or _OBJECT_ID.fullmatch(head_commit) is None:
        raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
    target = item.get("base")
    target_ref = target.get("ref") if isinstance(target, dict) else None
    if not isinstance(target_ref, str):
        raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
    if target_ref != base:
        return None
    state = _state_of(item)
    if head_commit != commit:
        if state is PullRequestState.MERGED:
            return None
        if state is not PullRequestState.CLOSED:
            raise PullRequestNotPublishedError(PublishProblem.BRANCH_MOVED)
    return PullRequestInfo(number, url, state)


# Which of several pull requests of the branch is the one: an open one, else a
# merged one, else a draft, else the newest closed one (GitHub lists newest first).
_PREFERENCE = (
    PullRequestState.OPEN,
    PullRequestState.MERGED,
    PullRequestState.DRAFT,
    PullRequestState.CLOSED,
)


def choose_pull_request(found: Sequence[PullRequestInfo]) -> PullRequestInfo | None:
    for state in _PREFERENCE:
        for pull_request in found:
            if pull_request.state is state:
                return pull_request
    return None


class GitHubPullRequestPublisher:
    """See the module docstring. ``runner`` is the deployment's ``GitRunner``
    (``SshGitRunner`` for another Linux user; its wrapper accepts the push form of
    :func:`push_arguments` only), ``gh`` its ``GhRunner`` (PAW-028), ``policy``
    the allowed GitHub hosts and the timeouts, ``gh_executable`` the ``gh`` the
    credential helper names (the one ``gh`` runs, as for ``GitClient``)."""

    def __init__(
        self,
        *,
        runner: GitRunner,
        gh: GhRunner,
        accounts: AccountDirectory,
        policy: RepositoryPolicy,
        authorizer: Authorizer,
        gh_executable: str = "gh",
    ) -> None:
        require_async_method(runner, "run", 1)
        require_async_method(gh, "run", 1)
        require_async_method(accounts, "account_of", 1)
        if not isinstance(policy, RepositoryPolicy):
            raise TypeError("policy must be a RepositoryPolicy")
        if not isinstance(authorizer, Authorizer):
            raise TypeError("authorizer must be an Authorizer")
        if type(gh_executable) is not str or not gh_executable:
            raise TypeError("gh_executable must be a non-empty str")
        self._runner = runner
        self._gh = gh
        self._accounts = accounts
        self._policy = policy
        self._authorizer = authorizer
        self._gh_executable = gh_executable
        self._git = WorktreeGit(runner, timeout_s=policy.git_timeout_s)

    async def publish(self, request: PublishRequest) -> PullRequestInfo:
        """Push the checked commit of ``request.target`` and return the pull
        request of its branch (made when there is none). Raises
        :class:`PullRequestNotPublishedError`; nothing else."""
        if not isinstance(request, PublishRequest):
            raise TypeError("request must be a PublishRequest")
        repository, target = request.repository, request.target
        if repository.role is not RepoRole.TARGET or repository.root is None:
            raise PullRequestNotPublishedError(PublishProblem.NOT_A_TARGET)
        branch = branch_name(request.task.id, request.run.attempt, INTEGRATION_KEY)
        if (
            target.repo_id != repository.repo_id
            or target.branch != branch
            or _OBJECT_ID.fullmatch(target.head) is None
        ):
            raise PullRequestNotPublishedError(PublishProblem.NOT_THE_INTEGRATION)
        github = self._github_repository(repository.remotes)
        await self._authorize(request)
        account = await self._account(request.task)
        base = await self._base(repository.root, account)
        await self._push(repository.root, github, target.head, branch, account)
        return await self._pull_request(request, github, branch, base, account)

    # -- steps --------------------------------------------------------------------

    def _github_repository(self, remotes: Collection[str]) -> GitHubRepo:
        for url in remotes:
            try:
                return parse_github_source(url, self._policy.clone_hosts, "remote")
            except InvalidRepositoryInputError:
                continue
        raise PullRequestNotPublishedError(PublishProblem.NO_GITHUB_REMOTE)

    async def _authorize(self, request: PublishRequest) -> None:
        repository = request.repository
        if repository.acl is None or request.project_state is None:
            raise PullRequestNotPublishedError(PublishProblem.NOT_AUTHORIZED)
        grant = AgentGrant(
            publisher_agent_id(request.task.id, request.run),
            {Capability.PROJECT_PR_CREATE},
            {repository.project_id},
        )
        decision = await self._authorizer.authorize_agent_action(
            request.task.created_by,
            grant,
            Capability.PROJECT_PR_CREATE,
            Resource.repository(
                repository.project_id, request.project_state, repository.acl
            ),
            correlation_id=uuid.uuid4(),
        )
        if not decision.allowed:
            raise PullRequestNotPublishedError(PublishProblem.NOT_AUTHORIZED)

    async def _account(self, task: TaskSnapshot) -> LinuxAccount:
        try:
            return await self._accounts.account_of(task.created_by)
        except LinuxAccountUnavailableError:
            raise PullRequestNotPublishedError(
                PublishProblem.ACCOUNT_UNAVAILABLE
            ) from None

    async def _base(self, checkout: str, account: LinuxAccount) -> str:
        try:
            base = await default_branch(self._git, checkout, account)
        except (WorktreeUnavailableError, GitCommandError):
            raise PullRequestNotPublishedError(PublishProblem.BASE_UNKNOWN) from None
        if in_namespace(base):  # pragma: no cover - default_branch refuses it
            raise PullRequestNotPublishedError(PublishProblem.BASE_UNKNOWN)
        return base

    async def _push(
        self,
        checkout: str,
        github: GitHubRepo,
        commit: str,
        branch: str,
        account: LinuxAccount,
    ) -> None:
        try:
            result = await self._runner.run(
                push_arguments(self._gh_executable, github.clone_url, commit, branch),
                account=account,
                cwd=checkout,
                timeout_s=self._policy.clone_timeout_s,
                ceiling=os.path.dirname(checkout) or "/",
            )
        except GitCommandError:
            raise PullRequestNotPublishedError(PublishProblem.PUSH_FAILED) from None
        if result.returncode != 0:
            logger.warning("Integration push failed (exit %d)", result.returncode)
            raise PullRequestNotPublishedError(PublishProblem.PUSH_FAILED)

    async def _gh_api(
        self, args: Sequence[str], github: GitHubRepo, account: LinuxAccount
    ) -> object | None:
        """``gh api --hostname <host> ...`` as the account; its JSON, or ``None``
        when gh exited non-zero (an HTTP error: gh prints nothing we keep)."""
        try:
            result = await self._gh.run(
                ["api", "--hostname", github.host, *args],
                account=account,
                hostname=github.host,
                timeout_s=self._policy.gh_timeout_s,
            )
        except Exception as error:
            # ``GhCommandError`` (not installed, not this account's user, a
            # timeout, ...) or a runner's own failure: the type only.
            logger.warning("gh api failed (%s)", error_class_of(error))
            raise PullRequestNotPublishedError(PublishProblem.GITHUB_FAILED) from None
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise PullRequestNotPublishedError(
                PublishProblem.INVALID_RESPONSE
            ) from None

    async def _existing(
        self,
        github: GitHubRepo,
        branch: str,
        base: str,
        commit: str,
        account: LinuxAccount,
    ) -> PullRequestInfo | None:
        listed = await self._gh_api(
            [
                "--method",
                "GET",
                f"repos/{github.owner}/{github.repo}/pulls",
                "-f",
                f"head={github.owner}:{branch}",
                "-f",
                "state=all",
                "-f",
                f"per_page={MAX_LISTED_PULL_REQUESTS}",
            ],
            github,
            account,
        )
        if listed is None:
            raise PullRequestNotPublishedError(PublishProblem.GITHUB_FAILED)
        if not isinstance(listed, list) or len(listed) > MAX_LISTED_PULL_REQUESTS:
            raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
        found = [
            parse_pull_request(item, github, branch, base, commit) for item in listed
        ]
        return choose_pull_request([item for item in found if item is not None])

    async def _pull_request(
        self,
        request: PublishRequest,
        github: GitHubRepo,
        branch: str,
        base: str,
        account: LinuxAccount,
    ) -> PullRequestInfo:
        existing = await self._existing(
            github, branch, base, request.target.head, account
        )
        if existing is not None:
            return existing
        created = await self._gh_api(
            [
                "--method",
                "POST",
                f"repos/{github.owner}/{github.repo}/pulls",
                "-f",
                f"title={pull_request_title(request.task)}",
                "-f",
                f"head={branch}",
                "-f",
                f"base={base}",
                "-f",
                f"body={pull_request_body(request)}",
            ],
            github,
            account,
        )
        if created is None:
            # Refused (not logged in, no permission) or made meanwhile by another
            # run: what exists now decides.
            existing = await self._existing(
                github, branch, base, request.target.head, account
            )
            if existing is None:
                raise PullRequestNotPublishedError(PublishProblem.GITHUB_FAILED)
            return existing
        made = parse_pull_request(created, github, branch, base, request.target.head)
        if made is None:
            raise PullRequestNotPublishedError(PublishProblem.INVALID_RESPONSE)
        return made
