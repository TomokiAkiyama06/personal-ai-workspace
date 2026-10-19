"""Codex CLI as a node's runtime, through ``codex app-server`` (Decision 0083,
1, 5, 6 and 8; Decision 0087, 4; Decision 0089; #208).

:class:`CodexCliRuntime` is an orchestrator ``AgentRuntime``, as
:class:`~paw_backend.agents.claude_cli.ClaudeCliRuntime`: for one node attempt
it makes the session's directory, serves the node's tools on its MCP bridge and
sends the session through :class:`~paw_backend.agents.cloud.CloudGate`. The
answer is the bridge's (the ``submit``'s outcome, or the failure that ended the
attempt); a session that ended without either is ``no_submission``.

:class:`CodexSessionAdapter` is the connection's session adapter
(``ConnectionService(session_adapters={CODEX: ...})``). It writes the session's
``$CODEX_HOME/config.toml`` (Decision 0089, 2: every default-on tool feature
off, web search off, the PAW relay as the only MCP server with its tools
approved: the Tool Broker approves each call), starts ``codex app-server`` and
talks to it (``codex_app_server.py``):

* a version that is not in the verified list sends nothing (the list is empty
  until the smoke test of Decision 0089, 2 passes for a version);
* every growth of the thread's tokens is reported at once (``on_usage``); when
  the service answers ``False`` the CLI is stopped;
* an item that can execute (or that the table does not know), a request from
  the server, a shrinking total: the CLI is stopped (``invalid_response``);
* when the bridge ends (a ``submit``, a failure of the attempt) the CLI is
  stopped; ``turn/completed`` ends the session;
* ``unauthorized`` is an expired credential, the usage and rate limits a rate
  limit.

The credential (the ChatGPT login's ``auth.json``) is placed into the session's
``CODEX_HOME`` and collected afterwards by a :class:`CodexCredentials` handler
(``codex_credentials.CodexAuthFile``, issue #239): collected once the CLI was
stopped, also after a failure or a cancellation (run to its end), and a
refreshed login is written back through the service's ``write_back``
(compare-and-swap, Decision 0083, 6). The adapter is ``exclusive_sessions``:
the service runs one session per connection at a time (its session slot, a
database lease taken before the admission). Without a handler a session is
refused before anything starts.

The CLI rung stays disabled at assembly (Decision 0083, 11's 4: until the
``CloudPolicy`` Decision; the provider's terms are still being confirmed by the
human; and no version is verified). :class:`InteractiveSubprocessLauncher`
runs the CLI as a child process for development and the tests;
:class:`SystemdCodexLauncher` is production's (the session's unit of
``cli_session.SystemdLauncher``, whose stream socket carries the CLI's stdin
as well as its stdout: ``app-server`` is a conversation, not one prompt).
"""

import asyncio
import contextlib
import json
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from paw_backend.agents.bridge import McpBridge
from paw_backend.agents.claude_cli import NODE_MARGIN_SECONDS, _ended, _out_of_memory
from paw_backend.agents.cli_session import (
    CliSessionSpec,
    SystemdLauncher,
    UnitNotStoppedError,
    _Subprocess,
    create_session,
    log_session_start,
    remove_session,
    stop_through_cancellation,
)
from paw_backend.agents.cloud import CloudGate
from paw_backend.agents.codex_app_server import (
    CREDENTIAL_ERRORS,
    MAX_LINE_BYTES,
    RATE_ERRORS,
    VERIFIED_VERSIONS,
    CodexStream,
    Ended,
    Initialized,
    Ready,
    StreamRefused,
    Turn,
    initialize,
    notification,
    thread_start,
    turn_start,
)
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
from paw_backend.connections.session import CredentialWriter
from paw_backend.orchestrator.domain import NodeRole
from paw_backend.orchestrator.runtime import NodeAssignment, NodeOutcome
from paw_backend.tools.interfaces import require_async_method

logger = logging.getLogger(__name__)

