"""The changed files of a delivered pull request (issue #185 item 6, Decision 0078
Proposed).

The PR screen shows which files a pull request changes, with the lines added and
deleted, and each file's diff (the Design's PullRequest and MobileDiff boards).
The backend has no other source of them: git's ``diff`` is not on the SSH
wrapper's allowlist (Decisions 0029 / 0036) and the worktrees belong to the task
creator's Linux account. So they are read **once, from GitHub, when the
Integration Gate delivered the pull request** (``gate.py`` calls
:class:`ChangeRecorder` right after the pull request was recorded on the
attempt), as the creator, with the same ``gh api`` the publisher used
(``publish.py``: the creator's own GitHub identity, the backend never sees a
token), and stored with the pull request's record (``pull_request_changes``,
revision 0190). What is shown is what was delivered: the checked commit
(``IntegrationTarget.head``) against the default branch.

Bounded at every step:

* at most :data:`MAX_FILES` files are kept (GitHub lists up to 3000); more is
  ``truncated``. A path is cut at :data:`MAX_PATH_CHARS` characters.
* the patch of the first :data:`MAX_PATCH_FILES` files only, each cut at
  :data:`MAX_PATCH_CHARS` characters on a whole line, after the credentials of a
  little more than that were redacted (``patch_truncated``); GitHub gives none for
  a binary or a very large file.
* every ``gh`` answer fits in ``MAX_GH_OUTPUT_BYTES`` whatever the names hold
  (``--jq`` cuts each field before gh prints it, and the page sizes are chosen
  for the worst case: :func:`listing_bytes`, :func:`patch_bytes`).
* the whole reading has one deadline (:class:`ChangeRecorder`).

What is stored is safe to show: control characters of a path become visible
escapes, and a patch has every recognisable credential redacted
(``tools.credentials.redact_text``) and its control characters other than tab and
line feed escaped. Nothing read is logged.

Reading the changes is best effort and never holds the task up: a failure (gh,
GitHub, a changed pull request, the deadline) stores nothing and the PR screen says
the changes were not recorded. A pull request that is read again replaces what
was stored for its record.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from paw_backend.db import Database
from paw_backend.integration.publish import PublishRequest
from paw_backend.orchestrator.errors import error_class_of
from paw_backend.repositories.accounts import AccountDirectory
from paw_backend.repositories.errors import (
    InvalidRepositoryInputError,
    LinuxAccountUnavailableError,
)
from paw_backend.repositories.github import GitHubRepo, parse_github_source
from paw_backend.repositories.github_connection import GhRunner
from paw_backend.repositories.limits import MAX_GH_OUTPUT_BYTES
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.repositories.policy import RepositoryPolicy
from paw_backend.tasks import PullRequestInfo
from paw_backend.tasks.models import (
    MAX_PULL_REQUEST_FILES,
    PullRequestChangesRow,
    TaskAttemptRepositoryRow,
)
from paw_backend.tools.credentials import redact_text
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

MAX_FILES = MAX_PULL_REQUEST_FILES
MAX_PATH_CHARS = 300
MAX_PATCH_FILES = 50
MAX_PATCH_CHARS = 9_000
# Read past the kept part, so that a credential that crosses its end is still
# whole when it is redacted (Codex review of #206); what is kept then ends on a
# whole line.
PATCH_LOOKAHEAD_CHARS = 1_000
PATCH_READ_CHARS = MAX_PATCH_CHARS + PATCH_LOOKAHEAD_CHARS
# GitHub's file statuses (REST "List pull requests files").
STATUSES = (
    "added",
    "removed",
    "modified",
    "renamed",
    "copied",
    "changed",
    "unchanged",
)
MAX_STATUS_CHARS = 16
_MAX_COUNT = 2_147_483_647
# How many files one listing page asks for: the most whose worst case fits.
LIST_PAGE_SIZE = 15
# The whole reading of one pull request (every gh call), in seconds.
DEFAULT_TIMEOUT_S = 120.0

# The bytes one character may take in gh's JSON: ``\\u001f`` for a control
# character (UTF-8 takes at most 4).
_BYTES_PER_CHAR = 6
_NUMBER_BYTES = 24
_LIST_ROW_OVERHEAD = 120  # keys, quotes, punctuation of one listed file
_PATCH_ROW_OVERHEAD = 80


# The head commit of the pull request (an object: gh prints a bare string raw).
HEAD_JQ = "{sha: (.head.sha | tostring | .[:64])}"


def _page(per_page: int, page: int) -> list[str]:
    return ["-f", f"per_page={per_page}", "-f", f"page={page}"]


def listing_jq() -> str:
    """One page of files, each field cut before gh prints it."""
    cut = f".[:{MAX_PATH_CHARS}]"
    return (
        "[.[] | {"
        f"filename: (.filename | tostring | {cut}), "
        "previous_filename: (if .previous_filename == null then null"
        f" else (.previous_filename | tostring | {cut}) end), "
        f"status: (.status | tostring | .[:{MAX_STATUS_CHARS}]), "
        "additions: .additions, deletions: .deletions, "
        "has_patch: (.patch != null)"
        "}]"
    )


def patch_jq() -> str:
    """One file (a page of one) with its patch cut, and the patch's full length."""
    return (
        "[.[] | {"
        f"filename: (.filename | tostring | .[:{MAX_PATH_CHARS}]), "
        "patch: (if .patch == null then null"
        f" else (.patch | tostring | .[:{PATCH_READ_CHARS}]) end), "
        "patch_chars: (if .patch == null then 0 else (.patch | tostring | length) end)"
        "}]"
    )


