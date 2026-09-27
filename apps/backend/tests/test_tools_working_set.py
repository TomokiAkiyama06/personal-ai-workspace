"""The Working Set in the Tool Broker (issue #85, Decision 0030; no database).

* the role ceiling (section 4): what ``referenced`` / ``working`` / ``target`` let a
  call do on a repository, on top of its ACL; an unresolved role denies everything;
  an approval never lifts the ceiling; execution in a ``referenced`` repository is
  refused (#85 constraint 2); a write behind a capability the ceiling does not
  cover is refused, at registration and at run time;
* an allowed repository write is recorded as a change of the repository, or it
  does not run; it holds a reservation until the call ended (Codex review), which
  the broker releases after the executor, or at once when the call does not run;
* the Working Set tools (section 3): one per operation, SCOPED_AUTO only to add a
  ``referenced`` repository, decided on the task's project AND on the repository
  (its registered ACL, with the permission of the change);
* the new capability's grants and delegation (#85 constraint 3).

The database side (``TaskService.change_working_set``, the executor) is in
``test_task_working_set``.
"""

import unittest
import uuid
from datetime import UTC, datetime

from paw_backend.authz import (
    CAPABILITIES,
    Capability,
    ProjectRole,
    ProjectState,
    Reason,
    RepoAcl,
    RepoPermission,
    SystemRole,
)
from paw_backend.authz.policy import DEFAULT_POLICY
from paw_backend.tasks import RepoRole, WorkingSetOperation, WorkingSetRepository
from paw_backend.tasks.domain import Actor
from paw_backend.tasks.working_set import WRITE_RESERVATION_SECONDS
from paw_backend.tools import (
    ROLE_WRITE_CEILING,
    WORKING_SET_TOOL_SPECS,
    ApprovalLevel,
    ArgumentKind,
    ArgumentSpec,
    BrokerReason,
    Environment,
    FailClosedUseGate,
    ScopedRepository,
    ToolBroker,
    ToolCapability,
    ToolRegistry,
    ToolSpec,
    Verdict,
    with_working_set_roles,
)
from paw_backend.tools.runner import MAX_EXECUTION_TIMEOUT, ExecutionStatus
from paw_backend.tools.working_set import TOOL_OPERATIONS

from .authz_support import P1, P2, U1, U2, FailingSink, StaticDirectory, principal
from .tools_support import (
    ROOT,
    FakeExecutor,
    Harness,
    Registrations,
    UseGate,
    make_call,
    make_context,
    make_grant,
    make_scope,
    sample_specs,
)

C = ToolCapability
A = ArgumentKind
R = BrokerReason
REFERENCED, WORKING, TARGET = RepoRole.REFERENCED, RepoRole.WORKING, RepoRole.TARGET
MANAGE = Capability.PROJECT_TASK_WORKING_SET_MANAGE

# Three repositories of P1, one per role (and one whose role is not resolved).
REF = uuid.UUID(int=801)
WRK = uuid.UUID(int=802)
TGT = uuid.UUID(int=803)
UNK = uuid.UUID(int=804)
# A registered repository of P1 that is not in the Working Set yet.
NEW = uuid.UUID(int=805)


def executes() -> ToolSpec:
    """Tests / a build / a command in a repository (``project.task.run``)."""
    return ToolSpec(
        "tests.run_in",
        frozenset({C.EXECUTE}),
        Capability.PROJECT_TASK_RUN,
        {"path": ArgumentSpec(A.PATH)},
    )


def url_writer() -> ToolSpec:
    """A write that names its repository with a URL only, behind a capability that
    is not a repository write: registration cannot see the repository."""
    return ToolSpec(
        "api.post",
        frozenset({C.WRITE, C.NETWORK}),
        Capability.PROJECT_TASK_RUN,
        {"url": ArgumentSpec(A.URL)},
    )


def registry() -> ToolRegistry:
    return ToolRegistry(
        [*sample_specs(), executes(), url_writer(), *WORKING_SET_TOOL_SPECS]
    )


def repository(repo_id, name, role, acl=None, project=P1):
    return ScopedRepository(
        repo_id,
        project,
        f"{ROOT}/{name}",
        RepoAcl.inherit(repo_id, project) if acl is None else acl,
        remotes=[f"https://github.com/org/{name}"],
        role=role,
    )


def scope(**overrides):
    arguments = {
        "repositories": [
            repository(REF, "ref", REFERENCED),
            repository(WRK, "wrk", WORKING),
            repository(TGT, "tgt", TARGET),
            repository(UNK, "unk", None),
        ]
    }
    arguments.update(overrides)
    return make_scope(**arguments)


def context(*capabilities, **overrides):
    grant = make_grant(
        *(
            capabilities
            or (
                Capability.PROJECT_READ,
                Capability.PROJECT_REPO_WRITE,
                Capability.PROJECT_TASK_RUN,
                Capability.PROJECT_PR_CREATE,
                MANAGE,
            )
        )
    )
    arguments = {"scope": scope(), "grant": grant}
    arguments.update(overrides)
    return make_context(**arguments)


