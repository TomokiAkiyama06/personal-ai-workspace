import dataclasses
import unittest
import uuid
from types import MappingProxyType

import paw_backend.authz as authz
from paw_backend.authz import (
    CAPABILITIES,
    DEFAULT_POLICY,
    AuditMode,
    Capability,
    Decision,
    Policy,
    Principal,
    ProjectRole,
    ProjectState,
    Reason,
    RepoAcl,
    RepoPermission,
    Resource,
    Scope,
    SystemRole,
    parse_capability,
)
from paw_backend.authz.capabilities import REPO_PERMISSION_OF, CapabilityInfo
from paw_backend.authz.policy import decide

from .authz_support import (
    CHAT,
    P1,
    P2,
    P3,
    REPO,
    REPO2,
    U1,
    U2,
    principal,
    project,
    repo_resource,
    uid,
)

# The expected matrix is spelled out literally (not derived from the policy
# tables) so that any change to who may do what fails a test.
USER_CAPS = {
    "chat.use",
    "agent.use",
    "workspace.use",
    "github.use",
    "memory.use",
    "pr.create",
    "shared_memory.read",
}
ADMIN_CAPS = USER_CAPS | {
    "shared_memory.manage",
    "shared_memory.create",
    "shared_memory.edit",
    "shared_memory.delete",
    "shared_memory.restore",
    "shared_memory.candidate.approve",
    "shared_memory.candidate.reject",
    "admin.users.manage",
    "admin.usage.view",
    "admin.quota.manage",
    "admin.audit.view",
    "admin.system_prompt.manage",
    "admin.models.manage",
    "admin.routing.manage",
    "admin.permissions.manage",
    "admin.config.manage",
    "admin.projects.manage",
    "project.lifecycle.manage",
}
OWNER_ONLY_CAPS = {
    "owner.admins.manage",
    "owner.ownership.transfer",
    "owner.recovery.manage",
    "owner.user_restore",
    "owner.backup.manage",
}
OWNER_CAPS = ADMIN_CAPS | OWNER_ONLY_CAPS
SYSTEM_MATRIX = {
    SystemRole.OWNER: OWNER_CAPS,
    SystemRole.ADMIN: ADMIN_CAPS,
    SystemRole.USER: USER_CAPS,
    SystemRole.SYSTEM: set(),
}

VIEWER_CAPS = {"project.read"}
CONTRIBUTOR_CAPS = VIEWER_CAPS | {
    "project.chat",
    "project.task.run",
    "project.repo.write",
    "project.agent.use",
    "project.pr.create",
    "project.memory.use",
}
MANAGER_CAPS = CONTRIBUTOR_CAPS | {
    "project.members.manage",
    "project.repo.add",
    "project.settings.manage",
    "project.agent_policy.manage",
    "project.memory.manage",
    "project.lifecycle.manage",
}
PROJECT_MATRIX = {
    ProjectRole.MANAGER: MANAGER_CAPS,
    ProjectRole.CONTRIBUTOR: CONTRIBUTOR_CAPS,
    ProjectRole.VIEWER: VIEWER_CAPS,
}

# Delegation is an allowlist: exactly these can ever be exercised by an agent.
DELEGABLE_CAPS = {
    "chat.use",
    "workspace.use",
    "github.use",
    "memory.use",
    "pr.create",
    "shared_memory.read",
    "project.read",
    "project.chat",
    "project.task.run",
    "project.repo.write",
    "project.pr.create",
    "project.memory.use",
}
NON_DELEGABLE_CAPS = {
    # Starting agents: not delegable until PAW-032 defines derived (subset)
    # grants for child agents.
    "agent.use",
    "project.agent.use",
    "shared_memory.manage",
    "shared_memory.create",
    "shared_memory.edit",
    "shared_memory.delete",
    "shared_memory.restore",
    "shared_memory.candidate.approve",
    "shared_memory.candidate.reject",
    "admin.users.manage",
    "admin.usage.view",
    "admin.quota.manage",
    "admin.audit.view",
    "admin.system_prompt.manage",
    "admin.models.manage",
    "admin.routing.manage",
    "admin.permissions.manage",
    "admin.config.manage",
    "admin.projects.manage",
    "owner.admins.manage",
    "owner.ownership.transfer",
    "owner.recovery.manage",
    "owner.user_restore",
    "owner.backup.manage",
    "project.memory.manage",
    "project.settings.manage",
    "project.repo.add",
    "project.members.manage",
    "project.agent_policy.manage",
    "project.lifecycle.manage",
}
# The only capabilities whose *allowed* decisions are not persisted.
READ_ONLY_CAPS = {"shared_memory.read", "project.read"}


