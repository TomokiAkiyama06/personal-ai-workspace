"""The CLI agents' models before a send (Decision 0103, 3 A, 4 A, 5 A, 9 A and
10; approved 2026-11-08; issue #341). No database: the policy's store is a
fake; the CLIs are the fakes of the runtime and probe tests.

* the repository's verified pairs: ``(2.1.301, claude-opus-5-5)`` only (no
  Codex pair: ``0.156.1 x gpt-6-sol`` failed its recorder smoke); no hidden
  Codex model, no Claude alias; every Codex version is one the version check
  runs. The rules and the gate are tested on a test pair of Codex
  (``agent_models_support``);
* the policy's rules: verified models only, at least one, no model twice,
  exactly one default, a short plain description;
* the gate: without a policy the configured model is the only allowed one;
  with one, its models; never a model verified for no version; the probe's
  default is the policy's (else the configuration's) and only a verified one;
* the runtimes: a model the gate refuses is ``model_not_allowed`` before any
  CLI starts, the gate or the placement is asked, a submit-free session
  included; the adapters refuse a version and model that are not a verified
  pair (Codex before its login is placed and before ``thread/start``, Claude
  at ``system/init``), and a verified pair runs as before; Codex's version
  check stays the version's;
* the probes: they check the gate's default model, send nothing for none (the
  connection's status kept), and refuse an unverified pair with the status
  kept;
* the wiring and the start-up line: a kind whose configured model is verified
  on no version gets no probe and the line says ``model: unverified``.
"""

import json
import unittest

from paw_backend.agent_models import (
    VERIFIED_MODELS,
    AgentModelGate,
    AllowedModel,
    InvalidModelPolicyError,
    KindPolicy,
    ModelNotVerifiedError,
    check_policy,
    is_verified,
    verified_models,
    verified_versions,
)
from paw_backend.agents.claude_stream import ClaudeStream, StreamRefused
from paw_backend.agents.cli_wiring import (
    CliAgentSetup,
    CliSetup,
    build_probe_adapters,
    configured_models_summary,
)
from paw_backend.agents.cloud_policy import CloudSettings
from paw_backend.agents.codex_app_server import VERIFIED_VERSIONS
from paw_backend.connections import ConnectionKind, FailureCode, ProbeOutcome, Secret

from . import test_agents_claude_cli as claude_cli
from . import test_agents_claude_probe as claude_probe
from . import test_agents_codex_cli as codex_cli
from . import test_agents_codex_probe as codex_probe
from .agent_models_support import TEST_CODEX_MODEL, verified_pairs_of
from .test_runtime_config_cloud import decision_text, load

CODEX, CLAUDE = ConnectionKind.CODEX, ConnectionKind.CLAUDE
MODEL = TEST_CODEX_MODEL
RELAY = ("python3", "-I", "/opt/paw/paw_mcp_relay.py")


class Store:
    """The policy's store: what ``read`` answers (or raises)."""

    def __init__(self, policies=None, error=None):
        self.policies = policies or {}
        self.error = error
        self.reads = 0

    async def read(self):
        self.reads += 1
        if self.error is not None:
            raise self.error
        return dict(self.policies)


class Gate:
    """A gate that answers as a test says."""

    def __init__(self, *, allows=True, default=None, error=None):
        self.answer = allows
        self.default = default
        self.error = error
        self.asked = []

    async def allows(self, kind, model):
        self.asked.append((kind, model))
        return self.answer

    async def default_model(self, kind):
        self.asked.append((kind, None))
        if self.error is not None:
            raise self.error
        return self.default


def policy(kind, *models, default=None):
    default = default or models[0]
    return KindPolicy(kind, tuple(AllowedModel(m, m == default) for m in models))