def listing_bytes(page_size: int = LIST_PAGE_SIZE) -> int:
    """The most one listing page can print."""
    row = (
        2 * MAX_PATH_CHARS * _BYTES_PER_CHAR
        + MAX_STATUS_CHARS * _BYTES_PER_CHAR
        + 2 * _NUMBER_BYTES
        + _LIST_ROW_OVERHEAD
    )
    return 2 + page_size * (row + 1)


def patch_bytes() -> int:
    """The most one patch call can print."""
    return (
        2
        + MAX_PATH_CHARS * _BYTES_PER_CHAR
        + PATCH_READ_CHARS * _BYTES_PER_CHAR
        + _NUMBER_BYTES
        + _PATCH_ROW_OVERHEAD
    )


# The page sizes and cuts above are chosen so that no answer is cut off by gh's
# output cap (which would fail the reading): checked once, at import.
if max(listing_bytes(), patch_bytes()) > MAX_GH_OUTPUT_BYTES:  # pragma: no cover
    raise RuntimeError("a gh answer of the changes may not fit in gh's output")


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    previous_path: str | None
    status: str
    additions: int
    deletions: int
    # ``None``: GitHub gave none (a binary or very large file), or it was not
    # read (beyond the first ``MAX_PATCH_FILES`` files).
    patch: str | None = None
    patch_truncated: bool = False


@dataclass(frozen=True, slots=True)
class PullRequestChanges:
    # The checked commit the pull request delivered (what the files are of).
    head_commit: str
    files: tuple[ChangedFile, ...]
    # GitHub listed more files than ``MAX_FILES``.
    truncated: bool


class ChangesNotReadError(Exception):
    """The changes could not be read (gh, GitHub, a pull request that changed
    meanwhile). Carries no detail: nothing gh printed is kept."""


def _visible(text: str, *, keep: str = "") -> str:
    """Control characters (but those in ``keep``) as visible escapes."""
    return "".join(
        char
        if char in keep or not (ord(char) < 32 or 127 <= ord(char) < 160)
        else f"\\u{ord(char):04x}"
        for char in text
    )


def safe_patch(patch: str, *, cut: bool = False) -> tuple[str, bool]:
    """A patch as it may be stored and shown, and whether it was cut: credentials
    redacted in all that was read (``cut``: GitHub's patch went on after it),
    control characters other than tab and line feed escaped, and then at most
    :data:`MAX_PATCH_CHARS` characters (an escape makes one character six).
    Redaction runs before the cut, and a cut patch keeps whole lines only: a
    credential at the end of what was read is never kept in part (Codex review
    of #206)."""
    redacted, _ = redact_text(patch)
    shown = _visible(redacted, keep="\t\n")
    if not cut and len(shown) <= MAX_PATCH_CHARS:
        return shown, False
    kept = shown[:MAX_PATCH_CHARS]
    line_end = kept.rfind("\n")
    return (kept[: line_end + 1] if line_end >= 0 else ""), True


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_COUNT:
        raise ChangesNotReadError()
    return value


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ChangesNotReadError()
    return value


