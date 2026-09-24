import dataclasses
import unittest

from paw_backend.authz import Capability
from paw_backend.tools import (
    ApprovalLevel,
    ArgumentKind,
    ArgumentSpec,
    Environment,
    ToolCapability,
    ToolRegistry,
    ToolSpec,
)

from .tools_support import sample_registry, sample_specs

C = ToolCapability
A = ArgumentKind


def spec(name="repo.read_file", caps=frozenset({C.READ}), authz=None, args=None, **kw):
    return ToolSpec(
        name,
        caps,
        authz or Capability.PROJECT_READ,
        {"path": ArgumentSpec(A.PATH)} if args is None else args,
        **kw,
    )


class ToolNameTest(unittest.TestCase):
    def test_valid_names_are_accepted(self):
        for name in ("a", "repo.read_file", "git.push2", "a_b.c_d.e", "x" * 48):
            with self.subTest(name=name):
                self.assertEqual(spec(name).name, name)

    def test_other_names_are_refused(self):
        bad = [
            "",
            "Repo.read",
            "repo..read",
            ".repo",
            "repo.",
            "1repo",
            "repo read",
            "repo-read",
            "repo.read​",
            "repo.read\n",
            "x" * 49,
            "unknown",  # the audit action of a name that is not registered
            "approval",
            "approval.approve",  # the audit actions of the approval lifecycle
            None,
            5,
            b"repo.read",
        ]
        for name in bad:
            with self.subTest(name=name):
                with self.assertRaises((ValueError, TypeError)):
                    spec(name)


class ToolCapabilityDeclarationTest(unittest.TestCase):
    def test_capabilities_must_be_enum_members_and_not_empty(self):
        for caps in (frozenset(), set(), [], ("read",), "read", {"read"}, None, 5):
            with self.subTest(caps=caps):
                with self.assertRaises((ValueError, TypeError)):
                    spec(caps=caps)

    def test_authz_capability_must_be_a_capability_member(self):
        with self.assertRaises(TypeError):
            spec(authz="project.read")

    def test_a_mutable_capability_set_is_copied(self):
        caps = {C.READ}
        tool = spec(caps=caps)
        caps.add(C.DESTRUCTIVE)
        self.assertEqual(tool.capabilities, frozenset({C.READ}))
        self.assertIsInstance(tool.capabilities, frozenset)

    def test_environment_and_level_are_validated_not_coerced_from_prose(self):
        with self.assertRaises(ValueError):
            spec(environment="Host")
        with self.assertRaises(ValueError):
            spec(min_level="AUTO")
        self.assertIs(spec(environment="host").environment, Environment.HOST)
        self.assertIs(spec(min_level="deny").min_level, ApprovalLevel.DENY)

    def test_flags_must_be_bools(self):
        with self.assertRaises(TypeError):
            spec(requires_budget="no")
        with self.assertRaises(TypeError):
            spec(returns_credential_plaintext=1)


class ConsistencyTest(unittest.TestCase):
    def refused(self, **kwargs):
        with self.assertRaises(ValueError):
            spec(**kwargs)

    def test_a_host_or_url_argument_makes_it_a_network_tool(self):
        for kind in (A.URL, A.HOST):
            with self.subTest(kind=kind):
                self.refused(args={"target": ArgumentSpec(kind)})
        tool = spec(
            caps=frozenset({C.READ, C.NETWORK}), args={"target": ArgumentSpec(A.URL)}
        )
        self.assertIn(C.NETWORK, tool.capabilities)

    def test_a_network_tool_must_name_its_host(self):
        self.refused(caps=frozenset({C.NETWORK}), args={})
        self.refused(caps=frozenset({C.NETWORK}), args={"p": ArgumentSpec(A.PATH)})

    def test_a_credential_handle_argument_makes_it_a_credential_tool(self):
        self.refused(args={"credential": ArgumentSpec(A.CREDENTIAL_HANDLE)})

    def test_a_credential_tool_takes_a_handle(self):
        self.refused(caps=frozenset({C.CREDENTIAL_USE}), args={})
        self.refused(
            caps=frozenset({C.CREDENTIAL_USE}), args={"secret": ArgumentSpec(A.TEXT)}
        )

    def test_an_optional_argument_cannot_be_what_names_the_target(self):
        optional = ArgumentSpec(A.PATH, required=False)
        cases = {
            "write": {"caps": {C.WRITE}, "args": {"p": optional}},
            "destructive": {"caps": {C.DESTRUCTIVE}, "args": {"p": optional}},
            "write, optional project": {
                "caps": {C.WRITE},
                "args": {"p": ArgumentSpec(A.PROJECT, required=False)},
            },
            "network": {
                "caps": {C.READ, C.NETWORK},
                "args": {"u": ArgumentSpec(A.URL, required=False)},
            },
            "credential": {
                "caps": {C.CREDENTIAL_USE},
                "args": {"c": ArgumentSpec(A.CREDENTIAL_HANDLE, required=False)},
            },
        }
        for label, case in cases.items():
            with self.subTest(label=label):
                self.refused(caps=frozenset(case["caps"]), args=case["args"])
        # ... one required target is enough, optional ones may come with it
        tool = spec(
            caps=frozenset({C.WRITE}),
            args={"p": ArgumentSpec(A.PATH), "q": optional},
        )
        self.assertIn(C.WRITE, tool.capabilities)

    def test_a_project_local_write_must_name_what_it_touches(self):
        for caps in ({C.WRITE}, {C.DESTRUCTIVE}, {C.WRITE, C.READ}):
            with self.subTest(caps=caps):
                self.refused(
                    caps=frozenset(caps),
                    args={"text": ArgumentSpec(A.TEXT), "n": ArgumentSpec(A.INTEGER)},
                )
        for kind in (A.PATH, A.PROJECT):
            with self.subTest(kind=kind):
                tool = spec(caps=frozenset({C.WRITE}), args={"t": ArgumentSpec(kind)})
                self.assertIn(C.WRITE, tool.capabilities)

    def test_a_host_wide_write_needs_no_target_because_it_is_never_automatic(self):
        tool = spec(
            caps=frozenset({C.WRITE}),
            args={"package": ArgumentSpec(A.TEXT)},
            environment=Environment.HOST,
        )
        self.assertIs(tool.environment, Environment.HOST)

    def test_only_a_credential_tool_can_return_credential_plaintext(self):
        self.refused(returns_credential_plaintext=True)