def harness(**overrides) -> Harness:
    overrides.setdefault("registry", registry())
    overrides.setdefault(
        "registrations",
        Registrations(
            *(RepoAcl.inherit(r, P1) for r in (REF, WRK, TGT, UNK, NEW)),
        ),
    )
    return Harness(**overrides)


async def decide(tool, arguments, ctx=None, h=None, approval_id=None):
    h = h or harness()
    return await h.broker.request(
        make_call(tool, arguments, context=ctx or context()), approval_id=approval_id
    )


def write(name):
    return {"path": f"{ROOT}/{name}/x.py", "content": "1"}


def outcome(decision):
    return (decision.verdict, decision.reason)


class RoleCeilingTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_ceiling_table_is_the_decisions(self):
        self.assertEqual(
            dict(ROLE_WRITE_CEILING),
            {
                REFERENCED: frozenset(),
                WORKING: frozenset({Capability.PROJECT_REPO_WRITE}),
                TARGET: frozenset(
                    {Capability.PROJECT_REPO_WRITE, Capability.PROJECT_PR_CREATE}
                ),
            },
        )

    async def test_a_referenced_repository_is_read_only(self):
        self.assertEqual(
            outcome(await decide("repo.read_file", {"path": f"{ROOT}/ref/x"})),
            (Verdict.ALLOW, R.AUTO),
        )
        for tool in ("repo.write_file", "repo.delete_tree"):
            with self.subTest(tool=tool):
                arguments = (
                    write("ref")
                    if tool == "repo.write_file"
                    else {"path": f"{ROOT}/ref/x"}
                )
                self.assertEqual(
                    outcome(await decide(tool, arguments)),
                    (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT),
                )

    async def test_a_working_repository_is_written_but_gets_no_pull_request(self):
        self.assertEqual(
            outcome(await decide("repo.write_file", write("wrk"))),
            (Verdict.ALLOW, R.SCOPED_AUTO),
        )
        pr = {
            "url": "https://github.com/org/wrk",
            "repository": str(WRK),
            "title": "t",
        }
        self.assertEqual(
            outcome(await decide("issues.create", pr)),
            (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT),
        )
        target_pr = {**pr, "url": "https://github.com/org/tgt", "repository": str(TGT)}
        self.assertEqual(
            outcome(await decide("issues.create", target_pr)),
            (Verdict.ALLOW, R.SCOPED_AUTO),
        )

    async def test_an_unresolved_role_denies_every_call_even_a_read(self):
        for tool, arguments in (
            ("repo.read_file", {"path": f"{ROOT}/unk/x"}),
            ("repo.write_file", write("unk")),
            ("tests.run_in", {"path": f"{ROOT}/unk"}),
        ):
            with self.subTest(tool=tool):
                self.assertEqual(
                    outcome(await decide(tool, arguments)),
                    (Verdict.DENY, R.REPOSITORY_ROLE_UNRESOLVED),
                )

    async def test_an_approval_cannot_lift_the_ceiling(self):
        # git.merge needs STRONG_APPROVAL: on a referenced repository no approval
        # is even opened, and an approval id changes nothing.
        h = harness()
        merge = {
            "remote": "https://github.com/org/ref",
            "repository": str(REF),
            "credential": "cred_" + "a1" * 16,
            "pull_request": 7,
        }
        ref_scope = scope(credential_handles={"cred_" + "a1" * 16: ["github.com"]})
        for approval_id in (None, uuid.uuid4()):
            with self.subTest(approval_id=approval_id):
                decision = await decide(
                    "git.merge",
                    merge,
                    context(scope=ref_scope),
                    h,
                    approval_id=approval_id,
                )
                self.assertEqual(
                    outcome(decision), (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT)
                )
        self.assertEqual(len(h.approvals._records), 0)

    async def test_a_path_in_a_target_nested_in_a_referenced_one_is_capped_by_both(
        self,
    ):
        nested = scope(
            repositories=[
                repository(REF, "outer", REFERENCED),
                repository(TGT, "outer/inner", TARGET),
            ]
        )
        self.assertEqual(
            outcome(
                await decide(
                    "repo.write_file", write("outer/inner"), context(scope=nested)
                )
            ),
            (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT),
        )

    async def test_the_acl_still_applies_inside_the_ceiling(self):
        read_only = RepoAcl.override(TGT, P1, {RepoPermission.READ})
        narrow = scope(repositories=[repository(TGT, "tgt", TARGET, read_only)])
        decision = await decide("repo.write_file", write("tgt"), context(scope=narrow))
        self.assertEqual(
            (decision.verdict, decision.reason, decision.authz_reason),
            (Verdict.DENY, R.AUTHZ_DENIED, Reason.REPO_ACL_FORBIDS),
        )


