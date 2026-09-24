import unittest
from types import MappingProxyType

from paw_backend.authz import (
    CAPABILITIES,
    DEFAULT_POLICY,
    Capability,
    Policy,
    Principal,
    ProjectRole,
    Reason,
    Resource,
    Scope,
    SystemRole,
    decide,
)

from .authz_support import principal

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

PRIVILEGED_CAPS = (
    {c for c in OWNER_CAPS if c.startswith(("admin.", "owner."))}
    | {"shared_memory.manage"}
    | {
        "project.members.manage",
        "project.agent_policy.manage",
        "project.lifecycle.manage",
    }
)


def resource_for(capability: Capability, who: Principal) -> Resource:
    """A resource on which ``capability`` could apply to ``who`` (their own)."""
    match CAPABILITIES[capability].scope:
        case Scope.SELF:
            return Resource.owned_by(who.user_id, "chat", "c1")
        case Scope.PROJECT:
            return Resource.project("p1")
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
        # A plain User who is a member of p1 with the given project role.
        for role, allowed in PROJECT_MATRIX.items():
            who = principal(SystemRole.USER, p1=role)
            for capability in Capability:
                if CAPABILITIES[capability].scope is not Scope.PROJECT:
                    continue
                with self.subTest(role=role.value, capability=capability.value):
                    decision = decide(who, capability, Resource.project("p1"))
                    self.assertEqual(decision.allowed, capability.value in allowed)

    def test_a_project_role_never_confers_a_non_project_capability(self):
        outsider = principal(SystemRole.USER)
        for role in ProjectRole:
            member = principal(SystemRole.USER, p1=role)
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
        for value in ADMIN_CAPS:
            who = principal(SystemRole.OWNER)
            capability = Capability(value)
            self.assertTrue(
                decide(who, capability, resource_for(capability, who)).allowed
            )

    def test_owner_only_operations_are_denied_to_admin(self):
        admin = principal(SystemRole.ADMIN)
        for value in OWNER_ONLY_CAPS:
            with self.subTest(capability=value):
                decision = decide(admin, value, Resource.system())
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_admin_capabilities_are_denied_to_a_plain_user(self):
        user = principal(SystemRole.USER)
        for value in ADMIN_CAPS - USER_CAPS - {"project.lifecycle.manage"}:
            with self.subTest(capability=value):
                self.assertFalse(decide(user, value, Resource.system()).allowed)

    def test_the_internal_system_identity_holds_nothing(self):
        system = principal(SystemRole.SYSTEM, p1=ProjectRole.VIEWER)
        for capability in Capability:
            if CAPABILITIES[capability].scope is Scope.PROJECT:
                continue
            with self.subTest(capability=capability.value):
                self.assertFalse(
                    decide(system, capability, resource_for(capability, system)).allowed
                )

    def test_privileged_flags(self):
        flagged = {c.value for c in Capability if CAPABILITIES[c].privileged}
        self.assertEqual(flagged, PRIVILEGED_CAPS)