def parse_listed(item: object) -> tuple[ChangedFile, bool]:
    """One listed file and whether GitHub has a patch for it."""
    if not isinstance(item, dict):
        raise ChangesNotReadError()
    status = item.get("status")
    if status not in STATUSES:
        raise ChangesNotReadError()
    previous = item.get("previous_filename")
    has_patch = item.get("has_patch")
    if type(has_patch) is not bool:
        raise ChangesNotReadError()
    changed = ChangedFile(
        path=_visible(_text(item.get("filename"), MAX_PATH_CHARS)),
        previous_path=None
        if previous is None
        else _visible(_text(previous, MAX_PATH_CHARS)),
        status=status,
        additions=_count(item.get("additions")),
        deletions=_count(item.get("deletions")),
    )
    return changed, has_patch


@dataclass(frozen=True, slots=True)
class GitHubChangeReader:
    """Reads the changed files of a pull request with ``gh api`` as the task's
    creator (module docstring). ``gh`` is the deployment's ``GhRunner``,
    ``accounts`` its account directory, ``policy`` the allowed GitHub hosts and
    gh's timeout (the publisher's own)."""

    gh: GhRunner
    accounts: AccountDirectory
    policy: RepositoryPolicy

    def __post_init__(self) -> None:
        require_async_method(self.gh, "run", 1)
        require_async_method(self.accounts, "account_of", 1)
        if not isinstance(self.policy, RepositoryPolicy):
            raise TypeError("policy must be a RepositoryPolicy")

    async def read(
        self, request: PublishRequest, pull_request: PullRequestInfo
    ) -> PullRequestChanges:
        """The changes of ``pull_request`` (of ``request.repository``); raises
        :class:`ChangesNotReadError`."""
        github = self._github_repository(request.repository.remotes)
        try:
            account = await self.accounts.account_of(request.task.created_by)
        except LinuxAccountUnavailableError:
            raise ChangesNotReadError() from None
        pull = f"repos/{github.owner}/{github.repo}/pulls/{pull_request.number}"
        endpoint = f"{pull}/files"
        # The pull request must still propose the checked commit before and after
        # the files are read: what is stored is labelled with that commit, and a
        # branch that moved in between would mix two commits (Codex review of
        # #206).
        await self._require_head(pull, request.target.head, github, account)
        listed: list[tuple[ChangedFile, bool]] = []
        truncated = False
        page = 1
        while True:
            answer = await self._gh_api(
                endpoint, _page(LIST_PAGE_SIZE, page), listing_jq(), github, account
            )
            if not isinstance(answer, list) or len(answer) > LIST_PAGE_SIZE:
                raise ChangesNotReadError()
            if len(listed) >= MAX_FILES:
                truncated = bool(answer)
                break
            listed.extend(parse_listed(item) for item in answer)
            if len(answer) < LIST_PAGE_SIZE:
                break
            page += 1
        if len(listed) > MAX_FILES:
            listed, truncated = listed[:MAX_FILES], True
        files = []
        for index, (changed, has_patch) in enumerate(listed):
            if has_patch and index < MAX_PATCH_FILES:
                changed = await self._with_patch(
                    endpoint, index, changed, github, account
                )
            files.append(changed)
        await self._require_head(pull, request.target.head, github, account)
        return PullRequestChanges(request.target.head, tuple(files), truncated)

    async def _require_head(
        self, pull: str, head: str, github: GitHubRepo, account: LinuxAccount
    ) -> None:
        answer = await self._gh_api(pull, [], HEAD_JQ, github, account)
        if not isinstance(answer, dict) or answer.get("sha") != head:
            raise ChangesNotReadError()

    async def _with_patch(
        self,
        endpoint: str,
        index: int,
        changed: ChangedFile,
        github: GitHubRepo,
        account: LinuxAccount,
    ) -> ChangedFile:
        answer = await self._gh_api(
            endpoint, _page(1, index + 1), patch_jq(), github, account
        )
        if not isinstance(answer, list) or len(answer) != 1:
            raise ChangesNotReadError()
        if not isinstance(answer[0], dict):
            raise ChangesNotReadError()
        item = answer[0]
        filename = item.get("filename")
        # The same file at the same place, or the pull request changed meanwhile.
        if not isinstance(filename, str) or _visible(filename) != changed.path:
            raise ChangesNotReadError()
        patch, length = item.get("patch"), item.get("patch_chars")
        if patch is None:
            return changed
        if (
            not isinstance(patch, str)
            or len(patch) > PATCH_READ_CHARS
            or type(length) is not int
            or length < len(patch)
        ):
            raise ChangesNotReadError()
        shown, cut = safe_patch(patch, cut=length > len(patch))
        return ChangedFile(
            changed.path,
            changed.previous_path,
            changed.status,
            changed.additions,
            changed.deletions,
            patch=shown,
            patch_truncated=cut,
        )

    def _github_repository(self, remotes: Sequence[str]) -> GitHubRepo:
        for url in remotes:
            try:
                return parse_github_source(url, self.policy.clone_hosts, "remote")
            except InvalidRepositoryInputError:
                continue
        raise ChangesNotReadError()

    async def _gh_api(
        self,
        endpoint: str,
        fields: list[str],
        jq: str,
        github: GitHubRepo,
        account: LinuxAccount,
    ) -> object:
        try:
            result = await self.gh.run(
                [
                    "api",
                    "--hostname",
                    github.host,
                    "--method",
                    "GET",
                    endpoint,
                    *fields,
                    "--jq",
                    jq,
                ],
                account=account,
                hostname=github.host,
                timeout_s=self.policy.gh_timeout_s,
            )
        except Exception as error:
            logger.warning("gh api failed (%s)", error_class_of(error))
            raise ChangesNotReadError() from None
        if result.returncode != 0:
            raise ChangesNotReadError()
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise ChangesNotReadError() from None


