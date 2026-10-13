"""Claude Code as a node's runtime, on a subscription token (Decision 0083, 1,
5, 6 and 8; Decision 0087, 4; #208).

:class:`ClaudeCliRuntime` is an orchestrator ``AgentRuntime``. For one node
attempt it makes the session's directory, serves the node's tools on its MCP
bridge (:class:`~paw_backend.agents.bridge.McpBridge` over a
:class:`~paw_backend.agents.toolkit.NodeToolkit`: the same tools and ``submit``
as the local model's loop, through the Tool Broker), and sends the session
through :class:`~paw_backend.agents.cloud.CloudGate` (``CloudPolicy`` ->
admission -> placement -> the adapter). The answer is the bridge's: the
``submit``'s outcome, or the failure that ended the attempt; a session that
ended without either is ``no_submission``.

:class:`ClaudeSessionAdapter` is the connection's session adapter
(``ConnectionService(session_adapters={CLAUDE: ...})``). It starts the CLI
with the flags of Decision 0087, 4, the subscription token
(``claude setup-token``) in ``CLAUDE_CODE_OAUTH_TOKEN`` (handed to the
launcher, never in an argument or a log), the task on stdin, and reads its
``stream-json`` (``claude_stream.py``):

* ``system/init`` must show exactly the PAW tools (and ``EndConversation``)
  and the ``paw`` server connected, or the CLI is stopped (``invalid_response``);
* every model call is reported at once (``on_usage``); when the service
  answers ``False`` (the budget, the lease, the run) the CLI is stopped;
* when the bridge ends (a ``submit``, or a failure of the attempt) the CLI is
  stopped;
* an ``api_retry`` for an authentication failure stops it as ``expired``;
* the ``result``'s ``modelUsage`` gives the totals (unknown when missing or
  zero: the service then counts its estimate).

Stopping is SIGTERM, five seconds, SIGKILL (the launcher). Nothing of the
conversation is kept or logged.

The CLI rung stays disabled at assembly (Decision 0083, 11's 4: until the
``CloudPolicy`` Decision; and the provider's terms are still being confirmed
by the human). Production's launcher (a container under a systemd template
unit) is a later change; :class:`~.cli_session.SubprocessLauncher` runs a CLI
as a child process for development and the tests.
"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from paw_backend.agents.bridge import McpBridge
from paw_backend.agents.claude_stream import (
    CREDENTIAL_ERRORS,
    RATE_ERRORS,
    ClaudeStream,
    Retry,
    StreamRefused,
    Totals,
    Turn,
    paw_tool_names,
)
from paw_backend.agents.cli_session import (
    CliLaunch,
    CliLauncher,
    CliSessionSpec,
    create_session,
    remove_session,
)
from paw_backend.agents.cloud import CloudGate
from paw_backend.agents.failures import ErrorClassifier, Failure, RuntimeFailure
from paw_backend.agents.local import node_prompts
from paw_backend.agents.toolkit import NodeToolkit, ToolkitLimits
from paw_backend.connections import (
    AgentSessionRequest,
    AgentSessionResult,
    ConnectionKind,
    FailureCode,
    Secret,
    SessionRequest,
    UsagePurpose,
)
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.errors import InvalidNodeResultError, NodeStopped
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

AGENT = "claude"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
#: The CLI ends this long before the node's timeout (Decision 0083, 8).
NODE_MARGIN_SECONDS = 60.0

_PURPOSES = {
    NodeRole.WORKER: UsagePurpose.CODING,
    NodeRole.PLANNER: UsagePurpose.CODING,
    NodeRole.REVIEWER: UsagePurpose.REVIEW,
    NodeRole.RESEARCHER: UsagePurpose.RESEARCH,
}
_FAILURES = {
    FailureCode.EXPIRED: Failure.CREDENTIAL_EXPIRED,
    FailureCode.RATE_LIMITED: Failure.RATE_LIMITED,
    FailureCode.TIMEOUT: Failure.TIMEOUT,
    FailureCode.INVALID_RESPONSE: Failure.MALFORMED_RESPONSE,
}


@dataclass(frozen=True, slots=True)
class ClaudeAttachment:
    """What the runtime hands its adapter through the service
    (``SessionRequest.attachment``)."""

    spec: CliSessionSpec
    bridge: McpBridge = field(repr=False)
    tools: frozenset[str]
    system_prompt: str = field(repr=False)
    # Set by the adapter when the CLI could not be stopped: the runtime then
    # keeps the session's directory (the unit still has it mounted) for the
    # operator, instead of deleting it under a running session.
    unstopped: list[bool] = field(default_factory=list, repr=False)


class ClaudeSessionAdapter:
    """See the module."""

    kind = ConnectionKind.CLAUDE

    def __init__(
        self,
        launcher: CliLauncher,
        *,
        executable: tuple[str, ...] = ("claude",),
        proxy_url: str | None = None,
        restricted: bool = True,
    ) -> None:
        require_async_method(launcher, "start", 1)
        if not executable or not all(isinstance(p, str) and p for p in executable):
            raise ValueError("executable")
        self._launcher = launcher
        self._executable = executable
        self._proxy_url = proxy_url
        self._restricted = restricted

    def __repr__(self) -> str:
        return "ClaudeSessionAdapter()"

    def arguments(self, attachment: ClaudeAttachment, model: str) -> tuple[str, ...]:
        """The CLI's command line (Decision 0087, 4). The task is on stdin."""
        spec = attachment.spec
        argv = [
            *self._executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            str(spec.mcp_config_path),
            "--allowedTools",
            "mcp__paw__*",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--setting-sources",
            "user",
            "--settings",
            str(spec.settings_path),
            "--model",
            model,
            "--system-prompt",
            attachment.system_prompt,
        ]
        if self._restricted:
            argv.append("--restricted")
        return tuple(argv)

    def environment(self, attachment: ClaudeAttachment) -> dict[str, str]:
        env = {
            "HOME": str(attachment.spec.directory),
            # The CLI's user settings are the session's own (empty) directory.
            "CLAUDE_CONFIG_DIR": str(attachment.spec.config_dir),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        }
        if self._proxy_url is not None:
            env["HTTPS_PROXY"] = self._proxy_url
        return env

    async def run_session(
        self, secret: Secret, request: AgentSessionRequest
    ) -> AgentSessionResult:
        attachment = request.attachment
        if not isinstance(attachment, ClaudeAttachment):
            raise TypeError("a Claude session needs its attachment")
        process = await self._launcher.start(
            CliLaunch(
                self.arguments(attachment, request.model),
                self.environment(attachment),
                secret,
                TOKEN_ENV,
                request.prompt.encode("utf-8"),
                attachment.spec,
            )
        )
        stream = ClaudeStream(attachment.tools)
        totals: Totals | None = None
        failure: FailureCode | None = None
        retry: str | None = None
        # The CLI was stopped on purpose (the service said no more turns):
        # its exit status is the stop's, not a failure.
        stopped = False
        turns = 0
        ended = asyncio.create_task(attachment.bridge.ended.wait())
        try:
            while True:
                reading = asyncio.create_task(process.stdout.readline())
                done, _ = await asyncio.wait(
                    {reading, ended}, return_when=asyncio.FIRST_COMPLETED
                )
                if reading not in done:
                    reading.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reading
                    break  # the bridge ended: a submit, or the attempt failed
                line = reading.result()
                if not line:
                    break
                try:
                    item = stream.feed(line)
                except StreamRefused as refused:
                    logger.warning("Claude session refused: %s", refused.reason)
                    failure = FailureCode.INVALID_RESPONSE
                    break
                if isinstance(item, Turn):
                    if not stream.initialized:
                        failure = FailureCode.INVALID_RESPONSE
                        break
                    turns += 1
                    if not await request.on_usage(
                        item.input_tokens, item.output_tokens
                    ):
                        stopped = True
                        break
                elif isinstance(item, Retry):
                    retry = item.error
                    if item.error in CREDENTIAL_ERRORS:
                        failure = FailureCode.EXPIRED
                        break
                elif isinstance(item, Totals):
                    totals = item
        finally:
            ended.cancel()
            try:
                await process.stop()
            except OSError:
                attachment.unstopped.append(True)
                raise
        code = await process.wait()
        if failure is None and not stream.initialized:
            failure = FailureCode.INVALID_RESPONSE
        if (
            failure is None
            and totals is not None
            and (totals.input_tokens or totals.output_tokens)
            and turns == 0
        ):
            # Tokens were used but no call was reported on the way: a CLI
            # that does not report its turns is not run (Decision 0083, 5).
            failure = FailureCode.INVALID_RESPONSE
        if failure is None and totals is not None and totals.is_error:
            failure = (
                FailureCode.RATE_LIMITED
                if retry in RATE_ERRORS
                else FailureCode.INTERNAL_ERROR
            )
        if (
            failure is None
            and totals is None
            and code != 0
            and not stopped
            and not attachment.bridge.ended.is_set()
        ):
            failure = FailureCode.INTERNAL_ERROR
        return AgentSessionResult(
            submission=None,
            input_tokens=None if totals is None else totals.input_tokens,
            output_tokens=None if totals is None else totals.output_tokens,
            failure=failure,
        )


