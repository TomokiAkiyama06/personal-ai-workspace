"""The cloud's sections of the runtime configuration (Decision 0099, 7).

``[cloud]``, ``[agents.claude]`` / ``[agents.codex]`` and
``[orchestrator.assignment]`` are parsed and checked, and every start-up refusal
of the Decision's section 7 has a test here (at least one each, as Decision
0093, 6 asks of the other sections). The CLI rungs stay disabled in the
assembly (``CLI_RUNGS_ENABLED``) until the CLI smoke (Decision 0099, "PR の分け方"
6): a test that needs a ladder with a CLI runtime turns the flag on itself.
"""

import unittest
from unittest import mock

from paw_backend.agents.cloud_policy import CloudSettings
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.runtime_config import (
    CLI_RUNGS_ENABLED,
    CliAgentSetup,
    RoleAssignment,
    RuntimeConfigError,
    RuntimeWiring,
    check_environment,
    load_runtime_config,
)

from .runtime_config_support import EXAMPLE, PYTHON, FakeHost, standard_host
from .support import make_settings

PATH = "/etc/paw/runtime.toml"

LOCAL_LADDERS = (
    '[orchestrator.ladders]\nplanner = ["local"]\nworker = ["local"]\n'
    'researcher = ["local"]\nreviewer = ["local"]\n'
)
CLOUD = "[cloud]\nenabled = true\nsingle_user_only = true\n"
AGENTS = (
    '[agents.claude]\nmodel = "claude-opus-4-1"\nsession_base = "/run/paw-cli"\n'
    'relay_command = ["python3", "-I", "/opt/paw/paw_mcp_relay.py"]\n'
    '[agents.codex]\nmodel = "gpt-5-codex"\nsession_base = "/run/paw-cli"\n'
    'relay_command = ["python3", "-I", "/opt/paw/paw_mcp_relay.py"]\n'
)
LADDERS = (
    "[orchestrator.ladders]\n"
    'planner = ["claude", "local"]\n'
    'worker = ["local", "codex"]\n'
    'researcher = ["local", "claude"]\n'
    'reviewer = ["claude", "codex"]\n'
)
ASSIGNMENT = (
    "[orchestrator.assignment]\n"
    'worker = { allowed = ["local"], default = "local" }\n'
    'researcher = { allowed = ["local", "claude"], default = "local" }\n'
    'reviewer = { allowed = ["claude", "codex"] }\n'
    'planner = { allowed = ["local"], default = "local" }\n'
)


def decision_text(
    *,
    cloud: str = CLOUD,
    agents: str = AGENTS,
    ladders: str = LADDERS,
    assignment: str = ASSIGNMENT,
) -> str:
    """The example with the Decision's section 7 (cloud, agents, ladders,
    assignment)."""
    base = EXAMPLE.replace("[orchestrator]\n" + LOCAL_LADDERS, "")
    assert base != EXAMPLE
    return base + cloud + agents + "[orchestrator]\n" + ladders + assignment


def load(text: str, host: FakeHost | None = None) -> RuntimeWiring:
    host = host if host is not None else standard_host()
    host.file(PATH, text, mode=0o640, uid=0)
    return load_runtime_config(
        PATH, make_settings(), host=host, python_executable=PYTHON
    )


def cli_rungs(enabled: bool = True):
    return mock.patch("paw_backend.runtime_config.CLI_RUNGS_ENABLED", enabled)


class Refused(unittest.TestCase):
    def assertRefused(self, text, where, *, reason=None):
        with self.assertRaises(RuntimeConfigError) as caught:
            load(text)
        self.assertEqual(caught.exception.where, where, str(caught.exception))
        if reason is not None:
            self.assertIn(reason, caught.exception.reason)
        return caught.exception