AGENT = "codex"
CONFIG_NAME = "config.toml"
CLIENT_VERSION = "1"
#: The default-on features of ``rust-v0.162.1`` that offer the model a tool or
#: reach out (``features/src/lib.rs``), all off (Decision 0089, 2). A version
#: is verified only after its own list was compared and the smoke test saw
#: exactly the PAW tools in the request.
DISABLED_FEATURES: Final = (
    "shell_tool",
    "unified_exec",
    "view_image",
    "sleep_tool",
    "multi_agent",
    "apps",
    "plugins",
    "remote_plugin",
    "tool_suggest",
    "image_generation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "in_app_browser",
    "skill_search",
    "skill_mcp_dependency_install",
    "hooks",
    "goals",
    "guardian_approval",
    "tool_call_mcp_elicitation",
    "auth_elicitation",
    "code_mode_host",
    "realtime_conversation",
)
#: A PAW tool call runs at most 300 seconds (Decision 0083, 8); the CLI waits
#: a little longer for its answer.
TOOL_TIMEOUT_SECONDS: Final = 360
STARTUP_TIMEOUT_SECONDS: Final = 30
#: After the bridge ended (a ``submit``), the stream is read this much longer
#: for the report of the model call that made it (see ``_drain_after``).
DRAIN_SECONDS: Final = 5.0
#: ``codex --version`` answers within this time.
VERSION_SECONDS: Final = 30.0
_VERSION_LINE = re.compile(r"\b([0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*)")

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
    FailureCode.UNAVAILABLE: Failure.CONNECTION_UNAVAILABLE,
}