def files_json(changes: PullRequestChanges) -> tuple[list[dict], list[str | None]]:
    """``files`` and ``patches`` of the stored row (the same order)."""
    files = [
        {
            "path": changed.path,
            "previous_path": changed.previous_path,
            "status": changed.status,
            "additions": changed.additions,
            "deletions": changed.deletions,
            "patch_truncated": changed.patch_truncated,
        }
        for changed in changes.files
    ]
    return files, [changed.patch for changed in changes.files]


class PullRequestChangeStore:
    """The stored changes of the pull request records (``pull_request_changes``)."""

    def __init__(self, database: Database) -> None:
        if not isinstance(database, Database):
            raise TypeError("database must be a Database")
        self._database = database

    async def record(
        self,
        task_id: uuid.UUID,
        attempt: int,
        repository_id: uuid.UUID,
        pull_request: PullRequestInfo,
        changes: PullRequestChanges,
    ) -> bool:
        """Store ``changes`` for the record of the repository in the attempt, if
        it still holds ``pull_request`` (replacing what was stored); ``False``
        when it does not."""
        if len(changes.files) > MAX_FILES:
            raise ValueError("too many files")
        files, patches = files_json(changes)
        records = TaskAttemptRepositoryRow
        async with self._database.session() as session, session.begin():
            record_id = (
                await session.execute(
                    select(records.id).where(
                        records.task_id == task_id,
                        records.attempt == attempt,
                        records.repository_id == repository_id,
                        records.pr_number == pull_request.number,
                    )
                )
            ).scalar_one_or_none()
            if record_id is None:
                return False
            values = {
                "head_commit": changes.head_commit,
                "truncated": changes.truncated,
                "files": files,
                "patches": patches,
            }
            statement = insert(PullRequestChangesRow).values(
                record_id=record_id, **values
            )
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=[PullRequestChangesRow.record_id],
                    set_={**values, "recorded_at": statement.excluded.recorded_at},
                )
            )
        return True


class ChangeRecorder:
    """What the Integration Gate calls once a pull request was recorded: read its
    changes and store them, within one deadline. Never raises (best effort: the
    task does not wait on it); ``True`` when they were stored."""

    def __init__(
        self,
        reader: object,
        store: object,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        require_async_method(reader, "read", 2)
        require_async_method(store, "record", 5)
        if isinstance(timeout_s, bool) or not (
            isinstance(timeout_s, int | float) and timeout_s > 0
        ):
            raise ValueError("timeout_s must be positive")
        self._reader = reader
        self._store = store
        self._timeout_s = float(timeout_s)

    async def record(
        self, request: PublishRequest, pull_request: PullRequestInfo
    ) -> bool:
        try:
            async with asyncio.timeout(self._timeout_s):
                changes = await self._reader.read(request, pull_request)
                return await self._store.record(
                    request.task.id,
                    request.run.attempt,
                    request.repository.repo_id,
                    pull_request,
                    changes,
                )
        except Exception as error:
            logger.warning(
                "Changes of a pull request not recorded (%s)", error_class_of(error)
            )
            return False
