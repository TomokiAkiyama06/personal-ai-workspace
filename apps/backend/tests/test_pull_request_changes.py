"""The changed files of a delivered pull request (issue #185 item 6, Decision 0078
Proposed): reading them from GitHub (``integration/changes.py``) and revision
0190.

GitHub's API is a fake ``GhRunner`` that answers ``pulls/<n>/files`` like ``gh
api``, applying ``--jq`` with the real ``jq`` (as gh does before printing) and
refusing an answer longer than ``SubprocessGhRunner``'s cap. No network, no
credential, no other Linux user. The store and the gate are covered on
PostgreSQL by ``test_pull_request_boards_api.py`` and ``test_worktrees_gate.py``.
"""

import asyncio
import importlib.util
import io
import json
import shutil
import subprocess
import unittest
import uuid
from types import SimpleNamespace

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text

from paw_backend.db import Base
from paw_backend.integration.changes import (
    LIST_PAGE_SIZE,
    MAX_FILES,
    MAX_PATCH_CHARS,
    MAX_PATCH_FILES,
    MAX_PATH_CHARS,
    ChangedFile,
    ChangeRecorder,
    ChangesNotReadError,
    GitHubChangeReader,
    PullRequestChanges,
    listing_bytes,
    listing_jq,
    patch_bytes,
    patch_jq,
    safe_patch,
)
from paw_backend.repositories import RepositoryPolicy
from paw_backend.repositories.errors import (
    GhCommandError,
    GhFailure,
    LinuxAccountUnavailableError,
)
from paw_backend.repositories.github_connection import GhResult
from paw_backend.repositories.limits import MAX_GH_OUTPUT_BYTES
from paw_backend.repositories.paths import LinuxAccount
from paw_backend.tasks import PullRequestInfo, PullRequestState, TaskRun
from paw_backend.tasks.models import MAX_PULL_REQUEST_FILES

from .memory_support import migrate as migrate_by_action
from .memory_support import sync_database_url
from .support import paw_environment
from .task_support import requires_postgres
from .test_migration_grants import VERSIONS
from .test_migrations import offline_config

HOST, OWNER, REPO = "github.com", "octo", "repo"
PULL_REQUEST = PullRequestInfo(
    7, f"https://{HOST}/{OWNER}/{REPO}/pull/7", PullRequestState.OPEN
)
HEAD = "c" * 40
REVISION = "0190"
requires_jq = unittest.skipUnless(shutil.which("jq"), "jq is not installed")


def github_file(name, *, patch="@@ -1 +1 @@\n-a\n+b\n", status="modified", **extra):
    item = {
        "sha": "f" * 40,
        "filename": name,
        "status": status,
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "blob_url": "https://example.invalid/blob",
    }
    if patch is not None:
        item["patch"] = patch
    item.update(extra)
    return item


class FakeGitHub:
    """``gh api repos/<o>/<r>/pulls/<n>/files`` for one pull request."""

    def __init__(self, files) -> None:
        self.files = list(files)
        self.calls: list[tuple[str, ...]] = []
        self.accounts: list = []
        self.fail = False
        self.raises: Exception | None = None
        self.on_call = None
        self.head = HEAD

    async def run(self, args, *, account, hostname, timeout_s):
        self.calls.append(tuple(args))
        self.accounts.append(account)
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if self.raises is not None:
            raise self.raises
        if self.fail:
            return GhResult(1, "")
        assert args[:5] == ["api", "--hostname", HOST, "--method", "GET"]
        assert hostname == HOST
        pull = f"repos/{OWNER}/{REPO}/pulls/{PULL_REQUEST.number}"
        jq = args[args.index("--jq") + 1]
        if args[5] == pull:
            answer = json.dumps(
                {"number": PULL_REQUEST.number, "head": {"sha": self.head}}
            )
            return await self._projected(answer, jq)
        assert args[5] == f"{pull}/files"
        fields = dict(args[i + 1].split("=", 1) for i in (6, 8))
        size, page = int(fields["per_page"]), int(fields["page"])
        answer = json.dumps(self.files[(page - 1) * size : page * size])
        return await self._projected(answer, jq)

    async def _projected(self, answer, jq):
        projected = await asyncio.to_thread(
            subprocess.run,
            ["jq", "-c", jq],
            input=answer,
            capture_output=True,
            text=True,
        )
        if projected.returncode != 0:
            return GhResult(1, "")
        if len(projected.stdout.encode()) > MAX_GH_OUTPUT_BYTES:
            raise GhCommandError("api", GhFailure.OUTPUT_TOO_LARGE)
        return GhResult(0, projected.stdout)

    def pages(self) -> list[tuple[str, str]]:
        return [(call[7], call[9]) for call in self.calls if call[5].endswith("/files")]

    def head_reads(self) -> int:
        return sum(1 for call in self.calls if not call[5].endswith("/files"))