def resource_for(capability: Capability, who: Principal) -> Resource:
    """A resource on which ``capability`` could apply to ``who`` (their own)."""
    match CAPABILITIES[capability].scope:
        case Scope.SELF:
            return Resource.owned_by(who.user_id, "chat", CHAT)
        case Scope.PROJECT:
            return project(P1)
        case _:
            return Resource.system()


class MatrixTest(unittest.TestCase):
    def test_every_capability_is_covered_by_the_literal_matrix(self):
        known = {c.value for c in Capability}
        self.assertEqual(known, OWNER_CAPS | MANAGER_CAPS)

    def test_system_role_matrix(self):
        for role, allowed in SYSTEM_MATRIX.items():
            who = principal(role)
            for capability in Capability:
                with self.subTest(role=role.value, capability=capability.value):
                    decision = decide(who, capability, resource_for(capability, who))
                    self.assertEqual(decision.allowed, capability.value in allowed)

    def test_project_role_matrix(self):
        # A plain User who is a member of P1 with the given project role.
        for role, allowed in PROJECT_MATRIX.items():
            who = principal(SystemRole.USER, projects={P1: role})
            for capability in Capability:
                if CAPABILITIES[capability].scope is not Scope.PROJECT:
                    continue
                with self.subTest(role=role.value, capability=capability.value):
                    decision = decide(who, capability, project(P1))
                    self.assertEqual(decision.allowed, capability.value in allowed)

    def test_a_project_role_never_confers_a_non_project_capability(self):
        outsider = principal(SystemRole.USER)
        for role in ProjectRole:
            member = principal(SystemRole.USER, projects={P1: role})
            for capability in Capability:
                if CAPABILITIES[capability].scope is Scope.PROJECT:
                    continue
                with self.subTest(role=role.value, capability=capability.value):
                    self.assertEqual(
                        decide(member, capability, resource_for(capability, member)),
                        decide(
                            outsider, capability, resource_for(capability, outsider)
                        ),
                    )

    def test_owner_includes_every_admin_capability(self):
        self.assertLess(ADMIN_CAPS, OWNER_CAPS)
        who = principal(SystemRole.OWNER)
        for value in ADMIN_CAPS:
            capability = Capability(value)
            self.assertTrue(
                decide(who, capability, resource_for(capability, who)).allowed
            )

    def test_owner_only_operations_are_denied_to_admin(self):
        admin = principal(SystemRole.ADMIN)
        for value in OWNER_ONLY_CAPS:
            with self.subTest(capability=value):
                decision = decide(admin, Capability(value), Resource.system())
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_admin_capabilities_are_denied_to_a_plain_user(self):
        user = principal(SystemRole.USER)
        for value in ADMIN_CAPS - USER_CAPS - {"project.lifecycle.manage"}:
            with self.subTest(capability=value):
                decision = decide(user, Capability(value), Resource.system())
                self.assertFalse(decision.allowed)

    def test_the_internal_system_identity_holds_nothing(self):
        system = principal(SystemRole.SYSTEM, projects={P1: ProjectRole.VIEWER})
        for capability in Capability:
            if CAPABILITIES[capability].scope is Scope.PROJECT:
                continue
            with self.subTest(capability=capability.value):
                resource = resource_for(capability, system)
                self.assertFalse(decide(system, capability, resource).allowed)


class CapabilityTableTest(unittest.TestCase):
    def test_every_capability_has_an_explicit_delegation_decision(self):
        # A new Capability must be added to exactly one of the two literal sets.
        self.assertEqual(DELEGABLE_CAPS & NON_DELEGABLE_CAPS, set())
        self.assertEqual(
            DELEGABLE_CAPS | NON_DELEGABLE_CAPS, {c.value for c in Capability}
        )

    def test_the_table_agrees_with_the_literal_allowlist(self):
        delegable = {c.value for c in Capability if CAPABILITIES[c].delegable}
        self.assertEqual(delegable, DELEGABLE_CAPS)

    def test_delegable_has_no_default_so_it_cannot_be_forgotten(self):
        by_name = {f.name: f for f in dataclasses.fields(CapabilityInfo)}
        self.assertIs(by_name["delegable"].default, dataclasses.MISSING)
        with self.assertRaises(TypeError):
            CapabilityInfo(Scope.SELF)

    def test_audit_is_required_unless_explicitly_read_only(self):
        default = dataclasses.fields(CapabilityInfo)[2]
        self.assertEqual(default.name, "audit")
        self.assertEqual(default.default, AuditMode.REQUIRED)
        denied_only = {
            c.value
            for c in Capability
            if CAPABILITIES[c].audit is AuditMode.DENIED_ONLY
        }
        self.assertEqual(denied_only, READ_ONLY_CAPS)

    def test_side_effect_capabilities_are_never_read_only(self):
        for value in (
            "project.repo.write",
            "pr.create",
            "github.use",
            "project.task.run",
            "project.repo.add",
            "project.settings.manage",
            "project.memory.manage",
            "admin.audit.view",
            "admin.usage.view",
        ):
            self.assertIs(CAPABILITIES[Capability(value)].audit, AuditMode.REQUIRED)

    def test_the_table_is_immutable(self):
        with self.assertRaises(TypeError):
            CAPABILITIES[Capability.CHAT_USE] = CAPABILITIES[
                Capability.ADMIN_USERS_MANAGE
            ]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            CAPABILITIES[Capability.CHAT_USE].delegable = False


