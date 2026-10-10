"""Task-scoped approval grants: "このタスクの間は許可" (Decision 0085).

What a grant covers (``grant_pattern.py``), what can never be granted, how the
broker uses one (in memory; ``test_task_grants_postgres.py`` runs the store on
PostgreSQL) and the human side (``ApprovalService.approve_for_task`` /
``revoke_grant`` / the end of the task).
"""

import unittest
import uuid

from paw_backend.authz import Capability, InMemoryAuditSink, SystemRole
from paw_backend.tools import (
    WORKING_SET_TOOL_SPECS,
    ApprovalLevel,
    ApprovalOutcome,
    ApprovalService,
    ApprovalStatus,
    ArgumentKind,
    ArgumentSpec,
    BrokerReason,
    Environment,
    GrantArgument,
    GrantMatch,
    GrantPattern,
    GrantUse,
    GrantUseOutcome,
    InMemoryApprovalStore,
    InMemoryTaskGrantStore,
    ScopeStatus,
    TaskActivity,
    TaskGrantStatus,
    TaskRun,
    ToolCapability,
    ToolSpec,
    Verdict,
    grant_pattern_of,
    grantable,
)

from .authz_support import AGENT, SECRET, FailingSink, principal
from .tools_support import (
    P1,
    REPO,
    RUN,
    TASK,
    U1,
    U2,
    FakeTaskActivity,
    Harness,
    make_call,
    make_context,
    sample_registry,
)

R = BrokerReason
C = ToolCapability
A = ArgumentKind
U3 = uuid.UUID(int=3)
FETCH_DOCS = {"url": "https://docs.example.org/guide"}
INSTALL = {"package": "ripgrep"}


def spec(name: str) -> ToolSpec:
    found = sample_registry().get(name)
    assert found is not None
    return found


def pattern(name: str, values: dict, scope=ScopeStatus.IN_SCOPE) -> GrantPattern:
    made = grant_pattern_of(spec(name), values, scope, ApprovalLevel.APPROVAL)
    assert made is not None
    return made


class SequenceActivity(FakeTaskActivity):
    """Answers ``ACTIVE`` until told otherwise by ``then``, for the n-th question on."""

    def __init__(self) -> None:
        super().__init__(TaskActivity.ACTIVE)
        self.later: tuple[int, TaskActivity] | None = None

    def then(self, after: int, answer: TaskActivity) -> None:
        self.later = (len(self.checks) + after, answer)

    async def check(self, task_id, run):
        await super().check(task_id, run)
        if self.later is not None and len(self.checks) > self.later[0]:
            return self.later[1]
        return TaskActivity.ACTIVE


class FailingGrants(InMemoryTaskGrantStore):
    async def active_grants(self, *args):
        raise ConnectionError(SECRET)