class CodexCredentials(Protocol):
    """Places the connection's credential in a session's ``CODEX_HOME`` and
    takes it back afterwards, writing a refreshed ``auth.json`` back through
    ``write_back`` (Decision 0083, 6; ``codex_credentials.CodexAuthFile``)."""

    async def place(self, secret: Secret, codex_home: Path) -> None: ...

    async def collect(
        self,
        secret: Secret,
        codex_home: Path,
        write_back: CredentialWriter | None,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class CodexLaunch:
    """One ``codex app-server`` to start. ``env`` holds no secret."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    spec: CliSessionSpec


class CodexProcess(Protocol):
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader

    async def wait(self) -> int: ...

    async def stop(self) -> None: ...


class CodexLauncher(Protocol):
    async def start(self, launch: CodexLaunch) -> CodexProcess: ...


class _Interactive(_Subprocess):
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        super().__init__(process)
        assert process.stdin is not None
        self.stdin = process.stdin


class InteractiveSubprocessLauncher:
    """Starts the CLI as a child process with its stdin kept open
    (development, the tests). Only ``PATH`` and the launch's ``env`` are in its
    environment; its stderr is not read. Stopping is the process group's
    SIGTERM, five seconds, SIGKILL (``cli_session``)."""

    def __init__(self, *, path: str = "/usr/bin:/bin") -> None:
        self._path = path

    async def start(self, launch: CodexLaunch) -> CodexProcess:
        process = await asyncio.create_subprocess_exec(
            *launch.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PATH": self._path, **launch.env},
            cwd=launch.spec.directory,
            start_new_session=True,
            limit=MAX_LINE_BYTES,
        )
        return _Interactive(process)


class SystemdCodexLauncher:
    """Production's launcher: the session's unit (``SystemdLauncher``, #236)
    in its Codex form (``start_interactive``: the stream socket is the CLI's
    stdin and stdout; no credential file, no prompt). The version probe and
    ``app-server`` each start the session's unit in turn."""

    def __init__(self, systemd: SystemdLauncher) -> None:
        require_async_method(systemd, "start_interactive", 3)
        self._systemd = systemd

    async def start(self, launch: CodexLaunch) -> CodexProcess:
        return await self._systemd.start_interactive(
            launch.argv, launch.env, launch.spec
        )


@dataclass(frozen=True, slots=True)
class CodexAttachment:
    """What the runtime hands its adapter through the service
    (``SessionRequest.attachment``)."""

    spec: CliSessionSpec
    bridge: McpBridge = field(repr=False)
    tools: frozenset[str]
    system_prompt: str = field(repr=False)
    # Set by the adapter when the stream was refused (Decision 0089, 2), also
    # after a submit: the attempt then fails whatever the bridge returned.
    refused: list[bool] = field(default_factory=list, repr=False)
    # Set by the adapter when the CLI (the version probe or ``app-server``)
    # could not be stopped: the runtime then keeps the session's directory
    # (the unit still has it mounted) for the operator, instead of deleting
    # it under a running session (as Claude's).
    unstopped: list[bool] = field(default_factory=list, repr=False)
    # Set by the adapter when the CLI's unit was ended by the OOM killer
    # (``Result=oom-kill``, Decision 0092, 4): without a submitted result the
    # attempt is ``AgentOutOfMemory``, before any other failure.
    out_of_memory: list[bool] = field(default_factory=list, repr=False)
    # Set by the adapter as it places the login in ``CODEX_HOME``: from then
    # on a CLI that could not be stopped may hold the login (the version
    # probe before it never does).
    placed: list[bool] = field(default_factory=list, repr=False)


def session_config(spec: CliSessionSpec, tools: Iterable[str]) -> str:
    """The session's ``config.toml`` (Decision 0089, 2; the auth settings of
    Decision 0087, 4)."""
    relay = spec.relay_command
    paw = {
        "command": relay[0],
        "args": [
            *relay[1:],
            "--socket",
            str(spec.socket_path),
            "--token-file",
            str(spec.token_path),
        ],
        "required": True,
        "enabled_tools": sorted(tools),
        "default_tools_approval_mode": "approve",
        "startup_timeout_sec": STARTUP_TIMEOUT_SECONDS,
        "tool_timeout_sec": TOOL_TIMEOUT_SECONDS,
    }
    tables: list[tuple[str, Mapping[str, object]]] = [
        ("analytics", {"enabled": False}),
        (
            "otel",
            {"exporter": "none", "trace_exporter": "none", "metrics_exporter": "none"},
        ),
        ("history", {"persistence": "none"}),
        ("tools.update_plan", {"enabled": False}),
        ("tools.experimental_request_user_input", {"enabled": False}),
        ("features", {name: False for name in DISABLED_FEATURES}),
        ("mcp_servers.paw", paw),
    ]
    lines = [
        _toml_pair("cli_auth_credentials_store", "file"),
        _toml_pair("forced_login_method", "chatgpt"),
        _toml_pair("web_search", "disabled"),
        _toml_pair("check_for_update_on_startup", False),
    ]
    for name, values in tables:
        lines.append(f"\n[{name}]")
        lines.extend(_toml_pair(key, value) for key, value in values.items())
    return "\n".join(lines) + "\n"


def _toml_pair(key: str, value: object) -> str:
    return f"{key} = {_toml_value(value)}"


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # A JSON string is a TOML basic string (its escapes are TOML's).
        return json.dumps(value)
    if isinstance(value, Sequence):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError("not a TOML value")


class CodexSessionAdapter:
    """See the module. ``verified_versions``: the CLI versions that may run
    (:data:`~.codex_app_server.VERIFIED_VERSIONS`, empty, unless a test or a
    later verified release says otherwise)."""

    kind = ConnectionKind.CODEX
    # One session per connection at a time (Decision 0083, 6): the ChatGPT
    # login's refresh token is single-use.
    exclusive_sessions = True

    def __init__(
        self,
        launcher: CodexLauncher,
        *,
        credentials: CodexCredentials | None = None,
        executable: tuple[str, ...] = ("codex",),
        proxy_url: str | None = None,
        verified_versions: frozenset[str] = VERIFIED_VERSIONS,
        drain_seconds: float = DRAIN_SECONDS,
    ) -> None:
        require_async_method(launcher, "start", 1)
        if credentials is not None:
            require_async_method(credentials, "place", 2)
            require_async_method(credentials, "collect", 3)
        if not executable or not all(isinstance(p, str) and p for p in executable):
            raise ValueError("executable")
        self._launcher = launcher
        self._credentials = credentials
        self._executable = executable
        self._proxy_url = proxy_url
        self._verified = frozenset(verified_versions)
        self._drain = float(drain_seconds)

    def __repr__(self) -> str:
        return "CodexSessionAdapter()"

    def arguments(self) -> tuple[str, ...]:
        return (*self._executable, "app-server", "--listen", "stdio://")

    def environment(self, attachment: CodexAttachment) -> dict[str, str]:
        env = {
            "HOME": str(attachment.spec.directory),
            "CODEX_HOME": str(attachment.spec.config_dir),
        }
        if self._proxy_url is not None:
            env["HTTPS_PROXY"] = self._proxy_url
        return env

    async def run_session(
        self, secret: Secret, request: AgentSessionRequest
    ) -> AgentSessionResult:
        attachment = request.attachment
        if not isinstance(attachment, CodexAttachment):
            raise TypeError("a Codex session needs its attachment")
        if self._credentials is None:
            # No way to hand the login over (Decision 0089, 4).
            return AgentSessionResult(failure=FailureCode.UNAVAILABLE)
        try:
            return await self._session(secret, request, attachment)
        finally:
            if (
                attachment.unstopped
                and attachment.placed
                and request.cli_running is not None
            ):
                # The CLI may still run with the login: the connection's
                # slot stays held (Decision 0083, 6). A version probe that
                # could not be stopped has no login: the slot goes back.
                request.cli_running()

    async def _session(
        self,
        secret: Secret,
        request: AgentSessionRequest,
        attachment: CodexAttachment,
    ) -> AgentSessionResult:
        assert self._credentials is not None
        log_session_start(attachment.spec, "codex", request.connection_id)
        # The version first, without any credential: ``app-server`` may reach
        # the provider as it starts with a login (Decision 0089, 2: an
        # unverified version sends nothing).
        version = await self._version(attachment)
        if version is None or version not in self._verified:
            logger.warning("Codex session refused: version")
            return AgentSessionResult(failure=FailureCode.INVALID_RESPONSE)
        codex_home = attachment.spec.config_dir
        _write(
            codex_home / CONFIG_NAME, session_config(attachment.spec, attachment.tools)
        )
        try:
            attachment.placed.append(True)
            await self._credentials.place(secret, codex_home)
            return await self._converse(request, attachment)
        finally:
            # After the CLI was stopped (``_converse``'s own ``finally``), run
            # to its end even through a cancellation: the refreshed login must
            # be written back while this session still holds the
            # connection's slot (Decision 0083, 6).
            if await _through_cancellation(
                self._collect(secret, codex_home, request.write_back)
            ):
                raise asyncio.CancelledError

    async def _version(self, attachment: CodexAttachment) -> str | None:
        """``codex --version``'s version (``codex-cli 0.162.1``), or ``None``."""
        process = await self._start(
            CodexLaunch(
                (*self._executable, "--version"),
                self.environment(attachment),
                attachment.spec,
            ),
            attachment,
        )
        try:
            async with asyncio.timeout(VERSION_SECONDS):
                line = await process.stdout.readline()
        except (TimeoutError, ValueError):
            line = b""
        finally:
            await _stop(process, attachment)
        if getattr(process, "oom_killed", False):
            # The probe's unit was ended by the OOM killer (Decision 0092, 4):
            # the attempt ran out of memory, whatever the probe printed.
            attachment.out_of_memory.append(True)
        match = _VERSION_LINE.search(line.decode("ascii", "replace"))
        return match.group(1) if match else None

    async def _start(
        self, launch: CodexLaunch, attachment: CodexAttachment
    ) -> CodexProcess:
        try:
            return await self._launcher.start(launch)
        except UnitNotStoppedError:
            # The start failed and its cleanup could not stop the unit: it
            # may run with the session's files although no process came back.
            attachment.unstopped.append(True)
            raise

    async def _collect(
        self,
        secret: Secret,
        codex_home: Path,
        write_back: CredentialWriter | None,
    ) -> None:
        assert self._credentials is not None
        try:
            await self._credentials.collect(secret, codex_home, write_back)
        except Exception as error:
            logger.error(
                "Codex credential could not be collected: exception_type=%s",
                type(error).__name__,
            )

    async def _converse(
        self, request: AgentSessionRequest, attachment: CodexAttachment
    ) -> AgentSessionResult:
        process = await self._start(
            CodexLaunch(
                self.arguments(), self.environment(attachment), attachment.spec
            ),
            attachment,
        )
        stream = CodexStream(attachment.tools, self._verified)
        ended: Ended | None = None
        failure: FailureCode | None = None
        stopped = False
        bridge_ended = asyncio.create_task(attachment.bridge.ended.wait())
        try:
            await _send(process, initialize(CLIENT_VERSION))
            while True:
                reading = asyncio.create_task(process.stdout.readline())
                done, _ = await asyncio.wait(
                    {reading, bridge_ended}, return_when=asyncio.FIRST_COMPLETED
                )
                if reading not in done:
                    # The bridge ended: a submit, or the attempt failed. The
                    # line being read is not lost: the drain reads on.
                    ended, stopped = await self._drain_after(
                        reading, process, stream, request, attachment
                    )
                    if attachment.refused:
                        failure = FailureCode.INVALID_RESPONSE
                    break
                line = reading.result()
                if not line:
                    break
                try:
                    item = stream.feed(line)
                except StreamRefused as refused:
                    logger.warning("Codex session refused: %s", refused.reason)
                    attachment.refused.append(True)
                    failure = FailureCode.INVALID_RESPONSE
                    break
                if isinstance(item, Initialized):
                    await _send(process, notification("initialized"))
                    await _send(
                        process, thread_start(request.model, attachment.system_prompt)
                    )
                elif isinstance(item, Ready):
                    await _send(process, turn_start(item.thread_id, request.prompt))
                elif isinstance(item, Turn):
                    if not await request.on_usage(
                        item.input_tokens, item.output_tokens
                    ):
                        stopped = True
                        break
                elif isinstance(item, Ended):
                    ended = item
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass  # the CLI went away; its exit says the rest
        finally:
            bridge_ended.cancel()
            await _stop(process, attachment)
        code = await process.wait()
        if getattr(process, "oom_killed", False):
            attachment.out_of_memory.append(True)
        if failure is None and not stream.initialized:
            failure = FailureCode.INVALID_RESPONSE
        if (
            failure is None
            and ended is not None
            and ended.status == "completed"
            and not stream.complete
        ):
            # A completed turn made at least one model call, and one after
            # its last tool call: a turn that did not report them is a CLI
            # that does not report its calls (Decision 0083, 5 (d); Decision
            # 0089, 3). (A failed turn may have failed before any call, an
            # expired login.)
            failure = FailureCode.INVALID_RESPONSE
        if failure is None and ended is not None and ended.status != "completed":
            if ended.error in CREDENTIAL_ERRORS:
                failure = FailureCode.EXPIRED
            elif ended.error in RATE_ERRORS:
                failure = FailureCode.RATE_LIMITED
            else:
                failure = FailureCode.INTERNAL_ERROR
        if (
            failure is None
            and ended is None
            and code != 0
            and not stopped
            and not attachment.bridge.ended.is_set()
        ):
            failure = FailureCode.INTERNAL_ERROR
        # The totals are the thread's as reported: known once the turn ended
        # with its reports, or once the call that made the bridge's last tool
        # call (the submit) was reported; not on a crash.
        known = (ended is not None and stream.complete) or (
            attachment.bridge.ended.is_set() and _submit_counted(stream, attachment)
        )
        input_tokens, output_tokens = stream.totals
        return AgentSessionResult(
            submission=None,
            input_tokens=input_tokens if known else None,
            output_tokens=output_tokens if known else None,
            failure=failure,
        )

    async def _drain_after(
        self,
        reading: "asyncio.Task[bytes]",
        process: CodexProcess,
        stream: CodexStream,
        request: AgentSessionRequest,
        attachment: CodexAttachment,
    ) -> tuple[Ended | None, bool]:
        """After the bridge ended: read on until the turn ends, the stream
        ends, the service says stop, a refusal, or ``drain_seconds`` pass.
        Not only until the model call that made the bridge's last tool call is
        reported (its usage comes when its response completes, which may be
        after the call began; the call's own item may still be on its way):
        what the CLI wrote after that report is checked too, so an item that
        can execute fails the attempt even after a submit (Decision 0089, 2).
        Reported calls go to ``on_usage`` as before; a refusal is kept on the
        attachment. Returns the end of the turn if it came, and whether the
        service said stop."""
        ended: Ended | None = None
        stopped = False
        try:
            async with asyncio.timeout(self._drain):
                while True:
                    line = await reading
                    if not line:
                        break
                    try:
                        item = stream.feed(line)
                    except StreamRefused as refused:
                        logger.warning("Codex session refused: %s", refused.reason)
                        attachment.refused.append(True)
                        break
                    if isinstance(item, Turn):
                        if not await request.on_usage(
                            item.input_tokens, item.output_tokens
                        ):
                            stopped = True
                            break
                    elif isinstance(item, Ended):
                        ended = item
                        break
                    reading = asyncio.ensure_future(process.stdout.readline())
        except TimeoutError:
            pass
        finally:
            if not reading.done():
                reading.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reading
        return ended, stopped


async def _through_cancellation(work) -> bool:
    """Run ``work`` to its end even while being cancelled; whether a
    cancellation came meanwhile (the caller raises it afterwards)."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    return cancelled


def _submit_counted(stream: CodexStream, attachment: CodexAttachment) -> bool:
    """The stream showed every call the bridge ran, and a report after the
    last of them: the call that made it is counted."""
    return stream.calls_seen >= attachment.bridge.calls and stream.call_counted


async def _stop(process: CodexProcess, attachment: CodexAttachment) -> None:
    """Stop the CLI. A cancellation (the session's deadline) that comes
    during the stop waits for it: a stop that did not end may leave the CLI
    running, and then its files must stay (``attachment.unstopped``)."""
    failed, cancelled = await stop_through_cancellation(process)
    if failed is not None:
        attachment.unstopped.append(True)
    if cancelled:
        raise asyncio.CancelledError
    if failed is not None:
        raise failed


async def _send(process: CodexProcess, line: bytes) -> None:
    process.stdin.write(line)
    await process.stdin.drain()


def _write(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


class CodexCliRuntime:
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
        return f"CodexCliRuntime(model={self._model!r})"

    async def run_node(self, assignment: NodeAssignment) -> NodeOutcome:
        return await self._classified(assignment, fallback=False)

    async def run_as_fallback(self, assignment: NodeAssignment) -> NodeOutcome:
        """``HybridRuntime``'s cloud path: a refused gate is
        ``ComputeUnavailable`` (the node stays on its local rung)."""
        return await self._classified(assignment, fallback=True)

    async def _classified(
        self, assignment: NodeAssignment, *, fallback: bool
    ) -> NodeOutcome:
        """The runtime's boundary, as Claude's and the local runtime's: an
        exception of the session's setup or launcher (a missing CLI, a
        directory that cannot be made) is classified
        (``ErrorClassifier.from_error``: its class and text are never read);
        ``NodeStopped``, a cancellation and ``MemoryError`` pass."""
        try:
            return await self._run(assignment, fallback=fallback)
        except Exception as error:
            return ErrorClassifier.from_error(error)

    async def _run(self, assignment: NodeAssignment, *, fallback: bool) -> NodeOutcome:
        if assignment.role is NodeRole.PLANNER:
            # Decision 0083, 1: a CLI is never on the planner's ladder.
            return ErrorClassifier.outcome(
                Failure.CLOUD_POLICY_DENIED, fallback=fallback
            )
        toolkit = NodeToolkit(assignment, self._limits)
        system, task = node_prompts(toolkit)
        spec = create_session(
            self._base,
            self._relay,
            task_id=assignment.task_id,
            node_key=assignment.node_key,
        )
        bridge = McpBridge(toolkit, token=spec.token)
        attachment = None
        try:
            server = await bridge.serve(str(spec.socket_path))
            try:
                tools = frozenset(t["function"]["name"] for t in toolkit.tools)
                attachment = CodexAttachment(spec, bridge, tools, system)
                request = SessionRequest(
                    self._model,
                    _PURPOSES[assignment.role],
                    task,
                    self._timeout,
                    attachment=attachment,
                )
                try:
                    result = await self._gate.session(
                        assignment,
                        kind=ConnectionKind.CODEX,
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
        if attachment is not None and attachment.out_of_memory:
            oom = _out_of_memory(bridge)
            if oom is not None:
                return oom
        if bridge.failure is not None:
            return _ended(bridge.failure)
        if attachment.refused:
            # The stream showed what this backend does not run (Decision
            # 0089, 2): not even a submit made meanwhile stands.
            return ErrorClassifier.outcome(Failure.MALFORMED_RESPONSE)
        if bridge.outcome is not None:
            return bridge.outcome
        failure = getattr(result, "failure", None)
        if failure is not None:
            return ErrorClassifier.outcome(_FAILURES.get(failure, Failure.OTHER))
        return ErrorClassifier.outcome(Failure.NO_SUBMISSION)


__all__ = [
    "AGENT",
    "DISABLED_FEATURES",
    "CodexAttachment",
    "CodexCliRuntime",
    "CodexCredentials",
    "CodexLaunch",
    "CodexLauncher",
    "CodexProcess",
    "CodexSessionAdapter",
    "InteractiveSubprocessLauncher",
    "SystemdCodexLauncher",
    "session_config",
]
