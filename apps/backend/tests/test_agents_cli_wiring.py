"""The CLI runtimes in the composition (Decision 0099, "PR の分け方" 6).

``create_app(cli=...)`` / ``build_task_execution(cli=...)`` builds the CLI
runtimes the ladders name, each behind the production ``CloudPolicy``, the
``DagStore``'s ``cloud_send_check`` (the policy's ``recheck_in``) and the
orchestrator's own ``ConnectionService`` (the deployment's secret store, the
budget, the queue lease, the session adapters on production's launcher). No
CLI is started here.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from paw_backend.agents.claude_cli import ClaudeCliRuntime, ClaudeSessionAdapter
from paw_backend.agents.claude_probe import ClaudeProbeAdapter
from paw_backend.agents.cli_session import SystemdLauncher
from paw_backend.agents.cli_wiring import (
    CLAUDE_EXECUTABLE,
    CODEX_EXECUTABLE,
    EGRESS_PROXY_URL,
    CliAgentSetup,
    CliSetup,
    build_cli_services,
    build_probe_adapters,
)
from paw_backend.agents.cloud_policy import CloudSettings, ConfiguredCloudPolicy
from paw_backend.agents.codex_cli import CodexCliRuntime, CodexSessionAdapter
from paw_backend.app import create_app
from paw_backend.auth.principals import DatabasePrincipalDirectory
from paw_backend.authz import Authorizer, InMemoryAuditSink
from paw_backend.connections import PROBE_TIMEOUT_SECONDS, ConnectionKind
from paw_backend.connections.secret_store import FileSecretStore
from paw_backend.connections.service import ConnectionService
from paw_backend.connections.wiring import NoSecretStore, build_connection_service
from paw_backend.db import Database
from paw_backend.orchestrator import OrchestratorConfig
from paw_backend.orchestrator.composition import build_task_execution
from paw_backend.orchestrator.config import RoleAssignment
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.gateway import QueueLeaseVerifier
from paw_backend.projects import ProjectStateGate
from paw_backend.tasks.queueing import BudgetTracker, TaskQueue

from .orchestrator_support import FakeRuntime
from .support import FakeDatabase, make_settings
from .test_scratch_janitor_lifespan import configured

KEY = bytes(range(32))
RELAY = ("python3", "-I", "/opt/paw/paw_mcp_relay.py")
AGENTS = {
    "claude": CliAgentSetup("claude", "claude-opus-4-1", "/run/paw-cli", RELAY),
    "codex": CliAgentSetup("codex", "gpt-5-codex", "/run/paw-cli", RELAY),
}
LADDERS = {
    NodeRole.PLANNER: ("claude", "local"),
    NodeRole.WORKER: ("local", "codex"),
    NodeRole.RESEARCHER: ("local", "claude"),
    NodeRole.REVIEWER: ("claude", "codex"),
}
ASSIGNMENT = {
    NodeRole.WORKER: RoleAssignment(("local",), "local"),
    NodeRole.RESEARCHER: RoleAssignment(("local", "claude"), "local"),
    NodeRole.REVIEWER: RoleAssignment(("claude", "codex"), None),
    NodeRole.PLANNER: RoleAssignment(("local",), "local"),
}


def decisions_config(ladders=LADDERS) -> OrchestratorConfig:
    assignment = ASSIGNMENT if ladders is LADDERS else None
    return OrchestratorConfig(ladders, assignment=assignment)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.chmod(root, 0o700)
        self.store = FileSecretStore(root, KEY)


class ComposedCliRuntimesTest(StoreCase):
    def compose(self, **options):
        settings, database = configured()
        return create_app(
            settings,
            database=database,
            agent_runtimes={"local": FakeRuntime()},
            orchestrator_config=decisions_config(),
            cli=CliSetup(CloudSettings(enabled=True), AGENTS),
            **options,
        )

    def build(self, *, cli=None, config=None, secret_store=None, ladders=LADDERS):
        settings = make_settings()
        return build_task_execution(
            settings,
            Database(settings),
            Authorizer(InMemoryAuditSink()),
            runtimes={"local": FakeRuntime()},
            orchestrator_config=config or decisions_config(ladders),
            cli=cli or CliSetup(CloudSettings(enabled=True), AGENTS),
            secret_store=secret_store or self.store,
        )

    def test_the_orchestrator_runs_the_cli_runtimes_behind_the_policy(self):
        execution = self.build()
        orchestrator = execution.orchestrator
        self.assertEqual(set(orchestrator._runtimes), {"local", "claude", "codex"})
        claude = orchestrator._runtimes["claude"]
        codex = orchestrator._runtimes["codex"]
        self.assertIsInstance(claude, ClaudeCliRuntime)
        self.assertIsInstance(codex, CodexCliRuntime)
        policy = claude._gate._policy
        self.assertIsInstance(policy, ConfiguredCloudPolicy)
        self.assertIs(codex._gate._policy, policy)
        self.assertTrue(policy.settings.enabled)
        # The record of a send asks the policy again (Decision 0099, 2), for
        # the nodes' attempts and the planner's calls alike.
        self.assertEqual(orchestrator._store.cloud_send_check, policy.recheck_in)
        self.assertEqual(orchestrator._plan_calls._cloud_send_check, policy.recheck_in)
        # The file's model, session base and relay; the node timeout's margin.
        self.assertEqual(claude._model, "claude-opus-4-1")
        self.assertEqual(claude._base, Path("/run/paw-cli"))
        self.assertEqual(claude._relay, RELAY)
        self.assertEqual(
            claude._timeout, orchestrator._config.node_timeout_seconds - 60.0
        )
        self.assertEqual(codex._model, "gpt-5-codex")

    def test_the_orchestrators_connections(self):
        execution = self.build()
        orchestrator = execution.orchestrator
        connections = orchestrator._connections
        self.assertIsInstance(connections, ConnectionService)
        self.assertIsInstance(orchestrator._principals, DatabasePrincipalDirectory)
        # The deployment's secret store resolves, verifies and writes back.
        self.assertIs(connections._secrets, self.store)
        self.assertIs(connections._credentials, self.store)
        # Counted into the task's budget, fenced on the worker's queue lease.
        self.assertIs(connections._budget, execution.budget)
        self.assertIsInstance(connections._lease, QueueLeaseVerifier)
        self.assertIs(connections._lease._queue, execution.queue)
        adapters = connections._session_adapters
        self.assertEqual(set(adapters), {ConnectionKind.CLAUDE, ConnectionKind.CODEX})
        claude = adapters[ConnectionKind.CLAUDE]
        self.assertIsInstance(claude, ClaudeSessionAdapter)
        self.assertIsInstance(claude._launcher, SystemdLauncher)
        self.assertEqual(claude._executable, CLAUDE_EXECUTABLE)
        self.assertEqual(claude._proxy_url, EGRESS_PROXY_URL)
        codex = adapters[ConnectionKind.CODEX]
        self.assertIsInstance(codex, CodexSessionAdapter)
        self.assertEqual(codex._executable, CODEX_EXECUTABLE)
        self.assertEqual(codex._proxy_url, EGRESS_PROXY_URL)
        self.assertIsNotNone(codex._credentials)
        # Decision 0096, 1 D: this process collects the slots it keeps.
        self.assertIsNotNone(connections._session_collector)

    def test_only_the_cli_runtimes_a_ladder_names(self):
        ladders = {**LADDERS, NodeRole.WORKER: ("local",)}
        ladders[NodeRole.REVIEWER] = ("claude",)
        execution = self.build(ladders=ladders)
        orchestrator = execution.orchestrator
        self.assertEqual(set(orchestrator._runtimes), {"local", "claude"})
        self.assertEqual(
            set(orchestrator._connections._session_adapters), {ConnectionKind.CLAUDE}
        )
        self.assertIsNone(orchestrator._connections._session_collector)

    def test_without_cli_nothing_is_sent_from_the_orchestrator(self):
        settings, database = configured()
        app = create_app(
            settings,
            database=database,
            agent_runtimes={"local": FakeRuntime()},
            orchestrator_config=OrchestratorConfig.uniform(("local",)),
        )
        orchestrator = app.state.task_execution.orchestrator
        self.assertIsNone(orchestrator._connections)
        self.assertIsNone(orchestrator._store.cloud_send_check)

    def test_create_app_needs_the_secret_store_and_a_database(self):
        # The test settings have no PAW_SECRET_STORE_DIR.
        with self.assertRaises(TypeError):
            self.compose()
        with self.assertRaises(TypeError):
            create_app(
                make_settings(),
                database=FakeDatabase(),
                cli=CliSetup(CloudSettings(enabled=True), AGENTS),
            )
        with self.assertRaises(TypeError):
            create_app(make_settings(), cli=object())

    def test_it_refuses_what_it_cannot_use(self):
        settings = make_settings()
        database = Database(settings)
        authorizer = Authorizer(InMemoryAuditSink())
        cli = CliSetup(CloudSettings(enabled=True), AGENTS)
        with self.assertRaises(TypeError):
            # No orchestrator_config.
            build_task_execution(
                settings, database, authorizer, cli=cli, secret_store=self.store
            )
        with self.assertRaises(TypeError):
            # No secret store.
            build_task_execution(
                settings,
                database,
                authorizer,
                runtimes={"local": FakeRuntime()},
                orchestrator_config=decisions_config(),
                cli=cli,
            )
        with self.assertRaises(TypeError):
            build_task_execution(
                settings,
                database,
                authorizer,
                runtimes={"local": FakeRuntime(), "claude": FakeRuntime()},
                orchestrator_config=decisions_config(),
                cli=cli,
                secret_store=self.store,
            )

    def test_build_cli_services_fails_closed(self):
        settings = make_settings()
        database = Database(settings)
        common = dict(
            database=database,
            authorizer=Authorizer(InMemoryAuditSink()),
            budget=BudgetTracker(database),
            queue=TaskQueue(database, project_gate=ProjectStateGate()),
            node_seconds=1800.0,
            database_timeout_seconds=5.0,
        )
        with self.assertRaises(TypeError):
            # A ladder's CLI without its section.
            build_cli_services(
                CliSetup(CloudSettings(enabled=True), {"claude": AGENTS["claude"]}),
                ladders=LADDERS,
                secret_store=self.store,
                **common,
            )
        with self.assertRaises(TypeError):
            # The switch off.
            build_cli_services(
                CliSetup(CloudSettings(enabled=False), AGENTS),
                ladders=LADDERS,
                secret_store=self.store,
                **common,
            )
        with self.assertRaises(TypeError):
            build_cli_services(
                CliSetup(CloudSettings(enabled=True), AGENTS),
                ladders=LADDERS,
                secret_store=None,
                **common,
            )
        with self.assertRaises(TypeError):
            CliSetup(CloudSettings(enabled=True), {"codex": AGENTS["claude"]})


class ProbeWiringTest(StoreCase):
    """Decision 0100, 4 and 11: the Claude connection's health probe in both
    connection services, whether a ladder names the CLI or not."""

    def assertClaudeProbe(self, service):
        self.assertEqual(service._adapters.kinds(), (ConnectionKind.CLAUDE,))
        probe = service._adapters.get(ConnectionKind.CLAUDE)
        self.assertIsInstance(probe, ClaudeProbeAdapter)
        self.assertIsInstance(probe._launcher, SystemdLauncher)
        self.assertEqual(probe._executable, CLAUDE_EXECUTABLE)
        self.assertEqual(probe._proxy_url, EGRESS_PROXY_URL)
        self.assertEqual(probe._model, "claude-opus-4-1")
        self.assertEqual(probe._base, Path("/run/paw-cli"))
        self.assertEqual(service._health_timeout, PROBE_TIMEOUT_SECONDS)

    def test_the_probes_need_the_cloud_and_claudes_section(self):
        self.assertEqual(build_probe_adapters(None), ())
        self.assertEqual(
            build_probe_adapters(CliSetup(CloudSettings(enabled=False), AGENTS)), ()
        )
        self.assertEqual(
            build_probe_adapters(
                CliSetup(CloudSettings(enabled=True), {"codex": AGENTS["codex"]})
            ),
            (),
        )
        (probe,) = build_probe_adapters(CliSetup(CloudSettings(enabled=True), AGENTS))
        self.assertIsInstance(probe, ClaudeProbeAdapter)

    def test_the_orchestrators_service_probes_claude(self):
        for ladders in (LADDERS, {role: ("local",) for role in NodeRole}):
            with self.subTest(ladders=ladders):
                settings = make_settings()
                execution = build_task_execution(
                    settings,
                    Database(settings),
                    Authorizer(InMemoryAuditSink()),
                    runtimes={"local": FakeRuntime()},
                    orchestrator_config=decisions_config(ladders),
                    cli=CliSetup(CloudSettings(enabled=True), AGENTS),
                    secret_store=self.store,
                )
                self.assertClaudeProbe(execution.orchestrator._connections)

    def test_the_http_service_probes_claude_with_the_secret_store(self):
        settings = make_settings()
        database = Database(settings)
        authorizer = Authorizer(InMemoryAuditSink())
        probes = build_probe_adapters(CliSetup(CloudSettings(enabled=True), AGENTS))
        service = build_connection_service(
            settings,
            database,
            authorizer,
            InMemoryAuditSink(),
            secret_store=self.store,
            probes=probes,
        )
        self.assertClaudeProbe(service)
        self.assertIs(service._secrets, self.store)
        # Still no call: no session adapter, no budget, no lease.
        self.assertEqual(service._session_adapters, {})
        self.assertIsNone(service._budget)
        with self.assertRaises(TypeError):
            build_connection_service(
                settings, database, authorizer, InMemoryAuditSink(), probes=probes
            )
        plain = build_connection_service(
            settings, database, authorizer, InMemoryAuditSink(), secret_store=self.store
        )
        self.assertEqual(len(plain._adapters), 0)
        self.assertIsInstance(plain._secrets, NoSecretStore)

    def test_create_app_without_cli_registers_no_probe(self):
        settings, database = configured()
        app = create_app(settings, database=database)
        self.assertEqual(len(app.state.connections._adapters), 0)


if __name__ == "__main__":
    unittest.main()