class DefaultDenyTest(unittest.TestCase):
    def test_unknown_capability_is_denied_even_to_the_owner(self):
        owner = principal(SystemRole.OWNER)
        for name in (
            "",
            "*",
            "admin.*",
            "admin.users.manage ",
            "ADMIN.USERS.MANAGE",
            "admin.users.delete",
            "chat.use\n",
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

    def test_a_known_capability_can_be_named_by_its_exact_string(self):
        decision = decide(
            principal(SystemRole.OWNER), "admin.users.manage", Resource.system()
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.capability, Capability.ADMIN_USERS_MANAGE)

    def test_role_missing_from_the_policy_holds_nothing(self):
        empty = Policy(system_grants={}, project_grants={})
        owner = principal(SystemRole.OWNER, p1=ProjectRole.MANAGER)
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
        decision = decide(None, Capability.CHAT_USE, Resource.owned_by("u1", "chat"))
        self.assertEqual(decision.reason, Reason.UNAUTHENTICATED)
        self.assertFalse(decision.allowed)

    def test_something_that_is_not_a_principal_is_unauthenticated(self):
        for imposter in ({"user_id": "u1", "system_role": "owner"}, "owner", 1):
            with self.subTest(imposter=imposter):
                decision = decide(imposter, Capability.CHAT_USE, Resource.system())
                self.assertEqual(decision.reason, Reason.UNAUTHENTICATED)

    def test_a_missing_or_wrong_resource_is_invalid(self):
        who = principal(SystemRole.OWNER)
        for resource in (None, "project", {"project_id": "p1"}):
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

    def test_decisions_are_deterministic_and_do_not_change_the_inputs(self):
        who = principal(SystemRole.USER, p1=ProjectRole.CONTRIBUTOR)
        resource = Resource.project("p1")
        first = decide(who, Capability.PROJECT_TASK_RUN, resource)
        second = decide(who, Capability.PROJECT_TASK_RUN, resource)
        self.assertEqual(first, second)
        self.assertEqual(dict(who.project_roles), {"p1": ProjectRole.CONTRIBUTOR})
        with self.assertRaises(TypeError):
            who.project_roles["p1"] = ProjectRole.MANAGER
        with self.assertRaises(AttributeError):
            who.system_role = SystemRole.OWNER


class IsolationTest(unittest.TestCase):
    def test_a_manager_of_one_project_has_nothing_on_another(self):
        manager = principal(SystemRole.USER, a=ProjectRole.MANAGER)
        for capability in Capability:
            if CAPABILITIES[capability].scope is not Scope.PROJECT:
                continue
            with self.subTest(capability=capability.value):
                on_a = decide(manager, capability, Resource.project("a"))
                on_b = decide(manager, capability, Resource.project("b"))
                self.assertTrue(on_a.allowed)
                self.assertFalse(on_b.allowed)
                self.assertEqual(on_b.reason, Reason.NOT_PROJECT_MEMBER)

    def test_roles_are_per_project(self):
        who = principal(SystemRole.USER, a=ProjectRole.MANAGER, b=ProjectRole.VIEWER)
        self.assertTrue(
            decide(
                who, Capability.PROJECT_SETTINGS_MANAGE, Resource.project("a")
            ).allowed
        )
        on_b_write = decide(
            who, Capability.PROJECT_SETTINGS_MANAGE, Resource.project("b")
        )
        self.assertEqual(on_b_write.reason, Reason.CAPABILITY_NOT_GRANTED)
        self.assertTrue(
            decide(who, Capability.PROJECT_READ, Resource.project("b")).allowed
        )

    def test_a_system_role_does_not_open_a_project_it_is_not_a_member_of(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            who = principal(role)
            for value in ("project.read", "project.chat", "project.repo.write"):
                with self.subTest(role=role.value, capability=value):
                    decision = decide(who, value, Resource.project("p1"))
                    self.assertEqual(decision.reason, Reason.NOT_PROJECT_MEMBER)

    def test_owner_and_admin_may_run_administrative_project_operations(self):
        for role in (SystemRole.OWNER, SystemRole.ADMIN):
            decision = decide(
                principal(role),
                Capability.PROJECT_LIFECYCLE_MANAGE,
                Resource.project("anywhere"),
            )
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.reason, Reason.GRANTED_BY_SYSTEM_ROLE)

    def test_contributor_and_viewer_cannot_archive_or_delete(self):
        for role in (ProjectRole.CONTRIBUTOR, ProjectRole.VIEWER):
            who = principal(SystemRole.USER, p1=role)
            decision = decide(
                who, Capability.PROJECT_LIFECYCLE_MANAGE, Resource.project("p1")
            )
            self.assertEqual(decision.reason, Reason.CAPABILITY_NOT_GRANTED)

    def test_private_data_is_the_owners_alone_even_for_the_workspace_owner(self):
        owner = principal(SystemRole.OWNER, user_id="owner1")
        theirs = Resource.owned_by("u2", "memory", "m1")
        mine = Resource.owned_by("owner1", "memory", "m1")
        denied = decide(owner, Capability.MEMORY_USE, theirs)
        self.assertEqual(denied.reason, Reason.NOT_RESOURCE_OWNER)
        allowed = decide(owner, Capability.MEMORY_USE, mine)
        self.assertEqual(allowed.reason, Reason.GRANTED_TO_RESOURCE_OWNER)

    def test_a_user_cannot_use_another_users_workspace(self):
        decision = decide(
            principal(SystemRole.USER, user_id="u1"),
            Capability.WORKSPACE_USE,
            Resource.owned_by("u2", "workspace"),
        )
        self.assertEqual(decision.reason, Reason.NOT_RESOURCE_OWNER)


class ValueObjectTest(unittest.TestCase):
    def test_unknown_roles_are_refused_without_echoing_them(self):
        with self.assertRaises(ValueError):
            Principal("u1", "superuser")
        with self.assertRaises(ValueError):
            Principal("u1", SystemRole.USER, {"p1": "god"})

    def test_roles_can_be_given_as_their_stored_strings(self):
        who = Principal("u1", "admin", {"p1": "manager"})
        self.assertIs(who.system_role, SystemRole.ADMIN)
        self.assertIs(who.project_roles["p1"], ProjectRole.MANAGER)

    def test_identifiers_are_restricted(self):
        bad = ("", " ", "a b", "a/b", "a;b", "x" * 129, "ü", "a\n", None, 7)
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Principal(value, SystemRole.USER)
                if value is not None:  # a resource may have no id
                    with self.assertRaises(ValueError):
                        Resource(kind="project", id=value)
                with self.assertRaises(ValueError):
                    Principal("u1", SystemRole.USER, {value: ProjectRole.VIEWER})
        self.assertEqual(Principal("x" * 128, SystemRole.USER).user_id, "x" * 128)
        self.assertEqual(
            Principal("0b8e-4f2a:u.1_x", SystemRole.USER).user_id, "0b8e-4f2a:u.1_x"
        )

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
            Resource.project("p1", repo_id="r1"),
            Resource(kind="project", id="p1", project_id="p1", repo_id="r1"),
        )
        self.assertEqual(
            Resource.owned_by("u1", "chat", "c1"),
            Resource(kind="chat", id="c1", owner_id="u1"),
        )


if __name__ == "__main__":
    unittest.main()