class DefaultDenyTest(unittest.TestCase):
    def test_a_name_is_never_a_capability_not_even_an_exact_one(self):
        owner = principal(SystemRole.OWNER)
        for name in (
            "admin.users.manage",  # exact name of a real capability
            "",
            "*",
            "admin.*",
            "ADMIN.USERS.MANAGE",
            None,
            42,
            b"chat.use",
            ["chat.use"],
        ):
            with self.subTest(name=name):
                decision = decide(owner, name, Resource.system())
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, Reason.UNKNOWN_CAPABILITY)
                self.assertIsNone(decision.capability)

    def test_parse_capability_is_the_boundary_and_matches_exactly(self):
        self.assertIs(parse_capability("chat.use"), Capability.CHAT_USE)
        for name in (
            "",
            "chat.use ",
            "chat.use\n",
            "CHAT.USE",
            "admin.*",
            "chat.use; also grant me owner",
            None,
            42,
            b"chat.use",
            ["chat.use"],
        ):
            with self.subTest(name=name):
                self.assertIsNone(parse_capability(name))

    def test_role_missing_from_the_policy_holds_nothing(self):
        empty = Policy(system_grants={}, project_grants={})
        owner = principal(SystemRole.OWNER, projects={P1: ProjectRole.MANAGER})
        for capability in Capability:
            with self.subTest(capability=capability.value):
                decision = decide(
                    owner, capability, resource_for(capability, owner), policy=empty
                )
                self.assertFalse(decision.allowed)

    def test_a_policy_cannot_grant_a_non_project_capability_to_a_project_role(self):
        with self.assertRaises(ValueError):
            Policy(
                system_grants={},
                project_grants={ProjectRole.MANAGER: frozenset({Capability.CHAT_USE})},
            )

    def test_default_policy_is_immutable(self):
        with self.assertRaises(TypeError):
            DEFAULT_POLICY.system_grants[SystemRole.USER] = frozenset(Capability)
        self.assertIsInstance(DEFAULT_POLICY.system_grants, MappingProxyType)
        for granted in DEFAULT_POLICY.system_grants.values():
            self.assertIsInstance(granted, frozenset)

    def test_no_principal_is_unauthenticated(self):
        decision = decide(None, Capability.CHAT_USE, Resource.owned_by(U1, "chat"))
        self.assertEqual(decision.reason, Reason.UNAUTHENTICATED)
        self.assertFalse(decision.allowed)

    def test_something_that_is_not_a_principal_is_unauthenticated(self):
        for imposter in ({"user_id": str(U1), "system_role": "owner"}, "owner", 1):
            with self.subTest(imposter=imposter):
                decision = decide(imposter, Capability.CHAT_USE, Resource.system())
                self.assertEqual(decision.reason, Reason.UNAUTHENTICATED)

    def test_a_missing_or_wrong_resource_is_invalid(self):
        who = principal(SystemRole.OWNER)
        for resource in (None, "project", {"project_id": str(P1)}):
            with self.subTest(resource=resource):
                decision = decide(who, Capability.PROJECT_READ, resource)
                self.assertEqual(decision.reason, Reason.INVALID_RESOURCE)

    def test_project_capability_needs_a_project_and_self_capability_an_owner(self):
        owner = principal(SystemRole.OWNER)
        self.assertEqual(
            decide(
                owner, Capability.PROJECT_LIFECYCLE_MANAGE, Resource.system()
            ).reason,
            Reason.INVALID_RESOURCE,
        )
        self.assertEqual(
            decide(owner, Capability.CHAT_USE, Resource.system()).reason,
            Reason.INVALID_RESOURCE,
        )

    def test_a_project_resource_without_its_state_is_invalid(self):
        # No default state: forgetting it must not silently mean "active".
        who = principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})
        bare = Resource(kind="project", id=P1, project_id=P1)
        self.assertIsNone(bare.project_state)
        decision = decide(who, Capability.PROJECT_TASK_RUN, bare)
        self.assertEqual(decision.reason, Reason.INVALID_RESOURCE)

    def test_decisions_are_deterministic_and_do_not_change_the_inputs(self):
        who = principal(SystemRole.USER, projects={P1: ProjectRole.CONTRIBUTOR})
        resource = project(P1)
        first = decide(who, Capability.PROJECT_TASK_RUN, resource)
        second = decide(who, Capability.PROJECT_TASK_RUN, resource)
        self.assertEqual(first, second)
        self.assertEqual(dict(who.project_roles), {P1: ProjectRole.CONTRIBUTOR})
        with self.assertRaises(TypeError):
            who.project_roles[P1] = ProjectRole.MANAGER
        with self.assertRaises(AttributeError):
            who.system_role = SystemRole.OWNER

    def test_a_decision_is_truthy_only_when_it_allows(self):
        # `if await authorizer.authorize(...)` must not treat a denial as true.
        allowed = decide(
            principal(SystemRole.OWNER), Capability.ADMIN_USAGE_VIEW, Resource.system()
        )
        denied = decide(
            principal(SystemRole.USER), Capability.ADMIN_USAGE_VIEW, Resource.system()
        )
        self.assertTrue(allowed)
        self.assertFalse(denied)
        self.assertIs(bool(Decision.deny(Reason.UNAUTHENTICATED, None)), False)

    def test_the_bare_policy_functions_are_not_part_of_the_public_api(self):
        # They record nothing; callers must go through the Authorizer.
        for name in ("decide", "decide_agent", "decide_role_change"):
            self.assertFalse(hasattr(authz, name), name)
            self.assertNotIn(name, authz.__all__)