class GrantPatternTest(unittest.TestCase):
    """What "the same or narrower" means, argument by argument."""

    def test_a_path_covers_itself_and_what_lies_below_it_only(self):
        granted = GrantPattern(
            ScopeStatus.IN_SCOPE,
            (GrantArgument("path", A.PATH, GrantMatch.UNDER, "/w/a"),),
        )
        for path, covered in (
            ("/w/a", True),
            ("/w/a/b/c.txt", True),
            ("/w/a-b", False),
            ("/w/ab", False),
            ("/w", False),
            ("/x/a", False),
        ):
            with self.subTest(path=path):
                call = GrantPattern(
                    ScopeStatus.IN_SCOPE,
                    (GrantArgument("path", A.PATH, GrantMatch.UNDER, path),),
                )
                self.assertIs(granted.covers(call), covered)

    def test_a_url_without_a_query_covers_the_urls_below_it_without_a_query(self):
        granted = pattern("web.fetch", FETCH_DOCS, ScopeStatus.HOST_OUT_OF_SCOPE)
        for url, covered in (
            ("https://docs.example.org/guide", True),
            ("https://docs.example.org/guide/", True),
            ("https://docs.example.org/guide/install", True),
            ("https://docs.example.org/guide/install?page=2", False),
            ("https://docs.example.org/guide-old", False),
            ("https://docs.example.org/", False),
            ("https://docs.example.org/guide/%2e%2e/admin", False),
            ("https://docs.example.org/guide/../admin", False),
            ("https://docs.example.org/guide/..;/admin", False),
            ("https://evil.example.org/guide", False),
            ("http://docs.example.org/guide", False),
        ):
            with self.subTest(url=url):
                call = pattern("web.fetch", {"url": url}, ScopeStatus.HOST_OUT_OF_SCOPE)
                self.assertIs(granted.covers(call), covered)

    def test_a_url_with_a_query_covers_only_itself(self):
        url = "https://docs.example.org/search?q=a"
        granted = pattern("web.fetch", {"url": url}, ScopeStatus.HOST_OUT_OF_SCOPE)
        self.assertIs(granted.arguments[0].match, GrantMatch.DIGEST)
        self.assertTrue(
            granted.covers(
                pattern("web.fetch", {"url": url}, ScopeStatus.HOST_OUT_OF_SCOPE)
            )
        )
        self.assertFalse(
            granted.covers(
                pattern(
                    "web.fetch",
                    {"url": "https://docs.example.org/search?q=b"},
                    ScopeStatus.HOST_OUT_OF_SCOPE,
                )
            )
        )

    def test_text_is_compared_whole_and_never_kept(self):
        granted = pattern("host.install_package", INSTALL)
        self.assertTrue(granted.covers(pattern("host.install_package", INSTALL)))
        self.assertFalse(
            granted.covers(pattern("host.install_package", {"package": "ripgrep2"}))
        )
        self.assertNotIn("ripgrep", str(granted.to_json()))

    def test_integers_and_booleans_must_be_equal_and_present_alike(self):
        flag = ToolSpec(
            "flag.set",
            frozenset({C.EXECUTE}),
            Capability.PROJECT_TASK_RUN,
            {
                "enabled": ArgumentSpec(A.BOOLEAN),
                "count": ArgumentSpec(A.INTEGER, required=False, minimum=0, maximum=9),
            },
            environment=Environment.HOST,
        )

        def made(values):
            return grant_pattern_of(
                flag, values, ScopeStatus.IN_SCOPE, ApprovalLevel.APPROVAL
            )

        granted = made({"enabled": True, "count": 1})
        self.assertTrue(granted.covers(made({"enabled": True, "count": 1})))
        self.assertFalse(granted.covers(made({"enabled": False, "count": 1})))
        self.assertFalse(granted.covers(made({"enabled": True, "count": 2})))
        # an optional argument left out is not "narrower": it may mean a default
        self.assertFalse(granted.covers(made({"enabled": True})))
        self.assertFalse(made({"enabled": True}).covers(granted))

    def test_the_scope_status_may_only_narrow(self):
        outside = pattern(
            "host.install_package", INSTALL, ScopeStatus.HOST_OUT_OF_SCOPE
        )
        inside = pattern("host.install_package", INSTALL, ScopeStatus.IN_SCOPE)
        self.assertTrue(outside.covers(inside))
        self.assertFalse(inside.covers(outside))
        self.assertIsNone(
            grant_pattern_of(
                spec("host.install_package"),
                INSTALL,
                ScopeStatus.OUT_OF_SCOPE,
                ApprovalLevel.APPROVAL,
            )
        )

    def test_a_pattern_round_trips_and_garbage_is_refused(self):
        made = pattern("web.fetch", FETCH_DOCS, ScopeStatus.HOST_OUT_OF_SCOPE)
        self.assertEqual(GrantPattern.from_json(made.to_json()), made)
        for garbage in (
            None,
            [],
            {"v": 2, "scope": "in_scope", "arguments": []},
            {"v": 1, "scope": "out_of_scope", "arguments": []},
            {"v": 1, "scope": "in_scope", "arguments": [{"name": "x"}]},
            {
                "v": 1,
                "scope": "in_scope",
                "arguments": [
                    {"name": "x", "kind": "text", "match": "digest", "value": "short"}
                ],
            },
        ):
            with self.subTest(garbage=garbage), self.assertRaises(ValueError):
                GrantPattern.from_json(garbage)


