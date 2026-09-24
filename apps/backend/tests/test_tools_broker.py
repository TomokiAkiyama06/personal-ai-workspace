import asyncio
import dataclasses
import unittest
import uuid

from paw_backend.authz import (
    ALL_PROJECTS,
    Authorizer,
    Capability,
    InMemoryAuditSink,
    ProjectRole,
    ProjectState,
    Reason,
    SystemRole,
)
from paw_backend.tools import (
    DEFAULT_TOOL_POLICY,
    ApprovalLevel,
    BrokerReason,
    BudgetStatus,
    InMemoryApprovalStore,
    TaskContext,
    ToolBroker,
    ToolCall,
    ToolRegistry,
    Verdict,
)

from .authz_support import (
    AGENT,
    SECRET,
    FailingSink,
    HangingSink,
    StaticDirectory,
    principal,
)
from .tools_support import (
    HANDLE,
    NOW,
    OTHER_HANDLE,
    P1,
    P2,
    ROOT,
    TASK,
    U1,
    U2,
    DictResolver,
    FakeBudget,
    Harness,
    make_call,
    make_context,
    make_grant,
    make_scope,
    sample_registry,
)

R = BrokerReason
MARKER = "zz-unique-content-marker-4711"
GITHUB_TOKEN = "ghp_" + "a1B2" * 9

READ = {"path": f"{ROOT}/src/a.py"}
WRITE = {"path": f"{ROOT}/src/a.py", "content": "print(1)"}
FETCH = {"url": "https://github.com/org/repo"}
PUSH = {"remote": "https://github.com/org/repo.git", "credential": HANDLE}
MERGE = {**PUSH, "pull_request": 7}


def is_hex_digest(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= set("0123456789abcdef")
    )


class DecisionLevelsTest(unittest.IsolatedAsyncioTestCase):
    """AUTO / SCOPED_AUTO / APPROVAL / STRONG_APPROVAL / DENY, end to end."""

    async def asyncSetUp(self):
        self.h = Harness()

    async def request(self, tool, arguments=None, **kw):
        return await self.h.broker.request(make_call(tool, arguments, **kw))

    async def test_auto_allows_a_read_in_scope(self):
        decision = await self.request("repo.read_file", READ)
        self.assertEqual(
            (decision.verdict, decision.reason, decision.level),
            (Verdict.ALLOW, R.AUTO, ApprovalLevel.AUTO),
        )
        self.assertTrue(decision)
        self.assertEqual(decision.tool, "repo.read_file")
        self.assertTrue(is_hex_digest(decision.call_hash))
        self.assertEqual(
            dict(decision.invocation.arguments), {"path": f"{ROOT}/src/a.py"}
        )

    async def test_scoped_auto_allows_writes_execution_network_and_credential_use(self):
        cases = [
            ("repo.write_file", WRITE),
            ("tests.run", {}),
            ("tests.run", {"selector": "unit"}),
            ("web.fetch", FETCH),
            ("git.push", PUSH),
            ("issues.create", {"url": "https://api.github.com/x", "title": "t"}),
        ]
        for tool, arguments in cases:
            with self.subTest(tool=tool):
                decision = await self.request(tool, arguments)
                self.assertEqual(
                    (decision.verdict, decision.reason, decision.level),
                    (Verdict.ALLOW, R.SCOPED_AUTO, ApprovalLevel.SCOPED_AUTO),
                )

    async def test_approval_is_needed_for_destructive_and_host_wide_calls(self):
        for tool, arguments in (
            ("repo.delete_tree", {"path": f"{ROOT}/build"}),
            ("host.install_package", {"package": "ripgrep"}),
        ):
            with self.subTest(tool=tool):
                decision = await self.request(tool, arguments)
                self.assertEqual(
                    (decision.verdict, decision.reason, decision.level),
                    (
                        Verdict.NEEDS_APPROVAL,
                        R.APPROVAL_REQUIRED,
                        ApprovalLevel.APPROVAL,
                    ),
                )
                self.assertFalse(decision)
                self.assertIsNone(decision.invocation)
                record = await self.h.approvals.get(decision.approval_id)
                self.assertEqual(record.tool, tool)
                self.assertEqual(record.call_hash, decision.call_hash)

    async def test_strong_approval_is_needed_for_a_merge(self):
        decision = await self.request("git.merge", MERGE)
        self.assertEqual(
            (decision.verdict, decision.reason, decision.level),
            (
                Verdict.NEEDS_APPROVAL,
                R.STRONG_APPROVAL_REQUIRED,
                ApprovalLevel.STRONG_APPROVAL,
            ),
        )

    async def test_an_external_write_beyond_the_task_hosts_needs_approval(self):
        decision = await self.request(
            "issues.create", {"url": "https://example.org/issues", "title": "t"}
        )
        self.assertEqual(
            (decision.verdict, decision.reason, decision.level),
            (Verdict.NEEDS_APPROVAL, R.APPROVAL_REQUIRED, ApprovalLevel.APPROVAL),
        )

    async def test_an_external_read_beyond_the_task_hosts_is_denied(self):
        decision = await self.request("web.fetch", {"url": "https://example.org/x"})
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.HOST_OUT_OF_SCOPE)
        )

    async def test_credential_plaintext_retrieval_is_always_denied(self):
        for arguments in ({"credential": HANDLE}, {"credential": OTHER_HANDLE}):
            decision = await self.request("credentials.read", arguments)
            self.assertEqual(
                (decision.verdict, decision.reason, decision.level),
                (Verdict.DENY, R.CREDENTIAL_PLAINTEXT_DENIED, ApprovalLevel.DENY),
            )
        self.assertEqual(len(self.h.approvals._records), 0)  # nothing was requested

    async def test_credential_plaintext_retrieval_is_denied_even_with_an_approval_id(
        self,
    ):
        decision = await self.h.broker.request(
            make_call("credentials.read", {"credential": HANDLE}),
            approval_id=uuid.uuid4(),
        )
        self.assertEqual(
            (decision.verdict, decision.reason),
            (Verdict.DENY, R.CREDENTIAL_PLAINTEXT_DENIED),
        )

    async def test_a_policy_that_denies_a_class_denies_the_tool(self):
        from paw_backend.tools import (
            Environment,
            ScopeStatus,
            ToolCapability,
            ToolPolicy,
        )

        table = dict(DEFAULT_TOOL_POLICY.table)
        table[
            (ToolCapability.WRITE, Environment.PROJECT_LOCAL, ScopeStatus.IN_SCOPE)
        ] = ApprovalLevel.DENY
        h = Harness(policy=ToolPolicy(table))
        decision = await h.broker.request(make_call("repo.write_file", WRITE))
        self.assertEqual(
            (decision.verdict, decision.reason, decision.level),
            (Verdict.DENY, R.POLICY_DENIED, ApprovalLevel.DENY),
        )

    async def test_the_tool_minimum_level_raises_the_policy_level(self):
        # git.merge is write + network + credential-use (scoped_auto by class)
        # but pinned to STRONG_APPROVAL by its spec.
        spec = sample_registry().get("git.merge")
        self.assertEqual(spec.min_level, ApprovalLevel.STRONG_APPROVAL)
        decision = await self.request("git.merge", MERGE)
        self.assertEqual(decision.level, ApprovalLevel.STRONG_APPROVAL)

    async def test_a_tool_pinned_to_deny_is_never_allowed(self):
        specs = [
            dataclasses.replace(spec, min_level=ApprovalLevel.DENY)
            if spec.name == "repo.read_file"
            else spec
            for spec in (sample_registry().get(n) for n in sample_registry().names())
        ]
        h = Harness(registry=ToolRegistry(specs))
        decision = await h.broker.request(
            make_call("repo.read_file", READ), approval_id=uuid.uuid4()
        )
        self.assertEqual(
            (decision.verdict, decision.reason, decision.level),
            (Verdict.DENY, R.POLICY_DENIED, ApprovalLevel.DENY),
        )
        self.assertEqual(len(h.approvals._records), 0)


class TaskScopeEnforcementTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()

    async def deny(self, tool, arguments, reason, **kw):
        decision = await self.h.broker.request(make_call(tool, arguments, **kw))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, reason), arguments
        )
        self.assertIsNone(decision.invocation)
        return decision

    async def test_path_tricks_are_denied(self):
        for path in (
            "../outside.txt",
            f"{ROOT}/../outside.txt",
            f"{ROOT}/src/../../etc/passwd",
            "src/./../../x",
            "..",
            f"{ROOT}/%2e%2e/x",
            f"{ROOT}\\..\\x",
            "．．/x",
            f"{ROOT}/a​/../../x",
            "~/.ssh/id_ed25519",
        ):
            with self.subTest(path=path):
                await self.deny("repo.read_file", {"path": path}, R.INVALID_TARGET)

    async def test_paths_outside_the_roots_are_denied(self):
        for path in (
            "/etc/passwd",
            f"{ROOT}-evil/a",
            "/srv/paw-test",
            ROOT.upper() + "/a",
            "/proc/self/root/etc/passwd",
            "/",
        ):
            with self.subTest(path=path):
                for tool, extra in (
                    ("repo.read_file", {}),
                    ("repo.write_file", {"content": "x"}),
                ):
                    await self.deny(tool, {"path": path, **extra}, R.PATH_OUT_OF_SCOPE)

    async def test_a_relative_path_resolves_inside_the_first_root(self):
        decision = await self.h.broker.request(
            make_call("repo.read_file", {"path": "./src//a.py"})
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.invocation.arguments["path"], f"{ROOT}/src/a.py")

    async def test_two_spellings_of_one_path_are_one_call(self):
        a = await self.h.broker.request(
            make_call("repo.read_file", {"path": "src/a.py"})
        )
        b = await self.h.broker.request(
            make_call("repo.read_file", {"path": f"{ROOT}//src/./a.py"})
        )
        self.assertEqual(a.call_hash, b.call_hash)

    async def test_a_symlink_out_of_the_root_is_denied(self):
        h = Harness(path_resolver=DictResolver({f"{ROOT}/link": "/etc"}))
        decision = await h.broker.request(
            make_call("repo.read_file", {"path": f"{ROOT}/link/passwd"})
        )
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.PATH_OUT_OF_SCOPE)
        )
        inside = await h.broker.request(
            make_call("repo.read_file", {"path": "src/a.py"})
        )
        self.assertTrue(inside.allowed)

    async def test_a_resolver_failure_denies_and_never_leaks_its_message(self):
        class Broken:
            async def resolve(self, path):
                raise OSError(SECRET)

        h = Harness(path_resolver=Broken())
        with self.assertLogs(level="WARNING") as logs:
            decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason),
            (Verdict.DENY, R.PATH_RESOLUTION_UNAVAILABLE),
        )
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("OSError", "\n".join(logs.output))

    async def test_host_tricks_are_denied(self):
        for url in (
            "https://github.com@evil.com/x",
            "https://evil.com\\@github.com/x",
            "https://github.com:8443/x",
            "file:///etc/passwd",
            "ftp://github.com/x",
            "https://127.1/",
            "https://[::1/",
            "https://github.com/a b",
        ):
            with self.subTest(url=url):
                await self.deny("web.fetch", {"url": url}, R.INVALID_TARGET)

    async def test_user_information_with_a_password_is_credential_plaintext(self):
        await self.deny(
            "web.fetch",
            {"url": "https://user:pw12345678@github.com/x"},
            R.CREDENTIAL_PLAINTEXT_IN_ARGUMENTS,
        )
        await self.deny(
            "web.fetch", {"url": "https://user@github.com/x"}, R.INVALID_TARGET
        )

    async def test_look_alike_hosts_are_out_of_scope_not_in_scope(self):
        for url in (
            "https://github.com.evil.com/x",
            "https://evilgithub.com/x",
            "https://gist.github.com/x",
            "https://GITHUB.COM.evil.com/",
        ):
            with self.subTest(url=url):
                await self.deny("web.fetch", {"url": url}, R.HOST_OUT_OF_SCOPE)

    async def test_host_case_and_trailing_dot_spellings_of_a_task_host_are_in_scope(
        self,
    ):
        for url in (
            "https://GitHub.COM/x",
            "https://github.com./x",
            "https://github.com:443/x",
        ):
            with self.subTest(url=url):
                decision = await self.h.broker.request(
                    make_call("web.fetch", {"url": url})
                )
                self.assertTrue(decision.allowed)
                self.assertEqual(
                    decision.invocation.arguments["url"].split("/")[2], "github.com"
                )

    async def test_a_project_outside_the_scope_is_denied(self):
        await self.deny(
            "project.export",
            {"project": str(P2), "path": f"{ROOT}/x"},
            R.PROJECT_OUT_OF_SCOPE,
        )
        await self.deny(
            "project.export",
            {"project": "not-a-uuid", "path": f"{ROOT}/x"},
            R.INVALID_TARGET,
        )
        decision = await self.h.broker.request(
            make_call("project.export", {"project": str(P1), "path": f"{ROOT}/x"})
        )
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.ALLOW, R.SCOPED_AUTO)
        )

    async def test_a_credential_handle_the_task_was_not_given_is_denied(self):
        await self.deny(
            "git.push", {**PUSH, "credential": OTHER_HANDLE}, R.CREDENTIAL_OUT_OF_SCOPE
        )

    async def test_a_credential_is_never_sent_to_a_host_it_is_not_valid_for(self):
        # The task may talk to both hosts, but HANDLE is a GitHub credential.
        scope = make_scope(
            hosts=["github.com", "hooks.other-service.example", "example.org"]
        )
        context = make_context(scope=scope)
        for remote in (
            "https://hooks.other-service.example/services/x",
            "https://example.org/x",
            "https://github.com.evil.com/x",
        ):
            with self.subTest(remote=remote):
                decision = await self.deny(
                    "git.push",
                    {"remote": remote, "credential": HANDLE},
                    R.CREDENTIAL_OUT_OF_SCOPE,
                    context=context,
                )
                self.assertEqual(decision.level, ApprovalLevel.DENY)
        ok = await self.h.broker.request(make_call("git.push", PUSH, context=context))
        self.assertEqual((ok.verdict, ok.reason), (Verdict.ALLOW, R.SCOPED_AUTO))
        merge = await self.h.broker.request(
            make_call(
                "git.merge",
                {
                    "remote": "https://example.org/x",
                    "credential": HANDLE,
                    "pull_request": 7,
                },
                context=context,
            )
        )
        self.assertEqual(
            (merge.verdict, merge.reason), (Verdict.DENY, R.CREDENTIAL_OUT_OF_SCOPE)
        )
        self.assertEqual(len(self.h.approvals._records), 0)

    async def test_credential_use_is_by_opaque_handle_only(self):
        await self.deny(
            "git.push",
            {**PUSH, "credential": GITHUB_TOKEN},
            R.CREDENTIAL_PLAINTEXT_IN_ARGUMENTS,
        )
        for value in ("hunter2", HANDLE.upper(), HANDLE + "x", "", " " + HANDLE):
            with self.subTest(value=value):
                await self.deny(
                    "git.push",
                    {**PUSH, "credential": value},
                    R.CREDENTIAL_HANDLE_INVALID,
                )

    async def test_credential_plaintext_in_any_argument_is_denied(self):
        secrets = {
            "github": GITHUB_TOKEN,
            "url": "https://user:hunter2hunter2@github.com/org/repo.git",
            "pem": "-----BEGIN " + "PRIVATE KEY-----\nabc",
        }
        for label, secret in secrets.items():
            with self.subTest(label=label):
                await self.deny(
                    "repo.write_file",
                    {"path": f"{ROOT}/a", "content": f"token {secret}"},
                    R.CREDENTIAL_PLAINTEXT_IN_ARGUMENTS,
                )
        await self.deny(
            "repo.read_file",
            {"path": f"{ROOT}/{GITHUB_TOKEN}"},
            R.CREDENTIAL_PLAINTEXT_IN_ARGUMENTS,
        )
        await self.deny(
            "web.fetch",
            {"url": f"https://github.com/?access={GITHUB_TOKEN}"},
            R.CREDENTIAL_PLAINTEXT_IN_ARGUMENTS,
        )

    async def test_a_scope_without_roots_touches_no_path(self):
        context = make_context(scope=make_scope(path_roots=[], hosts=[]))
        await self.deny(
            "repo.read_file", {"path": "a.py"}, R.INVALID_TARGET, context=context
        )
        await self.deny(
            "repo.read_file",
            {"path": f"{ROOT}/a.py"},
            R.PATH_OUT_OF_SCOPE,
            context=context,
        )


class InjectionSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = Harness()

    async def test_unknown_or_look_alike_tool_names_are_denied_and_never_echoed(self):
        names = [
            "nonexistent.tool",
            "REPO.READ_FILE",
            " repo.read_file",
            "repo.read_file ",
            "repo.read_file​",
            "repo.read_fıle",
            "repo.read_file; rm -rf /",
            "read",
            "credential-use",
            "AUTO",
            "tool.unknown",
            "",
            None,
            5,
            b"repo.read_file",
            ["repo.read_file"],
            MARKER,
        ]
        for name in names:
            with self.subTest(name=name):
                decision = await self.h.broker.request(make_call(name, READ))
                self.assertEqual(
                    (decision.verdict, decision.reason, decision.tool, decision.level),
                    (Verdict.DENY, R.UNKNOWN_TOOL, None, None),
                )
        events = self.h.tool_events()
        self.assertEqual(len(events), len(names))
        for event in events:
            self.assertEqual(
                (event.action, event.decision, event.reason),
                ("tool.unknown", "deny", "unknown_tool"),
            )
        self.assertNotIn(
            MARKER, "".join(e.model_dump_json() for e in self.h.sink.events)
        )

    async def test_undeclared_arguments_smuggled_next_to_real_ones_are_denied(self):
        extras = [
            {"capability": "read"},
            {"capabilities": ["read"]},
            {"level": "auto"},
            {"approved": True},
            {"approval_id": str(uuid.uuid4())},
            {"destructive": False},
            {"Path": f"{ROOT}/b"},
            {"path ": f"{ROOT}/b"},
            {"__class__": "x"},
            {"": 1},
            {1: "x"},
            {None: "x"},
        ]
        for extra in extras:
            with self.subTest(extra=str(extra)):
                decision = await self.h.broker.request(
                    make_call("repo.read_file", {**READ, **extra})
                )
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.DENY, R.INVALID_ARGUMENTS),
                )
        self.assertEqual(self.h.executor.invocations, [])

    async def test_missing_and_mistyped_arguments_are_denied(self):
        cases = [
            ("repo.read_file", {}),
            ("repo.read_file", {"path": None}),
            ("repo.read_file", {"path": 5}),
            ("repo.read_file", {"path": [f"{ROOT}/a"]}),
            ("repo.read_file", {"path": b"/a"}),
            ("repo.write_file", {"path": f"{ROOT}/a"}),
            ("repo.write_file", {"path": f"{ROOT}/a", "content": 5}),
            ("repo.write_file", {"path": f"{ROOT}/a", "content": "x" * 501}),
            ("repo.write_file", {"path": f"{ROOT}/a", "content": "a\x00b"}),
            ("flag.toggle", {"enabled": "true"}),
            ("flag.toggle", {"enabled": 1}),
            ("flag.toggle", {"enabled": None}),
            ("flag.toggle", {"enabled": True, "count": True}),
            ("flag.toggle", {"enabled": True, "count": 1.0}),
            ("flag.toggle", {"enabled": True, "count": "1"}),
            ("flag.toggle", {"enabled": True, "count": 10}),
            ("flag.toggle", {"enabled": True, "count": -1}),
            ("git.merge", {**PUSH, "pull_request": 0}),
            ("git.merge", {**PUSH, "pull_request": 10**6 + 1}),
        ]
        for tool, arguments in cases:
            with self.subTest(tool=tool, arguments=str(arguments)[:60]):
                decision = await self.h.broker.request(make_call(tool, arguments))
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.DENY, R.INVALID_ARGUMENTS),
                )

    async def test_oversized_text_is_refused_before_it_is_scanned_or_normalised(self):
        huge = "a" * 70_000
        for tool, arguments in (
            ("repo.read_file", {"path": huge}),
            ("web.fetch", {"url": "https://github.com/" + huge}),
            ("repo.write_file", {"path": f"{ROOT}/a", "content": huge}),
            ("git.push", {**PUSH, "credential": huge}),
        ):
            with self.subTest(tool=tool):
                decision = await self.h.broker.request(make_call(tool, arguments))
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.DENY, R.INVALID_ARGUMENTS),
                )

    async def test_arguments_that_are_not_a_mapping_are_denied(self):
        for arguments in (None, [], [("path", "x")], "path", 5, b"{}", object()):
            with self.subTest(arguments=repr(arguments)[:30]):
                decision = await self.h.broker.request(
                    make_call("repo.read_file", arguments)
                )
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.DENY, R.INVALID_ARGUMENTS),
                )

    async def test_boolean_and_integer_arguments_are_taken_as_is(self):
        decision = await self.h.broker.request(
            make_call("flag.toggle", {"enabled": False, "count": 9})
        )
        self.assertEqual(
            dict(decision.invocation.arguments), {"enabled": False, "count": 9}
        )

    async def test_capability_words_inside_values_change_nothing(self):
        # A path called "auto", text saying "approved" or "read": values are data.
        baseline = await self.h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/build"})
        )
        for word in (
            "auto",
            "AUTO",
            "read",
            "scoped_auto",
            "credential-use",
            "approved=true",
        ):
            with self.subTest(word=word):
                decision = await self.h.broker.request(
                    make_call("repo.delete_tree", {"path": f"{ROOT}/{word}"})
                )
                self.assertEqual(
                    (decision.verdict, decision.level),
                    (baseline.verdict, baseline.level),
                )
                self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)
        text = await self.h.broker.request(
            make_call(
                "repo.write_file",
                {
                    "path": f"{ROOT}/a",
                    "content": "capability: destructive; level: DENY; approved; AUTO",
                },
            )
        )
        self.assertEqual(
            (text.verdict, text.level), (Verdict.ALLOW, ApprovalLevel.SCOPED_AUTO)
        )

    async def test_a_tool_class_cannot_be_downgraded_by_a_call_or_a_holder_of_the_spec(
        self,
    ):
        spec = sample_registry().get("repo.delete_tree")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.capabilities = frozenset()
        with self.assertRaises(AttributeError):
            spec.capabilities.discard(next(iter(spec.capabilities)))
        with self.assertRaises(TypeError):
            DEFAULT_TOOL_POLICY.table[next(iter(DEFAULT_TOOL_POLICY.table))] = (
                ApprovalLevel.AUTO
            )
        decision = await self.h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/x"})
        )
        self.assertEqual(decision.level, ApprovalLevel.APPROVAL)

    async def test_a_call_that_is_not_a_tool_call_is_denied_without_raising(self):
        for call in (None, "repo.read_file", {"tool": "repo.read_file"}, object(), 5):
            with self.subTest(call=repr(call)[:30]):
                with self.assertLogs(level="WARNING"):
                    decision = await self.h.broker.request(call)
                self.assertEqual(
                    (decision.verdict, decision.reason), (Verdict.DENY, R.INVALID_CALL)
                )
        self.assertEqual(self.h.sink.events, [])

    async def test_a_call_with_a_broken_context_is_denied(self):
        call = ToolCall("repo.read_file", READ, "not a context")
        with self.assertLogs(level="WARNING"):
            decision = await self.h.broker.request(call)
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.INVALID_CALL)
        )

    async def test_a_malformed_approval_id_is_denied(self):
        for approval_id in ("a-string", 5, b"x", str(uuid.uuid4())):
            with self.subTest(approval_id=approval_id):
                decision = await self.h.broker.request(
                    make_call("repo.delete_tree", {"path": f"{ROOT}/x"}),
                    approval_id=approval_id,
                )
                self.assertEqual(
                    (decision.verdict, decision.reason), (Verdict.DENY, R.INVALID_CALL)
                )

    async def test_an_approval_id_is_ignored_when_no_approval_is_needed(self):
        decision = await self.h.broker.request(
            make_call("repo.read_file", READ), approval_id=uuid.uuid4()
        )
        self.assertEqual((decision.verdict, decision.reason), (Verdict.ALLOW, R.AUTO))

    async def test_decisions_are_falsy_unless_allowed(self):
        allowed = await self.h.broker.request(make_call("repo.read_file", READ))
        pending = await self.h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/x"})
        )
        denied = await self.h.broker.request(make_call("nope", READ))
        self.assertEqual(
            [bool(d) for d in (allowed, pending, denied)], [True, False, False]
        )

    async def test_a_decision_never_carries_the_arguments_in_its_text_form(self):
        decision = await self.h.broker.request(
            make_call("repo.write_file", {"path": f"{ROOT}/a", "content": MARKER})
        )
        self.assertTrue(decision.allowed)
        self.assertNotIn(MARKER, repr(decision))
        self.assertNotIn(
            MARKER, repr(make_call("repo.write_file", {"content": MARKER}))
        )
        self.assertNotIn(MARKER, repr(decision.invocation))