class IsolationTest(unittest.TestCase):
    def test_a_manager_of_one_project_has_nothing_on_another(self):
        manager = principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})
        for capability in Capability:
            if CAPABILITIES[capability].scope is not Scope.PROJECT:
                continue
            with self.subTest(capability=capability.value):
                on_a = decide(manager, capability, project(P1))
                on_b = decide(manager, capability, project(P2))
                self.assertTrue(on_a.allowed)
                self.assertFalse(on_b.allowed)
                self.assertEqual(on_b.reason, Reason.NOT_PROJECT_MEMBER)

    def test_roles_are_per_project(self):
        who = principal(
            SystemRole.USER,
            projects={P1: ProjectRole.MANAGER, P2: ProjectRole.VIEWER},
        )
        manage = Capability.PROJECT_SETTINGS_MANAGE
        self.assertTrue(decide(who, manage, project(P1)).allowed)
        on_p2 = decide(who, manage, project(P2))
        self.assertEqual(on_p2.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertTrue(decide(who, Capability.PROJECT_READ, project(P2)).allowed)

    def test_a_system_role_does_not_open_a_project_it_is_not_a_member_of(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            who = principal(role)
            for capability in (
                Capability.PROJECT_READ,
                Capability.PROJECT_CHAT,
                Capability.PROJECT_REPO_WRITE,
            ):
                with self.subTest(role=role.value, capability=capability.value):
                    decision = decide(who, capability, project(P1))
                    self.assertEqual(decision.reason, Reason.NOT_PROJECT_MEMBER)

    def test_owner_and_admin_may_run_administrative_project_operations(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            decision = decide(
                principal(role), Capability.PROJECT_LIFECYCLE_MANAGE, project(P3)
            )
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.reason, Reason.GRANTED_BY_SYSTEM_ROLE)

    def test_contributor_and_viewer_cannot_archive_or_delete(self):
        for role in (ProjectRole.CONTRIBUTOR, ProjectRole.VIEWER):
            who = principal(SystemRole.USER, projects={P1: role})
            decision = decide(who, Capability.PROJECT_LIFECYCLE_MANAGE, project(P1))
            self.assertEqual(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_private_data_is_the_owners_alone_even_for_the_workspace_owner(self):
        owner = principal(SystemRole.OWNER, user_id=U1)
        theirs = Resource.owned_by(U2, "memory", CHAT)
        mine = Resource.owned_by(U1, "memory", CHAT)
        denied = decide(owner, Capability.MEMORY_USE, theirs)
        self.assertEqual(denied.reason, Reason.NOT_RESOURCE_OWNER)
        allowed = decide(owner, Capability.MEMORY_USE, mine)
        self.assertEqual(allowed.reason, Reason.GRANTED_TO_RESOURCE_OWNER)

    def test_a_user_cannot_use_another_users_workspace(self):
        decision = decide(
            principal(SystemRole.USER, user_id=U1),
            Capability.WORKSPACE_USE,
            Resource.owned_by(U2, "workspace"),
        )
        self.assertEqual(decision.reason, Reason.NOT_RESOURCE_OWNER)


class ProjectStateTest(unittest.TestCase):
    """Archived is read-only and Pending deletion is closed (REQUIREMENTS)."""

    def manager(self) -> Principal:
        return principal(SystemRole.USER, projects={P1: ProjectRole.MANAGER})

    def test_an_active_project_allows_what_the_role_allows(self):
        for value in MANAGER_CAPS:
            self.assertTrue(
                decide(self.manager(), Capability(value), project(P1)).allowed
            )

    def test_an_archived_project_is_read_only(self):
        archived = project(P1, ProjectState.ARCHIVED)
        allowed = {
            value
            for value in MANAGER_CAPS
            if decide(self.manager(), Capability(value), archived).allowed
        }
        # Reading, and bringing the project back (unarchive / delete).
        self.assertEqual(allowed, {"project.read", "project.lifecycle.manage"})
        blocked = decide(self.manager(), Capability.PROJECT_TASK_RUN, archived)
        self.assertEqual(blocked.reason, Reason.PROJECT_STATE_FORBIDS)

    def test_a_project_pending_deletion_stops_all_access_but_the_lifecycle(self):
        pending = project(P1, ProjectState.PENDING_DELETION)
        allowed = {
            value
            for value in MANAGER_CAPS
            if decide(self.manager(), Capability(value), pending).allowed
        }
        self.assertEqual(allowed, {"project.lifecycle.manage"})
        read = decide(self.manager(), Capability.PROJECT_READ, pending)
        self.assertEqual(read.reason, Reason.PROJECT_STATE_FORBIDS)

    def test_the_state_limits_administrators_as_well(self):
        admin = principal(SystemRole.ADMIN)
        pending = project(P1, ProjectState.PENDING_DELETION)
        self.assertTrue(
            decide(admin, Capability.PROJECT_LIFECYCLE_MANAGE, pending).allowed
        )
        # (and a viewer cannot restore: the role check still applies)
        viewer = principal(SystemRole.USER, projects={P1: ProjectRole.VIEWER})
        self.assertFalse(
            decide(viewer, Capability.PROJECT_LIFECYCLE_MANAGE, pending).allowed
        )

    def test_the_state_only_matters_for_project_capabilities(self):
        owner = principal(SystemRole.OWNER)
        stray = Resource(
            kind="system", project_id=P1, project_state=ProjectState.ARCHIVED
        )
        self.assertTrue(decide(owner, Capability.ADMIN_CONFIG_MANAGE, stray).allowed)

    def test_project_state_needs_a_project(self):
        with self.assertRaises(ValueError):
            Resource(kind="system", project_state=ProjectState.ACTIVE)
        with self.assertRaises(ValueError):
            Resource(kind="project", project_id=P1, project_state="frozen")


# The repository permission each repository-capable project capability needs.
REPO_PERMISSION_LITERAL = {
    "project.read": "read",
    "project.memory.use": "read",
    "project.repo.write": "write",
    "project.pr.create": "write",
    "project.task.run": "agent",
    "project.agent.use": "agent",
}


class RepositoryAclTest(unittest.TestCase):
    """Repositories inherit the project role unless an override narrows them."""

    def member(self, role: ProjectRole, project_id=P1) -> Principal:
        return principal(SystemRole.USER, projects={project_id: role})

    def test_the_repository_permissions_of_the_capabilities_are_fixed(self):
        self.assertEqual(
            {c.value: p.value for c, p in REPO_PERMISSION_OF.items()},
            REPO_PERMISSION_LITERAL,
        )
        for capability in REPO_PERMISSION_OF:
            self.assertIs(CAPABILITIES[capability].scope, Scope.PROJECT)

    def test_inherit_gives_exactly_what_the_project_role_gives(self):
        # The same answer as on the project itself, capability by capability.
        for role in ProjectRole:
            who = self.member(role)
            for capability in REPO_PERMISSION_OF:
                with self.subTest(role=role.value, capability=capability.value):
                    on_repo = decide(who, capability, repo_resource())
                    on_project = decide(who, capability, project(P1))
                    self.assertEqual(on_repo.allowed, on_project.allowed)
                    self.assertEqual(on_repo.reason, on_project.reason)

    def test_inherit_viewer_reads_but_cannot_write_contributor_writes(self):
        viewer = self.member(ProjectRole.VIEWER)
        contributor = self.member(ProjectRole.CONTRIBUTOR)
        read, write = Capability.PROJECT_READ, Capability.PROJECT_REPO_WRITE
        self.assertTrue(decide(viewer, read, repo_resource()).allowed)
        denied = decide(viewer, write, repo_resource())
        self.assertEqual(denied.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertTrue(decide(contributor, read, repo_resource()).allowed)
        self.assertTrue(decide(contributor, write, repo_resource()).allowed)
        self.assertTrue(
            decide(contributor, Capability.PROJECT_TASK_RUN, repo_resource()).allowed
        )

    def test_a_non_member_has_nothing_on_an_inherit_repository(self):
        outsider = principal(SystemRole.USER)
        for capability in REPO_PERMISSION_OF:
            with self.subTest(capability=capability.value):
                decision = decide(outsider, capability, repo_resource())
                self.assertEqual(decision.reason, Reason.NOT_PROJECT_MEMBER)
        # A workspace Owner is no member either.
        owner = principal(SystemRole.OWNER)
        self.assertFalse(
            decide(owner, Capability.PROJECT_READ, repo_resource()).allowed
        )

    def test_an_override_keeps_only_its_permissions_even_for_a_manager(self):
        manager = self.member(ProjectRole.MANAGER)
        for kept, expected in (
            ({RepoPermission.READ}, {"project.read", "project.memory.use"}),
            (
                {RepoPermission.READ, RepoPermission.WRITE},
                {
                    "project.read",
                    "project.memory.use",
                    "project.repo.write",
                    "project.pr.create",
                },
            ),
            (
                {RepoPermission.WRITE},
                {"project.repo.write", "project.pr.create"},
            ),
            (
                {RepoPermission.AGENT},
                {"project.task.run", "project.agent.use"},
            ),
            (set(), set()),  # "access denied"
            (set(RepoPermission), set(REPO_PERMISSION_LITERAL)),
        ):
            allowed = {
                c.value
                for c in REPO_PERMISSION_OF
                if decide(manager, c, repo_resource(kept)).allowed
            }
            with self.subTest(kept=sorted(p.value for p in kept)):
                self.assertEqual(allowed, expected)

    def test_a_refused_permission_is_reported_as_the_acl_forbidding_it(self):
        manager = self.member(ProjectRole.MANAGER)
        decision = decide(
            manager, Capability.PROJECT_REPO_WRITE, repo_resource({RepoPermission.READ})
        )
        self.assertEqual(decision.reason, Reason.REPO_ACL_FORBIDS)
        self.assertIs(decision.capability, Capability.PROJECT_REPO_WRITE)

    def test_an_override_never_widens_the_project_role(self):
        everything = set(RepoPermission)
        viewer = self.member(ProjectRole.VIEWER)
        for capability in (Capability.PROJECT_REPO_WRITE, Capability.PROJECT_TASK_RUN):
            decision = decide(viewer, capability, repo_resource(everything))
            self.assertEqual(decision.reason, Reason.CAPABILITY_NOT_GRANTED)
        outsider = principal(SystemRole.USER)
        self.assertEqual(
            decide(outsider, Capability.PROJECT_READ, repo_resource(everything)).reason,
            Reason.NOT_PROJECT_MEMBER,
        )

    def test_an_unresolved_acl_is_a_denial_never_inherit(self):
        # The caller named a repository but did not resolve its ACL.
        unresolved = Resource(
            kind="repository",
            id=REPO,
            project_id=P1,
            repo_id=REPO,
            project_state=ProjectState.ACTIVE,
        )
        for role in (ProjectRole.MANAGER, ProjectRole.VIEWER):
            for capability in REPO_PERMISSION_OF:
                with self.subTest(role=role.value, capability=capability.value):
                    decision = decide(self.member(role), capability, unresolved)
                    self.assertFalse(decision.allowed)
                    self.assertEqual(decision.reason, Reason.REPO_ACL_UNRESOLVED)
        owner = principal(SystemRole.OWNER)
        self.assertEqual(
            decide(owner, Capability.PROJECT_READ, unresolved).reason,
            Reason.REPO_ACL_UNRESOLVED,
        )

    def test_an_acl_of_another_project_or_repository_cannot_be_borrowed(self):
        manager_of_p1 = self.member(ProjectRole.MANAGER, P1)
        # The repository is stored under P2 (where it may even be "inherit"),
        # but the request names P1, where the user is a Manager.
        forged = repo_resource(project_id=P1, acl_project_id=P2)
        for capability in REPO_PERMISSION_OF:
            with self.subTest(capability=capability.value):
                decision = decide(manager_of_p1, capability, forged)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, Reason.REPO_ACL_MISMATCH)
        # The mirror image: a member of P2 cannot reach it through P1's ids.
        member_of_p2 = self.member(ProjectRole.MANAGER, P2)
        self.assertFalse(decide(member_of_p2, Capability.PROJECT_READ, forged).allowed)
        # An ACL resolved for another repository does not fit this one.
        other = RepoAcl.inherit(REPO2, P1)
        swapped = Resource(
            kind="repository",
            id=REPO,
            project_id=P1,
            repo_id=REPO,
            project_state=ProjectState.ACTIVE,
            repo_acl=other,
        )
        decision = decide(manager_of_p1, Capability.PROJECT_READ, swapped)
        self.assertEqual(decision.reason, Reason.REPO_ACL_MISMATCH)

    def test_a_restricted_repository_cannot_be_opened_through_another_ones_acl(self):
        # The resource is REPO but the ACL is REPO2's (an inherit one): whatever
        # it says, it is not REPO's ACL, so it decides nothing.
        manager = self.member(ProjectRole.MANAGER)
        inherit_of_other = RepoAcl.inherit(REPO2, P1)
        target = Resource(
            kind="repository",
            id=REPO,
            project_id=P1,
            repo_id=REPO,
            project_state=ProjectState.ACTIVE,
            repo_acl=inherit_of_other,
        )
        decision = decide(manager, Capability.PROJECT_REPO_WRITE, target)
        self.assertEqual(decision.reason, Reason.REPO_ACL_MISMATCH)

    def test_the_project_state_still_limits_a_repository(self):
        contributor = self.member(ProjectRole.CONTRIBUTOR)
        archived = repo_resource(state=ProjectState.ARCHIVED)
        self.assertTrue(decide(contributor, Capability.PROJECT_READ, archived).allowed)
        write = decide(contributor, Capability.PROJECT_REPO_WRITE, archived)
        self.assertEqual(write.reason, Reason.PROJECT_STATE_FORBIDS)
        pending = repo_resource(state=ProjectState.PENDING_DELETION)
        self.assertEqual(
            decide(contributor, Capability.PROJECT_READ, pending).reason,
            Reason.PROJECT_STATE_FORBIDS,
        )

    def test_a_capability_that_is_not_about_one_repository_refuses_a_repo(self):
        manager = self.member(ProjectRole.MANAGER)
        owner = principal(SystemRole.OWNER, user_id=U1)
        stray = repo_resource()
        for capability in (
            Capability.PROJECT_CHAT,
            Capability.PROJECT_SETTINGS_MANAGE,
            Capability.PROJECT_REPO_ADD,
            Capability.PROJECT_MEMBERS_MANAGE,
            Capability.PROJECT_LIFECYCLE_MANAGE,
            Capability.PROJECT_MEMORY_MANAGE,
        ):
            with self.subTest(capability=capability.value):
                decision = decide(manager, capability, stray)
                self.assertEqual(decision.reason, Reason.INVALID_RESOURCE)
        # Personal and workspace-wide capabilities have no repository either.
        for capability, resource in (
            (Capability.CHAT_USE, Resource(kind="chat", owner_id=U1, repo_id=REPO)),
            (Capability.ADMIN_CONFIG_MANAGE, Resource(kind="system", repo_id=REPO)),
        ):
            with self.subTest(capability=capability.value):
                self.assertEqual(
                    decide(owner, capability, resource).reason, Reason.INVALID_RESOURCE
                )


class ValueObjectTest(unittest.TestCase):
    def test_unknown_roles_are_refused_without_echoing_them(self):
        with self.assertRaises(ValueError):
            Principal(U1, "superuser")
        with self.assertRaises(ValueError):
            Principal(U1, SystemRole.USER, {P1: "god"})

    def test_roles_can_be_given_as_their_stored_strings(self):
        who = Principal(U1, "admin", {P1: "manager"})
        self.assertIs(who.system_role, SystemRole.ADMIN)
        self.assertIs(who.project_roles[P1], ProjectRole.MANAGER)

    def test_ids_are_uuids_and_canonical_strings_normalise_to_uuid(self):
        who = Principal(str(U1), SystemRole.USER, {str(P1): ProjectRole.VIEWER})
        self.assertEqual(who.user_id, U1)
        self.assertIsInstance(who.user_id, uuid.UUID)
        self.assertEqual(list(who.project_roles), [P1])
        self.assertIsInstance(next(iter(who.project_roles)), uuid.UUID)
        # The same principal whichever way the ids were spelled.
        self.assertEqual(who, Principal(U1, SystemRole.USER, {P1: ProjectRole.VIEWER}))
        resource = Resource.owned_by(str(U2), "chat", str(CHAT))
        self.assertEqual((resource.owner_id, resource.id), (U2, CHAT))

    def test_only_canonical_uuids_are_accepted_as_ids(self):
        canonical = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
        self.assertEqual(str(uuid.UUID(canonical)), canonical)
        bad = (
            "",
            " ",
            "user.name",
            "a b",
            canonical.upper(),
            canonical.replace("-", ""),
            "{" + canonical + "}",
            "urn:uuid:" + canonical,
            canonical + "\n",
            "x" * 129,
            None,
            7,
            b"1234567890123456",
        )
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Principal(value, SystemRole.USER)
                with self.assertRaises(ValueError):
                    Principal(U1, SystemRole.USER, {value: ProjectRole.VIEWER})
                if value is not None:  # a resource may have no id
                    with self.assertRaises(ValueError):
                        Resource(kind="chat", id=value)
                    with self.assertRaises(ValueError):
                        Resource(kind="chat", owner_id=value)

    def test_the_same_project_spelled_twice_is_refused_not_last_wins(self):
        for roles in (
            {str(P1): ProjectRole.VIEWER, P1: ProjectRole.MANAGER},
            {P1: ProjectRole.MANAGER, str(P1): ProjectRole.VIEWER},
        ):
            with self.subTest(roles=roles):
                with self.assertRaises(ValueError) as caught:
                    Principal(U1, SystemRole.USER, roles)
                self.assertNotIn(str(P1), str(caught.exception))
        # Two different projects are of course fine.
        both = Principal(
            U1, SystemRole.USER, {str(P1): ProjectRole.VIEWER, P2: ProjectRole.MANAGER}
        )
        self.assertEqual(len(both.project_roles), 2)

    def test_error_does_not_echo_the_value(self):
        with self.assertRaises(ValueError) as caught:
            Principal("secret value here", SystemRole.USER)
        self.assertNotIn("secret value here", str(caught.exception))

    def test_resource_kind_is_restricted(self):
        for kind in ("", "Project", "a b", "1x", "x" * 65, None):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    Resource(kind=kind)
        self.assertEqual(Resource(kind="x" * 64).kind, "x" * 64)

    def test_resource_constructors(self):
        self.assertEqual(Resource.system(), Resource(kind="system"))
        self.assertEqual(
            Resource.project(P1, ProjectState.ARCHIVED),
            Resource(
                kind="project",
                id=P1,
                project_id=P1,
                project_state=ProjectState.ARCHIVED,
            ),
        )
        acl = RepoAcl.override(REPO, P1, {RepoPermission.READ})
        self.assertEqual(
            Resource.repository(P1, ProjectState.ARCHIVED, acl),
            Resource(
                kind="repository",
                id=REPO,
                project_id=P1,
                repo_id=REPO,
                project_state=ProjectState.ARCHIVED,
                repo_acl=acl,
            ),
        )
        self.assertEqual(
            Resource.owned_by(U1, "chat", CHAT),
            Resource(kind="chat", id=CHAT, owner_id=U1),
        )

    def test_repo_acl_values(self):
        inherit = RepoAcl.inherit(str(REPO), str(P1))
        self.assertEqual((inherit.repo_id, inherit.project_id), (REPO, P1))
        self.assertTrue(inherit.inherits)
        self.assertIsNone(inherit.allowed)
        override = RepoAcl.override(
            REPO, P1, [RepoPermission.READ, RepoPermission.READ]
        )
        self.assertFalse(override.inherits)
        self.assertEqual(override.allowed, frozenset({RepoPermission.READ}))
        self.assertEqual(RepoAcl.override(REPO, P1, []).allowed, frozenset())
        with self.assertRaises(AttributeError):
            override.allowed = None

    def test_repo_acl_refuses_loose_input(self):
        for bad in ("read", b"read", None, 5):
            with self.subTest(allowed=bad):
                with self.assertRaises(TypeError):
                    RepoAcl.override(REPO, P1, bad)
        for bad in (["read"], ["write", RepoPermission.READ], [None]):
            with self.subTest(allowed=bad):
                with self.assertRaises(ValueError):
                    RepoAcl.override(REPO, P1, bad)
        for bad_id in ("x", None, 5, str(REPO).upper() + "z"):
            with self.subTest(id=bad_id):
                with self.assertRaises(ValueError):
                    RepoAcl.inherit(bad_id, P1)
                with self.assertRaises(ValueError):
                    RepoAcl.inherit(REPO, bad_id)

    def test_a_repo_acl_needs_a_repository_and_a_real_acl(self):
        acl = RepoAcl.inherit(REPO, P1)
        with self.assertRaises(ValueError):
            Resource(kind="project", id=P1, project_id=P1, repo_acl=acl)
        for bad in ("inherit", {"allowed": None}, object()):
            with self.subTest(acl=bad):
                with self.assertRaises(ValueError):
                    Resource.repository(P1, ProjectState.ACTIVE, bad)
                with self.assertRaises(ValueError):
                    Resource(kind="repository", repo_id=REPO, repo_acl=bad)

    def test_uid_helper_gives_distinct_canonical_uuids(self):
        self.assertNotEqual(uid(1), uid(2))
        self.assertEqual(str(uid(1)), "00000000-0000-0000-0000-000000000001")


if __name__ == "__main__":
    unittest.main()
