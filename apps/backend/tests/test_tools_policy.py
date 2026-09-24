import dataclasses
import unittest

from paw_backend.tools import (
    DEFAULT_TOOL_POLICY,
    ApprovalLevel,
    Environment,
    ScopeStatus,
    ToolCapability,
    ToolPolicy,
)
from paw_backend.tools.capabilities import most_restrictive

C = ToolCapability
E = Environment
S = ScopeStatus
L = ApprovalLevel

# (capability, environment): the level in scope / host out of scope / out of scope.
# Written out as strings, cell by cell, so that a change of the table is a
# visible change of this test.
EXPECTED = {
    ("read", "project_local"): ("auto", "deny", "deny"),
    ("write", "project_local"): ("scoped_auto", "approval", "deny"),
    ("execute", "project_local"): ("scoped_auto", "deny", "deny"),
    ("network", "project_local"): ("scoped_auto", "approval", "deny"),
    ("credential-use", "project_local"): ("scoped_auto", "deny", "deny"),
    ("destructive", "project_local"): ("approval", "deny", "deny"),
    ("read", "host"): ("auto", "deny", "deny"),
    ("write", "host"): ("approval", "approval", "deny"),
    ("execute", "host"): ("approval", "deny", "deny"),
    ("network", "host"): ("scoped_auto", "approval", "deny"),
    ("credential-use", "host"): ("strong_approval", "deny", "deny"),
    ("destructive", "host"): ("strong_approval", "deny", "deny"),
}
SCOPES = (S.IN_SCOPE, S.HOST_OUT_OF_SCOPE, S.OUT_OF_SCOPE)


class DefaultTableTest(unittest.TestCase):
    def test_every_cell_of_the_default_table(self):
        self.assertEqual(len(EXPECTED), len(C) * len(E))
        for (capability, environment), levels in EXPECTED.items():
            for scope, expected in zip(SCOPES, levels, strict=True):
                with self.subTest(cap=capability, env=environment, scope=scope.value):
                    level = DEFAULT_TOOL_POLICY.level_for(
                        [C(capability)], E(environment), scope
                    )
                    self.assertEqual(level.value, expected)

    def test_the_table_has_exactly_those_36_entries(self):
        self.assertEqual(len(DEFAULT_TOOL_POLICY.table), 36)

    def test_nothing_out_of_scope_is_ever_allowed_or_approvable(self):
        for capability in C:
            for environment in E:
                with self.subTest(cap=capability.value, env=environment.value):
                    self.assertIs(
                        DEFAULT_TOOL_POLICY.level_for(
                            [capability], environment, S.OUT_OF_SCOPE
                        ),
                        L.DENY,
                    )

    def test_a_policy_cannot_be_built_that_allows_out_of_scope_targets(self):
        with self.assertRaises(ValueError):
            ToolPolicy({(C.READ, E.PROJECT_LOCAL, S.OUT_OF_SCOPE): L.APPROVAL})
        # ... while the other cells of that capability can be anything.
        ToolPolicy({(C.READ, E.PROJECT_LOCAL, S.IN_SCOPE): L.APPROVAL})


class CombinationTest(unittest.TestCase):
    def level(self, caps, env=E.PROJECT_LOCAL, scope=S.IN_SCOPE):
        return DEFAULT_TOOL_POLICY.level_for(caps, env, scope)

    def test_a_tool_takes_the_most_restrictive_level_of_its_classes(self):
        self.assertIs(self.level([C.READ, C.WRITE]), L.SCOPED_AUTO)
        self.assertIs(self.level([C.READ, C.WRITE, C.DESTRUCTIVE]), L.APPROVAL)
        self.assertIs(self.level([C.READ]), L.AUTO)
        self.assertIs(self.level({C.CREDENTIAL_USE, C.NETWORK, C.WRITE}), L.SCOPED_AUTO)

    def test_an_external_write_beyond_the_task_hosts_needs_approval(self):
        out = S.HOST_OUT_OF_SCOPE
        self.assertIs(self.level([C.WRITE, C.NETWORK], scope=out), L.APPROVAL)
        # ... and an external read does not: read is denied outside the scope.
        self.assertIs(self.level([C.READ, C.NETWORK], scope=out), L.DENY)

    def test_host_wide_changes_need_approval_or_more(self):
        self.assertIs(self.level([C.WRITE, C.EXECUTE], E.HOST), L.APPROVAL)
        self.assertIs(self.level([C.DESTRUCTIVE, C.WRITE], E.HOST), L.STRONG_APPROVAL)
        self.assertIs(self.level([C.READ], E.HOST), L.AUTO)

    def test_the_input_order_does_not_matter(self):
        self.assertIs(
            self.level([C.DESTRUCTIVE, C.READ]), self.level([C.READ, C.DESTRUCTIVE])
        )