class AuthorizationTest(unittest.IsolatedAsyncioTestCase):
    async def deny(self, h, tool, arguments, authz_reason, **kw):
        decision = await h.broker.request(make_call(tool, arguments, **kw))
        self.assertEqual(
            (decision.verdict, decision.reason, decision.authz_reason),
            (Verdict.DENY, R.AUTHZ_DENIED, authz_reason),
        )
        return decision

    async def test_the_grant_narrows_what_the_user_may_do(self):
        h = Harness()
        context = make_context(grant=make_grant(Capability.PROJECT_READ))
        await self.deny(
            h,
            "repo.write_file",
            WRITE,
            Reason.AGENT_CAPABILITY_NOT_GRANTED,
            context=context,
        )
        ok = await h.broker.request(make_call("repo.read_file", READ, context=context))
        self.assertTrue(ok.allowed)

    async def test_an_agent_cannot_exceed_its_user(self):
        h = Harness()  # U2 is only a Viewer of P1
        context = make_context(delegator_id=U2)
        await self.deny(
            h, "repo.write_file", WRITE, Reason.CAPABILITY_NOT_GRANTED, context=context
        )
        ok = await h.broker.request(make_call("repo.read_file", READ, context=context))
        self.assertTrue(ok.allowed)

    async def test_a_delegator_that_is_not_active_denies_everything(self):
        h = Harness(directory=StaticDirectory())
        await self.deny(h, "repo.read_file", READ, Reason.DELEGATOR_NOT_ACTIVE)

    async def test_a_role_change_takes_effect_on_the_next_call(self):
        directory = StaticDirectory(
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR})
        )
        h = Harness(directory=directory)
        self.assertTrue(
            (await h.broker.request(make_call("repo.write_file", WRITE))).allowed
        )
        directory.principals[U1] = principal(
            SystemRole.USER, U1, {P1: ProjectRole.VIEWER}
        )
        await self.deny(h, "repo.write_file", WRITE, Reason.CAPABILITY_NOT_GRANTED)

    async def test_the_project_state_is_enforced(self):
        directory = StaticDirectory(
            principal(
                SystemRole.USER,
                U1,
                {P1: ProjectRole.CONTRIBUTOR, P2: ProjectRole.CONTRIBUTOR},
            )
        )
        scope = make_scope(
            projects={P1: ProjectState.ACTIVE, P2: ProjectState.ARCHIVED}
        )
        h = Harness(directory=directory)
        context = make_context(scope=scope, grant=make_grant(projects=ALL_PROJECTS))
        arguments = {"project": str(P2), "path": f"{ROOT}/x"}
        await self.deny(
            h,
            "project.export",
            arguments,
            Reason.PROJECT_STATE_FORBIDS,
            context=context,
        )

    async def test_a_project_the_grant_does_not_cover_is_denied(self):
        directory = StaticDirectory(
            principal(
                SystemRole.USER,
                U1,
                {P1: ProjectRole.CONTRIBUTOR, P2: ProjectRole.CONTRIBUTOR},
            )
        )
        scope = make_scope(projects={P1: ProjectState.ACTIVE, P2: ProjectState.ACTIVE})
        h = Harness(directory=directory)
        context = make_context(scope=scope, grant=make_grant(projects={P1}))
        await self.deny(
            h,
            "project.export",
            {"project": str(P2), "path": f"{ROOT}/x"},
            Reason.AGENT_PROJECT_NOT_GRANTED,
            context=context,
        )

    async def test_approval_cannot_widen_what_the_agent_may_do(self):
        # admin.set_role is pinned to STRONG_APPROVAL, but the capability it
        # needs is not delegable: even an Owner's agent is refused before any
        # approval is opened.
        directory = StaticDirectory(
            principal(SystemRole.OWNER, U1, {P1: ProjectRole.MANAGER})
        )
        h = Harness(directory=directory)
        context = make_context(
            grant=make_grant(Capability.ADMIN_USERS_MANAGE, Capability.PROJECT_READ)
        )
        decision = await self.deny(
            h,
            "admin.set_role",
            {"project": str(P1)},
            Reason.AGENT_CAPABILITY_FORBIDDEN,
            context=context,
        )
        self.assertIsNone(decision.approval_id)
        self.assertEqual(decision.level, ApprovalLevel.STRONG_APPROVAL)
        self.assertEqual(len(h.approvals._records), 0)

    async def test_a_user_who_is_not_allowed_never_reaches_an_approval(self):
        h = Harness()
        await self.deny(
            h, "admin.set_role", {"project": str(P1)}, Reason.CAPABILITY_NOT_GRANTED
        )
        self.assertEqual(len(h.approvals._records), 0)

    async def test_personal_capabilities_need_an_all_projects_grant(self):
        h = Harness()
        narrow = make_context()
        await self.deny(
            h, "notes.list", {}, Reason.AGENT_PROJECT_NOT_GRANTED, context=narrow
        )
        broad = make_context(
            grant=make_grant(Capability.MEMORY_USE, projects=ALL_PROJECTS)
        )
        ok = await h.broker.request(make_call("notes.list", {}, context=broad))
        self.assertEqual((ok.verdict, ok.reason), (Verdict.ALLOW, R.AUTO))

    async def test_an_authorizer_that_raises_is_a_denial_without_its_message(self):
        class Raising:
            async def authorize_agent_action(self, *args, **kwargs):
                raise RuntimeError(SECRET)

        h = Harness(authorizer=Raising())
        with self.assertLogs(level="ERROR") as logs:
            decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason, decision.authz_reason),
            (Verdict.DENY, R.AUTHZ_UNAVAILABLE, None),
        )
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("RuntimeError", "\n".join(logs.output))

    async def test_an_authorizer_that_answers_nonsense_is_a_denial(self):
        class Nonsense:
            async def authorize_agent_action(self, *args, **kwargs):
                return True

        h = Harness(authorizer=Nonsense())
        decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.AUTHZ_UNAVAILABLE)
        )

    async def test_authorization_is_skipped_for_calls_the_policy_already_denies(self):
        h = Harness()
        await h.broker.request(make_call("repo.read_file", {"path": "/etc/passwd"}))
        self.assertEqual([e.action for e in h.sink.events], ["tool.repo.read_file"])

    async def test_the_authz_row_and_the_tool_row_share_the_correlation_id(self):
        h = Harness()
        correlation_id = uuid.uuid4()
        decision = await h.broker.request(
            make_call("repo.read_file", READ, correlation_id=correlation_id)
        )
        self.assertEqual(decision.correlation_id, correlation_id)
        rows = {e.action: e for e in h.sink.events}
        self.assertEqual(set(rows), {"project.read", "tool.repo.read_file"})
        self.assertEqual({e.correlation_id for e in rows.values()}, {correlation_id})
        self.assertEqual(rows["project.read"].agent_id, AGENT)
        self.assertEqual(rows["project.read"].actor_id, U1)

    async def test_a_missing_correlation_id_is_generated(self):
        h = Harness()
        a = await h.broker.request(make_call("repo.read_file", READ))
        b = await h.broker.request(make_call("repo.read_file", READ))
        self.assertNotEqual(a.correlation_id, b.correlation_id)
        bad = await h.broker.request(
            make_call("repo.read_file", READ, correlation_id="x")
        )
        self.assertIsInstance(bad.correlation_id, uuid.UUID)


class BudgetTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_default_provider_knows_no_budget_so_nothing_that_needs_one_runs(
        self,
    ):
        h = Harness(budget=None)
        decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.BUDGET_UNKNOWN)
        )
        pending = await h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/x"})
        )
        self.assertEqual(pending.reason, R.BUDGET_UNKNOWN)
        self.assertEqual(
            len(h.approvals._records), 0
        )  # no request for a call that could not run

    async def test_a_tool_that_declares_no_budget_is_not_asked(self):
        h = Harness(budget=None)
        decision = await h.broker.request(make_call("free.ping", {}))
        self.assertEqual((decision.verdict, decision.reason), (Verdict.ALLOW, R.AUTO))

    async def test_within_budget_allows_and_the_check_consumes_nothing(self):
        h = Harness(budget=FakeBudget(BudgetStatus.WITHIN_BUDGET))
        decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertTrue(decision.allowed)
        self.assertEqual(h.budget.checks, [(TASK, "repo.read_file")])
        self.assertEqual(h.budget.charges, [])

    async def test_an_exhausted_or_unknown_budget_denies(self):
        for status, reason in (
            (BudgetStatus.EXCEEDED, R.BUDGET_EXCEEDED),
            (BudgetStatus.UNKNOWN, R.BUDGET_UNKNOWN),
        ):
            with self.subTest(status=status):
                h = Harness(budget=FakeBudget(status))
                decision = await h.broker.request(make_call("repo.read_file", READ))
                self.assertEqual(
                    (decision.verdict, decision.reason), (Verdict.DENY, reason)
                )
                self.assertIsNone(decision.invocation)

    async def test_the_budget_is_checked_before_an_approval_is_requested(self):
        h = Harness(budget=FakeBudget(BudgetStatus.EXCEEDED))
        decision = await h.broker.request(
            make_call("repo.delete_tree", {"path": f"{ROOT}/x"})
        )
        self.assertEqual(decision.reason, R.BUDGET_EXCEEDED)
        self.assertEqual(len(h.approvals._records), 0)

    async def test_a_failing_budget_provider_is_a_denial_without_its_message(self):
        h = Harness(budget=FakeBudget(error=ConnectionError(SECRET)))
        with self.assertLogs(level="ERROR") as logs:
            decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.BUDGET_UNAVAILABLE)
        )
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("ConnectionError", "\n".join(logs.output))
        self.assertNotIn(SECRET, "".join(e.model_dump_json() for e in h.sink.events))

    async def test_a_provider_answering_something_else_is_a_denial(self):
        for answer in ("within_budget", True, 1, None, "WITHIN_BUDGET"):
            with self.subTest(answer=answer):
                budget = FakeBudget()
                budget.status = answer
                h = Harness(budget=budget)
                decision = await h.broker.request(make_call("repo.read_file", READ))
                self.assertEqual(
                    (decision.verdict, decision.reason),
                    (Verdict.DENY, R.BUDGET_UNAVAILABLE),
                )

    async def test_a_provider_that_never_answers_times_out_into_a_denial(self):
        class Stuck(FakeBudget):
            async def check(self, task_id, tool):
                await asyncio.Event().wait()

        h = Harness(budget=Stuck(), timeout_seconds=0.05)
        decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.BUDGET_UNAVAILABLE)
        )

    async def test_a_pending_approval_does_not_charge_the_budget(self):
        h = Harness()
        await h.broker.request(make_call("repo.delete_tree", {"path": f"{ROOT}/x"}))
        self.assertEqual(h.budget.charges, [])


class AuditTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_verdict_is_audited_with_ids_and_enums_only(self):
        h = Harness()
        cases = [
            ("repo.read_file", READ, "allow", "auto"),
            ("repo.write_file", WRITE, "allow", "scoped_auto"),
            ("repo.delete_tree", {"path": f"{ROOT}/x"}, "deny", "approval_required"),
            ("git.merge", MERGE, "deny", "strong_approval_required"),
            ("repo.read_file", {"path": "/etc/passwd"}, "deny", "path_out_of_scope"),
            (
                "credentials.read",
                {"credential": HANDLE},
                "deny",
                "credential_plaintext_denied",
            ),
            ("nope", {}, "deny", "unknown_tool"),
        ]
        for tool, arguments, decision, reason in cases:
            with self.subTest(tool=tool, reason=reason):
                before = len(h.tool_events())
                await h.broker.request(make_call(tool, arguments))
                events = h.tool_events()
                self.assertEqual(len(events), before + 1)
                event = events[-1]
                self.assertEqual((event.decision, event.reason), (decision, reason))
                self.assertEqual(
                    event.action, "tool.unknown" if tool == "nope" else f"tool.{tool}"
                )
                self.assertEqual(
                    (event.actor_id, event.agent_id, event.resource_kind),
                    (U1, AGENT, "task"),
                )
                self.assertEqual((event.resource_id, event.project_id), (TASK, P1))
                self.assertEqual(event.occurred_at, NOW)

    async def test_no_argument_content_reaches_the_audit_trail(self):
        h = Harness()
        await h.broker.request(
            make_call(
                "repo.write_file", {"path": f"{ROOT}/{MARKER}", "content": MARKER}
            )
        )
        await h.broker.request(
            make_call("repo.write_file", {"path": "/etc/x", "content": MARKER})
        )
        await h.broker.request(
            make_call("repo.write_file", {"path": f"{ROOT}/a", "content": GITHUB_TOKEN})
        )
        await h.broker.request(make_call(MARKER, {"content": MARKER}))
        await h.broker.request(
            make_call("repo.write_file", {"content": MARKER, "evil": MARKER})
        )
        blob = "".join(e.model_dump_json() for e in h.sink.events)
        self.assertGreaterEqual(len(h.sink.events), 5)
        for text in (MARKER, GITHUB_TOKEN, "/etc/x", ROOT):
            self.assertNotIn(text, blob)

    async def test_an_allow_that_cannot_be_recorded_is_a_denial(self):
        h = Harness(broker_sink=FailingSink())
        with self.assertLogs(level="ERROR") as logs:
            decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.AUDIT_UNAVAILABLE)
        )
        self.assertIsNone(decision.invocation)
        self.assertFalse(decision)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertIn("ConnectionError", "\n".join(logs.output))

    async def test_a_hanging_audit_sink_times_out_into_a_denial(self):
        h = Harness(broker_sink=HangingSink(), timeout_seconds=0.05)
        decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason), (Verdict.DENY, R.AUDIT_UNAVAILABLE)
        )

    async def test_a_denial_and_a_pending_approval_stay_what_they_are_when_audit_fails(
        self,
    ):
        h = Harness(broker_sink=FailingSink())
        with self.assertLogs(level="ERROR"):
            denied = await h.broker.request(make_call("nope", {}))
            pending = await h.broker.request(
                make_call("repo.delete_tree", {"path": f"{ROOT}/x"})
            )
        self.assertEqual(denied.reason, R.UNKNOWN_TOOL)
        self.assertEqual(
            (pending.verdict, pending.reason),
            (Verdict.NEEDS_APPROVAL, R.APPROVAL_REQUIRED),
        )

    async def test_an_authz_audit_failure_denies_the_call_through_authz(self):
        # The PAW-025 layer records an agent's allow with fail-closed audit.
        h = Harness(sink=FailingSink())
        with self.assertLogs(level="ERROR"):
            decision = await h.broker.request(make_call("repo.read_file", READ))
        self.assertEqual(
            (decision.verdict, decision.reason, decision.authz_reason),
            (Verdict.DENY, R.AUTHZ_DENIED, Reason.AUDIT_UNAVAILABLE),
        )