class ClaudeCliRuntime:
    """See the module. ``cloud_policy``: the deployment's ``CloudPolicy``
    (``None``: every session is refused)."""

    def __init__(
        self,
        *,
        model: str,
        session_base: Path,
        relay_command: tuple[str, ...],
        cloud_policy: object | None = None,
        limits: ToolkitLimits | None = None,
        node_seconds: float = 1800.0,
    ) -> None:
        if not isinstance(model, str) or not model:
            raise ValueError("model")
        if node_seconds <= NODE_MARGIN_SECONDS:
            raise ValueError("node_seconds")
        self._model = model
        self._base = Path(session_base)
        self._relay = tuple(relay_command)
        self._gate = CloudGate(cloud_policy)
        self._limits = limits
        self._timeout = float(node_seconds) - NODE_MARGIN_SECONDS

    def __repr__(self) -> str:
        return f"ClaudeCliRuntime(model={self._model!r})"

    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome:
        return await self._run(assignment, fallback=False)

    async def run_as_fallback(self, assignment: NodeAssignment) -> NodeOutcome:
        """``HybridRuntime``'s cloud path: a refused gate is
        ``ComputeUnavailable`` (the node stays on its local rung)."""
        return await self._run(assignment, fallback=True)

    async def _run(self, assignment: NodeAssignment, *, fallback: bool) -> NodeOutcome:
        if assignment.role is NodeRole.PLANNER:
            # Decision 0083, 1: a CLI is never on the planner's ladder.
            return ErrorClassifier.outcome(
                Failure.CLOUD_POLICY_DENIED, fallback=fallback
            )
        toolkit = NodeToolkit(assignment, self._limits)
        system, task = node_prompts(toolkit)
        spec = create_session(self._base, self._relay)
        bridge = McpBridge(toolkit, token=spec.token)
        attachment = None
        try:
            server = await bridge.serve(str(spec.socket_path))
            try:
                tools = [t["function"]["name"] for t in toolkit.tools]
                request = SessionRequest(
                    self._model,
                    _PURPOSES[assignment.role],
                    task,
                    self._timeout,
                    attachment=(
                        attachment := ClaudeAttachment(
                            spec, bridge, paw_tool_names(tools), system
                        )
                    ),
                )
                try:
                    result = await self._gate.session(
                        assignment,
                        kind=ConnectionKind.CLAUDE,
                        request=request,
                        agent=AGENT,
                        model=self._model,
                    )
                except RuntimeFailure as refused:
                    return ErrorClassifier.outcome(refused.failure, fallback=fallback)
                except Exception:
                    if not attachment.unstopped:
                        raise
                    result = None  # answered below: the CLI did not stop
            finally:
                server.close()
                await server.wait_closed()
        finally:
            if attachment is not None and attachment.unstopped:
                # The CLI's unit did not stop: its directory stays (the unit
                # has it mounted); the operator stops the unit and removes it.
                logger.error("A CLI session could not be stopped; its files stay")
            else:
                remove_session(spec)
        if attachment is not None and attachment.unstopped:
            return ErrorClassifier.outcome(Failure.OTHER)
        if bridge.failure is not None:
            return _ended(bridge.failure)
        if bridge.outcome is not None:
            return bridge.outcome
        failure = getattr(result, "failure", None)
        if failure is not None:
            return ErrorClassifier.outcome(_FAILURES.get(failure, Failure.OTHER))
        return ErrorClassifier.outcome(Failure.NO_SUBMISSION)


def _ended(error: BaseException) -> NodeOutcome:
    """The outcome of an attempt that a tool call ended (the local loop's)."""
    if isinstance(error, NodeStopped):
        raise error
    if isinstance(error, RuntimeFailure):
        return ErrorClassifier.outcome(error.failure)
    if isinstance(error, InvalidNodeResultError):
        return ErrorClassifier.outcome(Failure.INVALID_RESULT)
    return ErrorClassifier.from_error(error)


__all__ = [
    "AGENT",
    "TOKEN_ENV",
    "ClaudeAttachment",
    "ClaudeCliRuntime",
    "ClaudeSessionAdapter",
]