class TheDecisionsFileTest(Refused):
    def test_the_cli_rungs_are_disabled_in_the_assembly(self):
        self.assertFalse(CLI_RUNGS_ENABLED)
        # Every other check passes: the first ladder with a CLI runtime is
        # refused (the planner's).
        self.assertRefused(
            decision_text(),
            "orchestrator.ladders.planner",
            reason="cli runtimes are not enabled yet",
        )

    def test_the_decisions_file_parses_when_the_rungs_are_enabled(self):
        with cli_rungs():
            wiring = load(decision_text())
        self.assertEqual(wiring.cloud, CloudSettings(enabled=True))
        self.assertEqual(
            wiring.agents,
            {
                "claude": CliAgentSetup(
                    "claude",
                    "claude-opus-4-1",
                    "/run/paw-cli",
                    ("python3", "-I", "/opt/paw/paw_mcp_relay.py"),
                ),
                "codex": CliAgentSetup(
                    "codex",
                    "gpt-5-codex",
                    "/run/paw-cli",
                    ("python3", "-I", "/opt/paw/paw_mcp_relay.py"),
                ),
            },
        )
        self.assertEqual(
            wiring.assignment,
            {
                NodeRole.WORKER: RoleAssignment(("local",), "local"),
                NodeRole.RESEARCHER: RoleAssignment(("local", "claude"), "local"),
                NodeRole.REVIEWER: RoleAssignment(("claude", "codex"), None),
                NodeRole.PLANNER: RoleAssignment(("local",), "local"),
            },
        )
        self.assertEqual(
            wiring.orchestrator_config.ladders[NodeRole.PLANNER], ("claude", "local")
        )
        summary = wiring.summary()
        self.assertIn("cloud: enabled single_user", summary)
        self.assertIn("assignment: yes", summary)
        self.assertNotIn("/run/paw-cli", summary)
        self.assertNotIn("paw_mcp_relay", summary)
        # Not create_app arguments yet (the CLI runtimes are built in PR 6).
        self.assertEqual(
            set(wiring.create_app_arguments()),
            {"compute", "local_model", "sandbox", "git_runner", "orchestrator_config"},
        )

    def test_the_sections_without_a_cli_rung_load_today(self):
        # [cloud], [agents.*] and an assignment inside local-only ladders: no
        # CLI rung, so nothing is refused with the flag off.
        text = decision_text(
            ladders=LOCAL_LADDERS,
            assignment="",
        )
        wiring = load(text)
        self.assertTrue(wiring.cloud.enabled)
        self.assertEqual(set(wiring.agents), {"claude", "codex"})
        self.assertIsNone(wiring.assignment)
        self.assertIn("cloud: enabled single_user", wiring.summary())
        self.assertNotIn("assignment", wiring.summary())

    def test_cloud_disabled_and_absent(self):
        text = decision_text(cloud="[cloud]\n", ladders=LOCAL_LADDERS, assignment="")
        wiring = load(text)
        self.assertEqual(wiring.cloud, CloudSettings(enabled=False))
        self.assertIn("cloud: disabled", wiring.summary())
        wiring = load(EXAMPLE)
        self.assertIsNone(wiring.cloud)
        self.assertEqual(wiring.agents, {})
        self.assertNotIn("cloud", wiring.summary())


class CloudSectionTest(Refused):
    def test_the_values(self):
        for text, where in (
            ('version = 1\n[cloud]\nenabled = "yes"\n', "cloud.enabled"),
            ("version = 1\n[cloud]\nenabled = 1\n", "cloud.enabled"),
            (
                "version = 1\n[cloud]\nsingle_user_only = false\n",
                "cloud.single_user_only",
            ),
            (
                'version = 1\n[cloud]\nsingle_user_only = "x"\n',
                "cloud.single_user_only",
            ),
            ('version = 1\n[cloud]\nprojects = ["a"]\n', "cloud.projects"),
            ("version = 1\ncloud = true\n", "cloud"),
        ):
            with self.subTest(text=text):
                self.assertRefused(text, where)

    def test_cloud_needs_the_database(self):
        wiring = load("version = 1\n[cloud]\nenabled = true\n")
        with self.assertRaises(RuntimeConfigError) as caught:
            check_environment(wiring, make_settings())
        self.assertEqual(caught.exception.where, "cloud")
        check_environment(
            load("version = 1\n[cloud]\nenabled = false\n"),
            make_settings(),
        )