class ConstructionTest(unittest.TestCase):
    def build(self, **overrides):
        sink = InMemoryAuditSink()
        arguments = {
            "registry": sample_registry(),
            "authorizer": Authorizer(sink),
            "approvals": InMemoryApprovalStore(),
            "audit": sink,
        }
        arguments.update(overrides)
        return ToolBroker(**arguments)

    def test_a_valid_broker_builds(self):
        self.assertIsInstance(self.build(), ToolBroker)

    def test_adapters_are_validated_up_front(self):
        class NoMethods:
            pass

        class SyncRecord:
            def record(self, event):
                return None

        class WrongArity:
            async def record(self, a, b, c):
                return None

        class WrongCheck:
            async def check(self):
                return None

            async def charge(self, task_id, tool):
                return None

        for name, value in (
            ("registry", ["repo.read_file"]),
            ("authorizer", NoMethods()),
            ("approvals", NoMethods()),
            ("audit", NoMethods()),
            ("audit", SyncRecord()),
            ("audit", WrongArity()),
            ("budget", WrongCheck()),
            ("budget", NoMethods()),
            ("path_resolver", NoMethods()),
            ("policy", {}),
        ):
            with self.subTest(name=name, value=type(value).__name__):
                with self.assertRaises(TypeError):
                    self.build(**{name: value})

    def test_listeners_are_validated(self):
        for listener in ("not callable", lambda: None, lambda a, b: None):
            with self.subTest(listener=repr(listener)[:20]):
                with self.assertRaises(TypeError):
                    self.build(listeners=[listener])
        self.build(listeners=[lambda event: None])

    def test_the_approval_ttl_and_timeout_are_bounded(self):
        from datetime import timedelta

        for ttl in (
            timedelta(seconds=59),
            timedelta(hours=25),
            timedelta(0),
            3600,
            None,
        ):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    self.build(approval_ttl=ttl)
        self.build(approval_ttl=timedelta(minutes=1))
        self.build(approval_ttl=timedelta(hours=24))
        for timeout in (0, -1):
            with self.assertRaises(ValueError):
                self.build(timeout_seconds=timeout)

    def test_a_task_context_is_validated(self):
        with self.assertRaises(ValueError):
            make_context(primary_project_id=P2)  # not part of the scope
        with self.assertRaises(ValueError):
            make_context(delegator_id=AGENT)  # the agent is not the user it acts for
        with self.assertRaises(TypeError):
            make_context(grant="everything")
        with self.assertRaises(TypeError):
            make_context(scope={"roots": ["/"]})
        with self.assertRaises(ValueError):
            make_context(task_id="not-a-uuid")
        self.assertIsInstance(make_context(), TaskContext)


if __name__ == "__main__":
    unittest.main()