class ExecutionCeilingTest(unittest.IsolatedAsyncioTestCase):
    """#85 constraint 2: executing in a repository needs ``working`` / ``target``."""

    async def test_execution_is_refused_in_a_referenced_repository(self):
        self.assertEqual(
            outcome(await decide("tests.run_in", {"path": f"{ROOT}/ref"})),
            (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT),
        )

    async def test_execution_runs_in_a_working_or_target_repository(self):
        for name in ("wrk", "tgt"):
            with self.subTest(name=name):
                self.assertEqual(
                    outcome(await decide("tests.run_in", {"path": f"{ROOT}/{name}"})),
                    (Verdict.ALLOW, R.SCOPED_AUTO),
                )

    async def test_execution_outside_every_repository_is_not_capped(self):
        # The task's root but no repository of the Working Set.
        self.assertEqual(
            outcome(await decide("tests.run_in", {"path": f"{ROOT}/build"})),
            (Verdict.ALLOW, R.SCOPED_AUTO),
        )


class WriteCapabilityMismatchTest(unittest.IsolatedAsyncioTestCase):
    def test_registration_refuses_a_write_with_a_path_behind_another_capability(
        self,
    ):
        for caps in ({C.WRITE}, {C.DESTRUCTIVE}, {C.WRITE, C.EXECUTE}):
            for kind in (A.PATH, A.REPOSITORY):
                with self.subTest(caps=caps, kind=kind):
                    with self.assertRaises(ValueError):
                        ToolSpec(
                            "shell.write",
                            frozenset(caps),
                            Capability.PROJECT_TASK_RUN,
                            {"t": ArgumentSpec(kind)},
                        )
        # an optional path counts too
        with self.assertRaises(ValueError):
            ToolSpec(
                "shell.write",
                frozenset({C.WRITE}),
                Capability.PROJECT_TASK_RUN,
                {
                    "project": ArgumentSpec(A.PROJECT),
                    "p": ArgumentSpec(A.PATH, required=False),
                },
            )

    def test_execution_with_a_path_may_have_another_capability(self):
        self.assertIs(executes().authz_capability, Capability.PROJECT_TASK_RUN)

    async def test_a_write_that_reaches_a_repository_by_its_url_is_refused(self):
        # Registration cannot tell which repository a URL belongs to: the broker
        # checks the call, whatever the environment.
        for environment in (Environment.PROJECT_LOCAL, Environment.HOST):
            spec = ToolSpec(
                "api.post",
                frozenset({C.WRITE, C.NETWORK}),
                Capability.PROJECT_TASK_RUN,
                {"url": ArgumentSpec(A.URL)},
                environment=environment,
            )
            h = harness(registry=ToolRegistry([spec]))
            with self.subTest(environment=environment.value):
                decision = await decide(
                    "api.post", {"url": "https://github.com/org/tgt/issues"}, h=h
                )
                self.assertEqual(
                    outcome(decision),
                    (Verdict.DENY, R.REPOSITORY_WRITE_CAPABILITY_MISMATCH),
                )
                self.assertEqual(len(h.approvals._records), 0)

    async def test_a_url_of_no_repository_is_not_affected(self):
        decision = await decide(
            "api.post", {"url": "https://api.github.com/meta"}, context()
        )
        self.assertNotEqual(decision.reason, R.REPOSITORY_WRITE_CAPABILITY_MISMATCH)