class ArgumentDeclarationTest(unittest.TestCase):
    def test_argument_names_are_validated(self):
        for name in ("", "Path", "a-b", "1a", "a" * 33, "p​", 5):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    spec(args={name: ArgumentSpec(A.PATH)})

    def test_an_argument_must_be_an_argument_spec(self):
        with self.assertRaises(ValueError):
            spec(args={"path": "path"})

    def test_at_most_sixteen_arguments(self):
        many = {f"a{i}": ArgumentSpec(A.PATH) for i in range(17)}
        with self.assertRaises(ValueError):
            spec(args=many)
        spec(args={f"a{i}": ArgumentSpec(A.PATH) for i in range(16)})

    def test_argument_spec_bounds(self):
        with self.assertRaises(ValueError):
            ArgumentSpec(A.TEXT, max_length=0)
        with self.assertRaises(ValueError):
            ArgumentSpec(A.TEXT, max_length=70_000)
        with self.assertRaises(ValueError):
            ArgumentSpec(A.INTEGER, minimum=5, maximum=1)
        with self.assertRaises(TypeError):
            ArgumentSpec(A.INTEGER, minimum=True)
        with self.assertRaises(TypeError):
            ArgumentSpec(A.TEXT, required="yes")
        with self.assertRaises(ValueError):
            ArgumentSpec("free-text")


class ImmutabilityTest(unittest.TestCase):
    def test_a_spec_cannot_be_changed(self):
        tool = sample_registry().get("repo.read_file")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tool.capabilities = frozenset({C.DESTRUCTIVE})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            tool.min_level = ApprovalLevel.AUTO
        with self.assertRaises(TypeError):
            tool.arguments["extra"] = ArgumentSpec(A.TEXT)
        with self.assertRaises(AttributeError):
            tool.capabilities.add(C.WRITE)
        self.assertEqual(tool.capabilities, frozenset({C.READ}))

    def test_the_registry_has_no_way_to_change_after_construction(self):
        registry = sample_registry()
        for method in ("register", "add", "remove", "update", "set", "replace"):
            self.assertFalse(hasattr(registry, method), method)
        with self.assertRaises(TypeError):
            registry._specs["evil"] = spec("evil")
        self.assertIsNone(registry.get("evil"))

    def test_lookup_is_by_exact_name_only(self):
        registry = sample_registry()
        self.assertEqual(registry.get("repo.read_file").name, "repo.read_file")
        for name in (
            "REPO.READ_FILE",
            " repo.read_file",
            "repo.read_file ",
            "repo.read_file​",
            "repo.read_fıle",  # dotless i
            "read",
            "",
            None,
            5,
            b"repo.read_file",
            ["repo.read_file"],
        ):
            with self.subTest(name=name):
                self.assertIsNone(registry.get(name))

    def test_a_str_subclass_is_not_a_name(self):
        class Name(str):
            def __hash__(self):
                return hash("repo.read_file")

            def __eq__(self, other):
                return True

        self.assertIsNone(sample_registry().get(Name("anything")))

    def test_duplicates_and_foreign_objects_are_refused(self):
        with self.assertRaises(ValueError):
            ToolRegistry([spec(), spec()])
        with self.assertRaises(TypeError):
            ToolRegistry([spec(), "repo.read_file"])
        with self.assertRaises(TypeError):
            ToolRegistry("repo.read_file")

    def test_names_and_size(self):
        registry = sample_registry()
        self.assertEqual(len(registry), len(sample_specs()))
        self.assertIn("git.merge", registry.names())
        self.assertIsInstance(registry.names(), frozenset)


if __name__ == "__main__":
    unittest.main()