class GrantableTest(unittest.TestCase):
    """Decision 0085, section 2: what is never granted for a task."""

    def test_reads_and_host_commands_at_approval_can_be_granted(self):
        for name in ("web.fetch", "host.install_package", "repo.read_file"):
            with self.subTest(name=name):
                self.assertTrue(grantable(spec(name), ApprovalLevel.APPROVAL))

    def test_a_strong_approval_is_never_granted(self):
        self.assertFalse(
            grantable(spec("host.install_package"), ApprovalLevel.STRONG_APPROVAL)
        )
        self.assertFalse(grantable(spec("git.merge"), ApprovalLevel.STRONG_APPROVAL))

    def test_deletion_credentials_external_sends_and_administration_are_not(self):
        for name in (
            "repo.delete_tree",  # destructive
            "git.push",  # credential-use, a credential handle
            "git.merge",  # credential-use (and strong)
            "issues.create",  # network + write: an external send
            "admin.set_role",  # admin.*
        ):
            with self.subTest(name=name):
                self.assertFalse(grantable(spec(name), ApprovalLevel.APPROVAL))

    def test_working_set_and_permission_changes_are_not(self):
        for working_set in WORKING_SET_TOOL_SPECS:
            with self.subTest(tool=working_set.name):
                self.assertFalse(grantable(working_set, ApprovalLevel.APPROVAL))
        for capability in (
            Capability.PROJECT_MEMBERS_MANAGE,
            Capability.PROJECT_SETTINGS_MANAGE,
            Capability.PROJECT_AGENT_POLICY_MANAGE,
            Capability.PROJECT_REPO_ADD,
            Capability.ADMIN_PERMISSIONS_MANAGE,
            Capability.OWNER_ADMINS_MANAGE,
        ):
            members = ToolSpec(
                "acl.change",
                frozenset({C.READ}),
                capability,
                {"project": ArgumentSpec(A.PROJECT)},
                environment=Environment.HOST,
            )
            with self.subTest(capability=capability):
                self.assertFalse(grantable(members, ApprovalLevel.APPROVAL))