class RepositoryUseTest(unittest.IsolatedAsyncioTestCase):
    """Every allowed call that touches a repository is admitted on the roles
    stored NOW (Decision 0030, 4.1 / 4.6: the table is the one truth), not on the
    roles of the caller's task scope; a write or something executed marks the
    repository as changed (section 5)."""

    async def test_an_allowed_write_is_recorded_on_the_repositories_it_touches(self):
        h = harness()
        ctx = context()
        decision = await decide("repo.write_file", write("wrk"), ctx, h)
        self.assertTrue(decision.allowed)
        self.assertEqual(h.use_gate.changes(), [(ctx.task_id, ctx.run, (WRK,))])

    async def test_an_execution_marks_the_repository_changed_and_a_read_does_not(
        self,
    ):
        # A command or a build can write too: the backend cannot tell, so it counts
        # as a change (fail-closed).
        h = harness()
        ctx = context()
        await decide("repo.read_file", {"path": f"{ROOT}/wrk/x"}, ctx, h)
        await decide("tests.run_in", {"path": f"{ROOT}/wrk"}, ctx, h)
        self.assertEqual(h.use_gate.changes(), [(ctx.task_id, ctx.run, (WRK,))])
        # The read was admitted on the stored role too, without marking anything.
        self.assertEqual(
            [(use[2], use[3], use[4]) for use in h.use_gate.uses],
            [
                ((WRK,), Capability.PROJECT_READ, False),
                ((WRK,), Capability.PROJECT_TASK_RUN, True),
            ],
        )

    async def test_a_use_that_cannot_be_admitted_does_not_run(self):
        h = harness(use_gate=UseGate(error=OSError("down")))
        with self.assertLogs("paw_backend.tools.broker", "ERROR"):
            decision = await decide("repo.write_file", write("wrk"), h=h)
        self.assertEqual(
            outcome(decision), (Verdict.DENY, R.REPOSITORY_WRITE_UNRECORDED)
        )
        self.assertIsNone(decision.invocation)
        (row,) = h.tool_events()
        self.assertEqual(
            (row.decision, row.reason), ("deny", "repository_write_unrecorded")
        )
        # A read whose stored role cannot be read: unresolved, never allowed.
        with self.assertLogs("paw_backend.tools.broker", "ERROR"):
            read = await decide("repo.read_file", {"path": f"{ROOT}/wrk/x"}, h=h)
        self.assertEqual(outcome(read), (Verdict.DENY, R.REPOSITORY_ROLE_UNRESOLVED))

    async def test_without_a_gate_no_repository_is_used(self):
        h = harness()
        broker = ToolBroker(
            registry(),
            h.authorizer,
            h.approvals,
            h.sink,
            budget=h.budget,
            task_activity=h.task_activity,
            path_resolver=h.broker._resolver,
            registrations=h.registrations,
        )
        self.assertIsInstance(broker._use_gate, FailClosedUseGate)
        for tool, arguments, reason in (
            ("repo.write_file", write("wrk"), R.REPOSITORY_WRITE_UNRECORDED),
            ("repo.read_file", {"path": f"{ROOT}/wrk/x"}, R.REPOSITORY_ROLE_UNRESOLVED),
        ):
            with self.subTest(tool=tool):
                with self.assertLogs("paw_backend.tools.broker", "ERROR"):
                    decision = await broker.request(
                        make_call(tool, arguments, context=context())
                    )
                self.assertEqual(outcome(decision), (Verdict.DENY, reason))

    async def test_a_call_that_touches_no_repository_is_not_gated(self):
        h = harness(use_gate=UseGate(error=OSError("down")))
        decision = await decide("tests.run_in", {"path": f"{ROOT}/build"}, h=h)
        self.assertEqual(outcome(decision), (Verdict.ALLOW, R.SCOPED_AUTO))

    async def test_a_consumed_approval_is_recorded_before_the_write_runs(self):
        h = harness()
        ctx = context()
        delete = {"path": f"{ROOT}/tgt/build"}
        pending = await decide("repo.delete_tree", delete, ctx, h)
        self.assertEqual(pending.verdict, Verdict.NEEDS_APPROVAL)
        self.assertEqual(h.use_gate.changes(), [])
        await h.service.approve(
            pending.approval_id,
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
        )
        used = await decide(
            "repo.delete_tree", delete, ctx, h, approval_id=pending.approval_id
        )
        self.assertTrue(used.allowed)
        self.assertEqual(h.use_gate.changes(), [(ctx.task_id, ctx.run, (TGT,))])

    async def test_an_approval_is_not_used_up_by_a_call_that_is_not_admitted(self):
        """The use is admitted BEFORE the one-shot approval is consumed: a call
        the gate refuses (or cannot answer) leaves the human's approval usable."""
        h = harness()
        ctx = context()
        delete = {"path": f"{ROOT}/tgt/build"}
        pending = await decide("repo.delete_tree", delete, ctx, h)
        await h.service.approve(
            pending.approval_id,
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
        )
        h.use_gate.error = TimeoutError()
        with self.assertLogs("paw_backend.tools.broker", "ERROR"):
            failed = await decide(
                "repo.delete_tree", delete, ctx, h, approval_id=pending.approval_id
            )
        self.assertEqual(outcome(failed), (Verdict.DENY, R.REPOSITORY_WRITE_UNRECORDED))
        h.use_gate.error = None
        used = await decide(
            "repo.delete_tree", delete, ctx, h, approval_id=pending.approval_id
        )
        self.assertEqual(outcome(used), (Verdict.ALLOW, R.APPROVAL_CONSUMED))


class ObservingExecutor(FakeExecutor):
    """Notes which reservations were still held while it ran."""

    def __init__(self, gate: UseGate, **kwargs) -> None:
        super().__init__(**kwargs)
        self.gate = gate
        self.held: list[list[uuid.UUID]] = []

    async def execute(self, invocation):
        self.held.append(self.gate.in_flight())
        return await super().execute(invocation)