class VerifiedTest(unittest.TestCase):
    def test_the_evidenced_pairs_and_nothing_else(self):
        self.assertEqual(
            dict(VERIFIED_MODELS),
            {
                CODEX: frozenset(),
                CLAUDE: frozenset({("2.1.301", "claude-opus-5-5")}),
            },
        )
        self.assertTrue(is_verified(CLAUDE, "2.1.301", "claude-opus-5-5"))
        self.assertFalse(is_verified(CLAUDE, "2.1.316", "claude-opus-5-5"))
        self.assertFalse(is_verified(CLAUDE, None, "claude-opus-5-5"))
        self.assertEqual(verified_versions(CLAUDE, "claude-opus-5-5"), ("2.1.301",))

    def test_gpt_6_sol_is_not_verified(self):
        # Its recorder smoke on 0.156.1 offered tools outside ``tools``
        # (``additional_tools``: ``exec``, ``spawn_agent``, ...): not a pair.
        self.assertFalse(is_verified(CODEX, "0.156.1", "gpt-6-sol"))
        with self.assertRaises(ModelNotVerifiedError):
            check_policy(CODEX, [AllowedModel("gpt-6-sol", True)])

    def test_no_hidden_codex_model_and_no_claude_alias(self):
        codex = verified_models(CODEX)
        for hidden in ("gpt-5.4", "codex-auto-review"):
            self.assertNotIn(hidden, codex)
        self.assertFalse(any(m.startswith("gpt-daybreak") for m in codex))
        for model in verified_models(CLAUDE):
            # A full id: never an alias, a ``[1m]`` suffix or ``default``.
            self.assertRegex(model, r"^claude-[a-z]+-\d+(-\d+)*$")
        for alias in ("opus", "sonnet", "haiku", "default", "opusplan", "best"):
            self.assertNotIn(alias, verified_models(CLAUDE))

    def test_every_codex_version_is_one_the_version_check_runs(self):
        versions = {version for version, _ in VERIFIED_MODELS[CODEX]}
        self.assertLessEqual(versions, set(VERIFIED_VERSIONS))


class PolicyRulesTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(verified_pairs_of())

    def test_a_good_policy(self):
        checked = check_policy(CODEX, [AllowedModel(MODEL, True, "Coding")])
        self.assertEqual(checked, (AllowedModel(MODEL, True, "Coding"),))

    def test_a_model_the_repository_has_not_verified(self):
        for models in (
            [AllowedModel("gpt-6-astra", True)],  # not yet smoke-tested
            [AllowedModel("gpt-6-sol", True)],  # its smoke failed
            [AllowedModel("gpt-5.4", True)],  # hidden
            [AllowedModel("claude-opus-5-5", True)],  # the other kind's
        ):
            with self.subTest(models), self.assertRaises(ModelNotVerifiedError):
                check_policy(CODEX, models)
        with self.assertRaises(ModelNotVerifiedError):
            check_policy(CLAUDE, [AllowedModel("opus", True)])

    def test_exactly_one_default_and_the_other_rules(self):
        sol = AllowedModel(MODEL, True)
        for name, kind, models in (
            ("no default", CODEX, [AllowedModel(MODEL, False)]),
            ("two defaults", CLAUDE,
             [AllowedModel("claude-opus-5-5", True),
              AllowedModel("claude-opus-5-6", True)]),
            ("no model", CODEX, []),
            ("twice", CODEX, [sol, AllowedModel(MODEL, False)]),
            ("not a model", CODEX, [AllowedModel("gpt 6", True)]),
            ("not a kind", "codex", [sol]),
            ("not a list", CODEX, sol),
            ("a control character", CODEX,
             [AllowedModel(MODEL, True, "a\nb")]),
            ("an empty description", CODEX, [AllowedModel(MODEL, True, "")]),
            ("a long description", CODEX,
             [AllowedModel(MODEL, True, "d" * 121)]),
            ("spaces around", CODEX, [AllowedModel(MODEL, True, " x")]),
        ):  # fmt: skip
            with self.subTest(name), self.assertRaises(InvalidModelPolicyError):
                check_policy(kind, models)


class GateTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(verified_pairs_of())

    async def test_without_a_policy_the_configured_model_only(self):
        gate = AgentModelGate({CODEX: MODEL}, Store())
        self.assertTrue(await gate.allows(CODEX, MODEL))
        self.assertFalse(await gate.allows(CLAUDE, "claude-opus-5-5"))
        self.assertEqual(await gate.default_model(CODEX), MODEL)
        self.assertIsNone(await gate.default_model(CLAUDE))
        no_store = AgentModelGate({CLAUDE: "claude-opus-5-5"})
        self.assertTrue(await no_store.allows(CLAUDE, "claude-opus-5-5"))

    async def test_a_configured_model_verified_on_no_version(self):
        gate = AgentModelGate({CODEX: "gpt-5.5-codex"}, Store())
        self.assertFalse(gate.configured_verified(CODEX))
        self.assertFalse(await gate.allows(CODEX, "gpt-5.5-codex"))
        self.assertIsNone(await gate.default_model(CODEX))

    async def test_with_a_policy_its_models(self):
        store = Store({CLAUDE: policy(CLAUDE, "claude-opus-5-5")})
        gate = AgentModelGate({CLAUDE: "claude-other-model"}, store)
        self.assertTrue(await gate.allows(CLAUDE, "claude-opus-5-5"))
        self.assertFalse(await gate.allows(CLAUDE, "claude-other-model"))
        self.assertEqual(await gate.default_model(CLAUDE), "claude-opus-5-5")
        # Read on every call that gets past the verified models: a change is
        # seen without a restart.
        self.assertEqual(store.reads, 2)

    async def test_a_policy_model_that_is_no_longer_verified(self):
        # A release dropped the pair: the stored policy does not let it out.
        stale = KindPolicy(CODEX, (AllowedModel("gpt-6-astra", True),))
        gate = AgentModelGate({CODEX: MODEL}, Store({CODEX: stale}))
        self.assertFalse(await gate.allows(CODEX, "gpt-6-astra"))
        self.assertFalse(await gate.allows(CODEX, MODEL))
        self.assertIsNone(await gate.default_model(CODEX))

    async def test_an_unreadable_policy_raises(self):
        gate = AgentModelGate({CODEX: MODEL}, Store(error=TimeoutError()))
        with self.assertRaises(TimeoutError):
            await gate.allows(CODEX, MODEL)