class BrokerGrantTest(unittest.IsolatedAsyncioTestCase):
    """The broker runs what a grant covers, and asks a human for the rest."""

    async def asyncSetUp(self):
        self.h = Harness(with_grants=True)
        self.person = principal(SystemRole.USER, U1)

    async def request(self, tool, arguments, **kw):
        return await self.h.broker.request(make_call(tool, arguments, **kw))

    async def grant(self, tool="web.fetch", arguments=FETCH_DOCS):
        asked = await self.request(tool, arguments)
        self.assertEqual(asked.verdict, Verdict.NEEDS_APPROVAL)
        result = await self.h.service.approve_for_task(asked.approval_id, self.person)
        self.assertEqual(result.outcome, ApprovalOutcome.APPROVED_FOR_TASK)
        return asked, result

    async def test_the_request_carries_the_pattern_only_when_it_can_be_granted(self):
        for tool, arguments, grantable_call in (
            ("web.fetch", FETCH_DOCS, True),
            ("host.install_package", INSTALL, True),
            ("repo.delete_tree", {"path": "/srv/paw-test/worktree/build"}, False),
            (
                "issues.create",
                {
                    "url": "https://example.org/issues",
                    "repository": str(REPO),
                    "title": "t",
                },
                False,
            ),
        ):
            with self.subTest(tool=tool):
                asked = await self.request(tool, arguments)
                record = await self.h.approvals.get(asked.approval_id)
                self.assertIs(record.grant_pattern is not None, grantable_call)

    async def test_a_granted_call_and_narrower_ones_run_without_asking_again(self):
        asked, granted = await self.grant()
        # the waiting call uses its own approval once, as before
        used = await self.h.broker.request(
            make_call("web.fetch", FETCH_DOCS), approval_id=asked.approval_id
        )
        self.assertEqual(used.reason, R.APPROVAL_CONSUMED)
        approvals_before = len(self.h.approvals._records)
        for url in (
            "https://docs.example.org/guide",
            "https://docs.example.org/guide/install",
        ):
            with self.subTest(url=url):
                correlation = uuid.uuid4()
                decision = await self.request(
                    "web.fetch", {"url": url}, correlation_id=correlation
                )
                self.assertEqual(
                    (decision.verdict, decision.reason, decision.level),
                    (Verdict.ALLOW, R.TASK_GRANT_APPLIED, ApprovalLevel.APPROVAL),
                )
                self.assertEqual(decision.grant_id, granted.grant_id)
                self.assertIsNotNone(decision.invocation)
                rows = [
                    e
                    for e in self.h.sink.events
                    if e.correlation_id == correlation
                    and e.action == "tool.approval.grant.use"
                ]
                self.assertEqual(len(rows), 1)
                self.assertEqual(
                    (rows[0].resource_kind, rows[0].resource_id, rows[0].decision),
                    ("tool_task_grant", granted.grant_id, "allow"),
                )
        self.assertEqual(len(self.h.approvals._records), approvals_before)
        self.assertEqual([use[0] for use in self.h.grants.uses], [granted.grant_id] * 2)
        decisions = [
            e.reason for e in self.h.sink.events if e.action == "tool.web.fetch"
        ]
        self.assertEqual(decisions.count("task_grant_applied"), 2)

    async def test_a_wider_or_different_call_is_asked_again(self):
        await self.grant()
        for tool, arguments in (
            ("web.fetch", {"url": "https://docs.example.org/"}),
            ("web.fetch", {"url": "https://other.example.org/guide"}),
            ("web.fetch", {"url": "https://docs.example.org/guide?x=1"}),
            ("host.install_package", INSTALL),
        ):
            with self.subTest(arguments=arguments):
                decision = await self.request(tool, arguments)
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.NEEDS_APPROVAL, R.APPROVAL_REQUIRED),
                )
                self.assertIsNone(decision.grant_id)

    async def test_a_host_command_is_granted_for_the_same_command_only(self):
        await self.grant("host.install_package", INSTALL)
        same = await self.request("host.install_package", INSTALL)
        self.assertEqual(same.reason, R.TASK_GRANT_APPLIED)
        other = await self.request("host.install_package", {"package": "curl"})
        self.assertEqual(other.reason, R.APPROVAL_REQUIRED)

    async def test_another_run_of_the_task_is_asked_again(self):
        await self.grant()
        decision = await self.request(
            "web.fetch", FETCH_DOCS, context=make_context(run=TaskRun(2, 0))
        )
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)

    async def test_a_revoked_grant_is_asked_again(self):
        _asked, granted = await self.grant()
        revoked = await self.h.service.revoke_grant(granted.grant_id, self.person)
        self.assertEqual(revoked.outcome, ApprovalOutcome.REVOKED)
        decision = await self.request("web.fetch", FETCH_DOCS)
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)

    async def test_an_ended_task_uses_no_grant(self):
        await self.grant()
        self.h.task_activity.answer = TaskActivity.ENDED
        decision = await self.request("web.fetch", FETCH_DOCS)
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.TASK_NOT_ACTIVE)
        )
        self.assertEqual(self.h.grants.uses, [])

    async def test_a_task_that_ends_between_the_check_and_the_use_uses_nothing(self):
        activity = SequenceActivity()
        self.h = Harness(with_grants=True, task_activity=activity)
        await self.grant()
        # the broker's own check passes; the store's, at the use, sees the end
        activity.then(1, TaskActivity.ENDED)
        decision = await self.request("web.fetch", FETCH_DOCS)
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.TASK_NOT_ACTIVE)
        )
        self.assertEqual(self.h.grants.uses, [])

    async def test_a_use_that_cannot_be_audited_does_not_run(self):
        await self.grant()
        failing = FailingSink()
        self.h.broker._audit = failing
        decision = await self.request("web.fetch", FETCH_DOCS)
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.AUDIT_UNAVAILABLE)
        )
        self.assertIsNone(decision.invocation)

    async def test_grants_that_cannot_be_read_fall_back_to_asking(self):
        approvals = InMemoryApprovalStore()
        h = Harness(approvals=approvals)
        failing = FailingGrants(approvals)
        h.broker._grants = failing
        with self.assertLogs("paw_backend.tools.broker", "ERROR") as logs:
            decision = await h.broker.request(make_call("web.fetch", FETCH_DOCS))
        self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)
        self.assertNotIn(SECRET, "".join(logs.output))

    async def test_without_a_grant_store_nothing_is_offered_or_granted(self):
        h = Harness()
        asked = await h.broker.request(make_call("web.fetch", FETCH_DOCS))
        self.assertIsNone((await h.approvals.get(asked.approval_id)).grant_pattern)
        result = await h.service.approve_for_task(asked.approval_id, self.person)
        self.assertEqual(result.outcome, ApprovalOutcome.NOT_GRANTABLE)