class AgentsSectionTest(Refused):
    def agent(self, **values) -> str:
        lines = {
            "model": '"claude-opus-4-1"',
            "session_base": '"/run/paw-cli"',
            "relay_command": '["python3", "-I", "/opt/paw/paw_mcp_relay.py"]',
        }
        lines.update(values)
        body = "".join(
            f"{key} = {value}\n" for key, value in lines.items() if value is not None
        )
        return "version = 1\n[agents.claude]\n" + body

    def test_a_valid_section(self):
        wiring = load(self.agent())
        self.assertEqual(wiring.agents["claude"].model, "claude-opus-4-1")

    def test_required_keys(self):
        for key in ("model", "session_base", "relay_command"):
            with self.subTest(key=key):
                self.assertRefused(
                    self.agent(**{key: None}), f"agents.claude.{key}", reason="required"
                )

    def test_the_values(self):
        for key, value in (
            ("model", '""'),
            ("model", '"has space"'),
            ("model", "1"),
            ("session_base", '"run/paw-cli"'),
            ("session_base", '"/run/../etc"'),
            ("session_base", '"/"'),
            ("relay_command", '"python3 -I relay.py"'),
            ("relay_command", "[]"),
            ("relay_command", '[""]'),
            ("relay_command", "[1]"),
        ):
            with self.subTest(key=key, value=value):
                self.assertRefused(self.agent(**{key: value}), f"agents.claude.{key}")

    def test_unknown_and_fixed_keys(self):
        self.assertRefused(
            self.agent(token='"x"'), "agents.claude.token", reason="unknown key"
        )
        for key in ("node_seconds", "limits", "cloud_policy"):
            with self.subTest(key=key):
                self.assertRefused(
                    self.agent(**{key: "1"}),
                    f"agents.claude.{key}",
                    reason="not settable",
                )
        self.assertRefused(
            'version = 1\n[agents.gemini]\nmodel = "m"\n', "agents.gemini"
        )

    def test_the_local_label_is_not_a_cli_label(self):
        for label in ("claude", "codex"):
            with self.subTest(label=label):
                self.assertRefused(
                    EXAMPLE.replace(
                        'agent_label = "local"', f'agent_label = "{label}"'
                    ),
                    "local_model.agent_label",
                    reason="cli runtime",
                )


class LadderChecksTest(Refused):
    """Decision 0099, 7: a CLI label on a ladder needs [cloud] enabled and its
    [agents.<name>]; codex is never a planner's rung."""

    def test_a_cli_rung_needs_the_cloud_enabled(self):
        for cloud in ("", "[cloud]\nenabled = false\n"):
            with self.subTest(cloud=cloud), cli_rungs():
                self.assertRefused(
                    decision_text(cloud=cloud),
                    "orchestrator.ladders.planner",
                    reason="[cloud] enabled = true",
                )

    def test_a_cli_rung_needs_its_agents_section(self):
        claude_only = AGENTS.split("[agents.codex]")[0]
        with cli_rungs():
            self.assertRefused(
                decision_text(agents=claude_only),
                "orchestrator.ladders.worker",
                reason="[agents.codex]",
            )
            self.assertRefused(
                decision_text(agents=""),
                "orchestrator.ladders.planner",
                reason="[agents.claude]",
            )

    def test_codex_is_never_a_planners_rung(self):
        for planner in ('["codex", "local"]', '["local", "codex"]'):
            with self.subTest(planner=planner), cli_rungs():
                self.assertRefused(
                    decision_text(
                        ladders=LADDERS.replace(
                            'planner = ["claude", "local"]', f"planner = {planner}"
                        )
                    ),
                    "orchestrator.ladders.planner",
                    reason="other than claude",
                )


