"""The CLI runtimes in the application (Decision 0099, 2, 3, 7 and "PR の分け方"
6, Approved; Decision 0083, 1 and 6; issue #297).

The runtime configuration's ``[cloud]`` and ``[agents.claude]`` /
``[agents.codex]`` (``runtime_config.py``) become a :class:`CliSetup`, a
``create_app`` argument. With it, :func:`build_cli_services` builds, next to the
orchestrator (``orchestrator/composition.py``):

* the production ``CloudPolicy`` (:class:`~.cloud_policy.ConfiguredCloudPolicy`
  on the orchestrator's ladders, with the database's users and projects); its
  ``recheck_in`` is the ``DagStore``'s ``cloud_send_check`` (the record of a
  send asks it again, under the single-user lock: Decision 0099, 2);
* the orchestrator's own ``ConnectionService``: the deployment's secret store
  (the credentials, their write-back and rotation), the task budget (the
  session's turn-by-turn count, Decision 0083, 5), the queue lease and the
  session adapters of the configured CLIs, each on production's launcher
  (``SystemdLauncher``: ``paw-cli-session@<uuid>.service``, the image's
  ``/opt/paw/start-claude`` / ``start-codex`` behind the session's egress
  forwarder on ``127.0.0.1:3128``). With Codex, the login collector of Decision
  0096 is attached to it (a slot kept for a session whose CLI could not be
  stopped is collected by this process);
* the principals the sessions act for (``DatabasePrincipalDirectory``);
* the runtimes: ``ClaudeCliRuntime`` / ``CodexCliRuntime`` under their labels,
  for each configured CLI that a ladder names.

Nothing is sent by building them: every send still goes through ``CloudGate``
(policy -> admission -> placement record -> adapter). Codex sends nothing until
its CLI's version is verified (``codex_app_server.VERIFIED_VERSIONS``, Decision
0089, 2).

The health probes (Decision 0100, 4, 10 and 11; issue #306): with ``[cloud]
enabled = true``, a configured ``[agents.claude]`` gives the Claude connection
a :class:`~.claude_probe.ClaudeProbeAdapter` and a configured
``[agents.codex]`` the Codex connection a :class:`~.codex_probe.
CodexProbeAdapter` (one short session with a fixed prompt and no tools, on the
same launcher, image and egress; Codex's in the connection's session slot,
with its login's write-back) — whether a ladder names the CLI or not, so that
the connection is checked before it is used. :func:`build_probe_adapters`
builds them; they are registered in this orchestrator's ``ConnectionService``
and in the HTTP layer's (``connections/wiring.py``), each built with
``PROBE_TIMEOUT_SECONDS`` as its ``health_timeout_seconds`` and the secret
store as its credential store (the write-back). Codex's probe on the HTTP
layer's service is collected by that service's own collector (``app.py``'s
``build_codex_collection``) when its CLI could not be stopped.

The models (Decision 0103, 4 A, 5 A, 9 A and 10; issue #341): the runtimes and
the probes ask an :class:`~paw_backend.agent_models.AgentModelGate` (the
runtime configuration's models and the Admin's policy in the database) before
anything is sent: a runtime refuses a model the policy does not allow
(``model_not_allowed``), a probe checks the policy's default model (the
configuration's while no policy is stored). The session adapters and the
probes are given the repository's verified pairs
(:data:`~paw_backend.agent_models.VERIFIED_MODELS`) and refuse a pair of the
CLI's version and the model that is not one of them. A kind whose configured
model is verified on no version gets no probe (Decision 0103, 10 A, 2: the
backend still starts; ``configured_models_summary`` says ``model: unverified``
in the start-up line) and its runtime's every session is refused.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from paw_backend.agent_models import (
    VERIFIED_MODELS,
    AgentModelGate,
    AgentModelPolicyStore,
    verified_models,
)
from paw_backend.agents.claude_cli import ClaudeCliRuntime, ClaudeSessionAdapter
from paw_backend.agents.claude_probe import ClaudeProbeAdapter
from paw_backend.agents.cli_session import CliLauncher, SystemdLauncher
from paw_backend.agents.cloud_policy import (
    CLI_AGENT_LABELS,
    CloudSettings,
    ConfiguredCloudPolicy,
    PostgresCloudFacts,
)
from paw_backend.agents.codex_cli import (
    CodexCliRuntime,
    CodexSessionAdapter,
    SystemdCodexLauncher,
)
from paw_backend.agents.codex_collect import CodexLoginCollector, SystemdSessionUnits
from paw_backend.agents.codex_credentials import CodexAuthFile
from paw_backend.agents.codex_probe import CodexProbeAdapter
from paw_backend.auth.principals import DatabasePrincipalDirectory
from paw_backend.authz import Authorizer, PostgresAuditSink
from paw_backend.connections import PROBE_TIMEOUT_SECONDS, ConnectionKind
from paw_backend.connections.adapter import AdapterRegistry
from paw_backend.connections.service import ConnectionService
from paw_backend.db import Database
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.gateway import QueueLeaseVerifier
from paw_backend.orchestrator.runtime import AgentRuntime
from paw_backend.tasks.queueing import BudgetTracker, TaskQueue

#: The CLI images' entries (``deploy/cli-session/Containerfile*``).
CLAUDE_EXECUTABLE = ("/opt/paw/start-claude",)
CODEX_EXECUTABLE = ("/opt/paw/start-codex",)
#: The session's egress forwarder in the container (``start-claude`` /
#: ``start-codex``: ``paw_egress.py forward --listen-port 3128``, Decision 0091).
EGRESS_PROXY_URL = "http://127.0.0.1:3128"


@dataclass(frozen=True, slots=True)
class CliAgentSetup:
    """An ``[agents.<name>]`` section (Decision 0099, 7): the arguments of
    ``ClaudeCliRuntime`` / ``CodexCliRuntime`` the file sets. No secret: the
    credential is the connection's (Decision 0087)."""

    label: str
    model: str
    session_base: str
    relay_command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CliSetup:
    """What ``create_app`` needs to build the CLI runtimes: ``[cloud]`` and the
    ``[agents.<name>]`` sections."""

    cloud: CloudSettings
    agents: Mapping[str, CliAgentSetup] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.cloud, CloudSettings):
            raise TypeError("cloud must be CloudSettings")
        if not isinstance(self.agents, Mapping) or not all(
            label in CLI_AGENT_LABELS
            and isinstance(agent, CliAgentSetup)
            and agent.label == label
            for label, agent in self.agents.items()
        ):
            raise TypeError("agents must map a cli label to its CliAgentSetup")