class DefaultDenyTest(unittest.TestCase):
    def test_an_empty_policy_denies_everything(self):
        policy = ToolPolicy({})
        for capability in C:
            for environment in E:
                for scope in S:
                    self.assertIs(
                        policy.level_for([capability], environment, scope), L.DENY
                    )

    def test_a_class_without_an_entry_denies_the_whole_tool(self):
        policy = ToolPolicy({(C.READ, E.PROJECT_LOCAL, S.IN_SCOPE): L.AUTO})
        self.assertIs(policy.level_for([C.READ], E.PROJECT_LOCAL, S.IN_SCOPE), L.AUTO)
        self.assertIs(
            policy.level_for([C.READ, C.WRITE], E.PROJECT_LOCAL, S.IN_SCOPE), L.DENY
        )
        self.assertIs(policy.level_for([C.READ], E.HOST, S.IN_SCOPE), L.DENY)

    def test_anything_that_is_not_an_enum_member_is_denied(self):
        level_for = DEFAULT_TOOL_POLICY.level_for
        self.assertIs(level_for([], E.PROJECT_LOCAL, S.IN_SCOPE), L.DENY)
        self.assertIs(level_for(["read"], E.PROJECT_LOCAL, S.IN_SCOPE), L.DENY)
        self.assertIs(level_for([C.READ], "project_local", S.IN_SCOPE), L.DENY)
        self.assertIs(level_for([C.READ], E.PROJECT_LOCAL, "in_scope"), L.DENY)
        self.assertIs(level_for(None, E.PROJECT_LOCAL, S.IN_SCOPE), L.DENY)
        self.assertIs(level_for([C.READ, "AUTO"], E.PROJECT_LOCAL, S.IN_SCOPE), L.DENY)

    def test_entries_are_type_checked(self):
        for table in (
            {("read", E.PROJECT_LOCAL, S.IN_SCOPE): L.AUTO},
            {(C.READ, E.PROJECT_LOCAL, S.IN_SCOPE): "auto"},
            {(C.READ, E.PROJECT_LOCAL): L.AUTO},
            [],
        ):
            with self.subTest(table=table):
                with self.assertRaises(TypeError):
                    ToolPolicy(table)


class ImmutabilityTest(unittest.TestCase):
    def test_the_policy_cannot_be_edited(self):
        with self.assertRaises(TypeError):
            DEFAULT_TOOL_POLICY.table[(C.READ, E.PROJECT_LOCAL, S.IN_SCOPE)] = L.DENY
        with self.assertRaises(dataclasses.FrozenInstanceError):
            DEFAULT_TOOL_POLICY.table = {}

    def test_the_source_mapping_is_copied(self):
        source = {(C.READ, E.PROJECT_LOCAL, S.IN_SCOPE): L.AUTO}
        policy = ToolPolicy(source)
        source[(C.READ, E.PROJECT_LOCAL, S.IN_SCOPE)] = L.DENY
        self.assertIs(policy.level_for([C.READ], E.PROJECT_LOCAL, S.IN_SCOPE), L.AUTO)


class LevelOrderTest(unittest.TestCase):
    def test_severity_is_strictly_increasing_in_the_documented_order(self):
        order = [L.AUTO, L.SCOPED_AUTO, L.APPROVAL, L.STRONG_APPROVAL, L.DENY]
        self.assertEqual([level.severity for level in order], [0, 1, 2, 3, 4])
        self.assertEqual(
            [level.name for level in L],
            ["AUTO", "SCOPED_AUTO", "APPROVAL", "STRONG_APPROVAL", "DENY"],
        )

    def test_most_restrictive(self):
        self.assertIs(most_restrictive([L.AUTO, L.APPROVAL, L.SCOPED_AUTO]), L.APPROVAL)
        self.assertIs(most_restrictive([]), L.DENY)
        self.assertIs(most_restrictive([L.AUTO, "auto"]), L.DENY)
        self.assertIs(most_restrictive([L.STRONG_APPROVAL, L.DENY]), L.DENY)


if __name__ == "__main__":
    unittest.main()