class Accounts:
    def __init__(self, user_id) -> None:
        self.account = LinuxAccount(user_id, "alice", 1000, "/home/alice")

    async def account_of(self, user_id):
        if user_id != self.account.user_id:
            raise LinuxAccountUnavailableError()
        return self.account


@requires_jq
class ReaderTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user_id = uuid.uuid4()
        self.accounts = Accounts(self.user_id)
        self.github = FakeGitHub([github_file("src/app.py")])

    def reader(self, **options) -> GitHubChangeReader:
        return GitHubChangeReader(
            gh=self.github,
            accounts=options.pop("accounts", self.accounts),
            policy=RepositoryPolicy(),
        )

    def request(self, *, remotes=(f"https://{HOST}/{OWNER}/{REPO}.git",)):
        return SimpleNamespace(
            task=SimpleNamespace(id=uuid.uuid4(), created_by=self.user_id),
            run=TaskRun(1, 0),
            repository=SimpleNamespace(repo_id=uuid.uuid4(), remotes=remotes),
            target=SimpleNamespace(head=HEAD),
        )

    async def read(self, **options) -> PullRequestChanges:
        return await self.reader(**options).read(self.request(), PULL_REQUEST)

    async def not_read(self, request=None, **options):
        with self.assertRaises(ChangesNotReadError):
            await self.reader(**options).read(request or self.request(), PULL_REQUEST)

    async def test_the_files_with_their_patches_as_the_creator(self):
        self.github.files = [
            github_file("src/app.py"),
            github_file(
                "docs/new.md", status="renamed", previous_filename="docs/old.md"
            ),
            github_file("logo.png", patch=None, status="added"),
        ]

        changes = await self.read()

        self.assertEqual(changes.head_commit, HEAD)
        self.assertFalse(changes.truncated)
        self.assertEqual(
            changes.files,
            (
                ChangedFile(
                    "src/app.py", None, "modified", 1, 1, "@@ -1 +1 @@\n-a\n+b\n"
                ),
                ChangedFile(
                    "docs/new.md",
                    "docs/old.md",
                    "renamed",
                    1,
                    1,
                    "@@ -1 +1 @@\n-a\n+b\n",
                ),
                ChangedFile("logo.png", None, "added", 1, 1),
            ),
        )
        # One listing page, then one call for each file that has a patch.
        self.assertEqual(
            self.github.pages(),
            [
                (f"per_page={LIST_PAGE_SIZE}", "page=1"),
                ("per_page=1", "page=1"),
                ("per_page=1", "page=2"),
            ],
        )
        # The pull request's head is checked before and after.
        self.assertEqual(self.github.head_reads(), 2)
        self.assertEqual(
            {account.user_id for account in self.github.accounts}, {self.user_id}
        )

    async def test_a_pull_request_whose_head_is_not_the_checked_commit(self):
        # Codex review of #206: what is stored is labelled with the checked
        # commit, so a branch that moved before or during the reading is refused.
        self.github.head = "d" * 40
        await self.not_read()
        self.github.head = HEAD
        calls = len(self.github.calls)

        def move(number):
            if number == calls + 3:  # the last call: the head read after the files
                self.github.head = "d" * 40

        self.github.on_call = move
        await self.not_read()

    async def test_more_files_than_are_kept(self):
        self.github.files = [
            github_file(f"f{index}.py") for index in range(MAX_FILES + 1)
        ]

        changes = await self.read()

        self.assertTrue(changes.truncated)
        self.assertEqual(len(changes.files), MAX_FILES)
        self.assertEqual(
            sum(1 for changed in changes.files if changed.patch is not None),
            MAX_PATCH_FILES,
        )
        self.assertIsNone(changes.files[MAX_PATCH_FILES].patch)

    async def test_exactly_as_many_files_as_are_kept_is_not_truncated(self):
        self.github.files = [
            github_file(f"f{index}.py", patch=None) for index in range(MAX_FILES)
        ]
        changes = await self.read()
        self.assertFalse(changes.truncated)
        self.assertEqual(len(changes.files), MAX_FILES)

    async def test_a_long_patch_is_cut(self):
        self.github.files = [github_file("big.py", patch="+x\n" * MAX_PATCH_CHARS)]
        (changed,) = (await self.read()).files
        self.assertTrue(changed.patch_truncated)
        self.assertEqual(len(changed.patch), MAX_PATCH_CHARS)

    async def test_what_is_kept_is_safe_to_show(self):
        token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        self.github.files = [
            github_file(
                "a\x1bb.py",
                patch=f"@@ -1 +1 @@\n-x\n+TOKEN = '{token}'\n+\tz\x00\x1b[31m\n",
            )
        ]
        (changed,) = (await self.read()).files
        self.assertEqual(changed.path, "a\\u001bb.py")
        self.assertNotIn(token, changed.patch)
        self.assertIn("[REDACTED]", changed.patch)
        self.assertIn("+\tz\\u0000\\u001b[31m\n", changed.patch)

    async def test_a_pull_request_that_changed_meanwhile_is_not_read(self):
        self.github.files = [github_file("a.py"), github_file("b.py")]

        def change(number):
            if number == 3:  # after the head and the listing
                self.github.files.reverse()

        self.github.on_call = change
        await self.not_read()

    async def test_failures_are_not_read(self):
        self.github.fail = True
        await self.not_read()
        self.github.fail = False
        self.github.raises = GhCommandError("api", GhFailure.TIMEOUT)
        await self.not_read()
        self.github.raises = None
        await self.not_read(accounts=Accounts(uuid.uuid4()))
        await self.not_read(request=self.request(remotes=("https://gitlab.com/o/r",)))

    async def test_an_answer_that_is_not_github_s_is_not_read(self):
        for files in (
            [github_file("a.py", status="exploded")],
            [github_file("a.py", additions=-1)],
            [github_file("a.py", additions="1")],
            [github_file("")],
        ):
            with self.subTest(files=files):
                self.github.files = files
                await self.not_read()

    def test_every_answer_fits_in_gh_s_output(self):
        self.assertLessEqual(listing_bytes(), MAX_GH_OUTPUT_BYTES)
        self.assertLessEqual(patch_bytes(), MAX_GH_OUTPUT_BYTES)
        # The worst a real answer holds: names and patches longer than what is
        # kept, made of characters gh prints as 6 bytes.
        worst = github_file(
            "\x01" * (MAX_PATH_CHARS + 50),
            previous_filename="\x01" * (MAX_PATH_CHARS + 50),
            status="\x01" * 100,
            additions=2_147_483_647,
            deletions=2_147_483_647,
            patch="\x01" * (MAX_PATCH_CHARS + 50),
        )
        for jq, count in ((listing_jq(), LIST_PAGE_SIZE), (patch_jq(), 1)):
            projected = subprocess.run(
                ["jq", "-c", jq],
                input=json.dumps([worst] * count).encode(),
                capture_output=True,
                check=True,
            ).stdout
            self.assertLessEqual(len(projected), MAX_GH_OUTPUT_BYTES)