class WriteReservationTest(unittest.IsolatedAsyncioTestCase):
    """Codex review of #85 (P1): an admitted repository write keeps its
    admission (the reservation the gate returns) through the execution; the
    broker releases it only once the executor returned or failed, and at once
    for a call that does not run after all."""

    async def test_the_reservation_is_held_while_the_executor_runs(self):
        for error in (None, OSError("disk")):
            with self.subTest(error=error):
                gate = UseGate()
                executor = ObservingExecutor(gate, error=error)
                h = harness(use_gate=gate, executor=executor)
                ctx = context()
                if error is None:
                    result = await h.runner.run(
                        make_call("repo.write_file", write("wrk"), context=ctx)
                    )
                else:
                    with self.assertLogs("paw_backend.tools.runner", "ERROR"):
                        result = await h.runner.run(
                            make_call("repo.write_file", write("wrk"), context=ctx)
                        )
                (reservation,) = gate.reservations
                self.assertEqual(result.decision.reservation_id, reservation)
                self.assertEqual(executor.held, [[reservation]])
                self.assertEqual(gate.released, [(ctx.task_id, reservation)])
                self.assertEqual(
                    result.status,
                    ExecutionStatus.COMPLETED
                    if error is None
                    else ExecutionStatus.FAILED,
                )

    async def test_an_execution_is_reserved_and_a_read_is_not(self):
        h = harness()
        read = await decide("repo.read_file", {"path": f"{ROOT}/wrk/x"}, h=h)
        self.assertTrue(read.allowed)
        self.assertIsNone(read.reservation_id)
        run = await decide("tests.run_in", {"path": f"{ROOT}/wrk"}, h=h)
        self.assertTrue(run.allowed)
        self.assertEqual(h.use_gate.reservations, [run.reservation_id])

    async def test_a_write_admitted_without_a_reservation_does_not_run(self):
        h = harness()
        h.use_gate.reserve = False
        with self.assertLogs("paw_backend.tools.broker", "ERROR"):
            decision = await decide("repo.write_file", write("wrk"), h=h)
        self.assertEqual(
            outcome(decision), (Verdict.DENY, R.REPOSITORY_WRITE_UNRECORDED)
        )
        self.assertIsNone(decision.invocation)
        # A read needs none.
        read = await decide("repo.read_file", {"path": f"{ROOT}/wrk/x"}, h=h)
        self.assertTrue(read.allowed)

    async def test_an_approval_that_is_not_consumed_releases_the_reservation(self):
        h = harness()
        ctx = context()
        pending = await decide("repo.delete_tree", {"path": f"{ROOT}/tgt/a"}, ctx, h)
        await h.service.approve(
            pending.approval_id,
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
        )
        # The approval is for another call: admitted first, then not consumed.
        refused = await decide(
            "repo.delete_tree",
            {"path": f"{ROOT}/tgt/b"},
            ctx,
            h,
            approval_id=pending.approval_id,
        )
        self.assertEqual(refused.verdict, Verdict.DENY)
        self.assertIsNone(refused.reservation_id)
        (reservation,) = h.use_gate.reservations
        self.assertEqual(h.use_gate.released, [(ctx.task_id, reservation)])
        self.assertEqual(h.use_gate.in_flight(), [])

    async def test_a_decision_that_cannot_be_recorded_releases_the_reservation(self):
        h = harness(broker_sink=FailingSink())
        ctx = context()
        with self.assertLogs("paw_backend", "ERROR"):
            decision = await decide("repo.write_file", write("wrk"), ctx, h)
        self.assertEqual(outcome(decision), (Verdict.DENY, R.AUDIT_UNAVAILABLE))
        self.assertIsNone(decision.reservation_id)
        (reservation,) = h.use_gate.reservations
        self.assertEqual(h.use_gate.released, [(ctx.task_id, reservation)])

    async def test_a_release_that_fails_does_not_fail_the_call(self):
        """It expires instead (fail-closed: the repository stays held until then)."""
        h = harness()
        h.use_gate.release_error = OSError("down")
        with self.assertLogs("paw_backend.tools.broker", "ERROR"):
            result = await h.runner.run(
                make_call("repo.write_file", write("wrk"), context=context())
            )
        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        self.assertEqual(len(h.use_gate.in_flight()), 1)

    def test_a_reservation_outlives_the_longest_call(self):
        self.assertGreater(WRITE_RESERVATION_SECONDS, MAX_EXECUTION_TIMEOUT)