def configured_models(setup: CliSetup | None) -> dict[ConnectionKind, str]:
    """The runtime configuration's model of each ``[agents.<name>]``."""
    if setup is None:
        return {}
    return {ConnectionKind(label): agent.model for label, agent in setup.agents.items()}


def configured_models_summary(agents: Mapping[str, CliAgentSetup]) -> tuple[str, ...]:
    """The start-up line's parts of the ``[agents.<name>]`` sections (Decision
    0103, 10 A, 2): whether each configured model is a verified one (no model
    name, no path)."""
    return tuple(
        f"agents.{label}: model: "
        + (
            "verified"
            if agent.model in verified_models(ConnectionKind(label))
            else "unverified"
        )
        for label, agent in sorted(agents.items())
    )


def build_model_gate(
    setup: CliSetup | None, database: Database | None, *, timeout_seconds: float
) -> AgentModelGate:
    """The gate of the configured models and the Admin's policy (none read
    without a database)."""
    store = (
        AgentModelPolicyStore(database, timeout_seconds=timeout_seconds)
        if database is not None
        else None
    )
    return AgentModelGate(configured_models(setup), store)


@dataclass(frozen=True, slots=True)
class CliServices:
    """What :func:`build_cli_services` built (see the module)."""

    policy: ConfiguredCloudPolicy
    connections: ConnectionService
    principals: DatabasePrincipalDirectory
    runtimes: Mapping[str, AgentRuntime]