class SafePatchTest(unittest.TestCase):
    def test_tabs_and_line_feeds_stay(self):
        self.assertEqual(safe_patch("+\ta\n-b\r\n"), ("+\ta\n-b\\u000d\n", False))

    def test_escapes_do_not_grow_it_past_the_limit(self):
        # Codex review of #206: an escape makes one character six.
        shown, cut = safe_patch("\x01" * MAX_PATCH_CHARS)
        self.assertTrue(cut)
        self.assertEqual(len(shown), MAX_PATCH_CHARS)


class Reader:
    def __init__(self, answer=None, delay=0.0) -> None:
        self.answer = answer
        self.delay = delay

    async def read(self, request, pull_request):
        await asyncio.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class Store:
    def __init__(self, result=True) -> None:
        self.calls = []
        self.result = result

    async def record(self, task_id, attempt, repository_id, pull_request, changes):
        self.calls.append((task_id, attempt, repository_id, pull_request, changes))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RecorderTest(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return SimpleNamespace(
            task=SimpleNamespace(id=uuid.uuid4()),
            run=TaskRun(2, 1),
            repository=SimpleNamespace(repo_id=uuid.uuid4()),
        )

    async def test_what_is_read_is_stored_for_the_record(self):
        changes = PullRequestChanges(HEAD, (), False)
        store = Store()
        request = self.request()

        recorded = await ChangeRecorder(Reader(changes), store).record(
            request, PULL_REQUEST
        )

        self.assertTrue(recorded)
        self.assertEqual(
            store.calls,
            [
                (
                    request.task.id,
                    2,
                    request.repository.repo_id,
                    PULL_REQUEST,
                    changes,
                )
            ],
        )

    async def test_it_never_raises(self):
        changes = PullRequestChanges(HEAD, (), False)
        for reader, store in (
            (Reader(ChangesNotReadError()), Store()),
            (Reader(RuntimeError("boom")), Store()),
            (Reader(changes), Store(RuntimeError("db"))),
            (Reader(changes, delay=5), Store()),
        ):
            recorder = ChangeRecorder(reader, store, timeout_s=0.05)
            self.assertFalse(await recorder.record(self.request(), PULL_REQUEST))

    def test_it_is_built_with_what_it_needs(self):
        with self.assertRaises(TypeError):
            ChangeRecorder(object(), Store())
        with self.assertRaises(TypeError):
            ChangeRecorder(Reader(), object())
        for timeout in (0, -1, True):
            with self.assertRaises(ValueError):
                ChangeRecorder(Reader(), Store(), timeout_s=timeout)


def revision_module():
    spec = importlib.util.spec_from_file_location(
        "revision_0190", VERSIONS / "0190_pull_request_changes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def previous_revision() -> str:
    scripts = ScriptDirectory.from_config(offline_config(io.StringIO()))
    previous = scripts.get_revision(REVISION).down_revision
    assert isinstance(previous, str)
    return previous


class MigrationTest(unittest.TestCase):
    def sql(self, action: str, revisions: str, **environment: str) -> str:
        output = io.StringIO()
        with paw_environment(
            PAW_DATABASE_URL="postgresql://u:p@db.invalid/paw", **environment
        ):
            getattr(command, action)(offline_config(output), revisions, sql=True)
        return output.getvalue()

    def test_the_revision_writes_the_values_of_the_code(self):
        self.assertEqual(revision_module().MAX_FILES, MAX_PULL_REQUEST_FILES)

    def test_upgrade(self):
        sql = self.sql(
            "upgrade",
            f"{previous_revision()}:{REVISION}",
            PAW_APP_DATABASE_ROLE="paw_app",
        )
        self.assertIn("CREATE TABLE pull_request_changes", sql)
        self.assertIn(
            "REFERENCES task_attempt_repositories (id) ON DELETE CASCADE", sql
        )
        self.assertIn('GRANT INSERT, SELECT ON pull_request_changes TO "paw_app"', sql)
        self.assertIn(
            "GRANT UPDATE (head_commit, truncated, files, patches, recorded_at)"
            ' ON pull_request_changes TO "paw_app"',
            sql,
        )
        # Nothing else is granted: no DELETE, no other table.
        self.assertEqual(sql.count("GRANT "), 2)
        self.assertIn(
            "CREATE INDEX ix_audit_events_resource ON audit_events"
            " (resource_id, occurred_at)",
            sql,
        )
        self.assertIn(
            "CREATE INDEX ix_tool_approvals_pending_requester ON tool_approvals"
            " (requester_user_id, created_at) WHERE status = 'pending'",
            sql,
        )
        self.assertNotIn("ALTER TABLE audit_events", sql)

    def test_downgrade(self):
        sql = self.sql("downgrade", f"{REVISION}:{previous_revision()}")
        self.assertIn("DROP INDEX ix_tool_approvals_pending_requester", sql)
        self.assertIn("DROP INDEX ix_audit_events_resource", sql)
        self.assertIn("DROP TABLE pull_request_changes", sql)


_REVISION_INDEXES = ("ix_audit_events_resource", "ix_tool_approvals_pending_requester")


def only_revision_0190(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in ("pull_request_changes", "audit_events", "tool_approvals")
    if type_ == "index":
        return name in _REVISION_INDEXES
    table = getattr(obj, "table", None)
    return table is not None and table.name == "pull_request_changes"


@requires_postgres
class SchemaTest(unittest.TestCase):
    """At the head, the model and the migration agree (no drift)."""

    def setUp(self) -> None:
        migrate_by_action("upgrade", "head")
        self.engine = create_engine(sync_database_url())
        self.addCleanup(self.engine.dispose)

    def test_autogenerate_finds_no_difference(self):
        with self.engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "include_object": only_revision_0190,
                },
            )
            self.assertEqual(compare_metadata(context, Base.metadata), [])

    def test_the_drift_check_notices_a_missing_index(self):
        with self.engine.connect() as connection, connection.begin() as transaction:
            connection.execute(text("DROP INDEX ix_tool_approvals_pending_requester"))
            context = MigrationContext.configure(
                connection, opts={"include_object": only_revision_0190}
            )
            difference = compare_metadata(context, Base.metadata)
            transaction.rollback()
        self.assertEqual(
            [(step[0], step[1].name) for step in difference],
            [("add_index", "ix_tool_approvals_pending_requester")],
        )