class StoredRoleTest(unittest.IsolatedAsyncioTestCase):
    """A task scope built before a downgrade or a removal shows a role the stored
    Working Set no longer holds: the stored one decides (Claude review, P1)."""

    def gate(self, **roles):
        stored = {REF: REFERENCED, WRK: WORKING, TGT: TARGET}
        stored.update(roles)
        return UseGate(roles={k: v for k, v in stored.items() if v is not None})

    async def test_a_pull_request_on_a_target_downgraded_meanwhile_is_refused(self):
        h = harness(use_gate=self.gate())
        h.use_gate.roles[TGT] = WORKING  # the scope still says target
        pr = {
            "url": "https://github.com/org/tgt",
            "repository": str(TGT),
            "title": "t",
        }
        decision = await decide("issues.create", pr, h=h)
        self.assertEqual(
            outcome(decision), (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT)
        )
        self.assertEqual(h.use_gate.uses, [])

    async def test_execution_in_a_repository_downgraded_to_referenced_is_refused(
        self,
    ):
        h = harness(use_gate=self.gate())
        h.use_gate.roles[WRK] = REFERENCED
        decision = await decide("tests.run_in", {"path": f"{ROOT}/wrk"}, h=h)
        self.assertEqual(
            outcome(decision), (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT)
        )

    async def test_a_repository_removed_meanwhile_is_refused_even_for_a_read(self):
        h = harness(use_gate=self.gate())
        del h.use_gate.roles[WRK]
        for tool, arguments in (
            ("repo.read_file", {"path": f"{ROOT}/wrk/x"}),
            ("repo.write_file", write("wrk")),
        ):
            with self.subTest(tool=tool):
                decision = await decide(tool, arguments, h=h)
                self.assertEqual(
                    outcome(decision), (Verdict.DENY, R.REPOSITORY_ROLE_UNRESOLVED)
                )

    async def test_a_refused_use_leaves_the_approval_unused(self):
        h = harness(use_gate=self.gate())
        ctx = context()
        delete = {"path": f"{ROOT}/tgt/build"}
        pending = await decide("repo.delete_tree", delete, ctx, h)
        await h.service.approve(
            pending.approval_id,
            principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
        )
        h.use_gate.roles[TGT] = REFERENCED
        refused = await decide(
            "repo.delete_tree", delete, ctx, h, approval_id=pending.approval_id
        )
        self.assertEqual(
            outcome(refused), (Verdict.DENY, R.REPOSITORY_ROLE_INSUFFICIENT)
        )
        (record,) = h.approvals._records.values()
        self.assertIsNone(record.consumed_at)