class ClaudeStreamPairTest(unittest.TestCase):
    INIT = {"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}

    def feed(self, **init):
        stream = ClaudeStream(
            frozenset(),
            probe=True,
            verified_models=frozenset({("2.1.301", "claude-opus-5-5")}),
            model="claude-opus-5-5",
        )
        stream.feed(json.dumps({**self.INIT, **init}))
        return stream

    def test_the_init_needs_a_verified_version(self):
        self.assertTrue(self.feed(claude_code_version="2.1.301").initialized)
        for version in ({}, {"claude_code_version": "2.1.316"},
                        {"claude_code_version": 2}):  # fmt: skip
            with self.subTest(version), self.assertRaises(StreamRefused) as caught:
                self.feed(**version)
            self.assertEqual(caught.exception.reason, "model")
            self.assertFalse(caught.exception.unsupported)

    def test_the_tools_are_checked_first(self):
        with self.assertRaises(StreamRefused) as caught:
            self.feed(tools=["Bash"], claude_code_version="2.1.316")
        self.assertEqual(caught.exception.reason, "tools")


class ClaudeRuntimeTest(claude_cli.ClaudeCase):
    SUBMIT = [{"call": "submit", "arguments": {"summary": "It is in parse.py"}}]

    async def test_a_model_the_gate_refuses_starts_nothing(self):
        self.models = Gate(allows=False)
        outcome = await self.run_node(self.SUBMIT)
        self.assertEqual(
            (outcome.error_class, outcome.message, outcome.escalate),
            ("RuntimeError", "model_not_allowed", True),
        )
        self.assertEqual(self.models.asked, [(CLAUDE, "claude-sonnet-5")])
        self.assertFalse(self.report.exists())  # the CLI never started
        self.assertEqual(self.placement.recorded, [])
        self.assertFalse(hasattr(self.connections, "raw"))  # never admitted
        self.assertEqual(list(self.sessions.iterdir()), [])

    async def test_a_pair_that_is_not_verified_is_stopped_at_init(self):
        self.models = Gate()
        self.pairs = frozenset({("2.1.301", "claude-sonnet-5")})
        outcome = await self.run_node(
            [{"turn": [100, 10]}, *self.SUBMIT], claude_code_version="2.1.316"
        )
        self.assertEqual(outcome.message, "model_not_allowed")
        self.assertEqual(self.connections.turns, [])
        self.assertEqual(self.connections.raw.failure, FailureCode.INVALID_RESPONSE)

    async def test_a_verified_pair_runs(self):
        self.models = Gate()
        self.pairs = frozenset({("2.1.301", "claude-sonnet-5")})
        outcome = await self.run_node(
            [{"turn": [100, 10]}, *self.SUBMIT], claude_code_version="2.1.301"
        )
        self.assertEqual(outcome.result.summary, "It is in parse.py")
        self.assertEqual(self.placement.recorded[0][1:], ("claude", "claude-sonnet-5"))
        flags = self.read_report()["flags"]
        self.assertEqual(flags[flags.index("--model") + 1], "claude-sonnet-5")

    async def test_an_unreadable_policy_fails_closed(self):
        self.model = "claude-opus-5-5"
        self.models = AgentModelGate(
            {CLAUDE: "claude-opus-5-5"}, Store(error=OSError())
        )
        outcome = await self.run_node(self.SUBMIT)
        self.assertEqual(outcome.error_class, "AdapterError")
        self.assertFalse(self.report.exists())


class CodexRuntimeTest(codex_cli.CodexCase):
    SUBMIT = [
        {"call": "submit", "arguments": {"summary": "ok"}},
        {"usage": [3, 1]},
    ]

    async def test_a_model_the_gate_refuses_starts_nothing(self):
        self.models = Gate(allows=False)
        outcome = await self.run_node(self.SUBMIT)
        self.assertEqual(
            (outcome.error_class, outcome.message, outcome.escalate),
            ("RuntimeError", "model_not_allowed", True),
        )
        self.assertEqual(self.models.asked, [(CODEX, "gpt-5.5-codex")])
        self.assertEqual(self.probes(), 0)  # not even the version check
        self.assertEqual(self.placement.recorded, [])
        self.assertIsNone(self.connections.raw)

    async def test_a_pair_that_is_not_verified_places_no_login(self):
        self.models = Gate()
        self.pairs = frozenset({("0.162.1", "gpt-6-sol")})
        outcome = await self.run_node(self.SUBMIT)
        self.assertEqual(outcome.message, "model_not_allowed")
        self.assertEqual(self.probes(), 1)
        self.assertFalse(self.report.exists())  # app-server never started
        self.assertEqual(self.credentials.placed, [])

    async def test_initializes_version_is_checked_again(self):
        self.pairs = frozenset({("0.162.1", "gpt-5.5-codex")})
        outcome = await self.run_node(
            self.SUBMIT,
            adapter=self.adapter(verified=frozenset({"0.162.1", "0.163.0"})),
            user_agent_version="0.163.0",
        )
        self.assertEqual(outcome.message, "model_not_allowed")
        self.assertNotIn("thread/start", self.read_report()["requests"])
        self.assertEqual(self.connections.turns, [])

    async def test_a_verified_pair_runs(self):
        self.models = Gate()
        self.pairs = frozenset({("0.162.1", "gpt-5.5-codex")})
        outcome = await self.run_node(self.SUBMIT)
        self.assertEqual(outcome.result.summary, "ok")
        start = self.read_report()["requests"]["thread/start"]
        self.assertEqual(start["model"], "gpt-5.5-codex")
        self.assertEqual(self.placement.recorded[0][1:], ("codex", "gpt-5.5-codex"))

    async def test_an_unverified_version_is_still_the_versions_refusal(self):
        self.pairs = frozenset({("0.162.1", "gpt-5.5-codex")})
        outcome = await self.run_node(self.SUBMIT, version="0.170.0")
        self.assertEqual(outcome.message, "malformed_response")
        self.assertFalse(self.report.exists())


class ClaudeProbeModelTest(claude_probe.ProbeCase):
    async def check(self, actions, adapter, **scenario):
        self.scenario.write_text(json.dumps({"actions": actions, **scenario}))
        return await adapter.check_health(Secret(claude_probe.TOKEN))

    async def test_the_probe_checks_the_gates_default(self):
        gate = Gate(default="claude-opus-5-5")
        report = await self.check(claude_probe.GOOD, self.adapter(models=gate))
        self.assertEqual(report.outcome, ProbeOutcome.CONNECTED)
        self.assertEqual(report.model, "claude-opus-5-5")
        flags = self.reported()["flags"]
        self.assertEqual(flags[flags.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(gate.asked, [(CLAUDE, None)])

    async def test_no_default_sends_nothing_and_keeps_the_status(self):
        for gate, failure in (
            (Gate(default=None), FailureCode.INVALID_RESPONSE),
            (Gate(error=TimeoutError()), FailureCode.UNAVAILABLE),
        ):
            with self.subTest(failure):
                report = await self.check(claude_probe.GOOD, self.adapter(models=gate))
                self.assertEqual(
                    (report.outcome, report.failure, report.keeps_status),
                    (ProbeOutcome.UNAVAILABLE, failure, True),
                )
                self.assertFalse(self.report.exists())
                self.assertEqual(list(self.sessions.iterdir()), [])

    async def test_an_unverified_pair_keeps_the_status(self):
        adapter = self.adapter(
            verified_models=frozenset({("2.1.301", "claude-test-model")})
        )
        refused = await self.check(
            claude_probe.GOOD, adapter, claude_code_version="2.1.316"
        )
        self.assertEqual(
            (refused.outcome, refused.failure, refused.keeps_status),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE, True),
        )
        good = await self.check(
            claude_probe.GOOD, adapter, claude_code_version="2.1.301"
        )
        self.assertEqual(good.outcome, ProbeOutcome.CONNECTED)


class CodexProbeModelTest(codex_probe.ProbeCase):
    def adapter_with(self, **options):
        adapter = self.adapter()
        return type(adapter)(
            adapter._codex._launcher,
            model=codex_probe.MODEL,
            session_base=self.sessions,
            executable=adapter._codex._executable,
            verified_versions=frozenset({codex_probe.VERSION}),
            **options,
        )

    async def test_the_probe_checks_the_gates_default(self):
        gate = Gate(default="gpt-6-sol")
        report = await self.probe(codex_probe.GOOD, adapter=self.adapter_with(
            models=gate))  # fmt: skip
        self.assertEqual(report.outcome, ProbeOutcome.CONNECTED)
        self.assertEqual(report.model, "gpt-6-sol")
        self.assertEqual(self.sent()["requests"]["thread/start"]["model"], "gpt-6-sol")

    async def test_no_default_sends_nothing_and_keeps_the_status(self):
        report = await self.probe(
            codex_probe.GOOD, adapter=self.adapter_with(models=Gate(default=None))
        )
        self.assertEqual(
            (report.outcome, report.failure, report.keeps_status),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE, True),
        )
        self.assertFalse(self.version_checked())
        self.assertEqual(self.hooks.calls, [])

    async def test_an_unverified_pair_keeps_the_status(self):
        adapter = self.adapter_with(
            verified_models=frozenset({(codex_probe.VERSION, "gpt-other")})
        )
        report = await self.probe(codex_probe.GOOD, adapter=adapter)
        self.assertEqual(
            (report.outcome, report.failure, report.keeps_status),
            (ProbeOutcome.UNAVAILABLE, FailureCode.INVALID_RESPONSE, True),
        )
        self.assertTrue(self.version_checked())
        self.assertFalse(self.report.exists())  # app-server never started
        self.assertEqual(self.hooks.calls, [])  # no login bound or placed


class WiringTest(unittest.TestCase):
    def test_a_model_verified_on_no_version_gets_no_probe(self):
        agents = {
            "claude": CliAgentSetup("claude", "claude-opus-5-5", "/run/paw-cli", RELAY),
            "codex": CliAgentSetup("codex", "gpt-5.5-codex", "/run/paw-cli", RELAY),
        }
        (claude,) = build_probe_adapters(CliSetup(CloudSettings(enabled=True), agents))
        self.assertIs(claude.kind, CLAUDE)
        self.assertEqual(claude._pairs, VERIFIED_MODELS[CLAUDE])
        self.assertEqual(
            configured_models_summary(agents),
            ("agents.claude: model: verified", "agents.codex: model: unverified"),
        )

    def test_the_start_up_line_says_unverified(self):
        agents = (
            '[agents.claude]\nmodel = "claude-opus-5-5"\nsession_base ='
            ' "/run/paw-cli"\nrelay_command = ["python3", "-I",'
            ' "/opt/paw/paw_mcp_relay.py"]\n[agents.codex]\nmodel ='
            ' "gpt-5.5-codex"\nsession_base = "/run/paw-cli"\nrelay_command ='
            ' ["python3", "-I", "/opt/paw/paw_mcp_relay.py"]\n'
        )
        summary = load(decision_text(agents=agents)).summary()
        self.assertIn("agents.claude: model: verified", summary)
        self.assertIn("agents.codex: model: unverified", summary)
        self.assertNotIn("gpt-5.5-codex", summary)


if __name__ == "__main__":
    unittest.main()
