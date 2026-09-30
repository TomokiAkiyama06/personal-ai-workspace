"""Pushing the checked integration branch and opening its pull request (issue #132).

The real ``git`` pushes to a local bare repository that stands in for GitHub
(``World.runner``: ``https://github.com/`` is rewritten to a directory), as the
user running the tests (``SubprocessGitRunner``); GitHub's API is a fake
``GhRunner`` that answers like ``gh api``. No network, no credential, no other
Linux user.
"""

import json
import os
import unittest
import uuid
from types import SimpleNamespace

from paw_backend.authz import (
    Authorizer,
    Principal,
    ProjectState,
    RepoAcl,
    RepoPermission,
)
from paw_backend.authz.roles import ProjectRole, SystemRole
from paw_backend.integration import (
    GitHubPullRequestPublisher,
    IntegrationTarget,
    PublishProblem,
    PublishRequest,
    PullRequestNotPublishedError,
)
from paw_backend.integration.publish import (
    MAX_TITLE_CHARS,
    choose_pull_request,
    publisher_agent_id,
    pull_request_title,
    push_arguments,
)
from paw_backend.repositories import RepositoryPolicy
from paw_backend.repositories.errors import GhCommandError, GhFailure
from paw_backend.repositories.git import GitResult, command_name
from paw_backend.repositories.github_connection import GhResult
from paw_backend.tasks import PullRequestInfo, PullRequestState, RepoRole, TaskRun
from paw_backend.tools import ScopedRepository

from .auth_support import RecordingSink
from .authz_support import FailingSink, StaticDirectory
from .repositories_support import FakeAccounts, World, git, requires_git

RUN = TaskRun(1, 0)
HOST = "github.com"
OWNER, REPO = "octo", "repo"
REMOTE = f"https://{HOST}/{OWNER}/{REPO}.git"


class FakeGitHub:
    """``gh api`` for one repository: lists and creates pull requests.

    ``fail`` makes every call exit 1 (``gh``'s answer to an HTTP error);
    ``fail_create`` only the creation; ``raises`` makes ``run`` raise it."""

    def __init__(self, bare: str) -> None:
        self.bare = bare  # the "GitHub" repository: a head is its branch's tip
        self.pulls: list[dict] = []
        self.calls: list[tuple[str, ...]] = []
        self.accounts: list = []
        self.fail = False
        self.fail_create = False
        self.raises: Exception | None = None
        self.answer: object | None = None  # replaces the created pull request

    def pull(
        self,
        number,
        branch,
        *,
        state="open",
        draft=False,
        merged=False,
        base="main",
        sha=None,
    ):
        live = sha is None and state == "open"
        if sha is None:  # the tip of the branch on "GitHub" now
            sha = git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}",
                      cwd=self.bare, check=False) or "e" * 40  # fmt: skip
        return {
            "base": {"ref": base},
            "number": number,
            "html_url": f"https://{HOST}/{OWNER}/{REPO}/pull/{number}",
            "state": state,
            "draft": draft,
            "merged_at": "2026-09-29T00:00:00Z" if merged else None,
            # An open pull request follows its branch, as on GitHub.
            "live": live,
            "head": {
                "ref": branch,
                "sha": sha,
                "repo": {"full_name": f"{OWNER}/{REPO}"},
            },
        }

    async def run(self, args, *, account, hostname, timeout_s):
        self.calls.append(tuple(args))
        self.accounts.append(account)
        if self.raises is not None:
            raise self.raises
        if self.fail:
            return GhResult(1, "")
        assert args[:3] == ["api", "--hostname", HOST] and hostname == HOST
        method, endpoint = args[4], args[5]
        assert endpoint == f"repos/{OWNER}/{REPO}/pulls"
        fields = dict(args[i + 1].split("=", 1) for i in range(6, len(args), 2))
        if method == "GET":
            owner, _, branch = fields["head"].partition(":")
            assert owner == OWNER and fields["state"] == "all"
            found = [p for p in self.pulls if p["head"]["ref"] == branch]
            for pull in found:
                if pull["live"]:
                    pull["head"]["sha"] = self.pull(0, branch)["head"]["sha"]
            return GhResult(0, json.dumps(found))
        assert method == "POST"
        if self.fail_create:
            return GhResult(1, "")
        created = self.pull(len(self.pulls) + 1, fields["head"])
        created["title"], created["body"] = fields["title"], fields["body"]
        created["base"] = {"ref": fields["base"]}
        self.pulls.append(created)
        return GhResult(0, json.dumps(created if self.answer is None else self.answer))

    def methods(self) -> list[str]:
        return [call[4] for call in self.calls]