class WorkingSetToolSpecTest(unittest.TestCase):
    def test_one_tool_per_operation_at_its_level(self):
        specs = {spec.name: spec for spec in WORKING_SET_TOOL_SPECS}
        self.assertEqual(set(specs), set(TOOL_OPERATIONS))
        for name, spec in specs.items():
            with self.subTest(name=name):
                operation = TOOL_OPERATIONS[name]
                self.assertIs(spec.working_set_operation, operation)
                self.assertIs(spec.authz_capability, MANAGE)
                self.assertEqual(
                    {a.kind for a in spec.arguments.values()},
                    {A.WORKING_SET_REPOSITORY},
                )
                self.assertIs(
                    spec.min_level,
                    ApprovalLevel.SCOPED_AUTO
                    if operation is WorkingSetOperation.ADD_REFERENCED
                    else ApprovalLevel.STRONG_APPROVAL,
                )

    def spec(self, **overrides):
        arguments = {
            "name": "ws.change",
            "capabilities": frozenset({C.WRITE}),
            "authz_capability": MANAGE,
            "arguments": {"repository": ArgumentSpec(A.WORKING_SET_REPOSITORY)},
            "min_level": ApprovalLevel.STRONG_APPROVAL,
            "working_set_operation": WorkingSetOperation.SET_WORKING,
        }
        arguments.update(overrides)
        return ToolSpec(**arguments)

    def test_a_valid_declaration(self):
        self.assertIs(
            self.spec().working_set_operation, WorkingSetOperation.SET_WORKING
        )

    def test_what_registration_refuses(self):
        cases = {
            "manage without an operation": {"working_set_operation": None},
            "an operation without manage": {
                "authz_capability": Capability.PROJECT_TASK_RUN
            },
            "the repository kind elsewhere": {
                "authz_capability": Capability.PROJECT_REPO_WRITE,
                "working_set_operation": None,
                "arguments": {
                    "path": ArgumentSpec(A.PATH),
                    "repository": ArgumentSpec(A.WORKING_SET_REPOSITORY),
                },
            },
            "no repository": {"arguments": {"note": ArgumentSpec(A.TEXT)}},
            "an optional repository": {
                "arguments": {
                    "repository": ArgumentSpec(A.WORKING_SET_REPOSITORY, required=False)
                }
            },
            "two repositories": {
                "arguments": {
                    "a": ArgumentSpec(A.WORKING_SET_REPOSITORY),
                    "b": ArgumentSpec(A.WORKING_SET_REPOSITORY),
                }
            },
            "another target too": {
                "arguments": {
                    "repository": ArgumentSpec(A.WORKING_SET_REPOSITORY),
                    "path": ArgumentSpec(A.PATH),
                }
            },
            "below its operation's level": {"min_level": ApprovalLevel.APPROVAL},
            "adding referenced below scoped_auto": {
                "working_set_operation": WorkingSetOperation.ADD_REFERENCED,
                "min_level": ApprovalLevel.AUTO,
            },
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.spec(**overrides)

    def test_an_operation_is_a_member_not_prose(self):
        with self.assertRaises(ValueError):
            self.spec(working_set_operation="SET_WORKING")
        self.assertIs(
            self.spec(working_set_operation="set_working").working_set_operation,
            WorkingSetOperation.SET_WORKING,
        )


class CapabilityGrantTest(unittest.TestCase):
    """#85 constraint 3: who holds ``project.task.working_set.manage``."""

    def test_it_is_a_delegable_project_capability(self):
        info = CAPABILITIES[MANAGE]
        self.assertEqual((info.scope.value, info.delegable), ("project", True))

    def test_contributors_and_managers_hold_it_viewers_do_not(self):
        self.assertEqual(
            {
                role
                for role in ProjectRole
                if DEFAULT_POLICY.project_role_allows(role, MANAGE)
            },
            {ProjectRole.MANAGER, ProjectRole.CONTRIBUTOR},
        )


class WorkingSetDecisionTest(unittest.IsolatedAsyncioTestCase):
    """A Working Set tool is decided on the project AND on the repository."""

    ADD = "task.working_set.add_referenced"

    async def test_adding_a_registered_repository_as_referenced_is_scoped_auto(self):
        h = harness()
        decision = await decide(self.ADD, {"repository": str(NEW)}, h=h)
        self.assertEqual(outcome(decision), (Verdict.ALLOW, R.SCOPED_AUTO))
        self.assertEqual(h.registrations.asked, [NEW])
        rows = {(e.action, e.resource_kind, e.repo_id) for e in h.sink.events}
        self.assertIn(("project.task.working_set.manage", "project", None), rows)
        # the repository, on its proxy capability (READ): only denials of
        # ``project.read`` are persisted, so the allowed one leaves no row
        self.assertEqual(decision.invocation.arguments["repository"], str(NEW))

    async def test_every_other_change_needs_strong_approval(self):
        cases = {
            "task.working_set.set_working": NEW,
            "task.working_set.set_target": WRK,
            "task.working_set.downgrade_to_working": TGT,
            "task.working_set.downgrade_to_referenced": WRK,
            "task.working_set.remove": REF,
        }
        for tool, repo_id in cases.items():
            with self.subTest(tool=tool):
                decision = await decide(tool, {"repository": str(repo_id)})
                self.assertEqual(
                    (decision.verdict, decision.reason, decision.level),
                    (
                        Verdict.NEEDS_APPROVAL,
                        R.STRONG_APPROVAL_REQUIRED,
                        ApprovalLevel.STRONG_APPROVAL,
                    ),
                )

    async def test_an_operation_that_does_not_fit_the_role_is_refused(self):
        cases = {
            self.ADD: REF,  # already in the Working Set
            "task.working_set.set_working": WRK,
            "task.working_set.set_target": TGT,
            "task.working_set.downgrade_to_working": WRK,
            "task.working_set.downgrade_to_referenced": REF,
            "task.working_set.remove": NEW,  # not in the Working Set
        }
        for tool, repo_id in cases.items():
            with self.subTest(tool=tool):
                self.assertEqual(
                    outcome(await decide(tool, {"repository": str(repo_id)})),
                    (Verdict.DENY, R.WORKING_SET_CHANGE_INVALID),
                )

    async def test_a_repository_whose_role_is_unknown_cannot_be_narrowed(self):
        for tool in (
            "task.working_set.downgrade_to_referenced",
            "task.working_set.remove",
        ):
            with self.subTest(tool=tool):
                self.assertEqual(
                    outcome(await decide(tool, {"repository": str(UNK)})),
                    (Verdict.DENY, R.REPOSITORY_ROLE_UNRESOLVED),
                )

    async def test_an_unregistered_repository_is_refused(self):
        h = harness(registrations=Registrations())
        self.assertEqual(
            outcome(await decide(self.ADD, {"repository": str(NEW)}, h=h)),
            (Verdict.DENY, R.WORKING_SET_REPOSITORY_UNRESOLVED),
        )

    async def test_a_registration_that_fails_is_refused(self):
        h = harness(registrations=Registrations(error=OSError("db")))
        with self.assertLogs("paw_backend.tools.broker", "ERROR"):
            decision = await decide(self.ADD, {"repository": str(NEW)}, h=h)
        self.assertEqual(
            outcome(decision), (Verdict.DENY, R.WORKING_SET_REPOSITORY_UNRESOLVED)
        )

    async def test_a_registration_for_another_repository_is_refused(self):
        h = harness(registrations=Registrations())
        h.registrations.acls[NEW] = RepoAcl.inherit(uuid.uuid4(), P1)
        self.assertEqual(
            outcome(await decide(self.ADD, {"repository": str(NEW)}, h=h)),
            (Verdict.DENY, R.WORKING_SET_REPOSITORY_UNRESOLVED),
        )

    async def test_a_repository_of_a_project_outside_the_scope_is_refused(self):
        h = harness(registrations=Registrations(RepoAcl.inherit(NEW, P2)))
        self.assertEqual(
            outcome(await decide(self.ADD, {"repository": str(NEW)}, h=h)),
            (Verdict.DENY, R.REPOSITORY_OUT_OF_SCOPE),
        )

    async def test_the_repository_acl_must_allow_the_change(self):
        no_access = RepoAcl.override(NEW, P1, set())
        read_only = RepoAcl.override(
            NEW, P1, {RepoPermission.READ, RepoPermission.AGENT}
        )
        cases = [
            (self.ADD, no_access),
            ("task.working_set.set_working", read_only),
            ("task.working_set.set_target", read_only),
        ]
        for tool, acl in cases:
            with self.subTest(tool=tool, acl=acl.allowed):
                h = harness(registrations=Registrations(acl))
                decision = await decide(tool, {"repository": str(NEW)}, h=h)
                self.assertEqual(
                    (decision.verdict, decision.reason, decision.authz_reason),
                    (Verdict.DENY, R.AUTHZ_DENIED, Reason.REPO_ACL_FORBIDS),
                )
                self.assertEqual(len(h.approvals._records), 0)
        # read is enough to add a referenced one
        h = harness(registrations=Registrations(read_only))
        self.assertTrue((await decide(self.ADD, {"repository": str(NEW)}, h=h)).allowed)

    async def test_narrowing_needs_the_permission_of_the_role_it_had(self):
        read_only = {RepoPermission.READ, RepoPermission.AGENT}
        for repo_id, needs_write in ((REF, False), (WRK, True)):
            with self.subTest(repo_id=repo_id):
                h = harness(
                    registrations=Registrations(
                        RepoAcl.override(repo_id, P1, read_only)
                    )
                )
                decision = await decide(
                    "task.working_set.remove", {"repository": str(repo_id)}, h=h
                )
                if needs_write:
                    self.assertEqual(
                        (decision.verdict, decision.authz_reason),
                        (Verdict.DENY, Reason.REPO_ACL_FORBIDS),
                    )
                else:
                    self.assertEqual(decision.verdict, Verdict.NEEDS_APPROVAL)

    async def test_the_project_capability_is_needed_too(self):
        # A grant without it, and a Viewer who does not hold it.
        without = context(Capability.PROJECT_READ, Capability.PROJECT_REPO_WRITE)
        decision = await decide(self.ADD, {"repository": str(NEW)}, without)
        self.assertEqual(
            (decision.verdict, decision.authz_reason),
            (Verdict.DENY, Reason.AGENT_CAPABILITY_NOT_GRANTED),
        )
        viewer = context(delegator_id=U2)
        h = harness(
            directory=StaticDirectory(
                principal(SystemRole.USER, U1, {P1: ProjectRole.CONTRIBUTOR}),
                principal(SystemRole.USER, U2, {P1: ProjectRole.VIEWER}),
            )
        )
        decision = await decide(self.ADD, {"repository": str(NEW)}, viewer, h)
        self.assertEqual(
            (decision.verdict, decision.authz_reason),
            (Verdict.DENY, Reason.CAPABILITY_NOT_GRANTED),
        )

    async def test_an_archived_project_refuses_every_change(self):
        archived = context(scope=scope(projects={P1: ProjectState.ARCHIVED}))
        decision = await decide(self.ADD, {"repository": str(NEW)}, archived)
        self.assertEqual(
            (decision.verdict, decision.authz_reason),
            (Verdict.DENY, Reason.PROJECT_STATE_FORBIDS),
        )

    async def test_the_repository_is_named_as_a_uuid_only(self):
        for bad in ("not-a-uuid", "", str(uuid.UUID(int=0xABC)).upper(), NEW.hex, 5):
            with self.subTest(bad=bad):
                decision = await decide(self.ADD, {"repository": bad})
                self.assertEqual(decision.verdict, Verdict.DENY)
                self.assertIn(decision.reason, (R.INVALID_TARGET, R.INVALID_ARGUMENTS))


class WithWorkingSetRolesTest(unittest.TestCase):
    def test_roles_come_from_the_stored_working_set_only(self):
        now = datetime(2026, 9, 28, tzinfo=UTC)
        stored = (
            WorkingSetRepository(TGT, TARGET, "a" * 40, Actor.system(), now, now),
            WorkingSetRepository(REF, REFERENCED, None, Actor.system(), now, now),
        )
        entries = [
            repository(TGT, "tgt", None),
            repository(REF, "ref", None),
            # another checkout (encloses the task's): not in the Working Set
            repository(UNK, "unk", None),
        ]
        resolved = with_working_set_roles(entries, stored)
        self.assertEqual(
            [(r.repo_id, r.role) for r in resolved],
            [(TGT, TARGET), (REF, REFERENCED), (UNK, None)],
        )
        # everything else is kept as it was
        self.assertEqual(
            [(r.root, r.acl, r.remotes) for r in resolved],
            [(e.root, e.acl, e.remotes) for e in entries],
        )

    def test_a_role_is_a_role_member(self):
        with self.assertRaises(TypeError):
            ScopedRepository(TGT, P1, f"{ROOT}/tgt", None, role="target")
        with self.assertRaises(TypeError):
            with_working_set_roles([object()], ())
        with self.assertRaises(TypeError):
            with_working_set_roles([repository(TGT, "tgt", None)], [object()])