class ServiceGrantTest(unittest.IsolatedAsyncioTestCase):
    """Who may grant, what is refused, revoking, the end of the task."""

    async def asyncSetUp(self):
        self.h = Harness(with_grants=True)
        self.person = principal(SystemRole.USER, U1)

    async def ask(self, tool="web.fetch", arguments=FETCH_DOCS):
        return await self.h.broker.request(make_call(tool, arguments))

    async def status(self, approval_id):
        return (await self.h.approvals.get(approval_id)).status

    def audit(self, action):
        return [e for e in self.h.sink.events if e.action == action]

    async def test_granting_approves_the_approval_and_audits_both(self):
        asked = await self.ask()
        result = await self.h.service.approve_for_task(asked.approval_id, self.person)
        self.assertTrue(result)
        self.assertEqual(await self.status(asked.approval_id), ApprovalStatus.APPROVED)
        grant = await self.h.grants.get(result.grant_id)
        self.assertEqual(
            (grant.task_id, grant.task_run, grant.tool, grant.status),
            (TASK, RUN, "web.fetch", TaskGrantStatus.ACTIVE),
        )
        approve = self.audit("tool.approval.approve")[-1]
        self.assertEqual(
            (approve.decision, approve.reason, approve.actor_id),
            ("allow", "approved_for_task", U1),
        )
        made = self.audit("tool.approval.grant")[-1]
        self.assertEqual(
            (made.resource_kind, made.resource_id, made.project_id),
            ("tool_task_grant", result.grant_id, P1),
        )

    async def test_only_the_person_asked_may_grant(self):
        asked = await self.ask()
        for who, outcome in (
            (principal(SystemRole.USER, U2), ApprovalOutcome.NOT_FOUND),
            (principal(SystemRole.ADMIN, U3), ApprovalOutcome.NOT_FOUND),
            (principal(SystemRole.OWNER, U3), ApprovalOutcome.NOT_FOUND),
            (principal(SystemRole.USER, AGENT), ApprovalOutcome.SELF_APPROVAL),
        ):
            with self.subTest(who=who.user_id, role=who.system_role):
                result = await self.h.service.approve_for_task(asked.approval_id, who)
                self.assertEqual(result.outcome, outcome)
                self.assertIsNone(result.grant_id)
        self.assertEqual(await self.status(asked.approval_id), ApprovalStatus.PENDING)
        self.assertIn(
            "not_authorised",
            [e.reason for e in self.audit("tool.approval.approve")],
        )

    async def test_a_call_that_is_never_granted_stays_pending(self):
        for tool, arguments in (
            ("repo.delete_tree", {"path": "/srv/paw-test/worktree/build"}),
            (
                "git.merge",
                {
                    "remote": "https://github.com/org/repo.git",
                    "repository": str(REPO),
                    "credential": "cred_" + "a1" * 16,
                    "pull_request": 7,
                },
            ),
        ):
            with self.subTest(tool=tool):
                asked = await self.ask(tool, arguments)
                result = await self.h.service.approve_for_task(
                    asked.approval_id, self.person
                )
                self.assertEqual(result.outcome, ApprovalOutcome.NOT_GRANTABLE)
                self.assertEqual(
                    await self.status(asked.approval_id), ApprovalStatus.PENDING
                )

    async def test_a_task_that_cannot_act_gets_no_grant(self):
        asked = await self.ask()
        for answer in (
            TaskActivity.ENDED,
            TaskActivity.SUPERSEDED,
            TaskActivity.UNKNOWN,
        ):
            with self.subTest(answer=answer):
                self.h.task_activity.answer = answer
                result = await self.h.service.approve_for_task(
                    asked.approval_id, self.person
                )
                self.assertEqual(result.outcome, ApprovalOutcome.TASK_NOT_ACTIVE)
        self.assertEqual(await self.status(asked.approval_id), ApprovalStatus.PENDING)

    async def test_an_expired_or_decided_approval_is_not_granted(self):
        asked = await self.ask()
        self.h.clock.advance(hours=2)
        result = await self.h.service.approve_for_task(asked.approval_id, self.person)
        self.assertEqual(result.outcome, ApprovalOutcome.EXPIRED)
        self.assertEqual(await self.status(asked.approval_id), ApprovalStatus.EXPIRED)
        again = await self.ask()
        await self.h.service.approve(again.approval_id, self.person)
        result = await self.h.service.approve_for_task(again.approval_id, self.person)
        self.assertEqual(result.outcome, ApprovalOutcome.NOT_PENDING)

    async def test_the_number_of_active_grants_is_bounded(self):
        approvals = InMemoryApprovalStore()
        h = Harness(approvals=approvals, with_grants=True)
        service = ApprovalService(
            approvals,
            InMemoryAuditSink(),
            clock=h.clock,
            grants=h.grants,
            max_task_grants=1,
        )
        first = await h.broker.request(make_call("web.fetch", FETCH_DOCS))
        second = await h.broker.request(make_call("host.install_package", INSTALL))
        self.assertTrue(await service.approve_for_task(first.approval_id, self.person))
        result = await service.approve_for_task(second.approval_id, self.person)
        self.assertEqual(result.outcome, ApprovalOutcome.GRANT_LIMIT_REACHED)
        self.assertEqual(
            (await approvals.get(second.approval_id)).status, ApprovalStatus.PENDING
        )
        for bad in (0, 101, True):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ApprovalService(
                    approvals, InMemoryAuditSink(), grants=h.grants, max_task_grants=bad
                )

    async def test_the_person_or_an_administrator_may_revoke_nobody_else(self):
        asked = await self.ask()
        granted = await self.h.service.approve_for_task(asked.approval_id, self.person)
        for who in (principal(SystemRole.USER, U2), principal(SystemRole.USER, AGENT)):
            with self.subTest(who=who.user_id):
                result = await self.h.service.revoke_grant(granted.grant_id, who)
                self.assertEqual(result.outcome, ApprovalOutcome.NOT_FOUND)
        self.assertEqual(
            (await self.h.grants.get(granted.grant_id)).status, TaskGrantStatus.ACTIVE
        )
        admin = principal(SystemRole.ADMIN, U3)
        result = await self.h.service.revoke_grant(granted.grant_id, admin)
        self.assertEqual(result.outcome, ApprovalOutcome.REVOKED)
        record = await self.h.grants.get(granted.grant_id)
        self.assertEqual(
            (record.status, record.revoked_by), (TaskGrantStatus.REVOKED, U3)
        )
        again = await self.h.service.revoke_grant(granted.grant_id, self.person)
        self.assertEqual(again.outcome, ApprovalOutcome.NOT_OPEN)
        rows = self.audit("tool.approval.grant.revoke")
        self.assertEqual(
            [(e.decision, e.reason) for e in rows],
            [
                ("deny", "not_authorised"),
                ("deny", "not_authorised"),
                ("allow", "revoked"),
                ("deny", "not_open"),
            ],
        )

    async def test_the_end_of_the_task_revokes_its_grants(self):
        asked = await self.ask()
        granted = await self.h.service.approve_for_task(asked.approval_id, self.person)

        class Ended:
            task_id = TASK
            from_state = "running"
            to_state = "cancelled"

        await self.h.service.revoke_on_task_end(Ended())
        record = await self.h.grants.get(granted.grant_id)
        self.assertEqual(
            (record.status, record.revoked_by), (TaskGrantStatus.REVOKED, None)
        )
        row = self.audit("tool.approval.grant.revoke")[-1]
        self.assertEqual(
            (row.reason, row.resource_id, row.actor_id),
            ("task_ended", granted.grant_id, None),
        )

    async def test_a_use_must_match_the_grant(self):
        asked = await self.ask()
        granted = await self.h.service.approve_for_task(asked.approval_id, self.person)
        use = GrantUse(TASK, RUN, AGENT, U1, "web.fetch", "a" * 64, uuid.uuid4())
        for changed, outcome in (
            ({"tool": "host.install_package"}, GrantUseOutcome.MISMATCH),
            ({"requester_user_id": U2}, GrantUseOutcome.MISMATCH),
            ({"task_run": TaskRun(1, 1)}, GrantUseOutcome.SUPERSEDED),
        ):
            with self.subTest(changed=changed):
                values = {
                    "task_id": use.task_id,
                    "task_run": use.task_run,
                    "agent_id": use.agent_id,
                    "requester_user_id": use.requester_user_id,
                    "tool": use.tool,
                    "call_hash": use.call_hash,
                    "correlation_id": use.correlation_id,
                    **changed,
                }
                result = await self.h.grants.use(
                    granted.grant_id, GrantUse(**values), now=self.h.clock()
                )
                self.assertEqual(result, outcome)
        self.assertEqual(
            await self.h.grants.use(uuid.uuid4(), use, now=self.h.clock()),
            GrantUseOutcome.NOT_FOUND,
        )
        self.assertEqual(
            await self.h.grants.use(granted.grant_id, use, now=self.h.clock()),
            GrantUseOutcome.USED,
        )

    async def test_the_grant_lives_until_the_task_ends_not_for_the_approval_ttl(self):
        await self.ask()
        asked = await self.ask("host.install_package", INSTALL)
        await self.h.service.approve_for_task(asked.approval_id, self.person)
        self.h.clock.advance(hours=30)
        decision = await self.h.broker.request(
            make_call("host.install_package", INSTALL)
        )
        self.assertEqual(decision.reason, R.TASK_GRANT_APPLIED)