class RecordingRunner:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple[str, ...]] = []
        self.push_result: GitResult | None = None

    async def run(self, args, *, account, cwd, timeout_s, ceiling=None):
        self.calls.append(tuple(args))
        if self.push_result is not None and command_name(args) == "push":
            return self.push_result
        return await self.inner.run(
            args, account=account, cwd=cwd, timeout_s=timeout_s, ceiling=ceiling
        )

    def subcommands(self) -> list[str]:
        return [command_name(args) for args in self.calls]


@requires_git
class PublisherTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.close)
        self.accounts = FakeAccounts(self.world)
        self.user_id = uuid.uuid4()
        self.account = self.accounts.add(self.user_id, "alice")
        self.project_id = uuid.uuid4()
        self.repo_id = uuid.uuid4()
        self.task = SimpleNamespace(
            id=uuid.uuid4(), created_by=self.user_id, title="Fix the parser"
        )
        self.bare = self.world.make_bare(OWNER, REPO)
        self.checkout = f"{self.account.home}/workspaces/project/{REPO}"
        os.makedirs(os.path.dirname(self.checkout))
        git("clone", "--quiet", self.bare, self.checkout)
        self.main = git("rev-parse", "HEAD", cwd=self.checkout)
        self.branch = f"paw/{self.task.id}/1/_integration"
        self.integration = f"{self.world.root}/integration"
        git("worktree", "add", "--quiet", "-b", self.branch, self.integration,
            cwd=self.checkout)  # fmt: skip
        self.head = self.commit("change.txt", "integrated\n")
        self.sink = RecordingSink()
        self.directory = StaticDirectory(self.principal(ProjectRole.CONTRIBUTOR))
        self.github = FakeGitHub(self.bare)
        self.runner = RecordingRunner(self.world.runner())

    def commit(self, name, content):
        with open(f"{self.integration}/{name}", "w") as handle:
            handle.write(content)
        git("add", "-A", cwd=self.integration)
        git("commit", "--quiet", "-m", "merge", cwd=self.integration)
        return git("rev-parse", "HEAD", cwd=self.integration)

    def principal(self, role):
        return Principal(self.user_id, SystemRole.USER, {self.project_id: role})

    def publisher(self, **options):
        return GitHubPullRequestPublisher(
            runner=options.pop("runner", self.runner),
            gh=options.pop("gh", self.github),
            accounts=options.pop("accounts", self.accounts),
            policy=options.pop("policy", RepositoryPolicy()),
            authorizer=options.pop(
                "authorizer",
                Authorizer(options.pop("sink", self.sink), directory=self.directory),
            ),
            **options,
        )

    def request(self, **overrides):
        repository = ScopedRepository(
            self.repo_id,
            self.project_id,
            self.checkout,
            overrides.pop("acl", RepoAcl.inherit(self.repo_id, self.project_id)),
            remotes=overrides.pop(
                "remotes", (f"https://{HOST}/{OWNER}/{REPO}", REMOTE)
            ),
            role=overrides.pop("role", RepoRole.TARGET),
        )
        target = IntegrationTarget(
            self.repo_id,
            self.integration,
            overrides.pop("branch", self.branch),
            overrides.pop("head", self.head),
            True,
        )
        arguments = {
            "task": self.task,
            "run": RUN,
            "repository": repository,
            "project_state": ProjectState.ACTIVE,
            "target": target,
            "checks": (("test", 1), ("evaluator", 1), ("review", 2)),
        }
        arguments.update(overrides)
        return PublishRequest(**arguments)

    def remote_branch(self, branch=None) -> str | None:
        found = git("rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch or self.branch}", cwd=self.bare,
                    check=False)  # fmt: skip
        return found or None

    async def refused(self, problem, publisher=None, request=None):
        with self.assertRaises(PullRequestNotPublishedError) as caught:
            await (publisher or self.publisher()).publish(request or self.request())
        self.assertEqual(caught.exception.problem, problem)

    # -- the path that works ------------------------------------------------------

    async def test_the_checked_commit_is_pushed_and_its_pull_request_opened(self):
        pull_request = await self.publisher().publish(self.request())

        self.assertEqual(
            pull_request,
            PullRequestInfo(
                1, f"https://{HOST}/{OWNER}/{REPO}/pull/1", PullRequestState.OPEN
            ),
        )
        # GitHub (the bare repository) has the checked commit on the paw/ branch,
        # and its default branch did not move.
        self.assertEqual(self.remote_branch(), self.head)
        self.assertEqual(self.remote_branch("main"), self.main)
        (created,) = self.github.pulls
        self.assertEqual(created["head"]["ref"], self.branch)
        self.assertEqual(created["base"]["ref"], "main")
        self.assertEqual(created["title"], "[PAW] Fix the parser")
        body = created["body"]
        self.assertIn(str(self.task.id), body)
        self.assertIn(self.head, body)
        for row in ("| test | passed (1) |", "| review | passed (2) |"):
            self.assertIn(row, body)
        # As the task creator's own account, never another.
        self.assertTrue(all(a is self.account for a in self.github.accounts))
        self.assertEqual(self.github.methods(), ["GET", "POST"])

    async def test_the_decision_is_audited_as_the_creators_agent(self):
        await self.publisher().publish(self.request())

        (event,) = self.sink.events
        self.assertEqual(event.action, "project.pr.create")
        self.assertEqual(event.decision, "allow")
        self.assertEqual(event.actor_id, self.user_id)
        self.assertEqual(event.agent_id, publisher_agent_id(self.task.id, RUN))
        self.assertEqual(event.repo_id, self.repo_id)
        self.assertEqual(event.project_id, self.project_id)

    async def test_the_push_has_the_one_fixed_form(self):
        await self.publisher(gh_executable="/opt/gh/bin/gh").publish(self.request())

        (push,) = [call for call in self.runner.calls if command_name(call) == "push"]
        self.assertEqual(
            list(push),
            [
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!/opt/gh/bin/gh auth git-credential",
                "push",
                "--quiet",
                "--no-follow-tags",
                "--no-recurse-submodules",
                "--",
                REMOTE,
                f"{self.head}:refs/heads/{self.branch}",
            ],
        )
        self.assertEqual(
            list(push), push_arguments("/opt/gh/bin/gh", REMOTE, self.head, self.branch)
        )
        # Nothing but reads, the push and nothing that moves a local branch.
        self.assertEqual(set(self.runner.subcommands()), {"symbolic-ref", "push"})

    async def test_a_url_rewrite_of_the_checkout_never_redirects_the_push(self):
        # Codex review of #159: the checkout's own ``url.<base>.pushInsteadOf``
        # (or ``insteadOf``) would send the checked commit elsewhere; the local
        # runner refuses the push, as the SSH wrapper does (``redirects_push``).
        elsewhere = self.world.make_bare("elsewhere", REPO)
        for variable in ("pushInsteadOf", "insteadOf"):
            with self.subTest(variable=variable):
                key = f"url.file://{self.world.bare_root}/elsewhere/.{variable}"
                git("config", key, f"https://{HOST}/{OWNER}/", cwd=self.checkout)
                try:
                    await self.refused(PublishProblem.PUSH_FAILED)
                finally:
                    git("config", "--unset", key, cwd=self.checkout)
                found = git("rev-parse", "--verify", "--quiet",
                            f"refs/heads/{self.branch}", cwd=elsewhere,
                            check=False)  # fmt: skip
                self.assertEqual(found, "")
                self.assertIsNone(self.remote_branch())
                self.assertEqual(self.github.calls, [])

    async def test_a_rewrite_in_an_included_file_is_refused_too(self):
        elsewhere = self.world.make_bare("elsewhere", REPO)
        included = f"{self.world.root}/rewrite.config"
        key = f"url.file://{self.world.bare_root}/elsewhere/.pushInsteadOf"
        git("config", "--file", included, key, f"https://{HOST}/{OWNER}/")
        git("config", "include.path", included, cwd=self.checkout)

        await self.refused(PublishProblem.PUSH_FAILED)

        found = git("rev-parse", "--verify", "--quiet", f"refs/heads/{self.branch}",
                    cwd=elsewhere, check=False)  # fmt: skip
        self.assertEqual(found, "")

    async def test_configured_follow_tags_never_pushes_a_tag(self):
        # Codex review of #159: ``push.followTags=true`` in the checkout would
        # also push an annotated tag of the checked commit (outside ``paw/``).
        git("config", "push.followTags", "true", cwd=self.checkout)
        git("tag", "-a", "-m", "release", "v9", self.head, cwd=self.checkout)

        await self.publisher().publish(self.request())

        self.assertEqual(self.remote_branch(), self.head)
        self.assertEqual(git("tag", "--list", cwd=self.bare), "")

    async def test_a_commit_made_after_the_checks_is_not_pushed(self):
        checked = self.head
        self.commit("later.txt", "not checked\n")

        await self.publisher().publish(self.request(head=checked))

        self.assertEqual(self.remote_branch(), checked)

    async def test_publishing_again_reuses_the_pull_request(self):
        first = await self.publisher().publish(self.request())
        second = await self.publisher().publish(self.request())

        self.assertEqual(first, second)
        self.assertEqual(len(self.github.pulls), 1)
        self.assertEqual(self.github.methods(), ["GET", "POST", "GET"])

    async def test_a_newer_checked_commit_updates_the_branch_of_the_pull_request(self):
        await self.publisher().publish(self.request())
        newer = self.commit("more.txt", "more\n")

        await self.publisher().publish(self.request(head=newer))

        self.assertEqual(self.remote_branch(), newer)
        self.assertEqual(len(self.github.pulls), 1)

    async def test_an_existing_pull_request_is_recorded_as_it_is(self):
        cases = {
            PullRequestState.OPEN: {"sha": self.head},
            PullRequestState.DRAFT: {"draft": True, "sha": self.head},
            # Merged with the checked commit as its head.
            PullRequestState.MERGED: {
                "state": "closed",
                "merged": True,
                "sha": self.head,
            },
            # A human closed it: it is not replaced by a new one.
            PullRequestState.CLOSED: {"state": "closed"},
        }
        for state, fields in cases.items():
            with self.subTest(state=state.value):
                self.github.pulls = [self.github.pull(7, self.branch, **fields)]
                self.github.calls.clear()

                pull_request = await self.publisher().publish(self.request())

                self.assertEqual(pull_request.number, 7)
                self.assertEqual(pull_request.state, state)
                self.assertEqual(self.github.methods(), ["GET"])

    async def test_a_pull_request_to_another_base_is_not_the_one(self):
        # Codex review of #159: a pull request of the branch against another
        # branch than the default one does not propose the checked changes to
        # it; a new one is made against the default branch.
        self.github.pulls = [self.github.pull(5, self.branch, base="dev")]

        pull_request = await self.publisher().publish(self.request())

        self.assertEqual(pull_request.number, 2)
        self.assertEqual(self.github.pulls[-1]["base"]["ref"], "main")
        self.assertEqual(self.github.methods(), ["GET", "POST"])

    async def test_a_merged_pull_request_of_an_older_commit_is_not_the_one(self):
        # Codex review of #159: the branch advanced after its pull request was
        # merged; the newly checked commit is not in that pull request, so a
        # new one proposes it.
        older = self.head
        self.github.pulls = [
            self.github.pull(5, self.branch, state="closed", merged=True, sha=older)
        ]
        newer = self.commit("more.txt", "more\n")

        pull_request = await self.publisher().publish(self.request(head=newer))

        self.assertEqual(pull_request.number, 2)
        self.assertEqual(pull_request.state, PullRequestState.OPEN)
        self.assertEqual(self.remote_branch(), newer)

    async def test_an_open_pull_request_of_another_head_is_not_delivered(self):
        # Codex review of #159: the remote branch moved after the push (another
        # writer): the pull request proposes a commit that was not checked.
        for fields in ({}, {"draft": True}):
            with self.subTest(fields=fields):
                self.github.pulls = [
                    self.github.pull(5, self.branch, sha="d" * 40, **fields)
                ]
                await self.refused(PublishProblem.BRANCH_MOVED)
        self.github.pulls = []
        self.github.answer = self.github.pull(1, self.branch, sha="d" * 40)
        await self.refused(PublishProblem.BRANCH_MOVED)

    async def test_a_pull_request_without_a_head_commit_is_refused(self):
        self.github.answer = self.github.pull(1, self.branch, sha="not a sha")
        await self.refused(PublishProblem.INVALID_RESPONSE)

    async def test_a_created_pull_request_to_another_base_is_refused(self):
        self.github.answer = self.github.pull(1, self.branch, base="dev")
        await self.refused(PublishProblem.INVALID_RESPONSE)

    def test_the_open_pull_request_is_chosen_among_several(self):
        def info(number, state):
            return PullRequestInfo(number, f"https://x/{number}", state)

        found = [
            info(1, PullRequestState.CLOSED),
            info(2, PullRequestState.DRAFT),
            info(3, PullRequestState.OPEN),
        ]
        self.assertEqual(choose_pull_request(found).number, 3)
        self.assertEqual(choose_pull_request(found[:2]).number, 2)
        self.assertIsNone(choose_pull_request([]))

    async def test_a_pull_request_made_meanwhile_is_found_after_a_refused_create(self):
        self.github.fail_create = True
        publisher = self.publisher()
        original = self.github.run

        async def create_then_refuse(args, **options):
            if args[4] == "POST":
                self.github.pulls.append(self.github.pull(3, self.branch))
            return await original(args, **options)

        self.github.run = create_then_refuse

        pull_request = await publisher.publish(self.request())

        self.assertEqual(pull_request.number, 3)

    # -- refusals -----------------------------------------------------------------

    async def test_only_a_target_gets_a_pull_request(self):
        for role in (RepoRole.WORKING, RepoRole.REFERENCED, None):
            with self.subTest(role=role):
                await self.refused(
                    PublishProblem.NOT_A_TARGET, request=self.request(role=role)
                )
        self.assertIsNone(self.remote_branch())
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.sink.events, [])

    async def test_only_the_integration_branch_of_the_run_is_published(self):
        for branch in ("main", f"paw/{self.task.id}/2/_integration",
                       f"paw/{self.task.id}/1/worker"):  # fmt: skip
            with self.subTest(branch=branch):
                await self.refused(
                    PublishProblem.NOT_THE_INTEGRATION,
                    request=self.request(branch=branch),
                )
        await self.refused(
            PublishProblem.NOT_THE_INTEGRATION, request=self.request(head="HEAD")
        )
        self.assertIsNone(self.remote_branch())
        self.assertEqual(self.remote_branch("main"), self.main)

    async def test_a_repository_without_a_github_remote_is_refused(self):
        for remotes in ((), ("https://gitlab.example/octo/repo.git",)):
            with self.subTest(remotes=remotes):
                await self.refused(
                    PublishProblem.NO_GITHUB_REMOTE,
                    request=self.request(remotes=remotes),
                )
        self.assertEqual(self.github.calls, [])

    async def test_a_creator_who_may_not_create_pull_requests_is_refused(self):
        self.directory.principals[self.user_id] = self.principal(ProjectRole.VIEWER)

        await self.refused(PublishProblem.NOT_AUTHORIZED)

        (event,) = self.sink.events
        self.assertEqual((event.action, event.decision), ("project.pr.create", "deny"))
        self.assertIsNone(self.remote_branch())
        self.assertEqual(self.github.calls, [])

    async def test_a_repository_acl_that_keeps_agents_out_is_refused(self):
        acl = RepoAcl.override(
            self.repo_id, self.project_id, {RepoPermission.READ, RepoPermission.WRITE}
        )
        await self.refused(PublishProblem.NOT_AUTHORIZED, request=self.request(acl=acl))
        self.assertIsNone(self.remote_branch())

    async def test_an_archived_or_unknown_project_is_refused(self):
        for state in (ProjectState.ARCHIVED, None):
            with self.subTest(state=state):
                await self.refused(
                    PublishProblem.NOT_AUTHORIZED,
                    request=self.request(project_state=state),
                )
        self.assertIsNone(self.remote_branch())

    async def test_a_creator_who_is_gone_is_refused(self):
        self.directory.principals.clear()
        await self.refused(PublishProblem.NOT_AUTHORIZED)
        self.assertIsNone(self.remote_branch())

    async def test_nothing_is_pushed_without_a_record_of_the_decision(self):
        await self.refused(
            PublishProblem.NOT_AUTHORIZED, publisher=self.publisher(sink=FailingSink())
        )
        self.assertIsNone(self.remote_branch())

    async def test_a_creator_without_a_linux_account_is_refused(self):
        self.accounts.accounts.clear()
        await self.refused(PublishProblem.ACCOUNT_UNAVAILABLE)

    async def test_a_remote_branch_that_moved_elsewhere_is_never_forced(self):
        other = f"{self.world.root}/other"
        git("clone", "--quiet", self.bare, other)
        git("commit", "--quiet", "--allow-empty", "-m", "elsewhere", cwd=other)
        git("push", "--quiet", "origin", f"HEAD:refs/heads/{self.branch}", cwd=other)
        elsewhere = self.remote_branch()

        await self.refused(PublishProblem.PUSH_FAILED)

        self.assertEqual(self.remote_branch(), elsewhere)
        self.assertEqual(self.github.calls, [])

    async def test_a_git_failure_is_a_push_failure(self):
        self.runner.push_result = GitResult(128, "fatal: secret-ish detail")
        with self.assertLogs("paw_backend.integration.publish", "WARNING") as logs:
            await self.refused(PublishProblem.PUSH_FAILED)
        self.assertNotIn("secret-ish", "\n".join(logs.output))
        self.assertEqual(self.github.calls, [])

    async def test_github_failures_are_closed_problems(self):
        self.github.fail = True
        await self.refused(PublishProblem.GITHUB_FAILED)
        self.github.fail = False
        self.github.fail_create = True
        await self.refused(PublishProblem.GITHUB_FAILED)
        self.github.fail_create = False
        # gh itself could not run as the account (Decision 0029 does not switch
        # gh's identity: another Linux user is refused, fail closed).
        self.github.raises = GhCommandError("api", GhFailure.IDENTITY_MISMATCH)
        await self.refused(PublishProblem.GITHUB_FAILED)

    async def test_an_answer_that_is_not_the_pull_request_of_the_branch_is_refused(
        self,
    ):
        bad = {
            "another branch": self.github.pull(1, "paw/other/1/_integration"),
            "another repository": {
                **self.github.pull(1, self.branch),
                "html_url": f"https://{HOST}/evil/{REPO}/pull/1",
            },
            "a fork's branch": {
                **self.github.pull(1, self.branch),
                "head": {"ref": self.branch, "repo": {"full_name": f"evil/{REPO}"}},
            },
            "no number": {**self.github.pull(1, self.branch), "number": "1"},
            "an unknown state": {**self.github.pull(1, self.branch), "state": "x"},
            "not an object": [1],
        }
        for name, answer in bad.items():
            with self.subTest(answer=name):
                self.github.pulls.clear()
                self.github.answer = answer
                await self.refused(PublishProblem.INVALID_RESPONSE)

    async def test_an_answer_that_is_not_json_is_refused(self):
        async def garbage(args, **options):
            return GhResult(0, "not json")

        self.github.run = garbage
        await self.refused(PublishProblem.INVALID_RESPONSE)

    # -- the text -----------------------------------------------------------------

    def test_the_title_is_one_line_within_githubs_limit(self):
        task = SimpleNamespace(title="Fix\nthe   parser\t" + "x" * 400)
        title = pull_request_title(task)
        self.assertTrue(title.startswith("[PAW] Fix the parser x"))
        self.assertEqual(len(title), MAX_TITLE_CHARS)
        self.assertNotIn("\n", title)

    async def test_what_a_check_said_is_never_published(self):
        await self.publisher().publish(self.request())
        (created,) = self.github.pulls
        self.assertNotIn("summary", created["body"])