def build_cli_services(
    setup: CliSetup,
    *,
    database: Database,
    authorizer: Authorizer,
    budget: BudgetTracker,
    queue: TaskQueue,
    ladders: Mapping[NodeRole, tuple[str, ...]],
    node_seconds: float,
    secret_store,
    database_timeout_seconds: float,
    launcher: SystemdLauncher | None = None,
) -> CliServices:
    """See the module. ``secret_store``: the deployment's ``FileSecretStore``
    (required: ``TypeError`` without one). ``launcher``: production's
    ``SystemdLauncher()`` by default (tests pass another)."""
    if not isinstance(setup, CliSetup):
        raise TypeError("setup must be a CliSetup")
    if secret_store is None:
        raise TypeError("the cli runtimes need the secret store")
    used = {
        label
        for ladder in ladders.values()
        for label in ladder
        if label in CLI_AGENT_LABELS
    }
    if not used <= set(setup.agents):
        raise TypeError("a ladder names a cli runtime without its setup")
    if used and setup.cloud.enabled is not True:
        raise TypeError("a cli runtime needs [cloud] enabled = true")
    policy = ConfiguredCloudPolicy(
        setup.cloud,
        ladders=ladders,
        agents=tuple(setup.agents),
        facts=PostgresCloudFacts(database),
    )
    launcher = launcher if launcher is not None else SystemdLauncher()
    adapters = {}
    models = build_model_gate(setup, database, timeout_seconds=database_timeout_seconds)
    if "claude" in used:
        adapters[ConnectionKind.CLAUDE] = ClaudeSessionAdapter(
            launcher,
            executable=CLAUDE_EXECUTABLE,
            proxy_url=EGRESS_PROXY_URL,
            verified_models=VERIFIED_MODELS[ConnectionKind.CLAUDE],
        )
    if "codex" in used:
        adapters[ConnectionKind.CODEX] = CodexSessionAdapter(
            SystemdCodexLauncher(launcher),
            credentials=CodexAuthFile(),
            executable=CODEX_EXECUTABLE,
            proxy_url=EGRESS_PROXY_URL,
            verified_models=VERIFIED_MODELS[ConnectionKind.CODEX],
        )
    probes = AdapterRegistry()
    for probe in build_probe_adapters(setup, launcher=launcher, models=models):
        probes.register(probe)
    connections = ConnectionService(
        database,
        authorizer,
        PostgresAuditSink(database),
        probes,
        secret_store,
        budget=budget,
        lease=QueueLeaseVerifier(queue),
        session_adapters=adapters,
        credential_store=secret_store,
        database_timeout_seconds=database_timeout_seconds,
        health_timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    if "codex" in used or ConnectionKind.CODEX in probes:
        # Decision 0096, 1 D: a slot this process keeps for a session (or a
        # probe) whose CLI could not be stopped is collected here.
        connections.attach_session_collector(
            CodexLoginCollector(
                connections,
                SystemdSessionUnits(launcher),
                session_base=Path(setup.agents["codex"].session_base),
            )
        )
    runtimes: dict[str, AgentRuntime] = {}
    for label in sorted(used):
        agent = setup.agents[label]
        kind = ClaudeCliRuntime if label == "claude" else CodexCliRuntime
        runtimes[label] = kind(
            model=agent.model,
            session_base=Path(agent.session_base),
            relay_command=agent.relay_command,
            cloud_policy=policy,
            node_seconds=node_seconds,
            models=models,
        )
    return CliServices(
        policy=policy,
        connections=connections,
        principals=DatabasePrincipalDirectory(
            database, timeout_seconds=database_timeout_seconds
        ),
        runtimes=runtimes,
    )


def build_probe_adapters(
    setup: CliSetup | None,
    *,
    launcher: CliLauncher | None = None,
    models: AgentModelGate | None = None,
) -> tuple[ClaudeProbeAdapter | CodexProbeAdapter, ...]:
    """The connections' health probe adapters (Decision 0100, 4, 10 and 11):
    none without ``setup`` or with the cloud disabled (nothing is sent then);
    Claude's when ``[agents.claude]`` is written and Codex's when
    ``[agents.codex]`` is (each with its model and session directory), on
    production's launcher by default (Codex's through its interactive form,
    ``SystemdCodexLauncher``). None for a section whose model is verified on
    no CLI version (Decision 0103, 10 A, 2). Each checks the repository's
    verified pairs; with ``models`` it checks the default model the gate
    resolves (Decision 0103, 5 A) instead of the configured one."""
    if setup is None or setup.cloud.enabled is not True:
        return ()
    probes: list[ClaudeProbeAdapter | CodexProbeAdapter] = []
    claude = setup.agents.get("claude")
    codex = setup.agents.get("codex")
    if claude is not None and claude.model not in verified_models(
        ConnectionKind.CLAUDE
    ):
        claude = None
    if codex is not None and codex.model not in verified_models(ConnectionKind.CODEX):
        codex = None
    if claude is None and codex is None:
        return ()
    launcher = launcher if launcher is not None else SystemdLauncher()
    if claude is not None:
        probes.append(
            ClaudeProbeAdapter(
                launcher,
                model=claude.model,
                session_base=Path(claude.session_base),
                executable=CLAUDE_EXECUTABLE,
                proxy_url=EGRESS_PROXY_URL,
                verified_models=VERIFIED_MODELS[ConnectionKind.CLAUDE],
                models=models,
            )
        )
    if codex is not None:
        probes.append(
            CodexProbeAdapter(
                SystemdCodexLauncher(launcher),
                model=codex.model,
                session_base=Path(codex.session_base),
                credentials=CodexAuthFile(),
                executable=CODEX_EXECUTABLE,
                proxy_url=EGRESS_PROXY_URL,
                verified_models=VERIFIED_MODELS[ConnectionKind.CODEX],
                models=models,
            )
        )
    return tuple(probes)


__all__ = [
    "CLAUDE_EXECUTABLE",
    "CODEX_EXECUTABLE",
    "EGRESS_PROXY_URL",
    "CliAgentSetup",
    "CliServices",
    "CliSetup",
    "build_cli_services",
    "build_model_gate",
    "build_probe_adapters",
    "configured_models",
    "configured_models_summary",
]