class AssignmentTest(Refused):
    """Decision 0099, 4 and 7: ``[orchestrator.assignment]``."""

    def refused(self, assignment, where, reason=None, ladders=LADDERS):
        with cli_rungs():
            return self.assertRefused(
                decision_text(assignment=assignment, ladders=ladders),
                where,
                reason=reason,
            )

    def entry(self, role: str, value: str) -> str:
        lines = ASSIGNMENT.splitlines(keepends=True)
        return "".join(
            f"{role} = {value}\n" if line.startswith(f"{role} =") else line
            for line in lines
        )

    def test_allowed_outside_the_ladder(self):
        self.refused(
            self.entry(
                "researcher", '{ allowed = ["local", "codex"], default = "local" }'
            ),
            "orchestrator.assignment.researcher.allowed",
            "not on orchestrator.ladders.researcher",
        )
        self.refused(
            self.entry("planner", '{ allowed = ["qwen"], default = "qwen" }'),
            "orchestrator.assignment.planner.allowed",
            "not on orchestrator.ladders.planner",
        )

    def test_a_worker_is_never_assigned_a_cli_runtime(self):
        self.refused(
            self.entry("worker", '{ allowed = ["local", "codex"], default = "local" }'),
            "orchestrator.assignment.worker.allowed",
            "cli runtime",
        )

    def test_the_reviewer_needs_claude_and_codex(self):
        for allowed in ('["claude"]', '["codex"]'):
            with self.subTest(allowed=allowed):
                self.refused(
                    self.entry("reviewer", f"{{ allowed = {allowed} }}"),
                    "orchestrator.assignment.reviewer.allowed",
                    "both claude and codex",
                )
        # And a reviewer ladder without both cannot hold them.
        self.refused(
            ASSIGNMENT,
            "orchestrator.assignment.reviewer.allowed",
            "not on orchestrator.ladders.reviewer",
            ladders=LADDERS.replace(
                'reviewer = ["claude", "codex"]', 'reviewer = ["claude"]'
            ),
        )

    def test_the_default(self):
        self.refused(
            self.entry("worker", '{ allowed = ["local"] }'),
            "orchestrator.assignment.worker.default",
            "required",
        )
        self.refused(
            self.entry(
                "researcher", '{ allowed = ["local", "claude"], default = "codex" }'
            ),
            "orchestrator.assignment.researcher.default",
            "one of allowed",
        )
        self.refused(
            self.entry("reviewer", '{ allowed = ["claude", "codex"], default = "x" }'),
            "orchestrator.assignment.reviewer.default",
        )
        with cli_rungs():
            wiring = load(
                decision_text(
                    assignment=self.entry(
                        "reviewer",
                        '{ allowed = ["claude", "codex"], default = "claude" }',
                    )
                )
            )
        self.assertEqual(wiring.assignment[NodeRole.REVIEWER].default, "claude")

    def test_the_shape(self):
        for role, value, where in (
            ("worker", '{ allowed = [] , default = "local" }', "worker.allowed"),
            ("worker", '{ allowed = "local", default = "local" }', "worker.allowed"),
            (
                "worker",
                '{ allowed = ["local", "local"], default = "local" }',
                "worker.allowed",
            ),
            ("worker", '{ allowed = ["Local"], default = "Local" }', "worker.allowed"),
            ("worker", '{ allowed = ["local"], default = "local", x = 1 }', "worker.x"),
            ("worker", '"local"', "worker"),
        ):
            with self.subTest(value=value):
                self.refused(
                    self.entry(role, value), f"orchestrator.assignment.{where}"
                )
        missing = "".join(
            line
            for line in ASSIGNMENT.splitlines(keepends=True)
            if not line.startswith("planner =")
        )
        self.refused(missing, "orchestrator.assignment.planner", "required")
        self.refused(
            ASSIGNMENT + 'tester = { allowed = ["local"], default = "local" }\n',
            "orchestrator.assignment.tester",
            "unknown key",
        )


class NoValueInErrorsTest(unittest.TestCase):
    MARK = "zq-secret-marker"

    def test_no_value_in_any_error(self):
        mark = self.MARK
        cases = (
            f'version = 1\n[cloud]\nenabled = "{mark}"\n',
            f'version = 1\n[cloud]\nsingle_user_only = "{mark}"\n',
            f'version = 1\n[agents.claude]\nmodel = "{mark} x"\n'
            'session_base = "/run/paw-cli"\nrelay_command = ["r"]\n',
            f'version = 1\n[agents.claude]\nmodel = "m"\nsession_base = "{mark}"\n'
            'relay_command = ["r"]\n',
            'version = 1\n[agents.claude]\nmodel = "m"\nsession_base = "/run/p"\n'
            f'relay_command = "{mark}"\n',
            decision_text(
                assignment=ASSIGNMENT.replace(
                    'default = "local" }\nresearcher',
                    f'default = "{mark}" }}\nresearcher',
                )
            ),
            decision_text(
                assignment=ASSIGNMENT.replace(
                    'worker = { allowed = ["local"]',
                    f'worker = {{ allowed = ["{mark}"]',
                )
            ),
        )
        for text in cases:
            with self.subTest(text=text[-60:]), cli_rungs():
                with self.assertRaises(RuntimeConfigError) as caught:
                    load(text)
                self.assertNotIn(mark, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
